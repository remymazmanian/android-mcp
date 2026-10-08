"""Parse `uiautomator dump` XML into a compact element list, and find elements in it."""

import difflib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_FLAGS = ("scrollable", "long-clickable", "checkable", "checked", "selected", "focused", "password")


@dataclass
class Element:
    index: int
    text: str
    desc: str
    resource_id: str
    cls: str
    package: str
    clickable: bool
    bounds: tuple[int, int, int, int]
    tappable: bool  # the element or one of its ancestors is clickable
    label: str = ""  # descendant text, for clickable containers without text of their own
    flags: tuple[str, ...] = ()
    enabled: bool = True

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"index": self.index}
        if self.text:
            d["text"] = self.text
        if self.desc:
            d["desc"] = self.desc
        if self.resource_id:
            d["resource_id"] = self.resource_id
        d["class"] = self.cls.rsplit(".", 1)[-1]
        d["clickable"] = self.clickable
        if self.label:
            d["label"] = self.label
        for flag in self.flags:
            d[flag.replace("-", "_")] = True
        if not self.enabled:
            d["enabled"] = False
        d["bounds"] = list(self.bounds)
        d["center"] = list(self.center)
        return d

    def describe(self) -> str:
        name = self.text or self.desc or self.label or self.resource_id or self.cls.rsplit(".", 1)[-1]
        return f'[{self.index}] "{name}" at {self.center}'


def _attr_true(node: ET.Element, name: str) -> bool:
    return node.get(name) == "true"


def _descendant_label(node: ET.Element, limit: int = 3) -> str:
    parts: list[str] = []
    for child in node.iter("node"):
        if child is node:
            continue
        value = (child.get("text") or child.get("content-desc") or "").strip()
        if value and value not in parts:
            parts.append(value)
        if len(parts) >= limit:
            break
    label = " | ".join(parts)
    return label if len(label) <= 100 else label[:97] + "..."


def parse_hierarchy(xml_text: str) -> tuple[list[Element], dict[str, Any]]:
    """Return (elements, meta) from a uiautomator dump.

    Keeps nodes that carry text or a content description, or that the user can
    interact with (clickable, long-clickable, scrollable, checkable, editable).
    Plain layout containers and zero-size nodes are dropped.
    """
    root = ET.fromstring(xml_text)
    elements: list[Element] = []
    packages: list[str] = []

    def walk(node: ET.Element, ancestor_clickable: bool) -> None:
        for child in node.findall("node"):
            clickable = _attr_true(child, "clickable")
            text = (child.get("text") or "").strip()
            desc = (child.get("content-desc") or "").strip()
            cls = child.get("class") or ""
            m = _BOUNDS.fullmatch(child.get("bounds") or "")
            bounds = tuple(int(v) for v in m.groups()) if m else (0, 0, 0, 0)
            has_area = bounds[2] > bounds[0] and bounds[3] > bounds[1] and bounds[2] > 0 and bounds[3] > 0
            interactive = (
                clickable
                or _attr_true(child, "long-clickable")
                or _attr_true(child, "scrollable")
                or _attr_true(child, "checkable")
                or cls.endswith("EditText")
            )
            package = child.get("package") or ""
            if package and package not in packages:
                packages.append(package)
            if has_area and (text or desc or interactive):
                flags = tuple(f for f in _FLAGS if _attr_true(child, f))
                if _attr_true(child, "checkable") and "checked" not in flags:
                    flags += ("unchecked",)
                elements.append(
                    Element(
                        index=len(elements),
                        text=text,
                        desc=desc,
                        resource_id=child.get("resource-id") or "",
                        cls=cls,
                        package=package,
                        clickable=clickable,
                        bounds=bounds,  # type: ignore[arg-type]
                        tappable=clickable or ancestor_clickable,
                        label="" if (text or desc or not clickable) else _descendant_label(child),
                        flags=flags,
                        enabled=child.get("enabled") != "false",
                    )
                )
            walk(child, ancestor_clickable or clickable)

    walk(root, False)
    meta = {"rotation": int(root.get("rotation") or 0), "packages": packages}
    return elements, meta


def _norm(s: str) -> str:
    return " ".join(s.split()).casefold()


def _rid_exact(rid: str, wanted: str) -> bool:
    return rid == wanted or rid.endswith(":id/" + wanted)


def find(
    elements: list[Element],
    *,
    text: str | None = None,
    desc: str | None = None,
    resource_id: str | None = None,
) -> list[Element]:
    """Elements matching every given criterion, best tier first.

    Tiers: exact (case/whitespace-insensitive) -> substring -> substring on the
    descendant label of clickable containers. Within a tier, elements that can
    receive a tap (themselves or via a clickable ancestor) come first.
    """
    t, d = (_norm(text) if text else None), (_norm(desc) if desc else None)

    def exact(e: Element) -> bool:
        return (
            (t is None or _norm(e.text) == t)
            and (d is None or _norm(e.desc) == d)
            and (resource_id is None or _rid_exact(e.resource_id, resource_id))
        )

    def contains(e: Element) -> bool:
        return (
            (t is None or t in _norm(e.text))
            and (d is None or d in _norm(e.desc))
            and (resource_id is None or resource_id in e.resource_id)
        )

    def in_label(e: Element) -> bool:
        return (
            (t is None or t in _norm(e.label))
            and (d is None or d in _norm(e.desc) or d in _norm(e.label))
            and (resource_id is None or resource_id in e.resource_id)
        )

    for predicate in (exact, contains, in_label):
        hits = [e for e in elements if predicate(e)]
        if hits:
            return sorted(hits, key=lambda e: not e.tappable)
    return []


def closest(
    elements: list[Element],
    *,
    text: str | None = None,
    desc: str | None = None,
    resource_id: str | None = None,
    limit: int = 5,
) -> list[Element]:
    """Elements whose text/desc/label/resource-id look most like the query (for 'not found' errors)."""
    queries = [_norm(v) for v in (text, desc, resource_id) if v]
    if not queries:
        return []
    scored = []
    for e in elements:
        candidates = [_norm(v) for v in (e.text, e.desc, e.label, e.resource_id.rsplit("/", 1)[-1]) if v]
        if not candidates:
            continue
        score = max(difflib.SequenceMatcher(None, qv, c).ratio() for qv in queries for c in candidates)
        if score >= 0.35:
            scored.append((score, e))
    scored.sort(key=lambda pair: -pair[0])
    return [e for _, e in scored[:limit]]
