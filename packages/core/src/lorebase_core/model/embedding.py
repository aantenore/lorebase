"""Embedding templates (design 8.4, 9.3): the text of a Record that Scribe embeds.

A Kind opts into semantic search by setting ``search.embedding_template``, a text with
``{attribute}`` placeholders naming Attributes of the Kind by api name, plus ``{_kind}`` (the
Kind's display name, else its api name) and ``{_key}`` (the Record key). ``{{`` and ``}}``
write literal braces. Example::

    search:
      embedding_template: "{legal_name} ({country}). {description}"

Rendering is deterministic: absent values render as nothing, booleans as ``true``/``false``,
lists as their items joined by ``, ``, objects as compact JSON, and runs of whitespace collapse
to one space. Derived Attributes cannot be named (their values are not part of the Record's
content hash, which keys the stored vectors), and sensitive Attributes only when
``search.embedding_sensitive`` is true.

``KindTemplate.fingerprint`` changes whenever the rendered text of some Record could change
for reasons other than its values (template text, the Attributes the names resolve to, the
Kind name used by ``{_kind}``, the rendering rules), so a job can tell which Kinds need their
texts rendered again.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import JsonValue

from lorebase_core.model.kind import Kind

RENDERING_VERSION = 1
"""Bumped when the rendering rules change, which changes every fingerprint."""

KIND_FIELD = "_kind"
KEY_FIELD = "_key"
SPECIAL_FIELDS = frozenset({KIND_FIELD, KEY_FIELD})

_TOKEN = re.compile(r"\{\{|\}\}|\{([^{}]*)\}|[{}]")
_WHITESPACE = re.compile(r"\s+")


class TemplateError(ValueError):
    """An embedding template that cannot be parsed or names something it cannot use."""


@dataclass(frozen=True, slots=True)
class Placeholder:
    name: str


@dataclass(frozen=True, slots=True)
class EmbeddingTemplate:
    """A parsed template: literal text and placeholders, in order."""

    source: str
    parts: tuple[str | Placeholder, ...]

    @property
    def fields(self) -> tuple[str, ...]:
        """Placeholder names in order of first use."""
        return tuple(dict.fromkeys(p.name for p in self.parts if isinstance(p, Placeholder)))


def parse_template(source: str) -> EmbeddingTemplate:
    """Parse ``source``; `TemplateError` on an unbalanced brace or an empty placeholder."""
    parts: list[str | Placeholder] = []
    literal: list[str] = []
    position = 0
    for match in _TOKEN.finditer(source):
        literal.append(source[position : match.start()])
        position = match.end()
        token = match.group(0)
        if token in ("{{", "}}"):
            literal.append(token[0])
            continue
        name = match.group(1)
        if name is None:
            raise TemplateError(
                f"unbalanced {token!r} at offset {match.start()}; write {token * 2}"
            )
        name = name.strip()
        if not name:
            raise TemplateError(f"empty placeholder at offset {match.start()}")
        if any(literal):
            parts.append("".join(literal))
        literal = []
        parts.append(Placeholder(name))
    literal.append(source[position:])
    if any(literal):
        parts.append("".join(literal))
    if not any(isinstance(p, Placeholder) for p in parts):
        raise TemplateError("the template names no Attribute")
    return EmbeddingTemplate(source, tuple(parts))


@dataclass(frozen=True, slots=True)
class KindTemplate:
    """The embedding template of one Kind, resolved against its Attributes."""

    kind_id: str
    template: EmbeddingTemplate
    attributes: Mapping[str, str]
    """Placeholder name -> Attribute id (special fields are not listed)."""
    kind_label: str
    fingerprint: str

    def render(self, key: str, values: Mapping[str, JsonValue]) -> str:
        """The text of the Record ``key`` with ``values`` (keyed by Attribute id)."""
        out: list[str] = []
        for part in self.template.parts:
            if isinstance(part, str):
                out.append(part)
            elif part.name == KIND_FIELD:
                out.append(self.kind_label)
            elif part.name == KEY_FIELD:
                out.append(key)
            else:
                out.append(_format(values.get(self.attributes[part.name])))
        return _WHITESPACE.sub(" ", "".join(out)).strip()


def template_problems(kind: Kind) -> list[str]:
    """Why the Kind's template cannot be used; empty when it can (or there is none)."""
    try:
        _resolve(kind)
    except TemplateError as error:
        return [str(error)]
    return []


def kind_template(kind: Kind) -> KindTemplate | None:
    """The resolved template of ``kind``; None without one. Raises `TemplateError`."""
    return _resolve(kind)


def _resolve(kind: Kind) -> KindTemplate | None:
    source = kind.search.embedding_template
    if source is None or not source.strip():
        return None
    template = parse_template(source)
    allow_sensitive = bool(kind.search.embedding_sensitive)
    attributes: dict[str, str] = {}
    for name in template.fields:
        if name in SPECIAL_FIELDS:
            continue
        attribute = kind.attribute_by_api_name(name)
        if attribute is None:
            raise TemplateError(f"{{{name}}} is not an Attribute of Kind {kind.api_name!r}")
        if attribute.is_derived:
            raise TemplateError(f"{{{name}}} is derived; templates read stored values only")
        if attribute.sensitivity and not allow_sensitive:
            raise TemplateError(
                f"{{{name}}} is sensitive ({', '.join(attribute.sensitivity)}); set "
                "search.embedding_sensitive to embed it"
            )
        attributes[name] = attribute.id
    label = kind.display_name or kind.api_name
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "v": RENDERING_VERSION,
                "kind": kind.id,
                "template": source,
                "attributes": attributes,
                "label": label if KIND_FIELD in template.fields else None,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return KindTemplate(kind.id, template, attributes, label, fingerprint)


def _format(value: JsonValue) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return ", ".join(text for text in (_format(item) for item in value) if text)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
