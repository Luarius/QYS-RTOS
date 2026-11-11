import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from qys_rtos.bootstrap import (
    SetupError,
    build_install_plan,
    cmake_version_supported,
    find_python,
    git_network_failure_help,
    prerequisite_status,
    requirements_install_command,
    sanitize_console_output,
    setup_environment,
)


class SetupPlanTests(unittest.TestCase):
    def test_git_network_failures_have_actionable_proxy_and_tls_guidance(self):
        proxy_help = git_network_failure_help("fatal: unable to access: Could not resolve proxy")
        tls_help = git_network_failure_help("SSL certificate problem: unable to get local issuer certificate")

        self.assertIn("http.proxy", proxy_help)
        self.assertIn("不会修改", proxy_help)
        self.assertIn("证书", tls_help)
        self.assertNotIn("sslVerify=false", tls_help)
        self.assertIsNone(git_network_failure_help("fatal: repository not found"))

    def test_git_failure_to_connect_to_loopback_proxy_names_local_proxy_without_echoing_port(self):
        guidance = git_network_failure_help(
            "fatal: unable to access 'https://github.com/zephyrproject-rtos/zephyr': "
            "Failed to connect to 127.0.0.1 port 7890 after 2102 ms: Couldn't connect to server"
        )

        self.assertIsNotNone(guidance)
        self.assertIn("本机代理", guidance)
        self.assertIn("代理客户端", guidance)
        self.assertIn("不会修改", guidance)
        self.assertNotIn("7890", guidance)

    def test_console_output_hides_proxy_credentials_and_proxy_values(self):
        output = (
            "fatal: unable to access 'http://alice:top-secret@proxy.invalid:8443/path'\n"
            "HTTPS_PROXY=http://bob:other-secret@proxy.invalid:8443\n"
            "http.proxy=http://carol:third-secret@proxy.invalid:8443\n"
        )

        safe = sanitize_console_output(output)

        self.assertNotIn("top-secret", safe)
        self.assertNotIn("other-secret", safe)
        self.assertNotIn("third-secret", safe)
        self.assertIn("[凭据已隐藏]", safe)

    def test_plan_uses_isolated_workspace_and_expected_upstream_stages(self):
        with tempfile.TemporaryDirectory(prefix="qys_") as folder:
            destination = Path(folder) / "Zephyr环境"
            plan = build_install_plan(
                destination, ["C:/Python312/python.exe"], "v4.5.0",
                ["arm-zephyr-eabi", "riscv64-zephyr-elf"],
            )
            self.assertFalse(destination.exists(), "planning must not create files")
            self.assertEqual(len(plan), 6)
            self.assertEqual(plan[0].argv[1:3], ("-m", "venv"))
            self.assertIn(str(destination / ".venv"), plan[0].argv)
            self.assertIn("https://github.com/zephyrproject-rtos/zephyr", plan[2].argv)
            self.assertIn("v4.5.0", plan[2].argv)
            self.assertEqual(plan[3].argv[-1], "update")
            self.assertEqual(plan[4].argv[-2:], ("packages", "pip"))
            self.assertIn("arm-zephyr-eabi", plan[5].argv)
            self.assertIn("riscv64-zephyr-elf", plan[5].argv)
            self.assertTrue(all(isinstance(step.argv, tuple) for step in plan))

    def test_rejects_existing_nonempty_directory_without_touching_contents(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "existing"
            destination.mkdir()
            marker = destination / "keep.txt"
            marker.write_text("original", encoding="utf-8")
            with self.assertRaises(SetupError):
                build_install_plan(destination, ["python"], "v4.5.0", ["arm-zephyr-eabi"])
            self.assertEqual(marker.read_text(encoding="utf-8"), "original")

    def test_rejects_empty_python_and_unsafe_revision(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "new"
            with self.assertRaises(SetupError):
                build_install_plan(destination, [], "v4.5.0", ["arm-zephyr-eabi"])
            with self.assertRaises(SetupError):
                build_install_plan(destination, ["python"], "--upload-pack=bad", ["arm-zephyr-eabi"])

    def test_setup_rejects_spaces_in_the_workspace_path(self):
        with tempfile.TemporaryDirectory(prefix="qys_") as folder:
            with self.assertRaises(SetupError):
                build_install_plan(Path(folder) / "Zephyr workspace", ["python"], "v4.5.0", [])

    def test_cmake_must_meet_zephyr_minimum(self):
        self.assertFalse(cmake_version_supported("cmake version 3.27.9"))
        self.assertTrue(cmake_version_supported("cmake version 3.28.0"))
        self.assertTrue(cmake_version_supported("cmake version 4.4.3"))
        self.assertFalse(cmake_version_supported("not cmake"))

    def test_finds_supported_python_after_older_python_on_path(self):
        old_dir, new_dir = "old-python", "new-python"
        old_python, new_python = "old-python/python.exe", "new-python/python.exe"
        env = {"PATH": os.pathsep.join((old_dir, new_dir)), "LOCALAPPDATA": ""}

        def which(name, *, path):
            if name != "python":
                return None
            entries = path.split(os.pathsep)
            if old_dir in entries:
                return old_python
            return new_python if new_dir in entries else None

        def run(argv, **_kwargs):
            return SimpleNamespace(returncode=0 if argv[0] == new_python else 2)

        with patch("qys_rtos.bootstrap.shutil.which", side_effect=which), \
             patch("qys_rtos.bootstrap.subprocess.run", side_effect=run), \
             patch("qys_rtos.bootstrap.sys.frozen", True, create=True):
            self.assertEqual(find_python(env), (new_python,))

    def test_prerequisite_status_detects_wget_without_blocking_optional_tools(self):
        env = {"PATH": "C:/tools"}
        available = {"cmake": "C:/tools/cmake.exe", "wget": "C:/tools/wget.exe"}

        with patch("qys_rtos.bootstrap.shutil.which", side_effect=lambda name, path: available.get(name)), \
             patch(
                 "qys_rtos.bootstrap.subprocess.run",
                 return_value=SimpleNamespace(returncode=0, stdout="cmake version 3.28.0"),
             ), \
             patch("qys_rtos.bootstrap.find_python", return_value=("python",)):
            status = prerequisite_status(env)

        self.assertTrue(status["wget"])
        self.assertFalse(status["gperf"])
        self.assertFalse(status["dtc"])
        self.assertFalse(status["7-Zip"])

    def test_setup_environment_removes_python_path_overrides_without_mutating_parent(self):
        base = {
            "PATH": "C:/existing/tools",
            "PYTHONHOME": "C:/foreign/python",
            "PYTHONPATH": "C:/foreign/modules",
            "ZEPHYR_BASE": "C:/old/zephyr",
        }
        original = dict(base)

        env = setup_environment("C:/zephyrproject", base)

        self.assertNotIn("PYTHONHOME", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertEqual(base, original)
        self.assertEqual(env["PATH"], str(Path("C:/zephyrproject") / ".venv" / "Scripts") + os.pathsep + "C:/existing/tools")

    def test_python_detection_does_not_inherit_foreign_python_overrides(self):
        base = {"PATH": "C:/tools", "PYTHONHOME": "C:/other", "PYTHONPATH": "C:/other/modules"}
        with patch("qys_rtos.bootstrap.shutil.which", return_value="C:/tools/python.exe"), \
             patch("qys_rtos.bootstrap.subprocess.run", return_value=SimpleNamespace(returncode=0)) as probe, \
             patch("qys_rtos.bootstrap.sys.frozen", True, create=True):
            self.assertIsNotNone(find_python(base))
        self.assertNotIn("PYTHONHOME", probe.call_args.kwargs["env"])
        self.assertNotIn("PYTHONPATH", probe.call_args.kwargs["env"])
        self.assertIn("PYTHONHOME", base)

    def test_all_architectures_do_not_add_toolchain_filter(self):
        with tempfile.TemporaryDirectory() as folder:
            plan = build_install_plan(Path(folder) / "new", ["python"], "v4.5.0", [])
            self.assertNotIn("--gnu-toolchains", plan[-1].argv)

    def test_requirements_are_installed_after_west_exits_and_keep_spaces(self):
        with tempfile.TemporaryDirectory(prefix="qys space ") as folder:
            workspace = Path(folder)
            first = workspace / "module with spaces" / "requirements.txt"
            first.parent.mkdir()
            first.write_text("example==1\n", encoding="utf-8")
            second = workspace / "requirements.txt"
            second.write_text("other==1\n", encoding="utf-8")
            argv = requirements_install_command(workspace, f"-r {first}\n-r {second}\n")
            self.assertEqual(argv[1:4], ("-m", "pip", "install"))
            self.assertEqual(argv[4:], ("-r", str(first), "-r", str(second)))

    def test_missing_requirements_do_not_launch_pip(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(SetupError):
                requirements_install_command(Path(folder), "-r nonexistent.txt\n")


if __name__ == "__main__":
    unittest.main()
