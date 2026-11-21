"""PlatformIO board listings and an offline cache of the last online listing."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .board_support import BOARD_EXTENSIONS, BoardExtension, BoardSupportError


class BoardCatalogError(BoardSupportError):
    """The catalog or selected board/framework combination is invalid."""


CATALOG_LIMIT = 8 * 1024 * 1024
_BOARD_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+\-]{0,95}")
_FRAMEWORK_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,95}")
_PLATFORM = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/)?[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
_JSON_LIST = re.compile(r"(?m)^[ \t]*\[(?=\s*[\[{\]])")


def _platform_id(spec: str) -> str:
    return spec.split("@", 1)[0].removeprefix("platformio/").casefold()


@dataclass(frozen=True)
class CatalogBoard:
    board: str
    title: str
    platform: str
    frameworks: tuple[str, ...]
    vendor: str = ""
    mcu: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return _platform_id(self.platform), self.board.casefold()

    def extension(self, framework: str) -> BoardExtension:
        """Generate a project from the selected registry framework or built-in template."""
        if framework not in (self.frameworks or ("",)):
            raise BoardCatalogError("所选开发板的目录记录没有列出这个框架。")
        for built_in in BOARD_EXTENSIONS:
            if (_platform_id(built_in.platform), built_in.board.casefold(), built_in.framework) == (*self.key, framework):
                return built_in

        filename = "main.c"
        if framework == "arduino":
            filename = "main.cpp"
            source = '#include <Arduino.h>\n\nvoid setup() {\n}\n\nvoid loop() {\n    delay(1000);\n}\n'
        elif framework == "espidf":
            source = '#include "esp_log.h"\n\nvoid app_main(void)\n{\n    ESP_LOGI("qys", "Hello from QYS RTOS");\n}\n'
        elif framework == "zephyr":
            source = '#include <zephyr/sys/printk.h>\n\nint main(void)\n{\n    printk("Hello from QYS RTOS\\n");\n    return 0;\n}\n'
        elif _platform_id(self.platform) == "intel_mcs51":
            source = 'void main(void)\n{\n    for (;;) {\n    }\n}\n'
        else:
            source = 'int main(void)\n{\n    for (;;) {\n    }\n}\n'

        return BoardExtension(
            self.title, self.platform, self.board, framework, "", filename, source,
            f"{framework or '平台默认框架'} · 使用 PlatformIO 的默认烧录方式。",
        )


def builtin_catalog() -> tuple[CatalogBoard, ...]:
    return tuple(
        CatalogBoard(entry.board, entry.title, _platform_id(entry.platform),
                     (entry.framework,) if entry.framework else ())
        for entry in BOARD_EXTENSIONS
    )


def boards_catalog_command(core: Path, *, installed: bool = False) -> list[str]:
    command = [str(core), "--no-ansi", "boards", "--json-output"]
    if installed:
        command.append("--installed")
    return command


def _text(value: object, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _validated_records(data: object) -> tuple[CatalogBoard, ...]:
    if not isinstance(data, list):
        raise BoardCatalogError("PlatformIO 板卡目录不是有效的列表。")
    boards: dict[tuple[str, str], CatalogBoard] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        board, platform = item.get("id"), item.get("platform")
        frameworks = item.get("frameworks", [])
        if (not isinstance(board, str) or not _BOARD_ID.fullmatch(board)
                or not isinstance(platform, str) or not _PLATFORM.fullmatch(platform)
                or not isinstance(frameworks, list)
                or not all(isinstance(name, str) and _FRAMEWORK_ID.fullmatch(name) for name in frameworks)):
            continue
        record = CatalogBoard(board, _text(item.get("name"), 180) or board, platform,
                              tuple(dict.fromkeys(frameworks)), _text(item.get("vendor"), 90),
                              _text(item.get("mcu"), 60))
        boards.setdefault(record.key, record)
    if data and not boards:
        raise BoardCatalogError("PlatformIO 没有返回可用的板卡记录。")
    return tuple(boards.values())


def parse_catalog(output: str) -> tuple[CatalogBoard, ...]:
    """Accept compact or indented JSON after any PlatformIO notices."""
    if len(output) > CATALOG_LIMIT:
        raise BoardCatalogError("板卡目录超过大小上限。")
    decoder = json.JSONDecoder()
    for match in _JSON_LIST.finditer(output):
        try:
            data, _end = decoder.raw_decode(output, output.index("[", match.start()))
        except (ValueError, RecursionError):
            continue
        return _validated_records(data)
    raise BoardCatalogError("未能从 PlatformIO 输出读取板卡目录。")


def load_catalog(path: Path) -> tuple[CatalogBoard, ...]:
    try:
        with path.open("rb") as stream:
            encoded = stream.read(CATALOG_LIMIT + 1)
        if len(encoded) <= CATALOG_LIMIT:
            return parse_catalog(encoded.decode("utf-8"))
    except (OSError, UnicodeError, BoardCatalogError):
        pass
    return ()


def save_catalog(path: Path, boards: tuple[CatalogBoard, ...]) -> None:
    """Replace the last good cache only after writing the complete new catalog."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".qys-catalog-", suffix=".tmp", dir=path.parent)
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump([{"id": board.board, "name": board.title, "platform": board.platform,
                        "frameworks": list(board.frameworks), "vendor": board.vendor, "mcu": board.mcu}
                       for board in boards], stream, ensure_ascii=False, separators=(",", ":"))
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)
