import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qys_rtos import zephyr as zephyr_module
from qys_rtos.zephyr import (
    RunnerConfiguration,
    ZephyrEnvironment,
    ZephyrError,
    boards_command,
    build_command,
    discover_environment,
    flash_command,
    parse_boards,
    read_runner_configuration,
)


def make_environment(root: Path, *, sdk: Path | None = None) -> ZephyrEnvironment:
    workspace = root / "Zephyr 工作区"
    (workspace / "zephyr").mkdir(parents=True)
    scripts = workspace / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    west = scripts / "west.exe"
    west.write_text("fake west executable", encoding="utf-8")
    return ZephyrEnvironment(workspace=workspace, west=west, sdk=sdk)


def get_build_directory_validator(test_case: unittest.TestCase):
    validator = getattr(zephyr_module, "validate_build_directory", None)
    test_case.assertTrue(callable(validator), "validate_build_directory API is missing")
    return validator


class ZephyrEnvironmentTests(unittest.TestCase):
    def test_validate_accepts_existing_workspace_and_west(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = make_environment(Path(temporary))

            self.assertEqual(environment.validate(), [])

    def test_validate_reports_actionable_missing_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = ZephyrEnvironment(
                workspace=root / "missing workspace",
                west=root / "missing" / "west.exe",
                python=root / "missing" / "python.exe",
                sdk=root / "missing sdk",
            )

            errors = environment.validate()

            self.assertGreaterEqual(len(errors), 4)
            self.assertTrue(any("workspace" in error.lower() for error in errors))
            self.assertTrue(any("west" in error.lower() for error in errors))
            self.assertTrue(any("python" in error.lower() for error in errors))
            self.assertTrue(any("sdk" in error.lower() for error in errors))

    def test_process_env_prepends_workspace_tools_and_sets_existing_sdk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sdk = root / "Zephyr SDK"
            sdk.mkdir()
            original = make_environment(root, sdk=sdk)
            west_home = root / "separate west bin"
            west_home.mkdir()
            west = west_home / "west.exe"
            west.write_text("fake west executable", encoding="utf-8")
            environment = ZephyrEnvironment(workspace=original.workspace, west=west, sdk=sdk)
            inherited_path = str(root / "system tools")
            with patch.dict(os.environ, {"PATH": inherited_path}, clear=False):
                process_env = environment.process_env()

            path_entries = process_env["PATH"].split(os.pathsep)
            self.assertEqual(path_entries[:2], [
                str(environment.workspace / ".venv" / "Scripts"),
                str(environment.west.parent),
            ])
            self.assertIn(inherited_path, path_entries)
            self.assertEqual(process_env["ZEPHYR_BASE"], str(environment.workspace / "zephyr"))
            self.assertEqual(process_env["ZEPHYR_SDK_INSTALL_DIR"], str(sdk))

    def test_discover_prefers_workspace_west_and_understands_zephyr_base(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "custom workspace"
            (workspace / "zephyr").mkdir(parents=True)
            scripts = workspace / ".venv" / "Scripts"
            scripts.mkdir(parents=True)
            west = scripts / "west.exe"
            west.write_text("local west", encoding="utf-8")
            python = scripts / "python.exe"
            python.write_text("python", encoding="utf-8")
            with patch.dict(os.environ, {"ZEPHYR_BASE": str(workspace / "zephyr")}, clear=False):
                with patch("qys_rtos.zephyr._DEFAULT_WORKSPACE", root / "not-default"):
                    with patch("qys_rtos.zephyr.Path.home", return_value=root / "home"):
                        with patch("qys_rtos.zephyr.shutil.which", return_value=None):
                            environment = discover_environment()

            self.assertIsNotNone(environment)
            self.assertEqual(environment.workspace, workspace)
            self.assertEqual(environment.west, west)
            self.assertEqual(environment.python, python)

    def test_discover_returns_none_when_no_workspace_or_west_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, {"ZEPHYR_BASE": ""}, clear=False):
                with patch("qys_rtos.zephyr._DEFAULT_WORKSPACE", root / "not-default"):
                    with patch("qys_rtos.zephyr.Path.home", return_value=root / "home"):
                        with patch("qys_rtos.zephyr.shutil.which", return_value=None):
                            self.assertIsNone(discover_environment())

    def test_discover_uses_path_west_when_workspace_local_west_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            (workspace / "zephyr").mkdir(parents=True)
            path_west = root / "bin" / "west.exe"
            path_west.parent.mkdir()
            path_west.write_text("path west", encoding="utf-8")
            with patch.dict(os.environ, {"ZEPHYR_BASE": str(workspace / "zephyr")}, clear=False):
                with patch("qys_rtos.zephyr._DEFAULT_WORKSPACE", root / "not-default"):
                    with patch("qys_rtos.zephyr.Path.home", return_value=root / "home"):
                        with patch("qys_rtos.zephyr.shutil.which", return_value=str(path_west)) as which:
                            environment = discover_environment()

            which.assert_called_with("west.exe")
            self.assertEqual(environment.west, path_west)

    def test_discover_checks_default_workspace_before_user_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            default_workspace = root / "default"
            home_workspace = root / "home" / "zephyrproject"
            for workspace in (default_workspace, home_workspace):
                (workspace / "zephyr").mkdir(parents=True)
                scripts = workspace / ".venv" / "Scripts"
                scripts.mkdir(parents=True)
                (scripts / "west.exe").write_text("west", encoding="utf-8")

            with patch.dict(os.environ, {"ZEPHYR_BASE": ""}, clear=False):
                with patch("qys_rtos.zephyr._DEFAULT_WORKSPACE", default_workspace):
                    with patch("qys_rtos.zephyr.Path.home", return_value=root / "home"):
                        with patch("qys_rtos.zephyr.shutil.which", return_value=None):
                            environment = discover_environment()

            self.assertEqual(environment.workspace, default_workspace)


class WestCommandTests(unittest.TestCase):
    def test_boards_command_uses_absolute_west_executable(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = make_environment(Path(temporary))

            self.assertEqual(
                boards_command(environment),
                [str(environment.west.resolve()), "boards", "--all-targets"],
            )

    def test_parse_boards_keeps_valid_qualified_names_sorted_and_unique(self):
        listing = """\
Board name
z_board
esp32c6_devkitc/esp32c6/procpu
nrf52840dk/nrf52840
esp32c6_devkitc/esp32c6/procpu
warning: west did not query optional metadata
No boards found in diagnostic text
"""

        self.assertEqual(
            parse_boards(listing),
            ["esp32c6_devkitc/esp32c6/procpu", "nrf52840dk/nrf52840", "z_board"],
        )

    def test_build_command_preserves_qualified_board_and_unicode_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = make_environment(root)
            project = root / "我的项目 with spaces"
            build = root / "构建目录 with spaces"

            command = build_command(
                environment,
                "esp32c6_devkitc/esp32c6/procpu",
                project,
                build,
            )

            self.assertEqual(
                command,
                [
                    str(environment.west.resolve()),
                    "build",
                    "-b",
                    "esp32c6_devkitc/esp32c6/procpu",
                    "-c",
                    str(project),
                    "-d",
                    str(build),
                ],
            )

    def test_build_command_rejects_empty_board_and_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = make_environment(Path(temporary))
            valid_path = Path(temporary) / "project"
            for board, project, build in (
                ("  ", valid_path, valid_path),
                ("board", "", valid_path),
                ("board", valid_path, "\0invalid"),
            ):
                with self.subTest(board=board, project=project, build=build):
                    with self.assertRaises(ZephyrError):
                        build_command(environment, board, project, build)

            if os.name == "nt":
                with self.assertRaises(ZephyrError):
                    build_command(environment, "board", str(valid_path / "bad?name"), valid_path)


class BuildDirectoryValidationTests(unittest.TestCase):
    board = "esp32c6_devkitc/esp32c6/procpu"

    def _write_cache(
        self,
        build_dir: Path,
        environment: ZephyrEnvironment,
        project: Path,
        *,
        overrides: dict[str, str] | None = None,
    ) -> Path:
        build_dir.mkdir(parents=True, exist_ok=True)
        cache_values = {
            "CACHED_BOARD": self.board,
            "BOARD": self.board,
            "APPLICATION_SOURCE_DIR": str(project),
            "APP_DIR": str(project),
            "CMAKE_HOME_DIRECTORY": str(project),
            "ZEPHYR_BASE": str(environment.workspace / "zephyr"),
        }
        if environment.sdk is not None:
            cache_values["ZEPHYR_SDK_INSTALL_DIR"] = str(environment.sdk)
        cache_values.update(overrides or {})
        cache_lines = [
            f"{key}:PATH={value}" for key, value in cache_values.items()
        ]
        cache_file = build_dir / "CMakeCache.txt"
        cache_file.write_text("\n".join(cache_lines) + "\n", encoding="utf-8")
        return cache_file

    def _validate(
        self,
        test_case: unittest.TestCase,
        environment: ZephyrEnvironment,
        project: Path,
        build_dir: Path,
    ) -> None:
        validator = get_build_directory_validator(test_case)
        validator(environment, self.board, project, build_dir)

    def test_allows_new_or_empty_build_directory_without_creating_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = make_environment(root)
            project = root / "应用 project"
            project.mkdir()
            nonexistent = root / "新 build dir"
            empty = root / "empty build dir"
            empty.mkdir()

            self._validate(self, environment, project, nonexistent)
            self._validate(self, environment, project, empty)

            self.assertFalse(nonexistent.exists())
            self.assertEqual(list(empty.iterdir()), [])

    def test_rejects_nonempty_arbitrary_directory_without_changing_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = make_environment(root)
            project = root / "project"
            project.mkdir()
            build_dir = root / "user data"
            build_dir.mkdir()
            marker = build_dir / "keep.txt"
            marker.write_text("user data", encoding="utf-8")

            with self.assertRaisesRegex(ZephyrError, "CMakeCache"):
                self._validate(self, environment, project, build_dir)

            self.assertEqual(marker.read_text(encoding="utf-8"), "user data")
            self.assertEqual(sorted(path.name for path in build_dir.iterdir()), ["keep.txt"])

    def test_rejects_file_as_build_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = make_environment(root)
            project = root / "project"
            project.mkdir()
            build_file = root / "build file"
            build_file.write_text("leave me alone", encoding="utf-8")

            with self.assertRaisesRegex(ZephyrError, "directory"):
                self._validate(self, environment, project, build_file)

            self.assertEqual(build_file.read_text(encoding="utf-8"), "leave me alone")

    def test_allows_matching_cache_for_spaced_unicode_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sdk = root / "Zephyr SDK with spaces"
            sdk.mkdir()
            environment = make_environment(root, sdk=sdk)
            project = root / "项目 source with spaces"
            project.mkdir()
            build_dir = root / "build output with spaces"
            self._write_cache(build_dir, environment, project)

            self._validate(self, environment, project, build_dir)

    def test_prefers_cached_board_and_normalizes_windows_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = make_environment(root)
            project = root / "project with spaces"
            project.mkdir()
            build_dir = root / "build with spaces"
            self._write_cache(
                build_dir,
                environment,
                project,
                overrides={
                    "APPLICATION_SOURCE_DIR": str(project).swapcase().replace("\\", "/"),
                    "CMAKE_HOME_DIRECTORY": str(project / "wrong").replace("\\", "/"),
                    "BOARD": "different_board",
                    "ZEPHYR_BASE": str(environment.workspace / "zephyr").swapcase().replace("\\", "/"),
                },
            )

            self._validate(self, environment, project, build_dir)

    def test_rejects_cached_board_mismatch_even_if_fallback_board_matches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = make_environment(root)
            project = root / "project"
            project.mkdir()
            build_dir = root / "build"
            self._write_cache(
                build_dir,
                environment,
                project,
                overrides={"CACHED_BOARD": "other_board", "BOARD": self.board},
            )

            with self.assertRaisesRegex(ZephyrError, "board"):
                self._validate(self, environment, project, build_dir)

    def test_rejects_cached_project_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = make_environment(root)
            project = root / "project"
            project.mkdir()
            build_dir = root / "build"
            self._write_cache(
                build_dir,
                environment,
                project,
                overrides={"APPLICATION_SOURCE_DIR": str(root / "another project")},
            )

            with self.assertRaisesRegex(ZephyrError, "project"):
                self._validate(self, environment, project, build_dir)

    def test_rejects_cached_workspace_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = make_environment(root)
            project = root / "project"
            project.mkdir()
            build_dir = root / "build"
            self._write_cache(
                build_dir,
                environment,
                project,
                overrides={"ZEPHYR_BASE": str(root / "another workspace" / "zephyr")},
            )

            with self.assertRaisesRegex(ZephyrError, "ZEPHYR_BASE"):
                self._validate(self, environment, project, build_dir)

    def test_rejects_missing_cache_sdk_for_explicit_sdk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sdk = root / "selected SDK"
            sdk.mkdir()
            environment = make_environment(root, sdk=sdk)
            project = root / "project"
            project.mkdir()
            build_dir = root / "build"
            self._write_cache(build_dir, environment, project)
            cache_file = build_dir / "CMakeCache.txt"
            cache_file.write_text(
                "\n".join(
                    line for line in cache_file.read_text(encoding="utf-8").splitlines()
                    if not line.startswith("ZEPHYR_SDK_INSTALL_DIR:")
                ) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ZephyrError, "ZEPHYR_SDK_INSTALL_DIR"):
                self._validate(self, environment, project, build_dir)

    def test_rejects_cached_sdk_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sdk = root / "selected SDK"
            sdk.mkdir()
            environment = make_environment(root, sdk=sdk)
            project = root / "project"
            project.mkdir()
            build_dir = root / "build"
            self._write_cache(
                build_dir,
                environment,
                project,
                overrides={"ZEPHYR_SDK_INSTALL_DIR": str(root / "other SDK")},
            )

            with self.assertRaisesRegex(ZephyrError, "SDK"):
                self._validate(self, environment, project, build_dir)


class RunnerConfigurationTests(unittest.TestCase):
    def _write_runner_config(self, build: Path, content: str) -> None:
        runner_file = build / "zephyr" / "runners.yaml"
        runner_file.parent.mkdir(parents=True, exist_ok=True)
        runner_file.write_text(content, encoding="utf-8")

    def test_reads_flash_default_and_available_runners(self):
        with tempfile.TemporaryDirectory() as temporary:
            build = Path(temporary) / "build with spaces"
            self._write_runner_config(
                build,
                "runners:\n  - openocd\n  - esp32\nflash-runner: esp32\n",
            )

            self.assertEqual(
                read_runner_configuration(build),
                RunnerConfiguration(default="esp32", available=["openocd", "esp32"]),
            )

    def test_missing_runner_file_returns_empty_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(
                read_runner_configuration(Path(temporary) / "not-built"),
                RunnerConfiguration(default=None, available=[]),
            )

    def test_rejects_malformed_runner_yaml_with_actionable_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            build = Path(temporary) / "bad build"
            self._write_runner_config(build, "runners: [unterminated\n")

            with self.assertRaisesRegex(ZephyrError, "runners.yaml"):
                read_runner_configuration(build)


class FlashCommandTests(unittest.TestCase):
    def _ready_build(self, root: Path, content: str) -> Path:
        build = root / "firmware build"
        runner_file = build / "zephyr" / "runners.yaml"
        runner_file.parent.mkdir(parents=True)
        runner_file.write_text(content, encoding="utf-8")
        return build

    def test_uses_build_default_and_keeps_generic_runner_free_of_port_flags(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = make_environment(root)
            build = self._ready_build(
                root,
                "runners:\n  - openocd\n  - pyocd\nflash-runner: openocd\n",
            )

            command = flash_command(environment, build, port=None, extra_args=("--tool-opt", "value with spaces"))

            self.assertEqual(
                command,
                [
                    str(environment.west.resolve()),
                    "flash",
                    "--no-rebuild",
                    "-d",
                    str(build),
                    "-r",
                    "openocd",
                    "--tool-opt",
                    "value with spaces",
                ],
            )

    def test_maps_esp32_com_port_to_esp_device(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = make_environment(root)
            build = self._ready_build(root, "runners: [esp32]\nflash-runner: esp32\n")

            command = flash_command(environment, build, port="COM8")

            self.assertEqual(command[-4:], ["-r", "esp32", "--esp-device", "COM8"])

    def test_maps_stm32flash_com_port_to_device(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = make_environment(root)
            build = self._ready_build(root, "runners: [stm32flash]\nflash-runner: stm32flash\n")

            command = flash_command(environment, build, port="COM11")

            self.assertEqual(command[-4:], ["-r", "stm32flash", "--device", "COM11"])

    def test_rejects_com_port_for_generic_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = make_environment(root)
            build = self._ready_build(root, "runners: [nrfjprog]\nflash-runner: nrfjprog\n")

            with self.assertRaisesRegex(ZephyrError, "does not accept a COM port"):
                flash_command(environment, build, port="COM5")

    def test_requires_runner_metadata_before_flashing(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = make_environment(Path(temporary))

            with self.assertRaisesRegex(ZephyrError, "runner"):
                flash_command(environment, Path(temporary) / "not-built")


if __name__ == "__main__":
    unittest.main()
