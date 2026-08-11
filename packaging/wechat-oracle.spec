# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)


ROOT = Path(SPECPATH).resolve().parent

datas = [
    (str(ROOT / "src" / "wechat_oracle" / "schema.sql"), "wechat_oracle"),
]
datas += collect_data_files(
    "rapidocr_onnxruntime",
    includes=["config.yaml", "models/*.onnx"],
)
datas += collect_data_files(
    "faster_whisper",
    includes=["assets/*.onnx"],
)

binaries = []
binaries += collect_dynamic_libs("ctranslate2")
binaries += collect_dynamic_libs("onnxruntime")

hiddenimports = sorted(set(
    collect_submodules("wx4py")
    + collect_submodules(
        "comtypes",
        filter=lambda name: not name.startswith("comtypes.test"),
    )
    + collect_submodules("faster_whisper")
    + collect_submodules("rapidocr_onnxruntime")
    + collect_submodules("textual.drivers")
    + collect_submodules("textual.widgets")
))

a = Analysis(
    [str(ROOT / "packaging" / "wechat_oracle_entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "experimental"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WeChatOracle",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="WeChatOracle",
)
