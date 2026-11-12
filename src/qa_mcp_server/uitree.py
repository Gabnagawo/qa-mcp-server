"""UI Automator hierarchy parsing and selector resolution.

The XML comes from the device, so it is untrusted: it is parsed with
``defusedxml`` (no entity expansion, no external entities) and the output is
capped at ``max_nodes``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from xml.etree.ElementTree import Element  # noqa: S405 (type only; parsing uses defusedxml)

from defusedxml import ElementTree as SafeET

from qa_mcp_server.errors import CommandFailed
from qa_mcp_server.validation import Selector

_BOUNDS_RE = re.compile(r"^\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]$")
_FLAG_ATTRS = ("clickable", "long-clickable", "scrollable", "checkable", "checked", "focused",
               "selected", "password")


@dataclass
class UiNode:
    cls: str
    text: str
    resource_id: str
    content_desc: str
    bounds: tuple[int, int, int, int] | None
    flags: tuple[str, ...]
    package: str
    children: list[UiNode] = field(default_factory=list)

    @property
    def center(self) -> tuple[int, int] | None:
        if self.bounds is None:
            return None
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    @property
    def informative(self) -> bool:
        return bool(
            self.text
            or self.resource_id
            or self.content_desc
            or {"clickable", "scrollable", "checkable", "long-clickable"} & set(self.flags)
        )

    def to_dict(self, children: list[dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {"class": self.cls}
        if self.text:
            out["text"] = self.text
        if self.resource_id:
            out["resource_id"] = self.resource_id
        if self.content_desc:
            out["content_desc"] = self.content_desc
        if self.bounds is not None:
            out["bounds"] = list(self.bounds)
        if self.flags:
            out["flags"] = list(self.flags)
        if children:
            out["children"] = children
        return out


def extract_xml(raw: str) -> str:
    """`uiautomator dump /dev/tty` prints the XML followed by a status line."""
    start = raw.find("<?xml")
    if start < 0:
        start = raw.find("<hierarchy")
    end = raw.rfind("</hierarchy>")
    if start < 0 or end < 0:
        snippet = raw.strip().splitlines()[-1] if raw.strip() else "empty output"
        raise CommandFailed(f"uiautomator dump returned no hierarchy: {snippet[:200]}")
    return raw[start : end + len("</hierarchy>")]


def parse_bounds(value: str) -> tuple[int, int, int, int] | None:
    match = _BOUNDS_RE.match(value.strip())
    if not match:
        return None
    x1, y1, x2, y2 = (int(g) for g in match.groups())
    return x1, y1, x2, y2


def _convert(element: Element) -> UiNode:
    attrs = element.attrib
    flags = [name for name in _FLAG_ATTRS if attrs.get(name) == "true"]
    if attrs.get("enabled") == "false":
        flags.append("disabled")
    password = attrs.get("password") == "true"
    return UiNode(
        cls=attrs.get("class", "").rsplit(".", 1)[-1],
        text="" if password else attrs.get("text", ""),
        resource_id=attrs.get("resource-id", ""),
        content_desc=attrs.get("content-desc", ""),
        bounds=parse_bounds(attrs.get("bounds", "")),
        flags=tuple(flags),
        package=attrs.get("package", ""),
        children=[_convert(child) for child in element if child.tag == "node"],
    )


def parse_hierarchy(raw: str) -> list[UiNode]:
    """Parse dump output into a list of root nodes."""
    xml = extract_xml(raw)
    try:
        root = SafeET.fromstring(xml)
    except Exception as exc:  # defusedxml raises several exception types
        raise CommandFailed(f"invalid UI hierarchy XML: {exc}") from exc
    if root.tag != "hierarchy":
        raise CommandFailed("UI dump root element is not <hierarchy>")
    return [_convert(child) for child in root if child.tag == "node"]


def iter_nodes(nodes: list[UiNode]) -> Iterator[UiNode]:
    """Pre-order traversal (document order)."""
    for node in nodes:
        yield node
        yield from iter_nodes(node.children)


@dataclass
class _Budget:
    remaining: int
    truncated: bool = False


def _compact(node: UiNode, budget: _Budget) -> list[dict[str, Any]]:
    """Drop layout-only wrappers and hoist their children."""
    if budget.remaining <= 0:
        budget.truncated = True
        return []
    if not node.informative:
        hoisted: list[dict[str, Any]] = []
        for child in node.children:
            hoisted.extend(_compact(child, budget))
        return hoisted
    budget.remaining -= 1
    children: list[dict[str, Any]] = []
    for child in node.children:
        children.extend(_compact(child, budget))
    return [node.to_dict(children)]


def _full(node: UiNode, budget: _Budget) -> list[dict[str, Any]]:
    if budget.remaining <= 0:
        budget.truncated = True
        return []
    budget.remaining -= 1
    children: list[dict[str, Any]] = []
    for child in node.children:
        children.extend(_full(child, budget))
    return [node.to_dict(children)]


def to_tree(nodes: list[UiNode], *, compact: bool, max_nodes: int) -> dict[str, Any]:
    budget = _Budget(remaining=max_nodes)
    convert = _compact if compact else _full
    tree: list[dict[str, Any]] = []
    for node in nodes:
        tree.extend(convert(node, budget))
    packages = sorted({n.package for n in iter_nodes(nodes) if n.package})
    return {
        "packages": packages,
        "node_count": max_nodes - budget.remaining,
        "total_nodes": sum(1 for _ in iter_nodes(nodes)),
        "truncated": budget.truncated,
        "nodes": tree,
    }


def find_nodes(nodes: list[UiNode], selector: Selector) -> list[UiNode]:
    """All nodes matching ``selector`` in document order."""
    attr = selector.field
    wanted = selector.value
    matches = []
    for node in iter_nodes(nodes):
        value: str = getattr(node, attr)
        if not value:
            continue
        if selector.match == "exact":
            hit = value == wanted
            # Allow the short form "title_input" for "org.app:id/title_input".
            if not hit and attr == "resource_id" and ":id/" not in wanted:
                hit = value.endswith(f":id/{wanted}")
        else:
            hit = wanted.casefold() in value.casefold()
        if hit and node.bounds is not None:
            matches.append(node)
    return matches
