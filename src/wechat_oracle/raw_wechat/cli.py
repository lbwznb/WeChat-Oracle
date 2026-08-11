"""Authorized local WeChat discovery and synchronization commands."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from loguru import logger

from .crypto import apply_wal, decrypt_database, read_page1, verify_page1
from wechat_oracle.config import settings

from .importer import (
    import_authorized_group_text_messages_many_with_cursors,
    list_groups,
)
from .inventory import DatabaseCandidate, contact_database_for, discover_message_databases, stage_database
from .profile_41155 import WCDB_MEMORY_PROTECTION_MASK, verify_install
from .win_memory import ScanStats, find_process_executables, find_process_ids, scan_processes

COMMANDS = ("scan", "groups", "authorize", "revoke", "sync", "run", "status")
SNAPSHOT_NAME = re.compile(r"^(message_\d+|contact)(?:\.snapshot-\d+)?\.db$")


def _require_opt_in() -> None:
    if not settings.raw_wechat_enabled:
        raise SystemExit("refusing: enable local WeChat read access in the application first")


def _resolve_install_root(configured: Path) -> Path:
    candidates = [configured, *(path.parent for path in find_process_executables())]
    for candidate in candidates:
        if (candidate / "Weixin.exe").is_file():
            return candidate
    raise RuntimeError("a running supported Weixin installation was not found")


def _quick_check(path: Path) -> None:
    uri = f"file:{path.resolve().as_posix()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        result = conn.execute("PRAGMA quick_check").fetchone()
    finally:
        conn.close()
    if result is None or result[0] != "ok":
        raise RuntimeError("decrypted database failed SQLite quick_check")


def _unlock(
    candidate: DatabaseCandidate,
    workspace: Path,
    stats: ScanStats,
    cached_key: bytes | None = None,
) -> tuple[Path, bytes, str, int, int] | None:
    staged = stage_database(candidate, workspace)
    generation = hashlib.sha256(read_page1(staged)[:16]).hexdigest()[:12]
    key = cached_key
    if key is None or not verify_page1(key, read_page1(staged)):
        match = scan_processes(staged, stats=stats, xor_mask=WCDB_MEMORY_PROTECTION_MASK)
        if match is None:
            return None
        key = match.key

    destination = staged.parent.parent / "decrypted" / staged.name
    working = destination.with_name(f".{destination.stem}.tmp-{time.time_ns()}{destination.suffix}")
    try:
        pages = decrypt_database(key, staged, working)
        wal_frames = apply_wal(
            key,
            read_page1(staged)[:16],
            Path(f"{staged}-wal"),
            working,
        )
        _quick_check(working)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(working, destination)
        except PermissionError:
            destination = destination.with_name(
                f"{destination.stem}.snapshot-{time.time_ns()}{destination.suffix}"
            )
            os.replace(working, destination)
    finally:
        working.unlink(missing_ok=True)
    return destination, key, generation, pages, wal_frames


def _account_map(candidates: list[DatabaseCandidate]) -> dict[str, list[DatabaseCandidate]]:
    accounts: dict[str, list[DatabaseCandidate]] = {}
    for candidate in candidates:
        accounts.setdefault(candidate.account_fingerprint, []).append(candidate)
    return accounts


def _select_account(
    candidates: list[DatabaseCandidate],
    requested: str,
) -> tuple[str, list[DatabaseCandidate]]:
    accounts = _account_map(candidates)
    if requested:
        selected = accounts.get(requested)
        if selected is None:
            raise SystemExit("requested account fingerprint was not found")
        return requested, selected
    if not accounts:
        raise SystemExit("no WeChat message database found")
    if len(accounts) > 1:
        raise SystemExit("multiple WeChat accounts found; pass --account from probe output")
    fingerprint = max(accounts, key=lambda value: sum(item.size for item in accounts[value]))
    return fingerprint, accounts[fingerprint]


def _source_signature(candidate: DatabaseCandidate) -> dict[str, int]:
    stat = candidate.source.stat()
    signature = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    wal = Path(f"{candidate.source}-wal")
    if wal.is_file():
        wal_stat = wal.stat()
        signature.update({"wal_size": wal_stat.st_size, "wal_mtime_ns": wal_stat.st_mtime_ns})
    return signature


def _candidate_activity_ns(candidate: DatabaseCandidate) -> int:
    signature = _source_signature(candidate)
    return max(signature["mtime_ns"], signature.get("wal_mtime_ns", 0))


def _state_path(workspace: Path, account_fingerprint: str) -> Path:
    return workspace / account_fingerprint / "sync-state.json"


def _load_state(path: Path) -> dict:
    if not path.is_file():
        return {"databases": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"databases": {}}
    if not isinstance(value, dict) or not isinstance(value.get("databases"), dict):
        return {"databases": {}}
    return value


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{time.time_ns()}")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


@contextmanager
def _account_lock(workspace: Path, account_fingerprint: str):
    """Hold one Windows byte-range lock for sync/unlock publication."""
    import msvcrt

    path = workspace / account_fingerprint / "sync.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise SystemExit("another raw-WeChat sync process already holds this account lock") from exc
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


def _unlock_account(
    message_candidates: list[DatabaseCandidate],
    workspace: Path,
    *,
    retry_failures: bool = False,
) -> dict:
    account_fingerprint = message_candidates[0].account_fingerprint
    state_file = _state_path(workspace, account_fingerprint)
    state = _load_state(state_file)
    sources = [*message_candidates, contact_database_for(message_candidates[0])]
    results: list[dict] = []
    failures: list[str] = []
    memory_key: bytes | None = None

    for candidate in sources:
        signature = _source_signature(candidate)
        previous = state["databases"].get(candidate.source.name, {})
        previous_output = Path(str(previous.get("output", "")))
        if previous.get("signature") == signature and previous_output.is_file():
            if not previous.get("generation"):
                previous["generation"] = hashlib.sha256(
                    read_page1(candidate.source)[:16]
                ).hexdigest()[:12]
                previous["status"] = "decrypted"
                _save_state(state_file, state)
            results.append({"kind": candidate.source.stem, "status": "unchanged"})
            continue
        if (
            not retry_failures
            and previous.get("signature") == signature
            and previous.get("status") == "key_not_found"
            and float(previous.get("retry_after", 0)) > time.time()
        ):
            failures.append(candidate.source.name)
            results.append({"kind": candidate.source.stem, "status": "key_not_found_cached"})
            continue

        stats = ScanStats()
        unlocked = _unlock(candidate, workspace, stats, memory_key)
        if unlocked is None:
            failures.append(candidate.source.name)
            state["databases"][candidate.source.name] = {
                "signature": signature,
                "status": "key_not_found",
                "retry_after": time.time() + 60,
                "updated_at": time.time(),
            }
            _save_state(state_file, state)
            results.append({"kind": candidate.source.stem, "status": "key_not_found"})
            continue
        destination, memory_key, generation, pages, wal_frames = unlocked
        state["databases"][candidate.source.name] = {
            "signature": signature,
            "output": str(destination),
            "status": "decrypted",
            "generation": generation,
            "updated_at": time.time(),
        }
        _save_state(state_file, state)
        results.append({
            "kind": candidate.source.stem,
            "status": "decrypted",
            "pages": pages,
            "wal_frames": wal_frames,
        })

    return {
        "status": "partial" if failures else "decrypted",
        "databases": results,
        "failures": failures,
        "changed": sum(item["status"] == "decrypted" for item in results),
    }


def _unlock_contact(
    message_candidate: DatabaseCandidate,
    workspace: Path,
) -> dict:
    """Publish only the contact snapshot needed by the group picker."""
    candidate = contact_database_for(message_candidate)
    state_file = _state_path(workspace, candidate.account_fingerprint)
    state = _load_state(state_file)
    signature = _source_signature(candidate)
    previous = state["databases"].get("contact.db", {})
    previous_output = Path(str(previous.get("output", "")))
    if previous.get("signature") == signature and previous_output.is_file():
        return {"status": "unchanged", "failures": []}
    unlocked = _unlock(candidate, workspace, ScanStats())
    if unlocked is None:
        return {"status": "key_not_found", "failures": ["contact.db"]}
    destination, _, generation, pages, wal_frames = unlocked
    state["databases"]["contact.db"] = {
        "signature": signature,
        "output": str(destination),
        "status": "decrypted",
        "generation": generation,
        "updated_at": time.time(),
    }
    _save_state(state_file, state)
    return {
        "status": "decrypted",
        "failures": [],
        "pages": pages,
        "wal_frames": wal_frames,
    }


def _latest_decrypted(workspace: Path, account_fingerprint: str) -> tuple[list[Path], Path]:
    decrypted = workspace / account_fingerprint / "decrypted"
    latest: dict[str, Path] = {}
    for path in decrypted.glob("*.db"):
        match = SNAPSHOT_NAME.fullmatch(path.name)
        if match is None:
            continue
        kind = match.group(1)
        existing = latest.get(kind)
        if existing is None or path.stat().st_mtime_ns > existing.stat().st_mtime_ns:
            latest[kind] = path
    messages = [latest[key] for key in sorted(latest) if key.startswith("message_")]
    contact = latest.get("contact")
    if not messages or contact is None:
        raise SystemExit("required decrypted message/contact copies are missing")
    return messages, contact


def _latest_contact(workspace: Path, account_fingerprint: str) -> Path:
    decrypted = workspace / account_fingerprint / "decrypted"
    contacts = [
        path for path in decrypted.glob("contact*.db")
        if SNAPSHOT_NAME.fullmatch(path.name) is not None
    ]
    if not contacts:
        raise SystemExit("a verified contact snapshot is missing")
    return max(contacts, key=lambda path: path.stat().st_mtime_ns)


def _cleanup_decrypted(message_dbs: list[Path], contact_db: Path) -> None:
    """Remove temporary full-database plaintext copies after normalization."""
    for path in [*message_dbs, contact_db]:
        path.unlink(missing_ok=True)


def _authorize_group(
    *,
    workspace: Path,
    account_fingerprint: str,
    archive_path: Path,
    canonical_group_id: str,
    cleanup: bool = True,
) -> dict:
    from wechat_oracle.db import get_conn, init_db, transaction

    message_dbs: list[Path] = []
    contact_db = _latest_contact(workspace, account_fingerprint)
    options = {item.canonical_group_id: item.display_name for item in list_groups(contact_db)}
    display_name = options.get(canonical_group_id)
    if display_name is None:
        raise SystemExit("the selected canonical group is not present in the current contact snapshot")
    state = _load_state(_state_path(workspace, account_fingerprint))
    generation = str(state.get("databases", {}).get("contact.db", {}).get("generation", ""))
    if not generation:
        raise SystemExit("current contact generation is unavailable; scan groups again")
    init_db(archive_path)
    now = time.time()
    with get_conn(archive_path) as conn, transaction(conn):
        conn.execute(
            """
            INSERT INTO raw_group_authorizations
                (account_fingerprint, canonical_group_id, display_name,
                 contact_generation, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(account_fingerprint, canonical_group_id) DO UPDATE SET
                display_name=excluded.display_name,
                contact_generation=excluded.contact_generation,
                enabled=1,
                updated_at=excluded.updated_at
            """,
            (account_fingerprint, canonical_group_id, display_name, generation, now, now),
        )
    # A new or refreshed authorization may need a backfill even when the source
    # files have not changed since the previous sync cycle.
    state.pop("imported_signatures", None)
    _save_state(_state_path(workspace, account_fingerprint), state)
    if cleanup:
        _cleanup_decrypted(message_dbs, contact_db)
    return {
        "status": "authorized",
        "account": account_fingerprint,
        "canonical_group_id": canonical_group_id,
        "display_name": display_name,
    }


def _revoke_group(
    *,
    account_fingerprint: str,
    archive_path: Path,
    canonical_group_id: str,
) -> dict:
    from wechat_oracle.db import get_conn, init_db, transaction

    init_db(archive_path)
    with get_conn(archive_path) as conn, transaction(conn):
        changed = conn.execute(
            "UPDATE raw_group_authorizations SET enabled=0, updated_at=? "
            "WHERE account_fingerprint=? AND canonical_group_id=? AND enabled=1",
            (time.time(), account_fingerprint, canonical_group_id),
        ).rowcount
    return {"status": "revoked" if changed else "unchanged", "changed": bool(changed)}


def _sync_authorized_groups_once(
    *,
    workspace: Path,
    account_fingerprint: str,
    account_candidates: list[DatabaseCandidate],
    archive_path: Path,
) -> dict:
    from wechat_oracle.db import get_conn, init_db, transaction

    init_db(archive_path)
    selected_groups = tuple(settings.groups)
    if not selected_groups:
        return {"status": "idle", "authorized_groups": 0, "attempted": 0, "inserted": 0}
    placeholders = ",".join("?" for _ in selected_groups)
    with get_conn(archive_path) as archive:
        authorizations = archive.execute(
            f"""
            SELECT canonical_group_id, display_name, contact_generation
             FROM raw_group_authorizations
             WHERE account_fingerprint=? AND enabled=1
               AND (canonical_group_id IN ({placeholders})
                    OR display_name IN ({placeholders}))
             ORDER BY canonical_group_id
            """,
            (account_fingerprint, *selected_groups, *selected_groups),
        ).fetchall()
    if not authorizations:
        return {"status": "idle", "authorized_groups": 0, "attempted": 0, "inserted": 0}

    state_file = _state_path(workspace, account_fingerprint)
    state = _load_state(state_file)
    imported_signatures = state.get("imported_signatures", {})
    if not isinstance(imported_signatures, dict):
        imported_signatures = {}
    requested_signatures = {
        candidate.source.name: _source_signature(candidate)
        for candidate in account_candidates
    }
    changed_candidates = [
        candidate
        for candidate in account_candidates
        if imported_signatures.get(candidate.source.name)
        != requested_signatures[candidate.source.name]
    ]
    if not changed_candidates:
        return {
            "status": "idle",
            "authorized_groups": len(authorizations),
            "attempted": 0,
            "inserted": 0,
        }

    unlocked = _unlock_account(changed_candidates, workspace)
    if "contact.db" in unlocked["failures"]:
        raise RuntimeError("current contact snapshot could not be verified")
    message_dbs, contact_db = _latest_decrypted(workspace, account_fingerprint)
    current_groups = {item.canonical_group_id: item.display_name for item in list_groups(contact_db)}
    state = _load_state(_state_path(workspace, account_fingerprint))
    current_contact_generation = str(
        state.get("databases", {}).get("contact.db", {}).get("generation", "")
    )
    attempted_total = 0
    inserted_total = 0
    synchronized = 0

    with get_conn(archive_path) as archive:
        for auth in authorizations:
            group_id = str(auth["canonical_group_id"])
            if group_id not in current_groups:
                continue
            if str(auth["contact_generation"]) != current_contact_generation:
                logger.warning("raw sync paused one group because contact generation changed")
                continue
            generations: dict[str, str] = {}
            after_local_ids: dict[str, int] = {}
            for path in message_dbs:
                kind = SNAPSHOT_NAME.fullmatch(path.name).group(1)
                shard_id = kind.removeprefix("message_")
                generation = str(
                    state.get("databases", {}).get(f"{kind}.db", {}).get("generation", "")
                )
                generations[shard_id] = generation
                cursor = archive.execute(
                    """
                    SELECT database_generation, last_local_id
                      FROM raw_import_cursors
                     WHERE account_fingerprint=? AND canonical_group_id=? AND shard_id=?
                    """,
                    (account_fingerprint, group_id, shard_id),
                ).fetchone()
                if cursor is not None and str(cursor["database_generation"]) == generation:
                    after_local_ids[shard_id] = int(cursor["last_local_id"])

            _, attempted, inserted, cursors = (
                import_authorized_group_text_messages_many_with_cursors(
                    archive,
                    message_dbs,
                    contact_db,
                    group_id=group_id,
                    group_name=current_groups[group_id],
                    after_local_ids=after_local_ids,
                )
            )
            with transaction(archive):
                for shard_id, last_local_id in cursors.items():
                    archive.execute(
                        """
                        INSERT INTO raw_import_cursors
                            (account_fingerprint, canonical_group_id, shard_id,
                             database_generation, last_local_id, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(account_fingerprint, canonical_group_id, shard_id)
                        DO UPDATE SET
                            database_generation=excluded.database_generation,
                            last_local_id=excluded.last_local_id,
                            updated_at=excluded.updated_at
                        """,
                        (
                            account_fingerprint,
                            group_id,
                            shard_id,
                            generations.get(shard_id, ""),
                            last_local_id,
                            time.time(),
                        ),
                    )
            attempted_total += attempted
            inserted_total += inserted
            synchronized += 1
    result = {
        "status": "partial" if unlocked["failures"] else "synced",
        "authorized_groups": len(authorizations),
        "synchronized_groups": synchronized,
        "shards": len(message_dbs),
        "attempted": attempted_total,
        "inserted": inserted_total,
    }
    failed_sources = set(unlocked["failures"])
    state = _load_state(state_file)
    imported_signatures = state.setdefault("imported_signatures", {})
    for candidate in changed_candidates:
        if candidate.source.name not in failed_sources:
            imported_signatures[candidate.source.name] = requested_signatures[candidate.source.name]
    _save_state(state_file, state)
    _cleanup_decrypted(message_dbs, contact_db)
    return result


def _cleanup_account_decrypted(workspace: Path, account_fingerprint: str) -> None:
    decrypted = workspace / account_fingerprint / "decrypted"
    if not decrypted.is_dir():
        return
    for path in decrypted.glob("*.db"):
        if SNAPSHOT_NAME.fullmatch(path.name) is not None:
            path.unlink(missing_ok=True)


def _sync_authorized_groups(
    *,
    workspace: Path,
    account_fingerprint: str,
    account_candidates: list[DatabaseCandidate],
    archive_path: Path,
) -> dict:
    try:
        return _sync_authorized_groups_once(
            workspace=workspace,
            account_fingerprint=account_fingerprint,
            account_candidates=account_candidates,
            archive_path=archive_path,
        )
    finally:
        _cleanup_account_decrypted(workspace, account_fingerprint)


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Authorized read-only local WeChat synchronization")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--workspace", type=Path, default=settings.raw_wechat_workspace)
    parser.add_argument("--weixin-root", type=Path, default=settings.raw_wechat_install_root)
    parser.add_argument(
        "--account",
        default=settings.raw_wechat_account,
        help="anonymous account fingerprint reported by scan",
    )
    parser.add_argument("--canonical-id", default="")
    parser.add_argument("--archive", type=Path, default=settings.db_path)
    parser.add_argument("--interval", type=float, default=settings.raw_wechat_sync_interval_seconds)
    args = parser.parse_args(argv)
    candidates = discover_message_databases()

    if args.command == "scan":
        accounts = _account_map(candidates)
        _emit({
            "accounts": [
                {
                    "account_fingerprint": fingerprint,
                    "shards": len(items),
                    "latest_activity_ns": max(_candidate_activity_ns(item) for item in items),
                }
                for fingerprint, items in sorted(accounts.items())
            ],
            "account_count": len(accounts),
            "weixin_process_count": len(find_process_ids()),
        })
        return 0

    if args.command == "status":
        from wechat_oracle.db import get_conn, init_db
        init_db(args.archive)
        with get_conn(args.archive) as conn:
            rows = conn.execute(
                "SELECT account_fingerprint, canonical_group_id, display_name, enabled "
                "FROM raw_group_authorizations ORDER BY display_name, canonical_group_id"
            ).fetchall()
        _emit({
            "enabled": settings.raw_wechat_enabled,
            "authorizations": [dict(row) for row in rows],
            "weixin_process_count": len(find_process_ids()),
        })
        return 0

    _require_opt_in()
    account_fingerprint, account_candidates = _select_account(candidates, args.account)
    verify_install(_resolve_install_root(args.weixin_root))
    if args.command == "groups":
        with _account_lock(args.workspace, account_fingerprint):
            unlocked = _unlock_contact(account_candidates[0], args.workspace)
            if "contact.db" in unlocked["failures"]:
                raise SystemExit("current contact database could not be verified")
            message_dbs: list[Path] = []
            contact_db = _latest_contact(args.workspace, account_fingerprint)
            groups = list_groups(contact_db)
            _cleanup_decrypted(message_dbs, contact_db)
        _emit({
            "account": account_fingerprint,
            "groups": [
                {"canonical_group_id": item.canonical_group_id, "display_name": item.display_name}
                for item in groups
            ],
        })
        return 0
    if args.command == "authorize":
        if not args.canonical_id:
            raise SystemExit("--canonical-id is required")
        with _account_lock(args.workspace, account_fingerprint):
            unlocked = _unlock_contact(account_candidates[0], args.workspace)
            if "contact.db" in unlocked["failures"]:
                raise SystemExit("current contact database could not be verified")
            result = _authorize_group(
                workspace=args.workspace,
                account_fingerprint=account_fingerprint,
                archive_path=args.archive,
                canonical_group_id=args.canonical_id,
            )
        _emit(result)
        return 0
    if args.command == "revoke":
        if not args.canonical_id:
            raise SystemExit("--canonical-id is required")
        _emit(_revoke_group(
            account_fingerprint=account_fingerprint,
            archive_path=args.archive,
            canonical_group_id=args.canonical_id,
        ))
        return 0
    if args.command == "sync":
        with _account_lock(args.workspace, account_fingerprint):
            result = _sync_authorized_groups(
                workspace=args.workspace,
                account_fingerprint=account_fingerprint,
                account_candidates=account_candidates,
                archive_path=args.archive,
            )
        _emit(result)
        return 0

    if args.interval < 30:
        raise SystemExit("--interval must be at least 30 seconds")
    try:
        with _account_lock(args.workspace, account_fingerprint):
            while True:
                started = time.time()
                _, current_candidates = _select_account(discover_message_databases(), account_fingerprint)
                result = _sync_authorized_groups(
                    workspace=args.workspace,
                    account_fingerprint=account_fingerprint,
                    account_candidates=current_candidates,
                    archive_path=args.archive,
                )
                _emit(result)
                time.sleep(max(0.0, args.interval - (time.time() - started)))
    except KeyboardInterrupt:
        _emit({"status": "stopped"})
        return 0
