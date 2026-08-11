"""Hash-bound profile for the reviewed signed Weixin 4.1.11.55 build."""
from __future__ import annotations

import hashlib
from pathlib import Path

VERSION = "4.1.11.55"
EXE_SHA256 = "ac599744a7ce7b65640ebe18c939c0d4e4a06cd039d89cddee7f1e9afc56875d"
WCDB_MODULE_SHA256 = "ab925b9428239def44b252d970c337034d75e66b27eb5529633dc10669fc796a"
WCDB_MEMORY_PROTECTION_MASK = bytes([
    0x55, 0xE8, 0x9C, 0x9F, 0xCC, 0x23, 0xE3, 0x48,
    0x2F, 0x46, 0x54, 0xD4, 0xF9, 0xD7, 0x23, 0x7E,
    0x1A, 0xCC, 0x83, 0xE5, 0xCA, 0xD1, 0x41, 0x3C,
    0x7F, 0xC6, 0x59, 0xCB, 0x2A, 0x33, 0xAD, 0xAF,
])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_install(install_root: Path) -> None:
    exe = install_root / "Weixin.exe"
    module = install_root / VERSION / "Weixin.dll"
    if not exe.is_file() or not module.is_file():
        raise RuntimeError("exact Weixin 4.1.11.55 files were not found")
    if _sha256(exe) != EXE_SHA256 or _sha256(module) != WCDB_MODULE_SHA256:
        raise RuntimeError("Weixin build hashes do not match the reviewed 4.1.11.55 profile")
