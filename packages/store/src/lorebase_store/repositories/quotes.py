"""Evidence quotes of a purged document (design 9.2 "Purge").

Evidence passages (`{"document_id", "chunk", "start", "end", "quote"}`) and `evidence_gone`
signals (`{"document", ..., "quote"}`) are found anywhere in a JSON document; the quote of
those that name the purged document is replaced by the erased marker, locators unchanged. A
wrapped quote (`{"$sensitive": ...}`) is replaced whole.
"""

from __future__ import annotations

from typing import Any


def erase_quotes(node: Any, document_id: str, erased: str) -> tuple[Any, bool]:
    """`node` with the quotes of `document_id` replaced by `erased`, and whether any was."""
    if isinstance(node, list):
        changed = False
        out_list: list[Any] = []
        for item in node:
            new, hit = erase_quotes(item, document_id, erased)
            out_list.append(new)
            changed = changed or hit
        return out_list, changed
    if not isinstance(node, dict):
        return node, False
    names = node.get("document_id") == document_id or node.get("document") == document_id
    changed = False
    out: dict[str, Any] = {}
    for name, item in node.items():
        if names and name == "quote":
            out[name] = erased
            changed = changed or item != erased
            continue
        new, hit = erase_quotes(item, document_id, erased)
        out[name] = new
        changed = changed or hit
    return out, changed


__all__ = ["erase_quotes"]
