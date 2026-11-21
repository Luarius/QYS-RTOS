"""Per-user settings for QYS RTOS."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from . import __version__


@dataclass
class AppSettings:
    workspace: str = ""
    west: str = ""
    sdk: str = ""
    project: str = ""
    board: str = ""
    build_dir: str = ""
    port: str = ""
    ordinary_project: str = ""
    ordinary_environment: str = ""
    ordinary_port: str = ""
    platformio: str = ""


def legacy_config_dir() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home()) / "QYS RTOS"


def settings_path() -> Path:
    override = os.environ.get("QYS_CONFIG_HOME")
    base = Path(override) if override else legacy_config_dir().with_name(f"QYS RTOS {__version__}")
    return base / "settings.json"


def load_settings(path: Path | None = None) -> AppSettings:
    if path is None:
        path = settings_path()
        if not path.exists() and not os.environ.get("QYS_CONFIG_HOME"):
            path = legacy_config_dir() / "settings.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            return AppSettings()
        known = {field.name for field in fields(AppSettings)}
        return AppSettings(**{key: value for key, value in document.items() if key in known and isinstance(value, str)})
    except (OSError, UnicodeError, json.JSONDecodeError):
        return AppSettings()


def save_settings(settings: AppSettings, path: Path | None = None) -> None:
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".qys-settings-", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(asdict(settings), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
