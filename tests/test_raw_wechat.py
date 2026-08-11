import hashlib
import hmac
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wechat_oracle.raw_wechat.crypto import (
    HMAC_SIZE,
    PAGE_SIZE,
    RESERVE_SIZE,
    WAL_VERSION,
    _wal_checksum,
    apply_wal,
    decrypt_database,
    key_fingerprint,
    verify_page1,
)
from wechat_oracle.raw_wechat import inventory
from wechat_oracle.raw_wechat.profile_41155 import WCDB_MEMORY_PROTECTION_MASK
from wechat_oracle.raw_wechat.win_memory import candidate_keys


def _authenticated_page(key: bytes, salt: bytes) -> bytes:
    page = bytearray((index * 17 + 3) % 256 for index in range(PAGE_SIZE))
    page[:16] = salt
    mac_salt = bytes(value ^ 0x3A for value in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, dklen=32)
    digest = hmac.new(mac_key, page[16 : PAGE_SIZE - HMAC_SIZE], hashlib.sha512)
    digest.update((1).to_bytes(4, "little"))
    page[PAGE_SIZE - HMAC_SIZE :] = digest.digest()
    return bytes(page)


def test_verify_page1_accepts_only_matching_raw_key() -> None:
    key = bytes(range(32))
    page = _authenticated_page(key, bytes(range(16, 32)))
    assert verify_page1(key, page)
    assert not verify_page1(bytes(reversed(key)), page)
    assert len(key_fingerprint(key)) == 12


def test_decrypt_database_authenticates_every_main_page(tmp_path: Path) -> None:
    key = bytes(range(32))
    salt = bytes(range(16, 32))
    first = _authenticated_page(key, salt)
    second = bytearray((index * 9 + 5) % 256 for index in range(PAGE_SIZE))
    mac_salt = bytes(value ^ 0x3A for value in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, dklen=32)
    digest = hmac.new(mac_key, second[: PAGE_SIZE - HMAC_SIZE], hashlib.sha512)
    digest.update((2).to_bytes(4, "little"))
    second[PAGE_SIZE - HMAC_SIZE :] = digest.digest()
    source = tmp_path / "source.db"
    source.write_bytes(first + second)
    assert decrypt_database(key, source, tmp_path / "plain.db") == 2

    tampered = bytearray(source.read_bytes())
    tampered[PAGE_SIZE + 20] ^= 1
    bad = tmp_path / "bad.db"
    bad.write_bytes(tampered)
    with pytest.raises(ValueError, match="page 2"):
        decrypt_database(key, bad, tmp_path / "bad-plain.db")


def test_candidate_keys_matches_wcdb_key_and_salt_without_formatting_secret() -> None:
    key = bytes(range(32))
    salt = bytes(range(32, 48))
    encoded = b"prefix x'" + key.hex().encode() + salt.hex().encode() + b"' suffix"
    matches = candidate_keys(encoded, salt)
    assert sum(item[1] == key for item in matches) == 1
    assert key.hex() not in repr(matches)


def test_candidate_keys_supports_utf16_and_raw_adjacent_layouts() -> None:
    key = bytes(range(48, 80))
    salt = bytes(range(16))
    utf16 = (key.hex() + salt.hex()).encode("utf-16le")
    raw = key + salt
    assert any(item[1] == key for item in candidate_keys(b"prefix" + utf16, salt))
    assert any(item[1] == key for item in candidate_keys(b"prefix" + raw, salt))


def test_candidate_keys_decodes_41155_xor_protected_spec() -> None:
    key = bytes(range(32))
    salt = bytes(range(160, 176))
    plain = b"x'" + key.hex().encode() + salt.hex().encode() + b"'"
    protected = bytes(value ^ WCDB_MEMORY_PROTECTION_MASK[index & 31] for index, value in enumerate(plain))
    matches = candidate_keys(b"prefix" + protected + b"suffix", salt, WCDB_MEMORY_PROTECTION_MASK)
    assert any(item[1] == key for item in matches)


def test_apply_wal_authenticates_and_patches_committed_frame(tmp_path: Path) -> None:
    key = bytes(range(32))
    salt = bytes(range(16, 32))
    iv = bytes(range(32, 48))
    plaintext = bytes((index * 11 + 7) % 256 for index in range(PAGE_SIZE - RESERVE_SIZE))
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    encrypted = encryptor.update(plaintext) + encryptor.finalize()
    page = bytearray(encrypted + iv + bytes(HMAC_SIZE))
    mac_salt = bytes(value ^ 0x3A for value in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, dklen=32)
    digest = hmac.new(mac_key, page[: PAGE_SIZE - HMAC_SIZE], hashlib.sha512)
    digest.update((2).to_bytes(4, "little"))
    page[PAGE_SIZE - HMAC_SIZE :] = digest.digest()

    destination = tmp_path / "decrypted.db"
    destination.write_bytes(bytes(PAGE_SIZE * 2))
    wal = tmp_path / "source.db-wal"
    header = bytearray(struct.pack(">IIII", 0x377F0682, WAL_VERSION, PAGE_SIZE, 1))
    header[16:24] = b"wal-salt"
    checksum = _wal_checksum(header[:24], "<")
    header.extend(struct.pack(">II", *checksum))
    frame_prefix = struct.pack(">II", 2, 2) + b"wal-salt"
    checksum = _wal_checksum(frame_prefix[:8] + page, "<", checksum)
    frame = frame_prefix + struct.pack(">II", *checksum)
    wal.write_bytes(header + frame + page)

    assert apply_wal(key, salt, wal, destination) == 1
    result = destination.read_bytes()
    assert len(result) == PAGE_SIZE * 2
    assert result[PAGE_SIZE : PAGE_SIZE + len(plaintext)] == plaintext
    assert result[-RESERVE_SIZE:] == bytes(RESERVE_SIZE)


def test_apply_wal_ignores_valid_but_uncommitted_tail(tmp_path: Path) -> None:
    key = bytes(range(32))
    salt = bytes(range(16, 32))

    def encrypted_page(seed: int, page_number: int) -> tuple[bytes, bytes]:
        iv = bytes((seed + index) % 256 for index in range(16))
        plaintext = bytes((seed + index * 3) % 256 for index in range(PAGE_SIZE - RESERVE_SIZE))
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        page = bytearray(encryptor.update(plaintext) + encryptor.finalize() + iv + bytes(HMAC_SIZE))
        mac_salt = bytes(value ^ 0x3A for value in salt)
        mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, dklen=32)
        digest = hmac.new(mac_key, page[: PAGE_SIZE - HMAC_SIZE], hashlib.sha512)
        digest.update(page_number.to_bytes(4, "little"))
        page[PAGE_SIZE - HMAC_SIZE :] = digest.digest()
        return bytes(page), plaintext

    committed_page, committed_plaintext = encrypted_page(10, 2)
    tail_page, _ = encrypted_page(20, 2)
    header = bytearray(struct.pack(">IIII", 0x377F0682, WAL_VERSION, PAGE_SIZE, 1))
    header.extend(b"wal-salt")
    checksum = _wal_checksum(header[:24], "<")
    header.extend(struct.pack(">II", *checksum))
    frames = bytearray()
    for commit_pages, page in ((2, committed_page), (0, tail_page)):
        prefix = struct.pack(">II", 2, commit_pages) + b"wal-salt"
        checksum = _wal_checksum(prefix[:8] + page, "<", checksum)
        frames.extend(prefix + struct.pack(">II", *checksum) + page)

    destination = tmp_path / "decrypted.db"
    destination.write_bytes(bytes(PAGE_SIZE * 2))
    wal = tmp_path / "source.db-wal"
    wal.write_bytes(header + frames)
    assert apply_wal(key, salt, wal, destination) == 1
    result = destination.read_bytes()
    assert result[PAGE_SIZE : PAGE_SIZE + len(committed_plaintext)] == committed_plaintext


def test_apply_wal_rejects_bad_header_checksum(tmp_path: Path) -> None:
    header = bytearray(struct.pack(">IIII", 0x377F0682, WAL_VERSION, PAGE_SIZE, 1))
    header.extend(b"wal-salt")
    header.extend(struct.pack(">II", *_wal_checksum(header[:24], "<")))
    header[-1] ^= 0x01
    wal = tmp_path / "source.db-wal"
    wal.write_bytes(header + bytes(24 + PAGE_SIZE))
    destination = tmp_path / "decrypted.db"
    destination.write_bytes(bytes(PAGE_SIZE))
    with pytest.raises(ValueError, match="header checksum"):
        apply_wal(bytes(range(32)), bytes(range(16)), wal, destination)


def test_discovery_includes_all_numeric_message_shards(tmp_path: Path, monkeypatch) -> None:
    account = tmp_path / "account-a"
    message_dir = account / "db_storage" / "message"
    message_dir.mkdir(parents=True)
    for name in ("message_0.db", "message_3.db", "message_fts.db", "message_bad.db"):
        (message_dir / name).write_bytes(bytes(PAGE_SIZE))
    monkeypatch.setattr(inventory, "likely_roots", lambda: [tmp_path])

    candidates = inventory.discover_message_databases()
    assert [candidate.source.name for candidate in candidates] == ["message_0.db", "message_3.db"]
    assert len({candidate.account_fingerprint for candidate in candidates}) == 1


def test_stage_database_publishes_database_and_wal_only(tmp_path: Path, monkeypatch) -> None:
    account = tmp_path / "account-a"
    message_dir = account / "db_storage" / "message"
    message_dir.mkdir(parents=True)
    source = message_dir / "message_0.db"
    source.write_bytes(b"d" * PAGE_SIZE)
    Path(f"{source}-wal").write_bytes(b"w" * 64)
    Path(f"{source}-shm").write_bytes(b"s" * 64)
    monkeypatch.setattr(inventory, "likely_roots", lambda: [tmp_path])
    candidate = inventory.discover_message_databases()[0]

    staged = inventory.stage_database(candidate, tmp_path / "workspace")
    assert staged.read_bytes() == source.read_bytes()
    assert Path(f"{staged}-wal").read_bytes() == Path(f"{source}-wal").read_bytes()
    assert not Path(f"{staged}-shm").exists()
