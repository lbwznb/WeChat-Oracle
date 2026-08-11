"""Read-only Windows process scanner for WCDB raw-key cache strings."""
from __future__ import annotations

import ctypes
import hashlib
import os
import re
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

from .crypto import KEY_SIZE, read_page1, verify_page1

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
READABLE = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}
TH32CS_SNAPPROCESS = 0x00000002
ERROR_NO_MORE_FILES = 18
MAX_REGION = 500 * 1024 * 1024
CHUNK_SIZE = 16 * 1024 * 1024
OVERLAP = 256
KEY_PATTERN = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
    ]


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.WORD),
        ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD),
    ]


@dataclass(frozen=True)
class KeyMatch:
    pid: int
    address: int
    key: bytes = field(repr=False)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.key).hexdigest()[:12]


@dataclass
class ScanStats:
    processes_seen: int = 0
    processes_opened: int = 0
    readable_regions: int = 0
    bytes_read: int = 0
    wcdb_markers: int = 0
    salt_markers: int = 0
    candidates_tested: int = 0
    protected_specs: int = 0


def _kernel32():
    if os.name != "nt":
        raise OSError("WeChat process scanning is Windows-only")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.VirtualQueryEx.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t,
    ]
    kernel32.VirtualQueryEx.restype = ctypes.c_size_t
    kernel32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.ReadProcessMemory.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def find_process_ids(image_name: str = "Weixin.exe") -> list[int]:
    kernel32 = _kernel32()
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (0, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    result: list[int] = []
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.casefold() == image_name.casefold():
                result.append(int(entry.th32ProcessID))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return result


def candidate_keys(
    data: bytes,
    target_salt: bytes,
    xor_mask: bytes = b"",
) -> list[tuple[int, bytes]]:
    """Extract raw keys from WCDB x'<key><salt>' cache strings."""
    target_hex = target_salt.hex().casefold()
    results: list[tuple[int, bytes]] = []
    seen: set[bytes] = set()

    def add(position: int, key: bytes) -> None:
        if len(key) == KEY_SIZE and key not in seen:
            seen.add(key)
            results.append((position, key))

    def add_near(position: int) -> None:
        start = max(0, position - 4096)
        end = min(len(data) - KEY_SIZE, position + 4096)
        aligned = start + ((8 - start % 8) % 8)
        for key_position in range(aligned, end + 1, 8):
            add(key_position, data[key_position : key_position + KEY_SIZE])

    if xor_mask:
        if len(xor_mask) != 32:
            raise ValueError("WCDB protection mask must be 32 bytes")
        for position in range(0, max(0, len(data) - 66)):
            if data[position] ^ xor_mask[0] != ord("x") or data[position + 1] ^ xor_mask[1] != ord("'"):
                continue
            decoded = bytearray()
            for offset in range(2, min(195, len(data) - position)):
                value = data[position + offset] ^ xor_mask[offset & 31]
                if chr(value) not in "0123456789abcdefABCDEF":
                    break
                decoded.append(value)
            length = len(decoded)
            close_at = position + 2 + length
            recognized = length in (64, 96) or length > 96 and length % 2 == 0
            if not recognized or close_at >= len(data) or data[close_at] ^ xor_mask[(2 + length) & 31] != ord("'"):
                continue
            text = decoded.decode("ascii")
            if length < 96 or text[-32:].casefold() == target_hex:
                add(position, bytes.fromhex(text[:64]))

    for match in KEY_PATTERN.finditer(data):
        value = match.group(1).decode("ascii").casefold()
        possible: str | None = None
        if len(value) == 64:
            possible = value
        elif len(value) == 96 and value[64:] == target_hex:
            possible = value[:64]
        elif len(value) > 96 and len(value) % 2 == 0 and value[-32:] == target_hex:
            possible = value[:64]
        if possible is not None:
            key = bytes.fromhex(possible)
            add(match.start(), key)

    ascii_salt = target_hex.encode("ascii")
    start = 0
    while (position := data.find(ascii_salt, start)) >= 0:
        if position >= 64:
            encoded_key = data[position - 64 : position]
            if all(chr(value) in "0123456789abcdefABCDEF" for value in encoded_key):
                add(position - 64, bytes.fromhex(encoded_key.decode("ascii")))
        add_near(position)
        start = position + 1

    utf16_salt = target_hex.encode("utf-16le")
    start = 0
    while (position := data.find(utf16_salt, start)) >= 0:
        if position >= 128:
            encoded_key = data[position - 128 : position]
            try:
                text_key = encoded_key.decode("utf-16le")
            except UnicodeDecodeError:
                text_key = ""
            if len(text_key) == 64 and all(value in "0123456789abcdefABCDEF" for value in text_key):
                add(position - 128, bytes.fromhex(text_key))
        add_near(position)
        start = position + 2

    start = 0
    while (position := data.find(target_salt, start)) >= 0:
        if position >= KEY_SIZE:
            add(position - KEY_SIZE, data[position - KEY_SIZE : position])
        after = position + len(target_salt)
        if after + KEY_SIZE <= len(data):
            add(after, data[after : after + KEY_SIZE])
        add_near(position)
        start = position + 1
    return results


def scan_processes(
    database_copy: Path,
    image_name: str = "Weixin.exe",
    stats: ScanStats | None = None,
    xor_mask: bytes = b"",
) -> KeyMatch | None:
    """Return the first HMAC-verified key, retaining it only in memory."""
    page = read_page1(database_copy)
    salt = page[:16]
    kernel32 = _kernel32()
    pids = find_process_ids(image_name)
    stats = stats or ScanStats()
    stats.processes_seen = len(pids)
    for pid in pids:
        handle = kernel32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
        if not handle:
            continue
        stats.processes_opened += 1
        try:
            address = 0
            mbi = MEMORY_BASIC_INFORMATION()
            while address < 0x7FFFFFFFFFFF:
                queried = kernel32.VirtualQueryEx(handle, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi))
                if not queried:
                    break
                base = int(mbi.BaseAddress or 0)
                size = int(mbi.RegionSize)
                next_address = base + size
                protection = int(mbi.Protect) & 0xFF
                if mbi.State == MEM_COMMIT and not (mbi.Protect & PAGE_GUARD) and protection in READABLE and 0 < size < MAX_REGION:
                    stats.readable_regions += 1
                    offset = 0
                    tail = b""
                    while offset < size:
                        requested = min(CHUNK_SIZE, size - offset)
                        buffer = ctypes.create_string_buffer(requested)
                        read = ctypes.c_size_t()
                        ok = kernel32.ReadProcessMemory(handle, ctypes.c_void_p(base + offset), buffer, requested, ctypes.byref(read))
                        if ok and read.value:
                            stats.bytes_read += int(read.value)
                            block = tail + buffer.raw[: read.value]
                            block_base = base + offset - len(tail)
                            stats.wcdb_markers += len(KEY_PATTERN.findall(block))
                            stats.salt_markers += (
                                block.count(salt)
                                + block.count(salt.hex().encode("ascii"))
                                + block.count(salt.hex().encode("utf-16le"))
                            )
                            candidates = candidate_keys(block, salt, xor_mask)
                            stats.candidates_tested += len(candidates)
                            if xor_mask:
                                stats.protected_specs += sum(
                                    1 for position, _ in candidates
                                    if position + 1 < len(block)
                                    and block[position] ^ xor_mask[0] == ord("x")
                                    and block[position + 1] ^ xor_mask[1] == ord("'")
                                )
                            for relative, key in candidates:
                                if verify_page1(key, page):
                                    return KeyMatch(pid, block_base + relative, key)
                            tail = block[-OVERLAP:]
                        else:
                            tail = b""
                        offset += requested
                if next_address <= address:
                    break
                address = next_address
        finally:
            kernel32.CloseHandle(handle)
    return None
