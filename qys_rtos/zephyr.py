"""Safe command construction and workspace discovery for Zephyr west."""

from __future__ import annotations

import os
import ntpath
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml


_DEFAULT_WORKSPACE = Path(r"D:\zephyrproject")
_BOARD_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+@-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.+@-]*)*$")
_RUNNER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")


class ZephyrError(ValueError):
    """Raised when a Zephyr environment or command argument is invalid."""


@dataclass(frozen=True)
class ZephyrEnvironment:
    workspace: Path
    west: Path
    python: Path | None = None
    sdk: Path | None = None

    def validate(self) -> list[str]:
        errors: list[str] = []

        if not _is_directory(self.workspace):
            errors.append(f"Zephyr workspace does not exist or is not a directory: {self.workspace}")
        elif not _is_directory(self.workspace / "zephyr"):
            errors.append(
                f"Zephyr source directory was not found at {self.workspace / 'zephyr'}; "
                "select the west workspace root."
            )

        if not _is_file(self.west):
            errors.append(f"west executable was not found: {self.west}; install west or select its executable.")

        if self.python is not None and not _is_file(self.python):
            errors.append(f"Python executable was not found: {self.python}; select a valid Python executable.")

        if self.sdk is not None and not _is_directory(self.sdk):
            errors.append(f"Zephyr SDK directory was not found: {self.sdk}; select a valid SDK installation.")

        return errors

    def process_env(self) -> dict[str, str]:
        """Return a child-process environment configured for this workspace."""
        environment = dict(os.environ)
        path_entries = [self.workspace / ".venv" / "Scripts", self.west.parent]
        inherited_path = environment.get("PATH", "")
        all_entries = [str(path) for path in path_entries]
        if inherited_path:
            all_entries.extend(inherited_path.split(os.pathsep))

        # Avoid repeating a directory when west is already in the workspace venv.
        unique_entries: list[str] = []
        seen: set[str] = set()
        for entry in all_entries:
            if not entry:
                continue
            key = os.path.normcase(os.path.normpath(entry)).casefold()
            if key not in seen:
                unique_entries.append(entry)
                seen.add(key)
        environment["PATH"] = os.pathsep.join(unique_entries)
        environment["ZEPHYR_BASE"] = str(self.workspace / "zephyr")

        if self.sdk is not None and _is_directory(self.sdk):
            environment["ZEPHYR_SDK_INSTALL_DIR"] = str(self.sdk)

        return environment


@dataclass(frozen=True)
class RunnerConfiguration:
    default: str | None
    available: list[str]


def discover_environment() -> ZephyrEnvironment | None:
    """Find an existing workspace without creating or changing any files."""
    candidates: list[Path] = []
    zephyr_base = os.environ.get("ZEPHYR_BASE", "").strip()
    if zephyr_base:
        base_path = Path(zephyr_base).expanduser()
        if base_path.name.casefold() == "zephyr" or (
            (base_path / "CMakeLists.txt").is_file() and (base_path / "boards").is_dir()
        ):
            candidates.append(base_path.parent)
        candidates.append(base_path)
    candidates.extend([_DEFAULT_WORKSPACE, Path.home() / "zephyrproject"])

    unique_candidates: list[Path] = []
    seen_candidates: set[str] = set()
    for candidate in candidates:
        try:
            candidate = candidate.expanduser()
            key = os.path.normcase(os.path.normpath(str(candidate))).casefold()
        except (OSError, ValueError):
            continue
        if key not in seen_candidates:
            unique_candidates.append(candidate)
            seen_candidates.add(key)

    path_west: Path | None = None
    found_path_west = shutil.which("west.exe")
    if found_path_west:
        path_west = Path(found_path_west)

    for workspace in unique_candidates:
        if not _is_directory(workspace) or not _is_directory(workspace / "zephyr"):
            continue

        local_west = workspace / ".venv" / "Scripts" / "west.exe"
        if _is_file(local_west):
            west = local_west
        elif path_west is not None and _is_file(path_west):
            west = path_west
        else:
            continue

        python_candidate = workspace / ".venv" / "Scripts" / "python.exe"
        python = python_candidate if _is_file(python_candidate) else None
        sdk_value = os.environ.get("ZEPHYR_SDK_INSTALL_DIR", "").strip()
        sdk_path = Path(sdk_value).expanduser() if sdk_value else None
        sdk = sdk_path if sdk_path is not None and _is_directory(sdk_path) else None

        return ZephyrEnvironment(
            workspace=workspace,
            west=west,
            python=python,
            sdk=sdk,
        )

    return None


def boards_command(environment: ZephyrEnvironment) -> list[str]:
    return [_absolute_west(environment), "boards", "--all-targets"]


def parse_boards(text: str) -> list[str]:
    """Extract board target names from west output, ignoring diagnostics."""
    boards: set[str] = set()
    for line in text.splitlines():
        value = line.strip()
        if not value or not _BOARD_NAME.fullmatch(value):
            continue
        if value.casefold() in {"board", "boards", "board_name", "board-name"}:
            continue
        boards.add(value)
    return sorted(boards)


def build_command(
    environment: ZephyrEnvironment,
    board: str,
    project: str | os.PathLike[str],
    build_dir: str | os.PathLike[str],
) -> list[str]:
    board_name = _valid_board(board)
    project_path = _path_argument("project", project)
    build_path = _path_argument("build directory", build_dir)
    return [
        _absolute_west(environment),
        "build",
        "-b",
        board_name,
        "-c",
        project_path,
        "-d",
        build_path,
    ]


def validate_build_directory(
    environment: ZephyrEnvironment,
    board: str,
    project: str | os.PathLike[str],
    build_dir: str | os.PathLike[str],
) -> None:
    """Allow a new build folder or verify that an existing one matches.

    Existing build output is only reusable when its CMake cache identifies the
    same board, application, Zephyr source tree, and explicitly selected SDK.
    This function is read-only and never cleans or edits a build directory.
    Relative paths are interpreted from the workspace, as west does when the
    GUI starts its commands with that directory as ``cwd``.
    """
    board_name = _valid_board(board)
    project_path = _path_argument("project", project)
    build_path = _path_argument("build directory", build_dir)
    workspace = Path(environment.workspace).expanduser()
    if not workspace.is_absolute():
        workspace = Path.cwd() / workspace

    resolved_build = _resolve_path_from(build_path, workspace)
    try:
        target_exists = os.path.lexists(resolved_build)
        if not target_exists:
            return
        if not resolved_build.is_dir():
            raise ZephyrError(f"Build directory is not a folder: {resolved_build}")
        has_contents = next(resolved_build.iterdir(), None) is not None
    except ZephyrError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ZephyrError(f"Could not inspect build directory {resolved_build}: {exc}") from exc

    if not has_contents:
        return

    cache_file = resolved_build / "CMakeCache.txt"
    if not cache_file.is_file():
        raise ZephyrError(
            f"Build directory already contains files but has no CMakeCache.txt: {resolved_build}. "
            "Choose an empty folder or a matching Zephyr build directory."
        )

    cache = _read_cmake_cache(cache_file)
    cached_board = _first_cache_value(cache, ("CACHED_BOARD", "BOARD"))
    if cached_board is None:
        raise ZephyrError(f"Cannot verify the existing build directory: CMakeCache.txt has no board value: {cache_file}")
    if cached_board != board_name:
        raise ZephyrError(
            f"Build directory is configured for board '{cached_board}', not '{board_name}'. "
            "Choose a separate empty build directory for this board."
        )

    cached_project = _first_cache_value(
        cache,
        ("APPLICATION_SOURCE_DIR", "APP_DIR", "CMAKE_HOME_DIRECTORY"),
    )
    if cached_project is None:
        raise ZephyrError(
            f"Cannot verify the existing build directory: CMakeCache.txt has no application source path: {cache_file}"
        )
    if not _cache_path_matches(cached_project, project_path, workspace):
        raise ZephyrError(
            f"Build directory is configured for a different project ('{cached_project}'). "
            "Choose a separate empty build directory for this project."
        )

    cached_zephyr_base = _first_cache_value(cache, ("ZEPHYR_BASE",))
    selected_zephyr_base = str(workspace / "zephyr")
    if cached_zephyr_base is None:
        raise ZephyrError(f"Cannot verify the existing build directory: CMakeCache.txt has no ZEPHYR_BASE: {cache_file}")
    if not _cache_path_matches(cached_zephyr_base, selected_zephyr_base, workspace):
        raise ZephyrError(
            f"Build directory has a different ZEPHYR_BASE ('{cached_zephyr_base}'). "
            "Choose a separate empty build directory for this workspace."
        )

    if environment.sdk is not None:
        cached_sdk = _first_cache_value(cache, ("ZEPHYR_SDK_INSTALL_DIR",))
        if cached_sdk is None:
            raise ZephyrError(
                "Cannot verify the selected Zephyr SDK: CMakeCache.txt has no ZEPHYR_SDK_INSTALL_DIR value. "
                "Choose a separate empty build directory."
            )
        if not _cache_path_matches(cached_sdk, str(environment.sdk), workspace):
            raise ZephyrError(
                f"Build directory uses a different Zephyr SDK ('{cached_sdk}'). "
                "Choose a separate empty build directory for this SDK."
            )


def read_runner_configuration(build_dir: str | os.PathLike[str]) -> RunnerConfiguration:
    """Read the runner metadata generated by a successful Zephyr build."""
    build_path = Path(_path_argument("build directory", build_dir))
    runner_file = build_path / "zephyr" / "runners.yaml"
    if not runner_file.is_file():
        return RunnerConfiguration(default=None, available=[])

    try:
        with runner_file.open("r", encoding="utf-8") as source:
            document = yaml.safe_load(source)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ZephyrError(f"Could not read Zephyr runner configuration {runner_file}: {exc}") from exc

    if document is None:
        document = {}
    if not isinstance(document, dict):
        raise ZephyrError(f"Invalid runner configuration in {runner_file}: expected a YAML mapping.")

    raw_runners = document.get("runners", [])
    if raw_runners is None:
        raw_runners = []
    if not isinstance(raw_runners, list):
        raise ZephyrError(f"Invalid runner configuration in {runner_file}: 'runners' must be a list.")

    available: list[str] = []
    seen: set[str] = set()
    for item in raw_runners:
        if not isinstance(item, str):
            raise ZephyrError(f"Invalid runner configuration in {runner_file}: runner names must be strings.")
        name = item.strip()
        if not name or not _RUNNER_NAME.fullmatch(name):
            raise ZephyrError(f"Invalid runner name in {runner_file}: {item!r}.")
        if name not in seen:
            available.append(name)
            seen.add(name)

    raw_default = document.get("flash-runner", document.get("default"))
    default: str | None
    if raw_default is None or raw_default == "":
        default = None
    elif isinstance(raw_default, str) and _RUNNER_NAME.fullmatch(raw_default.strip()):
        default = raw_default.strip()
    else:
        raise ZephyrError(f"Invalid default flash runner in {runner_file}: {raw_default!r}.")

    return RunnerConfiguration(default=default, available=available)


def flash_command(
    environment: ZephyrEnvironment,
    build_dir: str | os.PathLike[str],
    runner: str | None = None,
    port: str | None = None,
    extra_args: Iterable[str] = (),
) -> list[str]:
    build_path = _path_argument("build directory", build_dir)
    configuration = read_runner_configuration(build_path)

    selected_runner = _valid_runner(runner) if runner is not None else configuration.default
    if selected_runner is None:
        raise ZephyrError(
            f"No flash runner is available for {Path(build_path) / 'zephyr' / 'runners.yaml'}; "
            "build the project first or select a runner."
        )
    if configuration.available and selected_runner not in configuration.available:
        options = ", ".join(configuration.available)
        raise ZephyrError(
            f"Runner '{selected_runner}' is not configured for this build."
            + (f" Available runners: {options}." if options else "")
        )

    port_value = _optional_string("port", port)
    runner_key = selected_runner.casefold()
    port_flag: str | None = None
    if runner_key == "esp32":
        port_flag = "--esp-device"
    elif runner_key == "stm32flash":
        port_flag = "--device"
    elif port_value is not None:
        raise ZephyrError(
            f"Runner '{selected_runner}' does not accept a COM port option; "
            "pass runner-specific options in extra_args instead."
        )

    command = [
        _absolute_west(environment),
        "flash",
        "--no-rebuild",
        "-d",
        build_path,
        "-r",
        selected_runner,
    ]
    if port_value is not None and port_flag is not None:
        command.extend([port_flag, port_value])
    command.extend(_extra_arguments(extra_args))
    return command


def _absolute_west(environment: ZephyrEnvironment) -> str:
    try:
        west_path = Path(environment.west).expanduser().resolve()
    except (OSError, TypeError, ValueError) as exc:
        raise ZephyrError(f"Invalid west executable path: {environment.west!r}.") from exc
    if not west_path.is_file():
        raise ZephyrError(f"west executable was not found: {west_path}; install west or select its executable.")
    return str(west_path)


def _valid_board(board: str) -> str:
    if not isinstance(board, str):
        raise ZephyrError("Board target must be a string.")
    value = board.strip()
    if not value:
        raise ZephyrError("Board target cannot be empty; enter a Zephyr board name.")
    if not _BOARD_NAME.fullmatch(value):
        raise ZephyrError(f"Invalid board target {board!r}; use a board name and optional slash qualifiers.")
    return value


def _valid_runner(runner: str) -> str:
    if not isinstance(runner, str):
        raise ZephyrError("Flash runner must be a string.")
    value = runner.strip()
    if not value or not _RUNNER_NAME.fullmatch(value):
        raise ZephyrError(f"Invalid flash runner {runner!r}.")
    return value


def _path_argument(label: str, value: str | os.PathLike[str]) -> str:
    if value is None:
        raise ZephyrError(f"{label.capitalize()} path cannot be empty.")
    try:
        result = os.fspath(value)
    except TypeError as exc:
        raise ZephyrError(f"{label.capitalize()} path must be a string or path.") from exc
    if not isinstance(result, str):
        raise ZephyrError(f"{label.capitalize()} path must be a text path.")
    if not result.strip():
        raise ZephyrError(f"{label.capitalize()} path cannot be empty.")
    if "\0" in result:
        raise ZephyrError(f"{label.capitalize()} path contains a NUL character and is invalid.")
    if os.name == "nt" and _has_invalid_windows_path_component(result):
        raise ZephyrError(f"Invalid Windows {label} path {result!r}.")
    try:
        Path(result)
    except (OSError, TypeError, ValueError) as exc:
        raise ZephyrError(f"Invalid {label} path {result!r}: {exc}") from exc
    return result


def _resolve_path_from(value: str | os.PathLike[str], base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return Path(os.path.abspath(path))


def _read_cmake_cache(cache_file: Path) -> dict[str, str]:
    try:
        content = cache_file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ZephyrError(f"Could not read CMake cache {cache_file}: {exc}") from exc

    values: dict[str, str] = {}
    for line in content.splitlines():
        entry = line.strip()
        if not entry or entry.startswith(("#", "//")):
            continue
        key, separator, typed_value = entry.partition(":")
        _type, equals, value = typed_value.partition("=")
        if not separator or not equals or not key or not _type:
            continue
        values[key] = value
    return values


def _first_cache_value(cache: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = cache.get(key, "").strip()
        if value:
            return value
    return None


def _cache_path_matches(cached_value: str, selected_value: str, base: Path) -> bool:
    cached_path = _resolve_path_from(cached_value, base)
    selected_path = _resolve_path_from(selected_value, base)
    cached_key = _normalized_path_key(cached_path)
    selected_key = _normalized_path_key(selected_path)
    return cached_key == selected_key


def _normalized_path_key(path: Path) -> str:
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        resolved = Path(os.path.abspath(path))
    value = os.path.normpath(str(resolved))
    if os.name == "nt":
        return ntpath.normcase(value).casefold()
    return os.path.normcase(value)


def _has_invalid_windows_path_component(value: str) -> bool:
    normalized = value.replace("/", "\\")
    drive, _ = ntpath.splitdrive(normalized)
    forbidden = set('<>:"|?*')
    if any(character in forbidden - {":"} for character in drive):
        return True

    components = [part for part in normalized.split("\\") if part]
    for index, part in enumerate(components):
        if index == 0 and re.fullmatch(r"[A-Za-z]:", part):
            continue
        if any(character in forbidden for character in part):
            return True
        if part.endswith((" ", ".")):
            return True
        if any(ord(character) < 32 for character in part):
            return True
        device_name = part.split(".", 1)[0].upper()
        if device_name in {"CON", "PRN", "AUX", "NUL"}:
            return True
        if re.fullmatch(r"(?:COM|LPT)[1-9]", device_name):
            return True
    return False


def _optional_string(label: str, value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ZephyrError(f"{label.capitalize()} must be a string.")
    if "\0" in value:
        raise ZephyrError(f"{label.capitalize()} contains a NUL character and is invalid.")
    return value if value.strip() else None


def _extra_arguments(values: Iterable[str]) -> list[str]:
    if isinstance(values, str):
        raise ZephyrError("Extra flash arguments must be provided as a list of argument strings.")
    try:
        arguments = list(values)
    except TypeError as exc:
        raise ZephyrError("Extra flash arguments must be iterable.") from exc
    for argument in arguments:
        if not isinstance(argument, str):
            raise ZephyrError("Each extra flash argument must be a string.")
        if "\0" in argument:
            raise ZephyrError("An extra flash argument contains a NUL character and is invalid.")
    return arguments


def _is_directory(path: Path) -> bool:
    try:
        return Path(path).is_dir()
    except (OSError, TypeError, ValueError):
        return False


def _is_file(path: Path) -> bool:
    try:
        return Path(path).is_file()
    except (OSError, TypeError, ValueError):
        return False
