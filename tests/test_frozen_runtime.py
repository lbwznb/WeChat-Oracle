import importlib.util
import sys
from pathlib import Path

from wechat_oracle import cli


def _load_portable_entry():
    entry_path = Path(__file__).parents[1] / "packaging" / "wechat_oracle_entry.py"
    spec = importlib.util.spec_from_file_location("wechat_oracle_portable_entry", entry_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_self_command_uses_uv_in_source_mode(monkeypatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)

    assert cli._self_command("doctor") == ["uv", "run", "wechat-oracle", "doctor"]


def test_self_command_reuses_frozen_executable(monkeypatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Portable\WeChatOracle.exe")

    assert cli._self_command("ingest", "ui-live") == [
        r"C:\Portable\WeChatOracle.exe",
        "ingest",
        "ui-live",
    ]


def test_first_portable_launch_enters_setup(monkeypatch, tmp_path: Path) -> None:
    entry = _load_portable_entry()
    exe_path = tmp_path / "WeChatOracle.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_path))
    monkeypatch.setattr(sys, "argv", [str(exe_path)])

    entry.prepare_portable_runtime()

    assert Path.cwd() == tmp_path
    assert sys.argv == [str(exe_path), "setup"]


def test_configured_portable_launch_runs_assistant(monkeypatch, tmp_path: Path) -> None:
    entry = _load_portable_entry()
    exe_path = tmp_path / "WeChatOracle.exe"
    (tmp_path / ".env").write_text("WO_REPLY=False\n", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_path))
    monkeypatch.setattr(sys, "argv", [str(exe_path)])

    entry.prepare_portable_runtime()

    assert Path.cwd() == tmp_path
    assert sys.argv == [str(exe_path), "run"]
