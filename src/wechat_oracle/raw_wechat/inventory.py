"""Discover and stage WeChat 4 databases without exposing account names or paths."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

MESSAGE_DB_NAME = re.compile(r"^message_(\d+)\.db$")


@dataclass(frozen=True)
class DatabaseCandidate:
    source: Path
    account_fingerprint: str
    size: int


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:12]


def likely_roots() -> list[Path]:
    profile = Path(os.environ.get("USERPROFILE", Path.home()))
    roots = [
        Path(r"D:\xwechat_files"),
        profile / "Documents" / "xwechat_files",
        profile / "Documents" / "WeChat Files",
    ]
    seen: set[str] = set()
    existing: list[Path] = []
    for path in roots:
        identity = str(path).casefold()
        if path.is_dir() and identity not in seen:
            seen.add(identity)
            existing.append(path)
    return existing


def discover_message_databases() -> list[DatabaseCandidate]:
    found: list[DatabaseCandidate] = []
    for root in likely_roots():
        for account in root.iterdir():
            if not account.is_dir():
                continue
            message_dir = account / "db_storage" / "message"
            candidates = message_dir.glob("message_*.db") if message_dir.is_dir() else ()
            for db in candidates:
                if MESSAGE_DB_NAME.fullmatch(db.name) is None:
                    continue
                if db.is_file() and db.stat().st_size >= 4096:
                    found.append(DatabaseCandidate(db, _fingerprint(str(account.resolve())), db.stat().st_size))
    return sorted(
        found,
        key=lambda item: (
            item.account_fingerprint,
            int(MESSAGE_DB_NAME.fullmatch(item.source.name).group(1)),
        ),
    )


def stage_database(candidate: DatabaseCandidate, workspace: Path) -> Path:
    """Publish a stable DB/WAL pair without ever opening the source writable."""
    target_dir = workspace / candidate.account_fingerprint / "encrypted"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / candidate.source.name
    source_wal = Path(f"{candidate.source}-wal")

    def signature() -> tuple[tuple[int, int], tuple[bool, int, int]]:
        database_stat = candidate.source.stat()
        if source_wal.is_file():
            wal_stat = source_wal.stat()
            wal = (True, wal_stat.st_size, wal_stat.st_mtime_ns)
        else:
            wal = (False, 0, 0)
        return (database_stat.st_size, database_stat.st_mtime_ns), wal

    for attempt in range(3):
        before = signature()
        nonce = f"{os.getpid()}-{attempt}"
        temporary = target.with_name(f".{target.name}.tmp-{nonce}")
        temporary_wal = Path(f"{temporary}-wal")
        shutil.copy2(candidate.source, temporary)
        if before[1][0]:
            shutil.copy2(source_wal, temporary_wal)
        after = signature()
        if before == after:
            os.replace(temporary, target)
            target_wal = Path(f"{target}-wal")
            if temporary_wal.is_file():
                os.replace(temporary_wal, target_wal)
            elif target_wal.is_file():
                target_wal.unlink()
            return target
        temporary.unlink(missing_ok=True)
        temporary_wal.unlink(missing_ok=True)
    raise RuntimeError("source database changed during three snapshot attempts")


def contact_database_for(candidate: DatabaseCandidate) -> DatabaseCandidate:
    """Return the WeChat 4 contact DB belonging to a message candidate."""
    source = candidate.source
    if MESSAGE_DB_NAME.fullmatch(source.name) is None or source.parent.name != "message":
        raise ValueError("contact discovery is supported only for the reviewed WeChat 4 layout")
    contact = source.parent.parent / "contact" / "contact.db"
    if not contact.is_file() or contact.stat().st_size < 4096:
        raise FileNotFoundError("matching WeChat 4 contact.db was not found")
    return DatabaseCandidate(contact, candidate.account_fingerprint, contact.stat().st_size)
