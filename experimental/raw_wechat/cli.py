"""Command line entry point for the isolated raw-WeChat experiment."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from .crypto import apply_wal, decrypt_database, read_page1
from .importer import import_group_text_messages_many_with_cursors
from .inventory import DatabaseCandidate, contact_database_for, discover_message_databases, stage_database
from .profile_41155 import WCDB_MEMORY_PROTECTION_MASK, verify_install
from .win_memory import ScanStats, find_process_ids, scan_processes

COMMANDS = ("probe", "unlock-auto", "import-group", "sync-group", "watch-group")
SNAPSHOT_NAME = re.compile(r"^(message_\d+|contact)(?:\.snapshot-\d+)?\.db$")


def _require_opt_in() -> None:
    if os.environ.get("WO_EXPERIMENTAL_RAW_WECHAT") != "1":
        raise SystemExit("refusing: set WO_EXPERIMENTAL_RAW_WECHAT=1 to enable this read-only experiment")


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
) -> tuple[Path, str, str, int, int] | None:
    staged = stage_database(candidate, workspace)
    generation = hashlib.sha256(read_page1(staged)[:16]).hexdigest()[:12]
    match = scan_processes(staged, stats=stats, xor_mask=WCDB_MEMORY_PROTECTION_MASK)
    if match is None:
        return None

    destination = staged.parent.parent / "decrypted" / staged.name
    working = destination.with_name(f".{destination.stem}.tmp-{time.time_ns()}{destination.suffix}")
    pages = decrypt_database(match.key, staged, working)
    wal_frames = apply_wal(
        match.key,
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
    return destination, match.fingerprint, generation, pages, wal_frames


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


def _active_candidates(candidates: list[DatabaseCandidate]) -> list[DatabaseCandidate]:
    """Keep the current shard and recently rolled predecessors for live sync."""
    latest = max(_candidate_activity_ns(candidate) for candidate in candidates)
    seven_days_ns = 7 * 24 * 60 * 60 * 1_000_000_000
    return [
        candidate
        for candidate in candidates
        if _candidate_activity_ns(candidate) >= latest - seven_days_ns
    ]


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
            results.append({"kind": candidate.source.stem, "status": "unchanged", "database": str(previous_output)})
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
        unlocked = _unlock(candidate, workspace, stats)
        if unlocked is None:
            failures.append(candidate.source.name)
            state["databases"][candidate.source.name] = {
                "signature": signature,
                "status": "key_not_found",
                "retry_after": time.time() + 24 * 60 * 60,
                "updated_at": time.time(),
            }
            _save_state(state_file, state)
            results.append({"kind": candidate.source.stem, "status": "key_not_found", "scan": asdict(stats)})
            continue
        destination, fingerprint, generation, pages, wal_frames = unlocked
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
            "key_fingerprint": fingerprint,
            "pages": pages,
            "wal_frames": wal_frames,
            "database": str(destination),
            "scan": asdict(stats),
        })

    return {
        "status": "partial" if failures else "decrypted",
        "account_fingerprint": account_fingerprint,
        "databases": results,
        "failures": failures,
        "changed": sum(item["status"] == "decrypted" for item in results),
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


def _import_group(
    *,
    workspace: Path,
    account_fingerprint: str,
    archive_path: Path,
    group_name: str,
    only_kinds: set[str] | None = None,
) -> dict:
    from wechat_oracle.db import get_conn, init_db

    message_dbs, contact_db = _latest_decrypted(workspace, account_fingerprint)
    if only_kinds is not None:
        message_dbs = [
            path
            for path in message_dbs
            if SNAPSHOT_NAME.fullmatch(path.name).group(1) in only_kinds
        ]
        if not message_dbs:
            return {"status": "unchanged", "shards": 0, "attempted": 0, "inserted": 0}
    state_file = _state_path(workspace, account_fingerprint)
    state = _load_state(state_file)
    group_key = hashlib.sha256(group_name.encode("utf-8")).hexdigest()[:16]
    import_state = state.setdefault("imports", {}).setdefault(group_key, {})
    after_local_ids: dict[str, int] = {}
    generations: dict[str, str] = {}
    for path in message_dbs:
        kind = SNAPSHOT_NAME.fullmatch(path.name).group(1)
        shard_id = kind.removeprefix("message_")
        database_state = state["databases"].get(f"{kind}.db", {})
        generation = str(database_state.get("generation", ""))
        generations[shard_id] = generation
        cursor = import_state.get(shard_id, {})
        if generation and cursor.get("generation") == generation:
            after_local_ids[shard_id] = int(cursor.get("last_local_id", 0))

    init_db(archive_path)
    with get_conn(archive_path) as archive:
        group_id, attempted, inserted, cursors = import_group_text_messages_many_with_cursors(
            archive,
            message_dbs,
            contact_db,
            group_name=group_name,
            after_local_ids=after_local_ids,
        )
    for shard_id, last_local_id in cursors.items():
        import_state[shard_id] = {
            "generation": generations.get(shard_id, ""),
            "last_local_id": last_local_id,
            "updated_at": time.time(),
        }
    _save_state(state_file, state)
    return {
        "status": "imported",
        "group_id": group_id,
        "shards": len(message_dbs),
        "attempted": attempted,
        "inserted": inserted,
    }


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only WeChat 4 database experiment")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--workspace", type=Path, default=Path("data/experimental_raw"))
    parser.add_argument("--weixin-root", type=Path, default=Path(r"D:\0softwear\Weixin"))
    parser.add_argument(
        "--account",
        default=os.environ.get("WO_EXPERIMENTAL_WECHAT_ACCOUNT", ""),
        help="account fingerprint reported by probe",
    )
    parser.add_argument("--group", default="")
    parser.add_argument("--archive", type=Path, default=Path("data/wechat-oracle.db"))
    parser.add_argument("--interval", type=float, default=60.0)
    args = parser.parse_args(argv)
    candidates = discover_message_databases()

    if args.command == "probe":
        accounts = _account_map(candidates)
        _emit({
            "database_candidates": [
                {"account_fingerprint": item.account_fingerprint, "size": item.size, "kind": item.source.name}
                for item in candidates
            ],
            "account_count": len(accounts),
            "weixin_process_count": len(find_process_ids()),
        })
        return 0

    _require_opt_in()
    account_fingerprint, account_candidates = _select_account(candidates, args.account)
    if args.command == "import-group":
        if not args.group:
            raise SystemExit("--group is required")
        _emit(_import_group(
            workspace=args.workspace,
            account_fingerprint=account_fingerprint,
            archive_path=args.archive,
            group_name=args.group,
        ))
        return 0

    verify_install(args.weixin_root)
    if args.command == "unlock-auto":
        with _account_lock(args.workspace, account_fingerprint):
            result = _unlock_account(account_candidates, args.workspace, retry_failures=True)
        _emit(result)
        return 2 if result["failures"] else 0

    if not args.group:
        raise SystemExit("--group is required")
    if args.command == "sync-group":
        with _account_lock(args.workspace, account_fingerprint):
            unlocked = _unlock_account(_active_candidates(account_candidates), args.workspace)
            changed_kinds = {
                item["kind"]
                for item in unlocked["databases"]
                if item["status"] == "decrypted" and item["kind"].startswith("message_")
            }
            imported = _import_group(
                workspace=args.workspace,
                account_fingerprint=account_fingerprint,
                archive_path=args.archive,
                group_name=args.group,
                only_kinds=changed_kinds,
            )
        _emit({"status": "synced", "unlock": unlocked, "import": imported})
        return 2 if unlocked["failures"] else 0

    if args.interval < 30:
        raise SystemExit("--interval must be at least 30 seconds")
    try:
        with _account_lock(args.workspace, account_fingerprint):
            while True:
                started = time.time()
                _, current_candidates = _select_account(discover_message_databases(), account_fingerprint)
                unlocked = _unlock_account(_active_candidates(current_candidates), args.workspace)
                changed_kinds = {
                    item["kind"]
                    for item in unlocked["databases"]
                    if item["status"] == "decrypted" and item["kind"].startswith("message_")
                }
                imported = _import_group(
                    workspace=args.workspace,
                    account_fingerprint=account_fingerprint,
                    archive_path=args.archive,
                    group_name=args.group,
                    only_kinds=changed_kinds,
                )
                _emit({"status": "watch-cycle", "unlock": unlocked, "import": imported})
                time.sleep(max(0.0, args.interval - (time.time() - started)))
    except KeyboardInterrupt:
        _emit({"status": "stopped"})
        return 0
