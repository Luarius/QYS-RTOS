"""Create an NSIS installer which removes only files it installed."""

from __future__ import annotations

import argparse
import os
import runpy
import subprocess
from pathlib import Path

APP_VERSION = runpy.run_path(str(Path(__file__).resolve().parents[1] / "qys_rtos" / "__init__.py"))["__version__"]
APP_NAME = f"QYS RTOS {APP_VERSION}"


def nsis_literal(value: str) -> str:
    return value.replace("$", "$$").replace('"', '$\\"')


def find_nsis_root(compiler: Path) -> Path:
    """Locate NSIS resources for a compiler in either the root or Bin folder."""
    parent = compiler.resolve().parent
    if (parent / "Stubs").is_dir():
        return parent
    if parent.name.casefold() == "bin" and (parent.parent / "Stubs").is_dir():
        return parent.parent
    raise ValueError(f"Could not find NSIS Stubs beside compiler: {compiler}")


def create_script(app_dir: Path, output: Path, icon: Path, qa_target: Path | None = None) -> str:
    app_dir = app_dir.resolve(strict=True)
    if not (app_dir / "QYS RTOS.exe").is_file():
        raise ValueError(f"Missing packaged application: {app_dir}")
    files = sorted((path for path in app_dir.rglob("*") if path.is_file()), key=lambda path: str(path).casefold())
    directories = sorted((path for path in app_dir.rglob("*") if path.is_dir()), key=lambda path: (-len(path.parts), str(path).casefold()))
    deletions = []
    for path in files:
        relative = nsis_literal(str(path.relative_to(app_dir)))
        deletions.append(f'  Delete "$INSTDIR\\{relative}"')
    for path in directories:
        relative = nsis_literal(str(path.relative_to(app_dir)))
        deletions.append(f'  RMDir "$INSTDIR\\{relative}"')
    delete_block = "\n".join(deletions)
    source = nsis_literal(str(app_dir / "*"))
    output_path = nsis_literal(str(output.resolve()))
    icon_path = nsis_literal(str(icon.resolve()))
    resource_version = ".".join([*APP_VERSION.split("."), "0", "0", "0"][:4])
    install_marker = f"qys-installed-{APP_VERSION}.txt"
    uninstall_key = f"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{APP_NAME}"
    if qa_target is not None:
        install_dir = nsis_literal(str(qa_target.resolve()))
        qa_define = '!define QYS_QA_MODE\n'
    else:
        install_dir = f"$LOCALAPPDATA\\Programs\\{APP_NAME}"
        qa_define = ""
    return f'''Unicode True
!include "MUI2.nsh"
!include "LogicLib.nsh"
{qa_define}Name "{APP_NAME}"
OutFile "{output_path}"
InstallDir "{install_dir}"
RequestExecutionLevel user
SetCompressor /SOLID lzma
VIProductVersion "{resource_version}"
VIAddVersionKey "ProductName" "QYS RTOS"
VIAddVersionKey "FileDescription" "QYS RTOS Windows Setup"
VIAddVersionKey "FileVersion" "{APP_VERSION}"
VIAddVersionKey "LegalCopyright" "See THIRD_PARTY_NOTICES.txt"
!define MUI_ICON "{icon_path}"
!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\\QYS RTOS.exe"
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

Var QysDirHandle
Var QysDirEntry

Function .onVerifyInstDir
  Call VerifyInstallDirectory
FunctionEnd

Function VerifyInstallDirectory
  IfFileExists "$INSTDIR\\{install_marker}" valid_folder verify_empty
  verify_empty:
    ClearErrors
    FindFirst $QysDirHandle $QysDirEntry "$INSTDIR\\*"
    IfErrors valid_folder
  inspect_entry:
    StrCmp $QysDirEntry "." next_entry
    StrCmp $QysDirEntry ".." next_entry
    FindClose $QysDirHandle
    Goto invalid_folder
  next_entry:
    ClearErrors
    FindNext $QysDirHandle $QysDirEntry
    IfErrors 0 inspect_entry
    FindClose $QysDirHandle
    Goto valid_folder
  invalid_folder:
    Abort
  valid_folder:
    ClearErrors
FunctionEnd

Section "{APP_NAME}"
  Call VerifyInstallDirectory
  SetShellVarContext current
  SetOutPath "$INSTDIR"
  ClearErrors
  FileOpen $0 "$INSTDIR\\{install_marker}" w
  IfErrors marker_error
  FileWrite $0 "{APP_VERSION}"
  FileClose $0
  IfErrors marker_error
  Goto install_files
  marker_error:
    Abort
  install_files:
  File /r "{source}"
  WriteUninstaller "$INSTDIR\\Uninstall QYS RTOS.exe"
!ifndef QYS_QA_MODE
    WriteRegStr HKCU "{uninstall_key}" "DisplayName" "{APP_NAME}"
    WriteRegStr HKCU "{uninstall_key}" "DisplayVersion" "{APP_VERSION}"
    WriteRegStr HKCU "{uninstall_key}" "DisplayIcon" "$INSTDIR\\QYS RTOS.exe"
    WriteRegStr HKCU "{uninstall_key}" "UninstallString" '"$INSTDIR\\Uninstall QYS RTOS.exe"'
    WriteRegDWORD HKCU "{uninstall_key}" "NoModify" 1
    WriteRegDWORD HKCU "{uninstall_key}" "NoRepair" 1
    CreateDirectory "$SMPROGRAMS\\{APP_NAME}"
    IfFileExists "$SMPROGRAMS\\{APP_NAME}\\{APP_NAME}.lnk" skip_startmenu
    CreateShortcut "$SMPROGRAMS\\{APP_NAME}\\{APP_NAME}.lnk" "$INSTDIR\\QYS RTOS.exe"
    WriteRegDWORD HKCU "{uninstall_key}" "StartShortcutCreated" 1
    skip_startmenu:
    IfFileExists "$DESKTOP\\{APP_NAME}.lnk" skip_desktop
    CreateShortcut "$DESKTOP\\{APP_NAME}.lnk" "$INSTDIR\\QYS RTOS.exe"
    WriteRegDWORD HKCU "{uninstall_key}" "DesktopShortcutCreated" 1
    skip_desktop:
!endif
SectionEnd

Section "Uninstall"
  SetShellVarContext current
!ifndef QYS_QA_MODE
    ReadRegDWORD $0 HKCU "{uninstall_key}" "DesktopShortcutCreated"
    ${{If}} $0 == 1
      Delete "$DESKTOP\\{APP_NAME}.lnk"
    ${{EndIf}}
    ReadRegDWORD $0 HKCU "{uninstall_key}" "StartShortcutCreated"
    ${{If}} $0 == 1
      Delete "$SMPROGRAMS\\{APP_NAME}\\{APP_NAME}.lnk"
    ${{EndIf}}
    RMDir "$SMPROGRAMS\\{APP_NAME}"
    DeleteRegKey HKCU "{uninstall_key}"
!endif
{delete_block}
  Delete "$INSTDIR\\{install_marker}"
  Delete "$INSTDIR\\Uninstall QYS RTOS.exe"
  RMDir "$INSTDIR"
SectionEnd
'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("app_dir", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--compiler", type=Path, required=True)
    parser.add_argument("--qa-target", type=Path, help="Compile a test-only installer confined to test-artifacts")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if not args.compiler.is_file():
        raise SystemExit(f"NSIS compiler not found: {args.compiler}")
    try:
        nsis_root = find_nsis_root(args.compiler)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise SystemExit(f"Installer output already exists; choose a new path: {args.output}")
    script = args.output.parent / "qys-installer.nsi"
    if script.exists():
        raise SystemExit(f"Installer script already exists; choose a new output directory: {script}")
    qa_target = None
    if args.qa_target is not None:
        qa_target = args.qa_target.resolve()
        qa_root = (root / "test-artifacts").resolve()
        if qa_target == qa_root or not qa_target.is_relative_to(qa_root):
            raise SystemExit("QA target must be a child of the project's test-artifacts directory")
        if qa_target.exists() and (not qa_target.is_dir() or any(qa_target.iterdir())):
            raise SystemExit("QA target must not exist or must be empty")
    script.write_text(create_script(args.app_dir, args.output, root / "assets" / "qys_v1_1.ico", qa_target), encoding="utf-8-sig")
    env = dict(os.environ)
    env["NSISDIR"] = str(nsis_root)
    result = subprocess.run([str(args.compiler.resolve()), str(script)], env=env, check=False)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
