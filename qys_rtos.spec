# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller configuration for a relocatable Windows Qt application."""

import os
import re
from PyInstaller.utils.hooks import collect_submodules, copy_metadata

root = SPECPATH
a = Analysis(
    [os.path.join(root, "run_app.py")],
    pathex=[root],
    binaries=[],
    datas=[(os.path.join(root, "assets", "qys_v1_1.ico"), "assets")]
          + copy_metadata("pydantic-ai-slim") + copy_metadata("pydantic-graph")
          + copy_metadata("genai-prices") + copy_metadata("openai"),
    hiddenimports=collect_submodules("serial") + collect_submodules("openai.resources.chat")
                  + collect_submodules("pydantic_ai.profiles"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# Qt for Windows imports the Windows system ICU interface (unversioned
# exports). On a developer machine, PyInstaller can mistakenly collect a
# different icuuc.dll from another product on PATH whose exports are
# version-suffixed, causing QtCore to fail at startup. Let Windows load its
# own system ICU instead of bundling unrelated ICU binaries.
def is_foreign_icu(item):
    name = os.path.basename(item[0]).lower()
    return name == "icuuc.dll" or bool(re.fullmatch(r"icudt\d+\.dll", name))

a.binaries = [item for item in a.binaries if not is_foreign_icu(item)]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="QYS RTOS", debug=False,
    bootloader_ignore_signals=False, strip=False, upx=True, console=False,
    disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
    icon=[os.path.join(root, "assets", "qys_v1_1.ico")],
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=True, upx_exclude=[], name="QYS RTOS")
