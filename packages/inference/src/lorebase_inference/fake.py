"""Deterministic fake provider: chat, embeddings and spans without network access.

- Chat answers come from fixtures keyed by `prompt_hash`, or from a rule-based responder that
  builds a schema-valid value out of the text quoted in the prompt.
- Embeddings are unit vectors of the configured dimension derived from `sha256(text)`.
- Spans come from dictionary matching.

Rule-based responder limits: it understands type, enum, const, properties, required, items,
min/max keywords, minLength/maxLength, uniqueItems, anyOf, oneOf, allOf and local `$ref`. It
does not satisfy `pattern`, `not` or conditional keywords; supply a fixture for such schemas.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from lorebase_inference.interfaces import (
    ChatOptions,
    ChatResult,
    JsonSchema,
    Label,
    Message,
    Span,
    Usage,
)
from lorebase_inference.settings import EmbeddingSettings, FakeSettings, LlmSettings

FAKE_MODEL_PREFIX = "fake:"
_QUOTED = re.compile(r'"([^"\n]{1,200})"|“([^”\n]{1,200})”')
_FORMATS = {
    "date": "2000-01-01",
    "date-time": "2000-01-01T00:00:00Z",
    "time": "00:00:00Z",
    "email": "user@example.org",
    "uri": "https://example.org/",
    "url": "https://example.org/",
    "uuid": "00000000-0000-4000-8000-000000000000",
}
_MAX_EXTRA_DEPTH = 20


def prompt_hash(messages: Sequence[Message]) -> str:
    """Hex SHA-256 of the messages; names the fixture file holding the canned answer."""
    payload = json.dumps(
        [[m.role, m.content] for m in messages], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _token_count(text: str) -> int:
    return len(text.split())


class FakeChatModel:
    """`ChatModel` answering from fixtures or from a rule-based responder."""

    def __init__(
        self,
        llm: LlmSettings,
        fake: FakeSettings,
        fixtures: Mapping[str, Any] | None = None,
    ) -> None:
        self._model = f"{FAKE_MODEL_PREFIX}{llm.model}"
        self._fixtures_dir = fake.fixtures_dir
        self._max_depth = fake.max_schema_depth
        self._fixtures = dict(fixtures or {})

    async def complete_json(
        self,
        messages: Sequence[Message],
        schema: JsonSchema,
        options: ChatOptions | None = None,
    ) -> ChatResult:
        """Return the fixture for these messages, else a value built from the schema."""
        data = self._fixture(prompt_hash(messages))
        if data is None:
            quotes = _quoted_text(messages)
            data = _Responder(schema, quotes, self._max_depth).build()
        usage = Usage(
            input_tokens=sum(_token_count(m.content) for m in messages),
            output_tokens=_token_count(json.dumps(data, ensure_ascii=False)),
        )
        return ChatResult(data=data, usage=usage, model=self._model)

    def _fixture(self, key: str) -> Any:
        if key in self._fixtures:
            return copy.deepcopy(self._fixtures[key])
        if self._fixtures_dir is not None:
            path = Path(self._fixtures_dir) / f"{key}.json"
            if path.is_file():
                return json.loads(path.read_text(encoding="utf-8"))
        return None


def _quoted_text(messages: Sequence[Message]) -> list[str]:
    """Quoted passages of the prompt, else its distinct words, else a constant."""
    text = "\n".join(m.content for m in messages)
    quotes = [a or b for a, b in _QUOTED.findall(text) if (a or b).strip()]
    if not quotes:
        quotes = list(dict.fromkeys(re.findall(r"\w+", text)))
    return quotes or ["fake"]


class _Responder:
    """Builds a value that satisfies a JSON schema from a pool of strings."""

    def __init__(self, schema: JsonSchema, pool: list[str], max_depth: int) -> None:
        self._root = schema
        self._pool = pool
        self._max_depth = max_depth
        self._next = 0

    def build(self) -> Any:
        return self._value(self._root, 0)

    def _text(self) -> str:
        value = self._pool[self._next % len(self._pool)]
        self._next += 1
        return value

    def _resolve(self, schema: Any) -> Any:
        seen = 0
        while isinstance(schema, Mapping) and "$ref" in schema:
            ref = schema["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#"):
                raise ValueError(f"unsupported $ref {ref!r}: only local references are resolved")
            node: Any = self._root
            for part in filter(None, ref[1:].split("/")):
                node = node[part.replace("~1", "/").replace("~0", "~")]
            schema = node
            seen += 1
            if seen > _MAX_EXTRA_DEPTH:
                raise ValueError("cyclic $ref chain")
        return schema

    def _merged(self, schema: Mapping[str, Any]) -> Mapping[str, Any]:
        """Flatten `allOf` into one schema."""
        merged: dict[str, Any] = {k: v for k, v in schema.items() if k != "allOf"}
        for part in schema.get("allOf", []):
            part = self._merged(self._resolve(part))
            for key, value in part.items():
                if key == "properties":
                    merged["properties"] = {**merged.get("properties", {}), **value}
                elif key == "required":
                    merged["required"] = [*merged.get("required", []), *value]
                else:
                    merged.setdefault(key, value)
        return merged

    def _value(self, schema: Any, depth: int) -> Any:
        if depth > self._max_depth + _MAX_EXTRA_DEPTH:
            raise ValueError("schema recursion cannot be satisfied with finite output")
        schema = self._resolve(schema)
        if schema is True or not isinstance(schema, Mapping) or not schema:
            return self._text()
        schema = self._merged(schema)
        if "const" in schema:
            return copy.deepcopy(schema["const"])
        if schema.get("enum"):
            return copy.deepcopy(schema["enum"][0])
        for keyword in ("anyOf", "oneOf"):
            if keyword in schema:
                return self._value(self._preferred(schema[keyword]), depth)
        kind = self._kind(schema)
        if kind == "object":
            return self._object(schema, depth)
        if kind == "array":
            return self._array(schema, depth)
        if kind == "string":
            return self._string(schema)
        if kind in ("integer", "number"):
            return self._number(schema, integer=kind == "integer")
        if kind == "boolean":
            return True
        return None

    def _preferred(self, branches: Sequence[Any]) -> Any:
        for branch in branches:
            resolved = self._resolve(branch)
            if not (isinstance(resolved, Mapping) and resolved.get("type") == "null"):
                return resolved
        return branches[0]

    @staticmethod
    def _kind(schema: Mapping[str, Any]) -> str:
        declared = schema.get("type")
        if isinstance(declared, list):
            declared = next((t for t in declared if t != "null"), "null")
        if isinstance(declared, str):
            return declared
        if "properties" in schema or "required" in schema:
            return "object"
        if "items" in schema or "prefixItems" in schema:
            return "array"
        return "string"

    def _object(self, schema: Mapping[str, Any], depth: int) -> dict[str, Any]:
        properties: Mapping[str, Any] = schema.get("properties", {})
        required: list[str] = list(dict.fromkeys(schema.get("required", [])))
        deep = depth >= self._max_depth
        names = [n for n in properties if n in required] if deep else list(properties)
        names += [n for n in required if n not in properties]
        return {name: self._value(properties.get(name, {}), depth + 1) for name in names}

    def _array(self, schema: Mapping[str, Any], depth: int) -> list[Any]:
        minimum = int(schema.get("minItems", 0))
        maximum = schema.get("maxItems")
        count = minimum if depth >= self._max_depth else max(minimum, 1)
        if maximum is not None:
            count = min(count, int(maximum))
        prefix = schema.get("prefixItems")
        items = schema.get("items", {})
        values: list[Any] = []
        for index in range(count):
            part = prefix[index] if prefix and index < len(prefix) else items
            value = self._value(part, depth + 1)
            if schema.get("uniqueItems") and isinstance(value, str):
                while value in values:
                    value = f"{value} {index}"
            values.append(value)
        return values

    def _string(self, schema: Mapping[str, Any]) -> str:
        value = _FORMATS.get(str(schema.get("format"))) or self._text()
        value = value.ljust(int(schema.get("minLength", 0)), "x")
        maximum = schema.get("maxLength")
        return value if maximum is None else value[: int(maximum)]

    @staticmethod
    def _number(schema: Mapping[str, Any], *, integer: bool) -> int | float:
        low = schema.get("minimum", schema.get("exclusiveMinimum"))
        high = schema.get("maximum", schema.get("exclusiveMaximum"))
        low_open = "minimum" not in schema and low is not None
        high_open = "maximum" not in schema and high is not None
        if integer:
            lo = None if low is None else math.floor(low) + 1 if low_open else math.ceil(low)
            hi = None if high is None else math.ceil(high) - 1 if high_open else math.floor(high)
            value = 0 if lo is None or lo <= 0 else lo
            value = hi if hi is not None and value > hi else value
        else:
            if low is not None and high is not None:
                value = (low + high) / 2
            elif low is not None:
                value = low + (1 if low_open else 0)
            elif high is not None:
                value = min(0, high - (1 if high_open else 0))
            else:
                value = 0
        step = schema.get("multipleOf")
        if step:
            value = math.ceil(value / step) * step
            if high is not None and value > high:
                value = math.floor(high / step) * step
        return int(value) if integer else float(value)


class FakeEmbeddingModel:
    """`EmbeddingModel` producing deterministic unit vectors from `sha256(text)`."""

    def __init__(self, embeddings: EmbeddingSettings) -> None:
        self.name = f"{FAKE_MODEL_PREFIX}{embeddings.model}"
        self.dims = embeddings.dims

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One unit vector of `dims` floats per text."""
        return [embed_text(text, self.dims) for text in texts]


def embed_text(text: str, dims: int) -> list[float]:
    """Unit vector of `dims` components from a counter-extended SHA-256 of `text`."""
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    values: list[float] = []
    counter = 0
    while len(values) < dims:
        block = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        values.extend(
            int.from_bytes(block[i : i + 4], "big") / 2**31 - 1.0 for i in range(0, 32, 4)
        )
        counter += 1
    values = values[:dims]
    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0:
        return [1.0, *([0.0] * (dims - 1))]
    return [v / norm for v in values]


class FakeSpanExtractor:
    """`SpanExtractor` matching dictionary terms as whole words, case-insensitively."""

    def __init__(self, dictionary: Mapping[str, Sequence[str]] | None = None) -> None:
        self._dictionary = {k: list(v) for k, v in (dictionary or {}).items()}

    async def extract(self, text: str, labels: Sequence[Label]) -> list[Span]:
        """Non-overlapping spans, earliest then longest first, ordered by start offset."""
        candidates: list[tuple[int, int, int, str]] = []
        for index, label in enumerate(labels):
            terms = {t for t in (*label.examples, *self._dictionary.get(label.name, ())) if t}
            for term in terms:
                pattern = re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)
                candidates.extend(
                    (m.start(), m.end(), index, label.name) for m in pattern.finditer(text)
                )
        candidates.sort(key=lambda c: (c[0], -(c[1] - c[0]), c[2]))
        spans: list[Span] = []
        last_end = 0
        for start, end, _index, name in candidates:
            if start >= last_end:
                spans.append(Span(label=name, text=text[start:end], start=start, end=end))
                last_end = end
        return spans
