"""Conservative serial-port and Zephyr console diagnostics."""

from __future__ import annotations

import errno
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .devicetree import DeviceTreeError, parse_devicetree


@dataclass(frozen=True)
class ConsoleRoute:
    """The console target found in a generated Zephyr DeviceTree."""

    label: str
    status: str | None
    compatible: tuple[str, ...] = ()


@dataclass(frozen=True)
class SerialDiagnostic:
    """A human-readable summary and its best-effort console route."""

    console_route: ConsoleRoute | None
    severity: str
    text: str
    dts_path: Path | None = None


def diagnose_serial_setup(
    build_dir: str | Path | None,
    selected_port: str = "",
    port_info: Any | None = None,
) -> SerialDiagnostic:
    """Summarize port enumeration and the console selected by this build's DTS.

    Only ``<build_dir>/zephyr/zephyr.dts`` is inspected. Device descriptions
    and DTS configuration do not establish that the selected host port is
    physically wired to the configured console UART.
    """
    dts_path: Path | None = None
    try:
        if build_dir is not None and str(build_dir).strip():
            dts_path = Path(build_dir).expanduser() / "zephyr" / "zephyr.dts"
            dts = dts_path.read_text(encoding="utf-8-sig")
        else:
            dts = ""
    except (OSError, RuntimeError, TypeError, ValueError, UnicodeError):
        dts = ""

    prefix = _port_summary(selected_port, port_info)
    if not dts:
        if build_dir is None or not str(build_dir).strip():
            text = (
                prefix
                + "尚未提供有效构建目录，尚不能判断控制台路由。"
                + "请先成功编译，并传入对应的构建目录后复查。"
            )
        else:
            text = (
                prefix
                + "尚不能判断控制台路由：未能读取所选构建目录中的 zephyr/zephyr.dts。"
                + "请确认传入的是本次构建目录；如果尚未编译，请先成功编译后复查。"
            )
        return SerialDiagnostic(
            console_route=None,
            severity="unknown",
            text=text,
            dts_path=dts_path,
        )

    cleaned = _strip_comments(dts)
    chosen = _find_node(cleaned, "chosen")
    console_value = _direct_property(chosen, "zephyr,console") if chosen is not None else None
    if console_value is None:
        return SerialDiagnostic(
            console_route=None,
            severity="unknown",
            text=(
                prefix
                + "未找到 zephyr,console 属性，尚不能判断控制台路由。"
                + "请确认所选构建目录与目标固件一致；必要时重新构建后复查。"
            ),
            dts_path=dts_path,
        )

    target = _console_reference(console_value)
    if target is None:
        return SerialDiagnostic(
            console_route=None,
            severity="unknown",
            text=(
                prefix
                + "找到了 zephyr,console 属性，但无法解析其目标，尚不能判断控制台路由。"
                + "请确认所选构建目录与目标固件一致。"
            ),
            dts_path=dts_path,
        )

    if target.startswith("/"):
        resolved_node = _find_path_node(dts, target)
        node_found = resolved_node is not None
        status_value = resolved_node.properties.get("status") if resolved_node else None
        compatible_value = resolved_node.properties.get("compatible") if resolved_node else None
    else:
        node = _find_labeled_node(cleaned, target)
        node_found = node is not None
        status_value = _direct_property(node, "status") if node is not None else None
        compatible_value = _direct_property(node, "compatible") if node is not None else None

    status_values = _string_list(status_value)
    status = status_values[0] if status_values else None
    compatible = _string_list(compatible_value)
    route = ConsoleRoute(label=target, status=status, compatible=compatible)

    route_text = f"本构建的 zephyr,console 指向 {target}。"
    if not node_found:
        severity = "unknown"
        route_text += "未能在生成的 DTS 中解析对应节点，无法读取其状态。"
    elif status is None:
        severity = "unknown"
        route_text += "该节点未显式声明 status，不能据此判断节点启用状态。"
    elif status.casefold() in {"okay", "ok"}:
        severity = "info"
        route_text += f'该节点的 status 为 "{status}"。'
    elif status.casefold() == "disabled":
        severity = "warning"
        route_text += (
            '该节点在生成的 DTS 中标记为 "disabled"。请复查所选开发板、构建目录'
            "及 overlay 中的串口配置，再重新构建确认。"
        )
    else:
        severity = "warning"
        route_text += (
            f'该节点的 status 为 "{status}"，当前信息不足以确认其可用性；'
            "请复查构建配置。"
        )

    if compatible:
        route_text += " compatible：" + ", ".join(compatible) + "。"
    port_source = "USB 设备枚举信息" if _looks_usb(port_info) else "所选主机串口信息"
    route_text += (
        f"无法仅凭{port_source}与 DTS 路由确认两者物理连接对应；"
        "请按开发板资料核对接口、接线及终端波特率。"
    )
    return SerialDiagnostic(
        console_route=route,
        severity=severity,
        text=prefix + route_text,
        dts_path=dts_path,
    )


def format_serial_error(error: BaseException, *, operation: str = "open") -> str:
    """Explain a serial failure in Chinese without guessing its hardware cause."""
    action = operation.strip().casefold()
    if action in {"open", "connect"}:
        heading = "打开串口失败"
    elif action in {"read", "receive"}:
        heading = "读取串口失败"
    elif action in {"write", "send"}:
        heading = "串口发送失败"
    elif action == "close":
        heading = "关闭串口时遇到问题"
    else:
        heading = "串口操作失败"

    detail = str(error).strip() or type(error).__name__
    if _is_access_denied(error, detail):
        advice = (
            "端口可能被其他程序占用，或当前进程没有访问权限。"
            "请关闭可能使用该端口的终端或烧录工具，刷新端口列表后重试。"
        )
    elif _is_missing_port(error, detail):
        advice = "端口可能已断开或编号变化。请刷新端口列表并重新选择端口后重试。"
    elif _is_timeout(error, detail):
        if action in {"write", "send"}:
            advice = "发送等待超时。请确认设备和波特率设置无误，并检查设备是否仍连接后重试。"
        elif action in {"read", "receive"}:
            advice = "读取等待超时。请确认设备仍连接，并检查端口和波特率设置。"
        else:
            advice = "串口操作等待超时。请确认端口仍可用，刷新端口列表后重试。"
    elif action in {"read", "receive"}:
        advice = "请检查 USB/串口连接和设备供电；必要时刷新端口并重新打开。"
    elif action in {"write", "send"}:
        advice = "请确认设备仍连接，并检查所选端口和波特率设置后重试。"
    elif action == "close":
        advice = "串口可能未能正常释放。请刷新端口列表并重新确认连接状态。"
    else:
        advice = "请确认设备已连接、端口仍可用，并刷新端口列表后重试。"
    return f"{heading}：{advice} 系统信息：{detail}"


def _port_summary(selected_port: str, port_info: Any | None) -> str:
    details: list[str] = []
    device = _info_value(port_info, "device")
    selected = str(selected_port or "").strip()
    if selected:
        details.append(f"所选端口：{selected}。")
    elif device:
        details.append(f"检测到端口：{device}。")

    enumerated: list[str] = []
    for attribute, label in (
        ("description", "描述"),
        ("manufacturer", "厂商"),
        ("product", "产品"),
        ("hwid", "硬件 ID"),
    ):
        value = _info_value(port_info, attribute)
        if value and value.casefold() not in {"n/a", "unknown", "none"}:
            enumerated.append(f"{label} {value}")
    vid = _info_value(port_info, "vid")
    pid = _info_value(port_info, "pid")
    if vid is not None and pid is not None:
        enumerated.append(f"VID:PID {_hex_id(vid)}:{_hex_id(pid)}")
    if enumerated:
        details.append("系统枚举信息：" + "；".join(enumerated) + "。")
    return "".join(details)


def _info_value(info: Any | None, name: str) -> str | None:
    if info is None:
        return None
    try:
        value = info.get(name) if isinstance(info, dict) else getattr(info, name, None)
        if value is None:
            return None
        if isinstance(value, int) and name in {"vid", "pid"}:
            return value
        result = str(value).strip()
        return result or None
    except (AttributeError, TypeError, ValueError):
        return None


def _hex_id(value: str | int) -> str:
    try:
        return f"0x{int(value):04X}"
    except (TypeError, ValueError):
        return str(value)


def _looks_usb(info: Any | None) -> bool:
    if info is None:
        return False
    if _info_value(info, "vid") is not None and _info_value(info, "pid") is not None:
        return True
    metadata = " ".join(
        _info_value(info, attribute) or ""
        for attribute in ("description", "manufacturer", "product", "hwid")
    )
    return "usb" in metadata.casefold()


def _strip_comments(text: str) -> str:
    """Remove DTS comments while leaving quoted text intact."""
    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
        elif char == '"':
            in_string = True
            output.append(char)
            index += 1
        elif char == "/" and following == "/":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
        elif char == "/" and following == "*":
            index += 2
            while index + 1 < len(text) and text[index:index + 2] != "*/":
                if text[index] in "\r\n":
                    output.append(text[index])
                index += 1
            index = min(index + 2, len(text))
        else:
            output.append(char)
            index += 1
    return "".join(output)


def _mask_strings(text: str) -> str:
    """Mask quoted text for declaration searches without shifting offsets."""
    output = list(text)
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if char != '"':
                output[index] = " " if char not in "\r\n" else char
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
    return "".join(output)


def _find_node(text: str, name: str) -> str | None:
    searchable = _mask_strings(text)
    declaration = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}\s*\{{")
    match = declaration.search(searchable)
    return _node_body(text, match.end() - 1) if match else None


def _find_labeled_node(text: str, label: str) -> str | None:
    if label.startswith("/"):
        return None
    searchable = _mask_strings(text)
    declaration = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(label)}\s*:\s*[^\s{{}}]+\s*\{{"
    )
    match = declaration.search(searchable)
    return _node_body(text, match.end() - 1) if match else None


def _find_path_node(text: str, path: str):
    """Resolve an absolute DTS reference using the shared DeviceTree parser."""
    try:
        tree = parse_devicetree(text)
    except DeviceTreeError:
        return None
    if tree.chosen.get("zephyr,console") != path:
        return None
    return tree.find_node(path)


def _node_body(text: str, opening: int) -> str | None:
    depth = 0
    in_string = False
    escaped = False
    for index in range(opening, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[opening + 1:index]
    return None


def _direct_property(body: str | None, name: str) -> str | None:
    if body is None:
        return None
    declaration = re.compile(
        rf"(?<![A-Za-z0-9_,+-]){re.escape(name)}\s*=\s*([^;]+);\s*"
    )
    depth = 0
    in_string = False
    escaped = False
    index = 0
    while index < len(body):
        char = body[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            index += 1
            continue
        if char == "{":
            depth += 1
            index += 1
            continue
        if char == "}":
            depth = max(0, depth - 1)
            index += 1
            continue
        if depth == 0:
            match = declaration.match(body, index)
            if match:
                return match.group(1).strip()
        index += 1
    return None


def _console_reference(value: str) -> str | None:
    match = re.fullmatch(r"\s*&\s*([A-Za-z_][A-Za-z0-9_]*|\{[^{}]+\})\s*", value)
    if not match:
        return None
    reference = match.group(1)
    return reference[1:-1].strip() if reference.startswith("{") else reference


def _string_list(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(re.findall(r'"((?:\\.|[^"\\])*)"', value))


def _is_access_denied(error: BaseException, detail: str) -> bool:
    text = detail.casefold()
    return (
        isinstance(error, PermissionError)
        or getattr(error, "winerror", None) == 5
        or getattr(error, "errno", None) in {errno.EACCES, errno.EPERM}
        or any(marker in text for marker in ("access is denied", "access denied", "permission denied", "拒绝访问"))
    )


def _is_missing_port(error: BaseException, detail: str) -> bool:
    text = detail.casefold()
    return (
        isinstance(error, FileNotFoundError)
        or getattr(error, "winerror", None) == 2
        or getattr(error, "errno", None) == errno.ENOENT
        or any(marker in text for marker in ("cannot find the file", "no such file", "port not found", "device not found", "找不到系统指定的文件"))
    )


def _is_timeout(error: BaseException, detail: str) -> bool:
    text = detail.casefold()
    return (
        isinstance(error, TimeoutError)
        or "timeout" in type(error).__name__.casefold()
        or "timeout" in text
        or "timed out" in text
        or "超时" in detail
    )
