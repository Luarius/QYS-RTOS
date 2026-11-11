"""Safe plans and prerequisite discovery for guided Zephyr setup.

Commands follow the Zephyr v4.5 Windows getting-started guide. Creating a
plan performs no downloads or writes; the GUI runs each step only when the
user explicitly starts the setup.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


class SetupError(ValueError):
    """The chosen setup cannot proceed without risking existing files."""


_OUTPUT_URL_CREDENTIALS = re.compile(r"(?i)\b((?:https?|socks5h?|ftp)://)([^\s/@?#]+)@")
_OUTPUT_PROXY_VALUE = re.compile(
    r"(?i)\b(http\.proxy|https\.proxy|(?:https?|all|no)_proxy|git_proxy)\s*[:=]\s*([^\s\r\n]+)"
)
_OUTPUT_AUTH_HEADER = re.compile(r"(?i)(\bauthorization\s*:\s*(?:basic|bearer)\s+)\S+")
_OUTPUT_SECRET_PARAMETER = re.compile(r"(?i)([?&](?:access_token|token|password|passwd|secret)=)[^&#\s]+")


def sanitize_console_output(output: str) -> str:
    """Redact common credential forms before command output reaches the UI."""
    output = _OUTPUT_URL_CREDENTIALS.sub(r"\1[凭据已隐藏]@", output)
    output = _OUTPUT_PROXY_VALUE.sub(r"\1=[凭据已隐藏]", output)
    output = _OUTPUT_AUTH_HEADER.sub(r"\1[凭据已隐藏]", output)
    return _OUTPUT_SECRET_PARAMETER.sub(r"\1[凭据已隐藏]", output)


def git_network_failure_help(output: str) -> str | None:
    """Return safe, actionable guidance for common Git network failures.

    The returned text deliberately excludes raw Git output and configured
    proxy values, which can contain credentials.
    """
    message = output.casefold()
    local_proxy_target = re.search(
        r"(?<![\w.])(?:127(?:\.\d{1,3}){3}|localhost|\[::1\]|::1)(?![\w.])",
        message,
    )
    local_connection_failure = any(
        marker in message
        for marker in (
            "connection refused",
            "couldn't connect",
            "could not connect",
            "failed to connect",
            "timed out",
            "timeout",
            "could not resolve proxy",
        )
    )
    if local_proxy_target and local_connection_failure:
        return (
            "GitHub 连接检查无法连接到本机代理（回环地址），代理程序可能未启动或监听端口已变化。"
            "请检查代理客户端是否运行，以及 Git 的 http.proxy、https.proxy 和 HTTP_PROXY、HTTPS_PROXY "
            "是否指向正确端口；仅在确认配置失效后手动修正。向导不会读取并显示代理值，也不会修改任何代理设置。"
        )
    if any(marker in message for marker in ("proxy", "407 proxy", "proxy authentication")):
        return (
            "GitHub 连接失败，Git 输出指向代理故障。请检查 Git 的 http.proxy、https.proxy，"
            "以及 HTTP_PROXY、HTTPS_PROXY 环境变量是否指向当前可用的代理；只修正或清除已确认失效的设置。"
            "向导不会读取并显示代理值，也不会修改任何代理设置。"
        )
    if any(marker in message for marker in ("ssl certificate", "certificate verify failed", "schannel", "tls")):
        return (
            "GitHub TLS 证书验证失败。请检查系统日期和时间，以及当前网络或企业代理所需的根证书；"
            "不要关闭 Git 的证书验证。"
        )
    if any(marker in message for marker in ("could not resolve host", "name or service not known", "getaddrinfo failed")):
        return "无法解析 GitHub 域名。请检查网络连接、DNS、VPN 或防火墙，然后重新开始连接检查。"
    if any(marker in message for marker in ("timed out", "timeout", "connection refused", "failed to connect", "connection reset")):
        return "连接 GitHub 超时或被拒绝。请检查网络、代理和防火墙是否允许 Git 访问 GitHub，然后重新开始连接检查。"
    if any(marker in message for marker in ("authentication failed", "could not read username", "terminal prompts disabled")):
        return "Git 未能完成 GitHub 访问认证。请检查 Git Credential Manager 和网络策略；不要把令牌或凭据粘贴到日志中。"
    return None


@dataclass(frozen=True)
class SetupStep:
    title: str
    argv: tuple[str, ...]
    cwd: Path


def validate_workspace_destination(destination: str | Path) -> Path:
    """Resolve a new workspace path and reject existing project data without writing."""
    if not str(destination).strip():
        raise SetupError("请选择 Zephyr 工作区目录。")
    workspace = Path(destination).expanduser().resolve()
    if any(character.isspace() for character in str(workspace)):
        raise SetupError("Zephyr 官方建议工作区路径不要包含空格。请选择无空格路径，例如 D:\\zephyrproject。")
    if workspace.parent == workspace or not workspace.parent.is_dir():
        raise SetupError("工作区的上一级目录必须存在，且不能选择磁盘根目录。")
    if workspace.exists():
        if not workspace.is_dir():
            raise SetupError("所选工作区路径不是文件夹。")
        try:
            if next(workspace.iterdir(), None) is not None:
                raise SetupError("工作区目录已有文件。请选择一个不存在或为空的目录。")
        except PermissionError as exc:
            raise SetupError(f"无法读取工作区目录：{exc}") from exc
    if any((ancestor / ".west").exists() for ancestor in workspace.parents):
        raise SetupError("新工作区不能放在已有 Zephyr 工作区里面。")
    return workspace


def build_install_plan(
    destination: str | Path,
    python_command: Sequence[str],
    revision: str,
    toolchains: Sequence[str],
) -> list[SetupStep]:
    """Return setup commands without touching destination or the network.

    Existing non-empty folders and folders inside a west workspace are
    rejected, so a guided installation cannot overwrite existing projects.
    An empty ``toolchains`` sequence asks west to install all architectures.
    """
    workspace = validate_workspace_destination(destination)

    python = tuple(str(part) for part in python_command if str(part).strip())
    if not python:
        raise SetupError("没有找到可用于创建虚拟环境的 Python。")
    revision = revision.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/\-]{0,79}", revision) or ".." in revision:
        raise SetupError("Zephyr 版本只能包含字母、数字、点、下划线、斜杠和连字符。")
    selected = tuple(str(t).strip() for t in toolchains if str(t).strip())
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+\-]{0,79}", t) for t in selected):
        raise SetupError("工具链名称格式不正确。")

    venv = workspace / ".venv"
    python_exe = str(venv / "Scripts" / "python.exe")
    west_exe = str(venv / "Scripts" / "west.exe")
    sdk_args: tuple[str, ...] = (
        west_exe, "sdk", "install", "--install-base", str(workspace / ".qys-sdk"),
    )
    if selected:
        sdk_args += ("--gnu-toolchains", *selected)
    return [
        SetupStep("创建 Python 虚拟环境", (*python, "-m", "venv", str(venv)), workspace.parent),
        SetupStep("安装 west", (python_exe, "-m", "pip", "install", "west"), workspace.parent),
        SetupStep("下载 Zephyr 源码", (west_exe, "init", "-m", "https://github.com/zephyrproject-rtos/zephyr", "--mr", revision, str(workspace)), workspace.parent),
        SetupStep("下载 Zephyr 模块", (west_exe, "update"), workspace),
        SetupStep("获取 Python 依赖清单", (west_exe, "packages", "pip"), workspace),
        SetupStep("下载 Zephyr SDK", sdk_args, workspace / "zephyr"),
    ]


def requirements_install_command(workspace: str | Path, output: str) -> tuple[str, ...]:
    """Build a separate pip command from west's requirements-file listing.

    On Windows, installing packages inside the still-running west process can
    fail when pip updates west's own open files. Each file path is kept as one
    argv element, including spaces; the caller executes this only after the
    listing process has exited successfully.
    """
    files: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped.startswith("-r "):
            continue
        item = stripped[3:].strip()
        if len(item) >= 2 and item[0] == item[-1] and item[0] in "\"'":
            item = item[1:-1]
        path = Path(item)
        if not path.is_absolute():
            path = Path(workspace) / path
        if not path.is_file():
            raise SetupError(f"找不到 west 列出的依赖文件：{path}")
        files.append(str(path))
    if not files:
        raise SetupError("west 没有返回 Python 依赖清单，请检查工作区是否下载完整。")
    python = str(Path(workspace) / ".venv" / "Scripts" / "python.exe")
    arguments: list[str] = [python, "-m", "pip", "install"]
    for item in files:
        arguments.extend(("-r", item))
    return tuple(arguments)


def setup_environment(workspace: str | Path, base: dict[str, str] | None = None) -> dict[str, str]:
    """Isolate installation steps from an existing Zephyr selection."""
    env = dict(base if base is not None else os.environ)
    scripts = str(Path(workspace) / ".venv" / "Scripts")
    env["PATH"] = scripts + os.pathsep + env.get("PATH", "")
    env["VIRTUAL_ENV"] = str(Path(workspace) / ".venv")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("ZEPHYR_BASE", None)
    env.pop("ZEPHYR_SDK_INSTALL_DIR", None)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    return env


def refreshed_windows_environment() -> dict[str, str]:
    """Include PATH changes from newly installed winget packages on Windows."""
    env = dict(os.environ)
    if os.name != "nt":
        return env
    try:
        import winreg

        paths = [env.get("PATH", "")]
        for hive, key in (
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            (winreg.HKEY_CURRENT_USER, r"Environment"),
        ):
            try:
                with winreg.OpenKey(hive, key) as handle:
                    value, _ = winreg.QueryValueEx(handle, "Path")
                    paths.append(os.path.expandvars(value))
            except OSError:
                pass
        local_app_data = env.get("LOCALAPPDATA", "")
        if local_app_data:
            paths.extend([
                str(Path(local_app_data) / "Programs" / "Python" / "Python312"),
                str(Path(local_app_data) / "Programs" / "Python" / "Python312" / "Scripts"),
                str(Path(local_app_data) / "Microsoft" / "WindowsApps"),
            ])
        env["PATH"] = os.pathsep.join(part for part in paths if part)
    except (ImportError, OSError):
        pass
    return env


def find_python(env: dict[str, str] | None = None) -> tuple[str, ...] | None:
    """Locate a real Python >=3.12 which can create a virtual environment."""
    current = env or refreshed_windows_environment()
    probe_env = dict(current)
    probe_env.pop("PYTHONHOME", None)
    probe_env.pop("PYTHONPATH", None)
    search_path = current.get("PATH", "")
    candidates: list[tuple[str, ...]] = []
    launcher = shutil.which("py", path=search_path)
    if launcher:
        candidates.append((launcher, "-3.12"))
        candidates.append((launcher, "-3"))
    local_app_data = current.get("LOCALAPPDATA", "")
    if local_app_data:
        known = Path(local_app_data) / "Programs" / "Python" / "Python312" / "python.exe"
        if known.is_file():
            candidates.append((str(known),))
    # A PATH can contain several Python installations. Checking only the
    # first match hides a supported interpreter behind an older one.
    seen_python: set[str] = set()
    for path_entry in search_path.split(os.pathsep):
        if not path_entry:
            continue
        python = shutil.which("python", path=path_entry)
        if python:
            key = os.path.normcase(os.path.normpath(python))
            if key not in seen_python:
                candidates.append((python,))
                seen_python.add(key)
    # sys.executable is the app executable, not Python, when packaged.
    if not getattr(sys, "frozen", False):
        candidates.append((sys.executable,))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for candidate in candidates:
        try:
            result = subprocess.run(
                [*candidate, "-c", "import sys,venv; raise SystemExit(0 if sys.version_info >= (3,12) else 2)"],
                env=probe_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=8, creationflags=flags,
            )
            if result.returncode == 0:
                return candidate
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def cmake_version_supported(output: str) -> bool:
    """Check the documented CMake minimum for the default Zephyr version."""
    match = re.search(r"\bcmake\s+version\s+(\d+)\.(\d+)(?:\.(\d+))?", output, re.IGNORECASE)
    if not match:
        return False
    version = tuple(int(value or 0) for value in match.groups())
    return version >= (3, 28, 0)


def prerequisite_status(env: dict[str, str] | None = None) -> dict[str, bool]:
    """Find the prerequisites Zephyr documents for its Windows setup."""
    current = env or refreshed_windows_environment()
    path = current.get("PATH", "")
    cmake = shutil.which("cmake", path=path)
    cmake_ready = False
    if cmake:
        try:
            result = subprocess.run(
                [cmake, "--version"], env=current, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            cmake_ready = result.returncode == 0 and cmake_version_supported(result.stdout)
        except (OSError, subprocess.SubprocessError):
            pass
    return {
        "Python 3.12+": find_python(current) is not None,
        "Git": shutil.which("git", path=path) is not None,
        "CMake": cmake_ready,
        "Ninja": shutil.which("ninja", path=path) is not None,
        "gperf": shutil.which("gperf", path=path) is not None,
        "dtc": shutil.which("dtc", path=path) is not None,
        "wget": shutil.which("wget", path=path) is not None,
        "7-Zip": shutil.which("7z", path=path) is not None,
        "winget": shutil.which("winget", path=path) is not None,
    }
