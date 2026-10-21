"""Tier 2: one structured-output call per chunk to any chat model (design 9.2 steps 5 to 8).

The prompt holds the ontology fragment relevant to the chunk (Kinds mentioned by tiers 0 and 1,
or every Kind when none is, the Relations between them and their extractable Attributes), the
candidate Records found so far (key and title, so the model answers with `ref` instead of
inventing identities) and the chunk text. The answer must follow the JSON schema of that
fragment. The model has no tools.

Every fact must quote its evidence verbatim from the chunk (whitespace-insensitive); a value must
also appear in its quote (dates and numbers compared by value, text case-insensitively). Facts
that fail are discarded and counted. Answers are cached by model, prompt version, schema hash
and chunk hash, so a re-run on unchanged input makes no call.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from lorebase_core.model import Attribute
from lorebase_core.types import AttributeType, EncodingError, encode_value
from lorebase_inference.interfaces import ChatModel, ChatOptions, Message, Usage
from lorebase_scribe.extraction.candidates import (
    Evidence,
    LinkCandidate,
    Mention,
    Resolved,
    ValueCandidate,
)
from lorebase_scribe.extraction.matching import Resolver
from lorebase_scribe.extraction.ontology import (
    Ontology,
    fragment_description,
    fragment_schema,
)
from lorebase_scribe.extraction.settings import ExtractionSettings
from lorebase_scribe.extraction.text import Chunk
from lorebase_scribe.extraction.values import find_dates, find_numbers

TASK = "extraction"
SYSTEM_PROMPT = (
    "You extract facts from a document into a fixed schema. Report only facts the text states; "
    "never guess. For every record, value and link, copy the supporting words of the text "
    "verbatim into `evidence`. When a record is one of the candidate records, set `ref` to its "
    "key; otherwise set `ref` to an empty string and give its key Attribute among the values. "
    "Write dates as YYYY-MM-DD and numbers without thousands separators. Return an empty list "
    "when the text states nothing that fits."
)


class AnswerCache(Protocol):
    async def get(self, key: bytes) -> tuple[Any, Usage, str] | None: ...

    async def put(self, key: bytes, data: Any, usage: Usage, model: str) -> None: ...


@dataclass
class Tier2Result:
    values: list[ValueCandidate] = field(default_factory=list)
    links: list[LinkCandidate] = field(default_factory=list)
    mentions: list[Resolved] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    cached: bool = False
    discarded: dict[str, int] = field(default_factory=dict)

    def discard(self, reason: str) -> None:
        self.discarded[reason] = self.discarded.get(reason, 0) + 1


def relevant_kinds(ontology: Ontology, hints: Sequence[Resolved]) -> list[str]:
    """Kinds of the chunk's mentions, else every Kind; schema order."""
    mentioned = {h.kind_id for h in hints}
    return [k for k in ontology.kinds if not mentioned or k in mentioned]


def build_messages(
    ontology: Ontology, kind_ids: Sequence[str], chunk: Chunk, hints: Sequence[Resolved]
) -> list[Message]:
    candidates: list[str] = []
    seen: set[tuple[str, str]] = set()
    for hint in hints:
        for candidate in hint.resolution.candidates or ():
            if (candidate.kind_id, candidate.key) in seen:
                continue
            seen.add((candidate.kind_id, candidate.key))
            kind = ontology.kinds[candidate.kind_id].api_name
            title = f': "{candidate.title}"' if candidate.title else ""
            candidates.append(f'- {kind} "{candidate.key}"{title}')
        if hint.resolution.status == "new" and (hint.kind_id, hint.key) not in seen:
            seen.add((hint.kind_id, str(hint.key)))
            kind = ontology.kinds[hint.kind_id].api_name
            candidates.append(f'- {kind} "{hint.key}" (not stored yet)')
    user = (
        "Schema:\n"
        + fragment_description(ontology, kind_ids)
        + "\n\nCandidate records:\n"
        + ("\n".join(candidates) or "(none)")
        + "\n\nText:\n<<<\n"
        + chunk.text
        + "\n>>>"
    )
    return [Message("system", SYSTEM_PROMPT), Message("user", user)]


def cache_key(
    model: str, prompt_version: str, schema_hash: bytes, chunk: Chunk, kinds: Sequence[str]
) -> bytes:
    payload = json.dumps(
        [2, model, prompt_version, schema_hash.hex(), chunk.hash.hex(), sorted(kinds)],
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).digest()


def locate(quote: str, text: str) -> tuple[int, int] | None:
    """Offsets of `quote` in `text`, whitespace-insensitive; None when absent."""
    words = quote.split()
    if not words:
        return None
    found = text.find(quote.strip())
    if found != -1:
        return found, found + len(quote.strip())
    match = re.search(r"\s+".join(re.escape(w) for w in words), text)
    return None if match is None else (match.start(), match.end())


def grounded(attribute: Attribute, value: Any, quote: str) -> bool:
    """Whether `value` (canonical) is written in `quote`."""
    name = attribute.type.name
    if name is AttributeType.BOOLEAN:
        return True
    if name is AttributeType.DATE:
        return any(d.value == value for d in find_dates(quote, 0, len(quote)))
    if name in (AttributeType.INTEGER, AttributeType.DECIMAL, AttributeType.FLOAT):
        try:
            target = float(str(value))
        except ValueError:
            return False
        return any(float(n.value) == target for n in find_numbers(quote, 0, len(quote), ()))
    return str(value).casefold() in quote.casefold()


def _encode(attribute: Attribute, raw: Any) -> Any:
    try:
        return encode_value(attribute.type, raw)
    except (EncodingError, TypeError, ValueError):
        return None


async def run_tier2(
    chat: ChatModel,
    ontology: Ontology,
    chunk: Chunk,
    hints: Sequence[Resolved],
    resolver: Resolver,
    *,
    document_id: str,
    schema_hash: bytes,
    model_key: str,
    settings: ExtractionSettings,
    cache: AnswerCache | None,
) -> Tier2Result:
    """Candidates from one chunk; offsets of the result are document offsets."""
    result = Tier2Result()
    kinds = relevant_kinds(ontology, hints)
    if not kinds:
        return result
    schema = fragment_schema(ontology, kinds)
    key = cache_key(model_key, settings.prompt_version, schema_hash, chunk, kinds)
    stored = await cache.get(key) if cache is not None else None
    if stored is not None:
        data, usage, model = stored
        result.cached = True
    else:
        answer = await chat.complete_json(
            build_messages(ontology, kinds, chunk, hints), schema, ChatOptions(task=TASK)
        )
        data, usage, model = answer.data, answer.usage, answer.model
        if cache is not None:
            await cache.put(key, data, usage, model)
        result.usage = usage
    if not isinstance(data, dict):
        result.discard("invalid_answer")
        return result
    await _records(
        data.get("records") or [], ontology, chunk, resolver, document_id, model, settings, result
    )
    await _links(
        data.get("links") or [], ontology, chunk, resolver, document_id, model, settings, result
    )
    return result


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


async def _subject(
    view_kind: str,
    ref: str,
    key_value: Any,
    evidence: Evidence,
    resolver: Resolver,
    chunk: Chunk,
    model: str,
) -> Resolved | None:
    attempts: list[tuple[str, str]] = []
    if ref.strip():
        attempts.append((ref.strip(), "model"))
    if isinstance(key_value, str) and key_value.strip():
        attempts.append((key_value.strip(), "key"))
    for text, source in attempts:
        resolved = await resolver.resolve(
            Mention(
                kind_id=view_kind,
                text=text,
                start=evidence.start,
                end=evidence.end,
                chunk=chunk.seq,
                score=1.0,
                tier=2,
                source="key" if source == "key" else "model",
                extractor=model,
                document_id=evidence.document_id,
            )
        )
        if resolved.usable:
            return resolved
    return None


async def _records(
    entries: Sequence[Any],
    ontology: Ontology,
    chunk: Chunk,
    resolver: Resolver,
    document_id: str,
    model: str,
    settings: ExtractionSettings,
    result: Tier2Result,
) -> None:
    by_name = {view.api_name: view for view in ontology.kinds.values()}
    for entry in entries:
        if not isinstance(entry, dict):
            result.discard("invalid_record")
            continue
        view = by_name.get(str(entry.get("kind")))
        if view is None:
            result.discard("unknown_kind")
            continue
        evidence = _evidence(chunk, document_id, entry.get("evidence"))
        if evidence is None:
            result.discard("unverified_evidence")
            continue
        raw_values = entry.get("values")
        values: dict[str, Any] = raw_values if isinstance(raw_values, dict) else {}
        key_value = None
        if view.key_attribute is not None:
            raw = values.get(view.key_attribute.api_name)
            key_value = raw.get("value") if isinstance(raw, dict) else None
        subject = await _subject(
            view.id, str(entry.get("ref") or ""), key_value, evidence, resolver, chunk, model
        )
        if subject is None:
            result.discard("unresolved_record")
            continue
        result.mentions.append(subject)
        attributes = {a.api_name: a for a in view.attributes}
        for name, item in values.items():
            attribute = attributes.get(name)
            if attribute is None or not isinstance(item, dict):
                result.discard("unknown_attribute")
                continue
            if view.key_attribute is not None and attribute.id == view.key_attribute.id:
                continue
            value_evidence = _evidence(chunk, document_id, item.get("evidence"))
            if value_evidence is None:
                result.discard("unverified_evidence")
                continue
            value = _encode(attribute, item.get("value"))
            if value is None:
                result.discard("invalid_value")
                continue
            if not grounded(attribute, value, value_evidence.quote):
                result.discard("ungrounded_value")
                continue
            result.values.append(
                ValueCandidate(
                    subject=subject,
                    attribute_id=attribute.id,
                    value=value,
                    evidence=value_evidence,
                    confidence=settings.confidence.tier2,
                    tier=2,
                    model=model,
                )
            )


async def _links(
    entries: Sequence[Any],
    ontology: Ontology,
    chunk: Chunk,
    resolver: Resolver,
    document_id: str,
    model: str,
    settings: ExtractionSettings,
    result: Tier2Result,
) -> None:
    by_name = {r.api_name: r for r in ontology.relations.values()}
    for entry in entries:
        if not isinstance(entry, dict):
            result.discard("invalid_link")
            continue
        relation = by_name.get(str(entry.get("relation")))
        if relation is None:
            result.discard("unknown_relation")
            continue
        evidence = _evidence(chunk, document_id, entry.get("evidence"))
        if evidence is None:
            result.discard("unverified_evidence")
            continue
        source = await _subject(
            relation.from_kind, str(entry.get("from") or ""), None, evidence, resolver, chunk, model
        )
        target = await _subject(
            relation.to_kind, str(entry.get("to") or ""), None, evidence, resolver, chunk, model
        )
        if source is None or target is None:
            result.discard("unresolved_link")
            continue
        result.links.append(
            LinkCandidate(
                relation_id=relation.id,
                source=source,
                target=target,
                evidence=evidence,
                confidence=settings.confidence.tier2,
                tier=2,
                model=model,
            )
        )
