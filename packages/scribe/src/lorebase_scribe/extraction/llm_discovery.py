"""Dedicated schema discovery pass: one structured-output call per chunk, routed through
`inference.llm.tasks.schema_discovery`, that proposes Kinds and Attributes the schema lacks.

It is optional (`scribe.extraction.discovery.llm_pass`, off by default) and independent of
tier 2: the extraction model keeps reporting `unlisted` facts with its extraction call, while
this pass reads the document text only to find what the schema is missing, so a team can route
it to a larger model than bulk extraction uses. The model sees the Kinds and their Attributes,
never records, and has no tools.

Every proposal must quote its evidence verbatim from the chunk (whitespace-insensitive); an
Attribute value must appear in its quote, and an Attribute names a Kind of the schema or one
proposed in the same answer. Proposals that fail are discarded and counted. The result is a list
of `Observation`s that `Discovery.finish` filters against the schema like any other, and the
store aggregates into suggestions. Answers are cached by model, prompt version, schema hash and
chunk hash.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from lorebase_core.infer.suggestions import PROPOSED, Observation, SuggestionType, kind_name
from lorebase_inference.interfaces import ChatModel, ChatOptions, Message, Usage
from lorebase_scribe.extraction.candidates import Evidence
from lorebase_scribe.extraction.ontology import Ontology
from lorebase_scribe.extraction.settings import ExtractionSettings
from lorebase_scribe.extraction.text import Chunk
from lorebase_scribe.extraction.tier2 import AnswerCache, locate

TASK = "schema_discovery"
SYSTEM_PROMPT = (
    "You review a document against a data schema and report what the schema lacks. Propose a "
    "Kind only for a kind of thing the text names with codes or names of its own (not a section "
    "or a field of another thing), and an Attribute only for a field of a Kind that no listed "
    "Attribute covers. For every proposal copy the supporting words of the text verbatim into "
    "`evidence`; an Attribute's `value` must appear in its evidence. Never repeat what the "
    "schema has. Return empty lists when the text proposes nothing."
)


def answer_schema(max_items: int) -> dict[str, Any]:
    """JSON schema of an answer, each list holding at most `max_items` proposals."""
    text = {"type": "string"}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["kinds", "attributes"],
        "properties": {
            "kinds": {
                "type": "array",
                "maxItems": max_items,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "example", "evidence"],
                    "properties": {"name": text, "example": text, "evidence": text},
                },
            },
            "attributes": {
                "type": "array",
                "maxItems": max_items,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "name", "value", "evidence"],
                    "properties": {"kind": text, "name": text, "value": text, "evidence": text},
                },
            },
        },
    }


def describe(ontology: Ontology) -> str:
    """The Kinds with their Attribute names, one line each."""
    lines = []
    for view in ontology.kinds.values():
        names = ", ".join(a.api_name for a in view.attributes)
        lines.append(f"Kind {view.api_name}: {names}")
    return "\n".join(lines)


def build_messages(ontology: Ontology, chunk: Chunk) -> list[Message]:
    user = f"Schema:\n{describe(ontology)}\n\nText:\n<<<\n{chunk.text}\n>>>"
    return [Message("system", SYSTEM_PROMPT), Message("user", user)]


def cache_key(model: str, prompt_version: str, schema_hash: bytes, chunk: Chunk) -> bytes:
    parts = [1, TASK, model, prompt_version, schema_hash.hex(), chunk.hash.hex()]
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode("utf-8")).digest()


@dataclass
class DiscoveryPass:
    """What one chunk's discovery call produced."""

    observations: list[Observation] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    cached: bool = False
    discarded: dict[str, int] = field(default_factory=dict)

    def discard(self, reason: str) -> None:
        self.discarded[reason] = self.discarded.get(reason, 0) + 1


def _evidence(chunk: Chunk, document_id: str, quote: Any) -> Evidence | None:
    if not isinstance(quote, str):
        return None
    found = locate(quote, chunk.text)
    if found is None:
        return None
    start, end = found
    return Evidence(
        document_id, chunk.seq, chunk.start + start, chunk.start + end, chunk.text[start:end]
    )


async def run_llm_discovery(
    chat: ChatModel,
    ontology: Ontology,
    chunk: Chunk,
    *,
    document_id: str,
    schema_hash: bytes,
    model_key: str,
    settings: ExtractionSettings,
    cache: AnswerCache | None,
) -> DiscoveryPass:
    """Observations of the Kinds and Attributes the model proposes for one chunk; offsets are
    document offsets."""
    result = DiscoveryPass()
    key = cache_key(model_key, settings.prompt_version, schema_hash, chunk)
    stored = await cache.get(key) if cache is not None else None
    if stored is not None:
        data, _, model = stored
        result.cached = True
    else:
        answer = await chat.complete_json(
            build_messages(ontology, chunk),
            answer_schema(settings.discovery.llm_max_items),
            ChatOptions(task=TASK),
        )
        data, model = answer.data, answer.model
        if cache is not None:
            await cache.put(key, data, answer.usage, model)
        result.usage = answer.usage
    if not isinstance(data, dict):
        result.discard("invalid_answer")
        return result
    confidence = settings.confidence.tier2
    source = f"discovery:{model}"
    proposed: set[str] = set()
    for entry in _entries(data.get("kinds")):
        evidence = _evidence(chunk, document_id, entry.get("evidence"))
        name = entry.get("name")
        if evidence is None or not isinstance(name, str) or not name.strip():
            result.discard("unverified_evidence" if evidence is None else "invalid_kind")
            continue
        proposed.add(kind_name(name))
        example = entry.get("example")
        result.observations.append(
            Observation(
                type=SuggestionType.KIND,
                label=name.strip(),
                document_id=document_id,
                confidence=confidence,
                source=source,
                value=example.strip() if isinstance(example, str) and example.strip() else None,
                evidence=evidence.as_dict(),
            )
        )
    by_name = {view.api_name.casefold(): view.id for view in ontology.kinds.values()}
    for entry in _entries(data.get("attributes")):
        name, value, owner = entry.get("name"), entry.get("value"), entry.get("kind")
        evidence = _evidence(chunk, document_id, entry.get("evidence"))
        if evidence is None:
            result.discard("unverified_evidence")
            continue
        if not isinstance(name, str) or not name.strip() or not isinstance(owner, str):
            result.discard("invalid_attribute")
            continue
        text = value.strip() if isinstance(value, str) else ""
        if not text or text.casefold() not in evidence.quote.casefold():
            result.discard("ungrounded_value")
            continue
        kind = by_name.get(owner.strip().casefold())
        if kind is None and kind_name(owner) in proposed:
            kind = f"{PROPOSED}{kind_name(owner)}"
        if kind is None:
            result.discard("unknown_kind")
            continue
        result.observations.append(
            Observation(
                type=SuggestionType.ATTRIBUTE,
                label=name.strip(),
                document_id=document_id,
                confidence=confidence,
                source=source,
                kind=kind,
                value=text,
                evidence=evidence.as_dict(),
            )
        )
    return result


def _entries(raw: Any) -> Sequence[dict[str, Any]]:
    return [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []
