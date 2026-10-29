"""Tier 2: one structured-output call per chunk to any chat model (design 9.2 steps 5 to 8).

The prompt holds the ontology fragment relevant to the chunk (Kinds mentioned by tiers 0 and 1,
or every Kind when none is, the Relations between them and their extractable Attributes), the
candidate Records found so far (key and title, so the model answers with `ref` instead of
inventing identities) and the chunk text. The answer must follow the JSON schema of that
fragment. The model has no tools.

Every fact must quote its evidence verbatim from the chunk (whitespace-insensitive); a value must
also appear in its quote (dates and numbers compared by value, text case-insensitively). Facts
that fail are discarded and counted. A value of a `valid_time` Attribute may come with a
validity period (`valid`: `from`, `to`, design 10.6); each date of it must appear in the
value's quote like a date value, or the period is dropped (`ungrounded_period`) and the value
kept without it. Answers are cached by model, prompt version, schema hash
and chunk hash, so a re-run on unchanged input makes no call.

With schema discovery (`scribe.extraction.discovery.tier2`), the answer also lists `unlisted`
facts the schema has no place for, and records, values and links the model names outside the
schema are kept as observations besides being discarded; both need verified evidence
(`discovery`).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from lorebase_core.infer.suggestions import Observation, SuggestionType
from lorebase_core.model import Attribute, Relation
from lorebase_core.types import AttributeType, EncodingError, TypeSpec, encode_value
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
    link_attributes,
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
    "Write dates as YYYY-MM-DD and numbers without thousands separators. A link whose "
    "Relation lists link attributes carries them in `values`, each with its own verbatim "
    "`evidence`; always give the required ones, and propose the link only when the text "
    "states them. Return an empty list when the text states nothing that fits."
)
DISCOVERY_PROMPT = (
    " Facts the schema has no place for (another kind of thing, a field or a relationship it "
    "lacks) go to `unlisted`, each with its verbatim evidence."
)


class AnswerCache(Protocol):
    async def get(self, key: bytes) -> tuple[Any, Usage, str] | None: ...

    async def put(
        self, key: bytes, data: Any, usage: Usage, model: str, *, chunk_hash: bytes | None = None
    ) -> None:
        """Store an answer; `chunk_hash` names the chunk it was computed from, so a purge of
        the document drops it."""
        ...


@dataclass
class Tier2Result:
    values: list[ValueCandidate] = field(default_factory=list)
    links: list[LinkCandidate] = field(default_factory=list)
    mentions: list[Resolved] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    cached: bool = False
    discarded: dict[str, int] = field(default_factory=dict)
    observations: list[Observation] = field(default_factory=list)

    def discard(self, reason: str) -> None:
        self.discarded[reason] = self.discarded.get(reason, 0) + 1


def relevant_kinds(ontology: Ontology, hints: Sequence[Resolved]) -> list[str]:
    """Kinds of the chunk's mentions, else every Kind; schema order."""
    mentioned = {h.kind_id for h in hints}
    return [k for k in ontology.kinds if not mentioned or k in mentioned]


def build_messages(
    ontology: Ontology,
    kind_ids: Sequence[str],
    chunk: Chunk,
    hints: Sequence[Resolved],
    *,
    discover: bool = False,
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
    system = SYSTEM_PROMPT + (DISCOVERY_PROMPT if discover else "")
    return [Message("system", system), Message("user", user)]


def cache_key(
    model: str,
    prompt_version: str,
    schema_hash: bytes,
    chunk: Chunk,
    kinds: Sequence[str],
    *,
    discover: bool = False,
) -> bytes:
    parts: list[Any] = [2, model, prompt_version, schema_hash.hex(), chunk.hash.hex()]
    parts.append(sorted(kinds))
    if discover:
        parts.append("unlisted")
    payload = json.dumps(parts, separators=(",", ":"))
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


def grounded(attribute: Attribute, value: Any, quote: str, raw: Any = None) -> bool:
    """Whether `value` (canonical) is written in `quote`.

    A timestamp is grounded by its day: documents write "23 January 2024" for an approval
    time. Its day is taken from the canonical value or, when given, from the `raw` answer,
    whose offset may put the instant on another day in UTC.
    """
    name = attribute.type.name
    if name is AttributeType.BOOLEAN:
        return True
    if name is AttributeType.DATE:
        return any(d.value == value for d in find_dates(quote, 0, len(quote)))
    if name is AttributeType.TIMESTAMP:
        days = {str(value)[:10], str(raw)[:10] if isinstance(raw, str) else ""}
        return any(d.value in days for d in find_dates(quote, 0, len(quote)))
    if name in (AttributeType.INTEGER, AttributeType.DECIMAL, AttributeType.FLOAT):
        try:
            target = float(str(value))
        except ValueError:
            return False
        return any(float(n.value) == target for n in find_numbers(quote, 0, len(quote), ()))
    return str(value).casefold() in quote.casefold()


_NAIVE_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?")
_TIMESTAMP = TypeSpec.of(AttributeType.TIMESTAMP)


def _encode(attribute: Attribute, raw: Any) -> Any:
    return _encode_spec(attribute.type, raw)


def _encode_spec(spec: TypeSpec, raw: Any) -> Any:
    if (
        spec.name is AttributeType.TIMESTAMP
        and isinstance(raw, str)
        and _NAIVE_TIMESTAMP.fullmatch(raw.strip())
    ):
        # Documents rarely state an offset; models then answer a bare date or a local time,
        # which is read as UTC.
        day, _, time = raw.strip().partition("T")
        time = time or "00:00:00"
        raw = f"{day}T{time}{':00' if time.count(':') == 1 else ''}Z"
    try:
        return encode_value(spec, raw)
    except (EncodingError, TypeError, ValueError):
        return None


def period(
    attribute: Attribute, raw: Any, quote: str, result: Tier2Result
) -> tuple[str | None, str | None]:
    """The validity period a value item states, as canonical timestamps; each end must be
    written in the value's `quote` (module doc). Open ends are `None`."""
    if not attribute.valid_time or not isinstance(raw, dict):
        return None, None
    ends: list[str | None] = []
    for name in ("from", "to"):
        given = raw.get(name)
        if given is None or (isinstance(given, str) and not given.strip()):
            ends.append(None)
            continue
        stamp = _encode_spec(_TIMESTAMP, given)
        if not isinstance(stamp, str) or not any(
            d.value in {stamp[:10], str(given)[:10]} for d in find_dates(quote, 0, len(quote))
        ):
            result.discard("ungrounded_period")
            return None, None
        ends.append(stamp)
    start, stop = ends
    if start is not None and stop is not None and stop < start:
        result.discard("invalid_period")
        return None, None
    return start, stop


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
    discover: bool = False,
) -> Tier2Result:
    """Candidates from one chunk; offsets of the result are document offsets. With `discover`,
    observations of facts outside the schema too."""
    result = Tier2Result()
    kinds = relevant_kinds(ontology, hints)
    if not kinds:
        return result
    schema = fragment_schema(ontology, kinds, unlisted=discover)
    key = cache_key(
        model_key, settings.prompt_version, schema_hash, chunk, kinds, discover=discover
    )
    stored = await cache.get(key) if cache is not None else None
    if stored is not None:
        data, usage, model = stored
        result.cached = True
    else:
        answer = await chat.complete_json(
            build_messages(ontology, kinds, chunk, hints, discover=discover),
            schema,
            ChatOptions(task=TASK),
        )
        data, usage, model = answer.data, answer.usage, answer.model
        if cache is not None:
            await cache.put(key, data, usage, model, chunk_hash=chunk.hash)
        result.usage = usage
    if not isinstance(data, dict):
        result.discard("invalid_answer")
        return result
    seen = _Seen(document_id, model, settings.confidence.tier2, discover)
    await _records(data.get("records") or [], ontology, chunk, resolver, seen, settings, result)
    await _links(data.get("links") or [], ontology, chunk, resolver, hints, seen, settings, result)
    if discover:
        _unlisted(data.get("unlisted") or [], chunk, hints, seen, result)
    return result


@dataclass(frozen=True)
class _Seen:
    """Where tier 2 facts come from, and whether facts outside the schema are observed."""

    document_id: str
    model: str
    confidence: float
    discover: bool

    def observe(
        self,
        result: Tier2Result,
        type_: SuggestionType,
        label: Any,
        evidence: Evidence,
        *,
        kind: str | None = None,
        target: str | None = None,
        value: Any = None,
    ) -> None:
        if not self.discover or not isinstance(label, str) or not label.strip():
            return
        result.observations.append(
            Observation(
                type=type_,
                label=label.strip(),
                document_id=self.document_id,
                confidence=self.confidence,
                source=f"model:{self.model}",
                kind=kind,
                target=target,
                value=None if value is None or value == "" else str(value),
                evidence=evidence.as_dict(),
            )
        )


def _hinted(hints: Sequence[Resolved], text: Any) -> Resolved | None:
    """The chunk mention whose key or text is `text` (case-insensitive)."""
    if not isinstance(text, str) or not text.strip():
        return None
    folded = text.strip().casefold()
    for hint in hints:
        if hint.usable and folded in (str(hint.key).casefold(), hint.mention.text.casefold()):
            return hint
    return None


def _quoted(value: Any, evidence: Evidence) -> bool:
    """Whether a text value is written in its evidence."""
    text = "" if value is None else str(value).strip()
    return bool(text) and text.casefold() in evidence.quote.casefold()


def _unlisted(
    entries: Sequence[Any],
    chunk: Chunk,
    hints: Sequence[Resolved],
    seen: _Seen,
    result: Tier2Result,
) -> None:
    for entry in entries:
        if not isinstance(entry, dict):
            result.discard("invalid_unlisted")
            continue
        evidence = _evidence(chunk, seen.document_id, entry.get("evidence"))
        if evidence is None:
            result.discard("unverified_evidence")
            continue
        type_ = entry.get("type")
        name, value = entry.get("name"), entry.get("value")
        if type_ == "kind":
            seen.observe(result, SuggestionType.KIND, name, evidence, value=value)
            continue
        subject = _hinted(hints, entry.get("subject"))
        if subject is None:
            result.discard("unresolved_unlisted")
            continue
        if type_ == "attribute":
            if not _quoted(value, evidence):
                result.discard("ungrounded_value")
                continue
            seen.observe(
                result, SuggestionType.ATTRIBUTE, name, evidence, kind=subject.kind_id, value=value
            )
        elif type_ == "relation":
            target = _hinted(hints, value)
            if target is None:
                result.discard("unresolved_unlisted")
                continue
            seen.observe(
                result,
                SuggestionType.RELATION,
                name,
                evidence,
                kind=subject.kind_id,
                target=target.kind_id,
            )
        else:
            result.discard("invalid_unlisted")


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
    seen: _Seen,
    settings: ExtractionSettings,
    result: Tier2Result,
) -> None:
    document_id, model = seen.document_id, seen.model
    by_name = {view.api_name: view for view in ontology.kinds.values()}
    for entry in entries:
        if not isinstance(entry, dict):
            result.discard("invalid_record")
            continue
        view = by_name.get(str(entry.get("kind")))
        evidence = _evidence(chunk, document_id, entry.get("evidence"))
        if view is None:
            result.discard("unknown_kind")
            if evidence is not None:
                seen.observe(
                    result, SuggestionType.KIND, entry.get("kind"), evidence, value=entry.get("ref")
                )
            continue
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
                if isinstance(item, dict):
                    unknown = _evidence(chunk, document_id, item.get("evidence"))
                    raw = item.get("value")
                    if unknown is not None and _quoted(raw, unknown):
                        seen.observe(
                            result,
                            SuggestionType.ATTRIBUTE,
                            name,
                            unknown,
                            kind=subject.kind_id,
                            value=raw,
                        )
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
            if not grounded(attribute, value, value_evidence.quote, item.get("value")):
                result.discard("ungrounded_value")
                continue
            valid_from, valid_to = period(
                attribute, item.get("valid"), value_evidence.quote, result
            )
            result.values.append(
                ValueCandidate(
                    subject=subject,
                    attribute_id=attribute.id,
                    value=value,
                    evidence=value_evidence,
                    confidence=settings.confidence.tier2,
                    tier=2,
                    model=model,
                    valid_from=valid_from,
                    valid_to=valid_to,
                )
            )


async def _links(
    entries: Sequence[Any],
    ontology: Ontology,
    chunk: Chunk,
    resolver: Resolver,
    hints: Sequence[Resolved],
    seen: _Seen,
    settings: ExtractionSettings,
    result: Tier2Result,
) -> None:
    document_id, model = seen.document_id, seen.model
    by_name = {r.api_name: r for r in ontology.relations.values()}
    for entry in entries:
        if not isinstance(entry, dict):
            result.discard("invalid_link")
            continue
        relation = by_name.get(str(entry.get("relation")))
        evidence = _evidence(chunk, document_id, entry.get("evidence"))
        if relation is None:
            result.discard("unknown_relation")
            source_hint = _hinted(hints, entry.get("from"))
            target_hint = _hinted(hints, entry.get("to"))
            if evidence is not None and source_hint is not None and target_hint is not None:
                seen.observe(
                    result,
                    SuggestionType.RELATION,
                    entry.get("relation"),
                    evidence,
                    kind=source_hint.kind_id,
                    target=target_hint.kind_id,
                )
            continue
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
                values=_link_values(
                    entry.get("values"), relation, ontology, chunk, document_id, result
                ),
            )
        )


def _link_values(
    raw: Any,
    relation: Relation,
    ontology: Ontology,
    chunk: Chunk,
    document_id: str,
    result: Tier2Result,
) -> dict[str, Any]:
    """Encoded link Attribute values by Attribute id; unverified or ungrounded ones are dropped."""
    if not isinstance(raw, dict):
        return {}
    attributes = {a.api_name: a for a in link_attributes(relation, ontology.tags)}
    found: dict[str, Any] = {}
    for name, item in raw.items():
        attribute = attributes.get(name)
        if attribute is None or not isinstance(item, dict):
            result.discard("unknown_attribute")
            continue
        evidence = _evidence(chunk, document_id, item.get("evidence"))
        if evidence is None:
            result.discard("unverified_evidence")
            continue
        value = _encode(attribute, item.get("value"))
        if value is None:
            result.discard("invalid_value")
            continue
        if not grounded(attribute, value, evidence.quote, item.get("value")):
            result.discard("ungrounded_value")
            continue
        found[attribute.id] = value
    return found
