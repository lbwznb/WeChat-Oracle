"""Authenticated SQLCipher-4 snapshot decryption for the reviewed WeChat 4 build.

The database key is already the raw 32-byte AES key cached by WCDB.  It is
never formatted into an exception, log message, or return value intended for
display.  Parameters were cross-checked against two independent current
implementations; WeChat 4 uses 4096-byte pages, an 80-byte reserve, AES-CBC,
and HMAC-SHA512 with a two-round PBKDF2-derived MAC key.
"""
from __future__ import annotations

import hashlib
import hmac
import struct
from pathlib import Path

PAGE_SIZE = 4096
SALT_SIZE = 16
IV_SIZE = 16
HMAC_SIZE = 64
RESERVE_SIZE = IV_SIZE + HMAC_SIZE
KEY_SIZE = 32
SQLITE_HEADER = b"SQLite format 3\x00"
WAL_MAGIC = {0x377F0682: "<", 0x377F0683: ">"}
WAL_VERSION = 3_007_000


def key_fingerprint(key: bytes) -> str:
    """Return a short non-secret identifier suitable for diagnostics."""
    return hashlib.sha256(key).hexdigest()[:12]


def _mac_key(key: bytes, salt: bytes) -> bytes:
    if len(key) != KEY_SIZE or len(salt) != SALT_SIZE:
        raise ValueError("invalid WeChat 4 key or salt length")
    mac_salt = bytes(value ^ 0x3A for value in salt)
    return hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, dklen=KEY_SIZE)


def verify_page1(key: bytes, page: bytes) -> bool:
    """Verify a raw candidate key without decrypting or persisting it."""
    if len(key) != KEY_SIZE or len(page) < PAGE_SIZE:
        return False
    return verify_page(key, page, 1, page[:SALT_SIZE])


def verify_page(
    key: bytes,
    page: bytes,
    page_number: int,
    database_salt: bytes,
) -> bool:
    """Authenticate one encrypted main-database or WAL page."""
    if len(key) != KEY_SIZE or len(page) != PAGE_SIZE or page_number <= 0:
        return False
    salt_offset = SALT_SIZE if page_number == 1 else 0
    authenticated = page[salt_offset : PAGE_SIZE - HMAC_SIZE]
    expected = hmac.new(_mac_key(key, database_salt), digestmod=hashlib.sha512)
    expected.update(authenticated)
    expected.update(page_number.to_bytes(4, "little"))
    return hmac.compare_digest(expected.digest(), page[PAGE_SIZE - HMAC_SIZE : PAGE_SIZE])


def read_page1(path: Path) -> bytes:
    with path.open("rb") as handle:
        page = handle.read(PAGE_SIZE)
    if len(page) != PAGE_SIZE:
        raise ValueError("database is smaller than one encrypted page")
    return page


def _decrypt_page(key: bytes, page: bytes, page_number: int) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    salt_offset = SALT_SIZE if page_number == 1 else 0
    encrypted_end = PAGE_SIZE - RESERVE_SIZE
    encrypted = page[salt_offset:encrypted_end]
    iv = page[encrypted_end : encrypted_end + IV_SIZE]
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plaintext = decryptor.update(encrypted) + decryptor.finalize()
    output = bytearray(PAGE_SIZE)
    if page_number == 1:
        output[:SALT_SIZE] = SQLITE_HEADER
        output[SALT_SIZE : SALT_SIZE + len(plaintext)] = plaintext
    else:
        output[: len(plaintext)] = plaintext
    return bytes(output)


def decrypt_database(key: bytes, source: Path, destination: Path) -> int:
    """Decrypt an already staged database copy; return pages written."""
    if not verify_page1(key, read_page1(source)):
        raise ValueError("candidate key did not verify")
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher  # noqa: F401
    except ImportError as exc:  # experimental dependency, deliberately lazy
        raise RuntimeError("cryptography is required for experimental decryption") from exc

    size = source.stat().st_size
    if size % PAGE_SIZE:
        raise ValueError("encrypted database size is not page-aligned")
    destination.parent.mkdir(parents=True, exist_ok=True)
    pages = 0
    database_salt = read_page1(source)[:SALT_SIZE]
    with source.open("rb") as src, destination.open("xb") as dst:
        while page := src.read(PAGE_SIZE):
            pages += 1
            if not verify_page(key, page, pages, database_salt):
                raise ValueError(f"encrypted database page {pages} failed authentication")
            dst.write(_decrypt_page(key, page, pages))
    return pages


def _wal_checksum(
    data: bytes,
    byte_order: str,
    checksum: tuple[int, int] = (0, 0),
) -> tuple[int, int]:
    if len(data) % 8:
        raise ValueError("WAL checksum input must be a multiple of 8 bytes")
    values = struct.unpack(f"{byte_order}{len(data) // 4}I", data)
    s0, s1 = checksum
    for index in range(0, len(values), 2):
        s0 = (s0 + values[index] + s1) & 0xFFFFFFFF
        s1 = (s1 + values[index + 1] + s0) & 0xFFFFFFFF
    return s0, s1


def apply_wal(key: bytes, database_salt: bytes, wal_path: Path, destination: Path) -> int:
    """Apply only HMAC/checksum-valid frames through the last commit marker."""
    if not wal_path.is_file() or wal_path.stat().st_size <= 32:
        return 0
    valid_frames: list[tuple[int, int, bytes]] = []
    last_commit_index = -1
    last_commit_pages = 0
    with wal_path.open("rb") as wal:
        header = wal.read(32)
        if len(header) != 32:
            return 0
        magic, version, page_size = struct.unpack(">III", header[:12])
        byte_order = WAL_MAGIC.get(magic)
        if byte_order is None or version != WAL_VERSION or page_size != PAGE_SIZE:
            raise ValueError("invalid WAL header")
        checksum = _wal_checksum(header[:24], byte_order)
        if struct.pack(">II", *checksum) != header[24:32]:
            raise ValueError("invalid WAL header checksum")
        wal_salt = header[16:24]
        while True:
            frame_header = wal.read(24)
            if not frame_header:
                break
            if len(frame_header) != 24:
                break
            page = wal.read(PAGE_SIZE)
            if len(page) != PAGE_SIZE:
                break
            page_number, commit_pages = struct.unpack(">II", frame_header[:8])
            if page_number <= 0 or page_number > 1_000_000 or frame_header[8:16] != wal_salt:
                break
            next_checksum = _wal_checksum(frame_header[:8] + page, byte_order, checksum)
            if struct.pack(">II", *next_checksum) != frame_header[16:24]:
                break
            authenticated = page[(SALT_SIZE if page_number == 1 else 0) : PAGE_SIZE - HMAC_SIZE]
            expected = hmac.new(_mac_key(key, database_salt), digestmod=hashlib.sha512)
            expected.update(authenticated)
            expected.update(page_number.to_bytes(4, "little"))
            if not hmac.compare_digest(expected.digest(), page[PAGE_SIZE - HMAC_SIZE :]):
                break
            checksum = next_checksum
            valid_frames.append((page_number, commit_pages, page))
            if commit_pages:
                last_commit_index = len(valid_frames) - 1
                last_commit_pages = commit_pages

    if last_commit_index < 0:
        return 0
    with destination.open("r+b") as output:
        for page_number, _, page in valid_frames[: last_commit_index + 1]:
            output.seek((page_number - 1) * PAGE_SIZE)
            output.write(_decrypt_page(key, page, page_number))
        output.truncate(last_commit_pages * PAGE_SIZE)
    return last_commit_index + 1
