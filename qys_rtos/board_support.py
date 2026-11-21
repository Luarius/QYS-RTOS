"""PlatformIO board templates and commands for the ordinary MCU workspace."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .bootstrap import refreshed_windows_environment
from .config import previous_config_dirs, settings_path
from .project import ProjectError


class BoardSupportError(ProjectError):
    """An ordinary MCU project or its tool configuration is invalid."""


@dataclass(frozen=True)
class BoardExtension:
    title: str
    platform: str
    board: str
    framework: str
    upload_protocol: str
    filename: str
    source: str
    description: str
    uploader_package: str = ""

    def project_config(self) -> str:
        lines = ["[env:qys]", f"platform = {self.platform}", f"board = {self.board}"]
        if self.framework:
            lines.append(f"framework = {self.framework}")
        if self.uploader_package:
            lines.append(f"platform_packages = {self.uploader_package}")
        if self.upload_protocol:
            lines.append(f"upload_protocol = {self.upload_protocol}")
        lines.append("monitor_speed = 115200")
        return "\n".join(lines) + "\n"


BOARD_EXTENSIONS = (
    BoardExtension(
        "ESP32-S3 DevKitC-1 N8",
        "platformio/espressif32@6.12.0",
        "esp32-s3-devkitc-1",
        "arduino",
        "esptool",
        "main.cpp",
        """#include <Arduino.h>

void setup() {
    Serial.begin(115200);
}

void loop() {
    Serial.println("Hello from QYS RTOS");
    delay(1000);
}
""",
        "Arduino · esptool。模板用 115200 波特率从 UART 打印日志。选中开发板串口，"
        "无法自动进入下载模式时，按住 BOOT 再按一下 RESET，随后松开 BOOT。",
    ),
    BoardExtension(
        "STM32F103C8T6 · Blue Pill",
        "platformio/ststm32@19.7.1",
        "bluepill_f103c8",
        "stm32cube",
        "stlink",
        "main.c",
        """#include "stm32f1xx_hal.h"

void SysTick_Handler(void) {
    HAL_IncTick();
}

int main(void) {
    HAL_Init();
    __HAL_RCC_GPIOC_CLK_ENABLE();

    GPIO_InitTypeDef led = {0};
    led.Pin = GPIO_PIN_13;
    led.Mode = GPIO_MODE_OUTPUT_PP;
    led.Pull = GPIO_NOPULL;
    led.Speed = GPIO_SPEED_FREQ_LOW;
    HAL_GPIO_Init(GPIOC, &led);

    for (;;) {
        HAL_GPIO_TogglePin(GPIOC, GPIO_PIN_13);
        HAL_Delay(500);
    }
}
""",
        "STM32Cube HAL · ST-Link（SWD）。模板翻转 PC13。通过 ST-Link 烧录，串口可用于监视。",
        "platformio/tool-openocd@~3.1200.7",
    ),
    BoardExtension(
        "STC89C52RC",
        "platformio/intel_mcs51@2.2.0",
        "STC89C52RC",
        "",
        "stcgal",
        "main.c",
        """#include <8051.h>

static void delay_loop(void) {
    for (volatile unsigned int i = 0; i < 50000; ++i) {
    }
}

void main(void) {
    for (;;) {
        P1_0 = !P1_0;
        delay_loop();
    }
}
""",
        "8051 C／SDCC · stcgal。模板翻转 P1.0。选择 USB 转串口，等工具提示后给目标板重新上电。",
        "platformio/tool-stcgal@~1.110.0",
    ),
)

_EXTENSIONS_BY_BOARD = {entry.board.casefold(): entry for entry in BOARD_EXTENSIONS}
_SERIAL_PROTOCOLS = {"arduino", "avr109", "esptool", "serial", "stcgal", "stm32flash", "uart", "wiring"}
_PROBE_PROTOCOLS = {"cmsis-dap", "esp-builtin", "jlink", "stlink", "stlink-gdb-server"}


@dataclass(frozen=True)
class PioEnvironment:
    name: str
    platform: str
    board: str
    upload_protocol: str
    upload_port: str = ""

    @property
    def extension(self) -> BoardExtension | None:
        return _EXTENSIONS_BY_BOARD.get(self.board.casefold())

    @property
    def protocol(self) -> str:
        if self.upload_protocol:
            return self.upload_protocol.lower()
        return self.extension.upload_protocol if self.extension else ""

    @property
    def uses_probe(self) -> bool:
        return self.protocol in _PROBE_PROTOCOLS


def validate_board_project(value: str | Path) -> Path:
    try:
        root = Path(value).expanduser().resolve()
        if (root / "platformio.ini").is_file():
            return root
    except (OSError, RuntimeError, ValueError) as exc:
        raise BoardSupportError(f"工程路径无效：{value}") from exc
    raise BoardSupportError(f"请选择含 platformio.ini 的普通单片机工程目录：{root}")


def create_board_project(destination: Path, extension: BoardExtension) -> Path:
    """Prepare a complete template before exposing the new directory."""
    try:
        target = destination.expanduser().absolute()
        if os.path.lexists(target):
            raise BoardSupportError(f"文件夹已存在：{target}")
        with tempfile.TemporaryDirectory(prefix=".qys-template-", dir=target.parent) as temp:
            staged = Path(temp) / "project"
            (staged / "src").mkdir(parents=True)
            (staged / "platformio.ini").write_text(extension.project_config(), encoding="utf-8")
            (staged / ".gitignore").write_text(".pio/\n", encoding="utf-8")
            (staged / "src" / extension.filename).write_text(extension.source, encoding="utf-8")
            if extension.framework == "espidf":
                (staged / "CMakeLists.txt").write_text(
                    'cmake_minimum_required(VERSION 3.16)\n'
                    'include($ENV{IDF_PATH}/tools/cmake/project.cmake)\nproject(qys_app)\n',
                    encoding="utf-8",
                )
                (staged / "src" / "CMakeLists.txt").write_text(
                    f'idf_component_register(SRCS "{extension.filename}" INCLUDE_DIRS ".")\n',
                    encoding="utf-8",
                )
            elif extension.framework == "zephyr":
                (staged / "zephyr").mkdir()
                (staged / "zephyr" / "prj.conf").write_text("CONFIG_PRINTK=y\n", encoding="utf-8")
                (staged / "zephyr" / "CMakeLists.txt").write_text(
                    'cmake_minimum_required(VERSION 3.20.0)\n'
                    'find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})\nproject(qys_app)\n'
                    f'target_sources(app PRIVATE ../src/{extension.filename})\n',
                    encoding="utf-8",
                )
            if os.path.lexists(target):
                raise BoardSupportError(f"文件夹已存在：{target}")
            staged.rename(target)
        return target.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise BoardSupportError(f"无法创建工程：{exc}") from exc


def pio_environment() -> dict[str, str]:
    environment = refreshed_windows_environment()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment.setdefault("PLATFORMIO_SETTING_ENABLE_TELEMETRY", "no")
    return environment


def managed_core_paths(settings_dir: Path) -> tuple[Path, Path]:
    venv = settings_dir / "platformio-core"
    if os.name == "nt":
        scripts = venv / "Scripts"
        return scripts / "python.exe", scripts / "pio.exe"
    return venv / "bin" / "python", venv / "bin" / "pio"


def find_core(settings_dir: Path, preferred: str = "") -> Path | None:
    if preferred:
        configured = Path(preferred)
        return configured if configured.is_absolute() and configured.is_file() else None
    _python, managed = managed_core_paths(settings_dir)
    core_dir = Path(os.environ.get("PLATFORMIO_CORE_DIR") or Path.home() / ".platformio")
    standard = Path.home() / ".platformio"
    executable = Path("Scripts", "pio.exe") if os.name == "nt" else Path("bin", "pio")
    migrated = tuple(managed_core_paths(previous)[1] for previous in previous_config_dirs()
                     if settings_dir == settings_path().parent)
    candidates = (managed, *migrated, core_dir / "penv" / executable, standard / "penv" / executable)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    search_path = pio_environment().get("PATH")
    found = shutil.which("pio", path=search_path) or shutil.which("platformio", path=search_path)
    return Path(found).resolve() if found else None


def config_command(core: Path, project: Path) -> list[str]:
    return [str(core), "--no-ansi", "project", "config", "--project-dir", str(project), "--json-output"]


def extension_install_command(core: Path, project: Path) -> list[str]:
    return [str(core), "--no-ansi", "pkg", "install", "--project-dir", str(project), "--environment", "qys"]


def run_command(
    core: Path, project: Path, environment: str, *, upload: bool = False, port: str = ""
) -> list[str]:
    target = "upload" if upload else "buildprog"
    command = [
        str(core), "--no-ansi", "run", "--project-dir", str(project),
        "--environment", environment, "--target", target,
    ]
    if upload and port:
        command.extend(["--upload-port", port])
    return command


def parse_project_config(output: str) -> tuple[PioEnvironment, ...]:
    """Read the computed configuration, including inherited project settings."""
    for line in reversed(output.splitlines()):
        if not line.lstrip().startswith("["):
            continue
        try:
            data = json.loads(line)
            environments = []
            for section, entries in data:
                if section.startswith("env:"):
                    options = dict(entries)
                    environments.append(PioEnvironment(
                        section[4:], str(options.get("platform", "")), str(options.get("board", "")),
                        str(options.get("upload_protocol", "")), str(options.get("upload_port", "")),
                    ))
            if environments:
                return tuple(environments)
        except (AttributeError, TypeError, ValueError):
            continue
    raise BoardSupportError("未读取到有效的 PlatformIO 构建环境，请检查 platformio.ini。")


def upload_port_for(environment: PioEnvironment, selected_port: str) -> str:
    """Resolve a serial target explicitly and let debugger protocols find their probe."""
    if environment.uses_probe or not environment.protocol:
        return ""
    if environment.protocol in _SERIAL_PROTOCOLS:
        port = selected_port or environment.upload_port
        if os.name == "nt":
            port = port.upper()
            if not re.fullmatch(r"COM[1-9][0-9]*", port):
                raise BoardSupportError("请刷新串口列表，并为串口烧录选择明确的 COM 口。")
        elif not port.startswith(("/dev/", "COM")):
            raise BoardSupportError("请选择用于烧录的串口。")
        return port
    return selected_port
