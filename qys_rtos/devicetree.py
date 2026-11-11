"""Safe, read-only parsing helpers for Zephyr's generated device tree."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re


class DeviceTreeError(ValueError):
    """Raised when a device tree or an overlay preview input is invalid."""


@dataclass(slots=True)
class Node:
    """One node in a parsed device tree."""

    name: str
    path: str
    labels: tuple[str, ...] = ()
    properties: dict[str, str | None] = field(default_factory=dict)
    children: list[Node] = field(default_factory=list)

    @property
    def label(self) -> str | None:
        """Return the first label when this node has one."""
        return self.labels[0] if self.labels else None


class Tree:
    """Parsed nodes and the node paths exposed through aliases and chosen."""

    def __init__(self, root: Node) -> None:
        self.root = root
        self._nodes: dict[str, Node] = {}
        self._labels: dict[str, Node] = {}
        self._index_nodes(root)
        self.aliases = self._reference_properties("aliases", require_references=True)
        self.chosen = self._reference_properties("chosen", require_references=False)

    def find_node(self, path: str) -> Node | None:
        """Return the node at an absolute path, or ``None`` when absent."""
        if not isinstance(path, str):
            return None
        return self._nodes.get(path)

    def _index_nodes(self, node: Node) -> None:
        if node.path in self._nodes:
            raise DeviceTreeError(f"duplicate node path {node.path!r}")
        self._nodes[node.path] = node
        for label in node.labels:
            if label in self._labels:
                raise DeviceTreeError(f"duplicate node label {label!r}")
            self._labels[label] = node
        for child in node.children:
            self._index_nodes(child)

    def _reference_properties(self, node_name: str, *, require_references: bool) -> dict[str, str]:
        special_node = self._nodes.get(f"/{node_name}")
        if special_node is None:
            return {}

        result: dict[str, str] = {}
        for prop_name, value in special_node.properties.items():
            if value is None:
                continue
            target = _parse_node_reference(value)
            if target is None:
                if require_references:
                    raise DeviceTreeError(f"alias {prop_name!r} is not a node reference")
                continue
            resolved = self._nodes.get(target) if target.startswith("/") else None
            if resolved is None and not target.startswith("/"):
                resolved = self._labels.get(target)
            if resolved is None:
                raise DeviceTreeError(f"unresolved node reference {value.strip()!r} in /{node_name}")
            result[prop_name] = resolved.path
        return result


_LABEL = r"[A-Za-z_][A-Za-z0-9_]*"
_NODE_PART = r"[A-Za-z0-9,_+*#?.-]+"
_NODE_NAME = re.compile(rf"{_NODE_PART}(?:@{_NODE_PART})?\Z", re.ASCII)
_PROPERTY_NAME = re.compile(r"[A-Za-z0-9,_+.#?-]+\Z", re.ASCII)
_LABEL_RE = re.compile(_LABEL, re.ASCII)
_REFERENCE = re.compile(rf"&\s*(?:\{{\s*(.*?)\s*\}}|({_LABEL}))\Z", re.DOTALL)


def _parse_node_reference(value: str) -> str | None:
    """Parse a direct ``&label`` or ``&{/absolute/path}`` value."""
    stripped = value.strip()
    match = _REFERENCE.fullmatch(stripped)
    if not match:
        # A cell array with one phandle is accepted as well; arbitrary cell
        # expressions are not treated as a node path.
        if stripped.startswith("<") and stripped.endswith(">"):
            return _parse_node_reference(stripped[1:-1].strip())
        return None
    if match.group(1) is not None:
        path = match.group(1).strip()
        try:
            _validate_node_path(path)
        except DeviceTreeError:
            return None
        return path
    return match.group(2)


def _mask_comments(source: str) -> str:
    """Replace comments with spaces while preserving quoted strings and lines."""
    chars = list(source)
    i = 0
    while i < len(source):
        if source[i] == '"':
            i = _skip_string(source, i)
            continue
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            if end < 0:
                raise DeviceTreeError("unterminated block comment")
            for pos in range(i, end + 2):
                if chars[pos] not in "\r\n":
                    chars[pos] = " "
            i = end + 2
            continue
        if source.startswith("//", i):
            end = i + 2
            while end < len(source) and source[end] not in "\r\n":
                end += 1
            for pos in range(i, end):
                chars[pos] = " "
            i = end
            continue
        i += 1
    return "".join(chars)


def _skip_string(source: str, start: int) -> int:
    i = start + 1
    while i < len(source):
        if source[i] == "\\":
            i += 2
        elif source[i] == '"':
            return i + 1
        else:
            i += 1
    raise DeviceTreeError("unterminated string")


class _Parser:
    def __init__(self, source: str) -> None:
        if not isinstance(source, str):
            raise DeviceTreeError("device-tree source must be text")
        self.source = _mask_comments(source)
        self.index = 0

    def parse(self) -> Tree:
        self._skip_space()
        self._expect_literal("/dts-v1/")
        self._expect(";")
        self._skip_space()
        if self.source.startswith("/plugin/", self.index):
            self._expect_literal("/plugin/")
            self._expect(";")
            self._skip_space()

        while self.source.startswith("/memreserve/", self.index):
            self._expect_literal("/memreserve/")
            self._skip_to_semicolon("memreserve")
            self._skip_space()

        if self.index >= len(self.source) or self.source[self.index] != "/":
            raise DeviceTreeError("expected the device-tree root node")
        self.index += 1
        self._skip_space()
        self._expect("{")
        root = Node(name="/", path="/")
        self._parse_scope(root, depth=0)
        self._skip_space()
        if self.index != len(self.source):
            raise DeviceTreeError("unexpected data after the root node")
        return Tree(root)

    def _parse_scope(self, parent: Node, *, depth: int) -> None:
        if depth > 128:
            raise DeviceTreeError("device-tree nesting is too deep")
        while True:
            self._skip_space()
            if self.index >= len(self.source):
                raise DeviceTreeError(f"unclosed node {parent.path!r}")
            if self.source[self.index] == "}":
                self.index += 1
                self._skip_space()
                self._expect(";")
                return

            start = self.index
            delimiter = self._find_declaration_delimiter()
            header = self.source[start:delimiter].strip()
            if not header:
                raise DeviceTreeError("expected a node or property declaration")
            marker = self.source[delimiter]

            if marker == "=":
                labels, name = self._parse_labeled_name(header)
                if not _PROPERTY_NAME.fullmatch(name):
                    raise DeviceTreeError(f"invalid property name {name!r}")
                if name in parent.properties:
                    raise DeviceTreeError(f"duplicate property {name!r} in {parent.path}")
                self.index = delimiter + 1
                value = self._read_property_value()
                parent.properties[name] = value
                # Property labels are legal DTS syntax; they do not affect the
                # public node tree, so their declaration is consumed safely.
                del labels
                continue

            if marker == ";":
                labels, name = self._parse_labeled_name(header)
                if labels or not _PROPERTY_NAME.fullmatch(name):
                    raise DeviceTreeError(f"invalid boolean property {header!r}")
                if name in parent.properties:
                    raise DeviceTreeError(f"duplicate property {name!r} in {parent.path}")
                parent.properties[name] = None
                self.index = delimiter + 1
                continue

            if marker == "{":
                labels, name = self._parse_labeled_name(header)
                if not _NODE_NAME.fullmatch(name):
                    raise DeviceTreeError(f"invalid node name {name!r}")
                child_path = f"/{name}" if parent.path == "/" else f"{parent.path}/{name}"
                if any(child.name == name for child in parent.children):
                    raise DeviceTreeError(f"duplicate node {child_path!r}")
                child = Node(name=name, path=child_path, labels=labels)
                parent.children.append(child)
                self.index = delimiter + 1
                self._parse_scope(child, depth=depth + 1)
                continue

            raise DeviceTreeError(f"expected a declaration terminator near {header!r}")

    def _parse_labeled_name(self, header: str) -> tuple[tuple[str, ...], str]:
        labels: list[str] = []
        remaining = header.lstrip()
        while True:
            match = re.match(rf"({_LABEL})\s*:\s*", remaining, re.ASCII)
            if not match:
                break
            labels.append(match.group(1))
            remaining = remaining[match.end():]
        name = remaining.strip()
        if not name:
            raise DeviceTreeError("expected a name after label")
        return tuple(labels), name

    def _find_declaration_delimiter(self) -> int:
        i = self.index
        while i < len(self.source):
            char = self.source[i]
            if char == '"':
                i = _skip_string(self.source, i)
                continue
            if char in "{;=}":
                return i
            i += 1
        raise DeviceTreeError("expected a declaration terminator")

    def _read_property_value(self) -> str:
        start = self.index
        angle = square = paren = brace = 0
        i = start
        while i < len(self.source):
            char = self.source[i]
            if char == '"':
                i = _skip_string(self.source, i)
                continue
            if char == "<":
                angle += 1
            elif char == ">":
                angle -= 1
                if angle < 0:
                    raise DeviceTreeError("unexpected '>' in property value")
            elif char == "[":
                square += 1
            elif char == "]":
                square -= 1
                if square < 0:
                    raise DeviceTreeError("unexpected ']' in property value")
            elif char == "(":
                paren += 1
            elif char == ")":
                paren -= 1
                if paren < 0:
                    raise DeviceTreeError("unexpected ')' in property value")
            elif char == "{":
                brace += 1
            elif char == "}":
                brace -= 1
                if brace < 0:
                    raise DeviceTreeError("expected ';' before the node closes")
            elif char == ";" and not (angle or square or paren or brace):
                value = self.source[start:i].strip()
                if not value:
                    raise DeviceTreeError("expected a property value before ';'")
                self.index = i + 1
                return value
            i += 1
        if angle or square or paren or brace:
            raise DeviceTreeError("unclosed group in property value")
        raise DeviceTreeError("expected ';' after property value")

    def _skip_to_semicolon(self, context: str) -> None:
        while self.index < len(self.source) and self.source[self.index] != ";":
            self.index += 1
        if self.index >= len(self.source):
            raise DeviceTreeError(f"expected ';' after {context}")
        self.index += 1

    def _skip_space(self) -> None:
        while self.index < len(self.source) and self.source[self.index].isspace():
            self.index += 1

    def _expect_literal(self, literal: str) -> None:
        if not self.source.startswith(literal, self.index):
            raise DeviceTreeError(f"expected {literal!r}")
        self.index += len(literal)

    def _expect(self, char: str) -> None:
        self._skip_space()
        if self.index >= len(self.source) or self.source[self.index] != char:
            raise DeviceTreeError(f"expected {char!r}")
        self.index += 1


def parse_devicetree(text: str) -> Tree:
    """Parse DTS text without executing it or loading serialized objects."""
    return _Parser(text).parse()


def read_devicetree(path: str | Path) -> Tree:
    """Read and parse a generated DTS file as UTF-8, without modifying it."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, TypeError, ValueError, UnicodeError) as exc:
        raise DeviceTreeError(f"could not read device tree: {exc}") from exc
    return parse_devicetree(text)


def _validate_node_path(path: str) -> None:
    if not isinstance(path, str) or not path.startswith("/"):
        raise DeviceTreeError("node path must be an absolute device-tree path")
    if path == "/":
        return
    if path.endswith("/") or "//" in path:
        raise DeviceTreeError("node path must not contain empty components")
    for part in path[1:].split("/"):
        if part in {".", ".."} or not _NODE_NAME.fullmatch(part):
            raise DeviceTreeError(f"invalid device-tree path component {part!r}")


def make_status_overlay(path: str, status: str) -> str:
    """Return a reviewable overlay snippet for a validated node status."""
    _validate_node_path(path)
    if not isinstance(status, str) or status not in {"okay", "disabled"}:
        raise DeviceTreeError("status must be exactly 'okay' or 'disabled'")
    return f'&{{{path}}} {{\n    status = "{status}";\n}};'
