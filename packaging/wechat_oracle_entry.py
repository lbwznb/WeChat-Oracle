"""PyInstaller entry point for the portable Windows build."""
from __future__ import annotations

import multiprocessing
import os
import sys
from pathlib import Path


def prepare_portable_runtime() -> None:
    """Keep mutable config/data beside the executable, never in _MEIPASS."""
    if not getattr(sys, "frozen", False):
        return
    executable_dir = Path(sys.executable).resolve().parent
    os.chdir(executable_dir)
    if len(sys.argv) == 1:
        sys.argv.append("run" if (executable_dir / ".env").exists() else "setup")


def main() -> None:
    multiprocessing.freeze_support()
    prepare_portable_runtime()
    # Import only after chdir: Settings resolves .env and data paths at import.
    from wechat_oracle.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
