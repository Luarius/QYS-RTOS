"""Model endpoint configuration with Windows-protected API credentials."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import tempfile
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .config import legacy_config_dir, settings_path


class AgentConfigError(Exception):
    pass


@dataclass(frozen=True)
class AgentConfig:
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    api_key: str = ""


def agent_config_path() -> Path:
    return settings_path().parent / "agent.json"


def validate_agent_config(config: AgentConfig) -> None:
    try:
        url = urlsplit(config.base_url.strip())
        _ = url.port
    except ValueError as exc:
        raise AgentConfigError("模型接口地址格式不正确。") from exc
    if (url.scheme not in {"http", "https"} or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment):
        raise AgentConfigError("请输入不含账号、参数或片段的完整模型接口地址。")
    if url.scheme == "http" and url.hostname.lower() not in {"localhost", "127.0.0.1", "::1"}:
        raise AgentConfigError("云端模型接口必须使用 HTTPS；本机服务可使用 HTTP。")
    if not config.model.strip() or len(config.model) > 200:
        raise AgentConfigError("请填写模型名称。")


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _protect(data: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise AgentConfigError("保存密钥需要 Windows 用户保护功能。")
    source = ctypes.create_string_buffer(data, len(data))
    incoming = _DataBlob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = _DataBlob()
    crypt32 = ctypes.WinDLL("Crypt32.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    operation.argtypes = [ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    operation.restype = wintypes.BOOL
    if not operation(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise AgentConfigError("Windows 无法保护或读取模型密钥，请重新填写。")
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(outgoing.pbData, wintypes.HLOCAL))


def save_agent_config(config: AgentConfig, path: Path | None = None) -> None:
    validate_agent_config(config)
    path = path or agent_config_path()
    encrypted = base64.b64encode(_protect(config.api_key.encode("utf-8"))).decode("ascii") if config.api_key else ""
    protected_url = base64.b64encode(_protect(config.base_url.strip().encode("utf-8"))).decode("ascii")
    document = {"protected_base_url": protected_url, "model": config.model.strip(), "protected_key": encrypted}
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix=".qys-agent-", suffix=".tmp", dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_agent_config(path: Path | None = None) -> AgentConfig:
    if path is None:
        path = agent_config_path()
        legacy = legacy_config_dir() / "agent.json"
        if not path.exists() and not os.environ.get("QYS_CONFIG_HOME"):
            if legacy.is_file():
                config = load_agent_config(legacy)
                try:
                    save_agent_config(config, path)
                except OSError as exc:
                    raise AgentConfigError("无法迁移模型设置，请重新配置。") from exc
                return config
    if not path.exists():
        return AgentConfig()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        protected = document.get("protected_key", "")
        key = _protect(base64.b64decode(protected, validate=True), decrypt=True).decode("utf-8") if protected else ""
        protected_url = document.get("protected_base_url", "")
        base_url = (_protect(base64.b64decode(protected_url, validate=True), decrypt=True).decode("utf-8")
                    if protected_url else str(document.get("base_url", "")))
        config = AgentConfig(base_url, str(document.get("model", "")), key)
        validate_agent_config(config)
        return config
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError) as exc:
        raise AgentConfigError("无法读取模型设置，请重新配置。") from exc
