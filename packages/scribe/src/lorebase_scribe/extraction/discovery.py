"""Schema discovery: observations of Kinds, Attributes and Relations the schema does not have
(design 9.2 step 10).

Every tier can see something outside the schema:

- tier 0 (`rules`): a `**Label:** value` field line or a table column whose label no Attribute
  of the Record it describes matches (an Attribute; a Relation when the label names a Kind or
  the value names a Record of another Kind), and a heading naming a thing by a code that no
  Kind's key pattern matches (`# Quality notice QN-2025-114`: a Kind, which then owns the
  fields of its section);
- tier 1: spans found with `discovery.span_labels` (a Kind), relations found with
  `discovery.relation_labels` between Records of known Kinds (a Relation);
- tier 2: facts the chat model reports in `unlisted`, and records, values or links it answers
  with names outside the schema, each with verified evidence (see `tier2`).

`Discovery.finish` drops what the schema already has (`existing_match`: names after
normalization, synonyms, similarity), never keeps an observation that matches a sensitive
Attribute (`pii`, `confidential`, `restricted`, carried through Derivations), and leaves out the
evidence and sample of an observation whose quote holds a value of a sensitive Attribute of a
Record the job read, or a vaulted value of any Kind (found by its keyed fingerprint, see
`evidence_texts`; a text too long to check counts as holding one). Nothing tells a submitter
that evidence was withheld: the suggestion simply has none. The rest is aggregated per Domain
by the store
(`SchemaSuggestionRepository`) into suggestions that a reviewer turns into a schema change
Proposal; nothing here changes a schema.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

from lorebase_core.infer.suggestions import (
    PROPOSED,
    Observation,
    SuggestionOptions,
    SuggestionType,
    dangling_reference_names,
    existing_match,
    kind_is_filtered,
    proposed_kind,
)
from lorebase_core.model import Schema, Sensitivity
from lorebase_inference.interfaces import RelationExtractor, RelationLabel, Span
from lorebase_scribe.extraction.candidates import Evidence, Resolved
from lorebase_scribe.extraction.ontology import Ontology
from lorebase_scribe.extraction.settings import DiscoverySettings
from lorebase_scribe.extraction.text import Chunk, heading_sections
from lorebase_scribe.extraction.values import find_dates, find_numbers

RULES = "rules"
_FIELD = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+)?\*\*(?P<a>[^*:\n]{1,60}):\*\*[ \t]*(?P<v>\S[^\n]*)$"
    r"|^[ \t]*(?:[-*+][ \t]+)?\*\*(?P<b>[^*:\n]{1,60})\*\*:[ \t]*(?P<w>\S[^\n]*)$",
    re.MULTILINE,
)
_CODE = re.compile(r"(?<![\w-])[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+(?![\w-])")
_WORDS = re.compile(r"[A-Za-z][A-Za-z'-]*")
_TABLE_LINE = re.compile(r"^[ \t]*\|.*\|[ \t]*$", re.MULTILINE)
_SEPARATOR = re.compile(r"^[ \t]*\|?(?:[ \t]*:?-{2,}:?[ \t]*\|)+[ \t]*:?-*:?[ \t]*\|?[ \t]*$")
_MAX_HEADING_WORDS = 3


def chunk_of(chunks: Sequence[Chunk], offset: int) -> int:
    """Sequence number of the chunk holding `offset` (the last chunk when past the end)."""
    for chunk in chunks:
        if chunk.start <= offset < chunk.end:
            return chunk.seq
    return chunks[-1].seq if chunks else 0


def sample_value(text: str, limit: int) -> str | None:
    """A sample value as written, dates as ISO and lone numbers without separators; markup
    and a final period removed, cut to `limit` characters."""
    value = text.replace("**", "").replace("`", "").strip().rstrip(".").strip()
    if not value:
        return None
    dates = find_dates(value, 0, len(value))
    if len(dates) == 1 and dates[0].end - dates[0].start >= 0.6 * len(value):
        return str(dates[0].value)
    numbers = find_numbers(value, 0, len(value), ())
    if len(numbers) == 1 and numbers[0].end - numbers[0].start == len(value):
        return str(numbers[0].value)
    return value[:limit]


@dataclass(frozen=True, slots=True)
class _Owner:
    """What a section or a table row describes: a Record of a Kind, or a proposed Kind."""

    ref: str
    mention: Resolved | None = None


@dataclass
class Discovery:
    """Observations of one extraction job (see module doc)."""

    ontology: Ontology
    settings: DiscoverySettings
    options: SuggestionOptions
    observations: list[Observation] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    evidence_withheld: int = 0
    """Observations kept without evidence because it held a sensitive value. Only an aggregate
    for operators (a metric): never part of the job report, a suggestion or a log, so a
    submitter cannot learn which of their quotes held a stored value."""

    @property
    def schema(self) -> Schema:
        return self.ontology.schema

    def count(self, name: str, n: int = 1) -> None:
        self.stats[name] = self.stats.get(name, 0) + n

    def add(self, observation: Observation) -> None:
        if observation.confidence < self.settings.min_confidence:
            self.count("suggestion_observations_weak")
            return
        self.observations.append(observation)

    def _sample(self, text: str) -> str | None:
        return sample_value(text, self.settings.max_value_chars)

    def _kind_named(self, label: str) -> str | None:
        """Id of the Kind a label names, if any."""
        match = existing_match(
            self.schema, SuggestionType.KIND, label, options=self.options, tags={}
        )
        return None if match is None else match.id

    # Tier 0 -----------------------------------------------------------------------------

    def rules(
        self,
        text: str,
        chunks: Sequence[Chunk],
        resolved: Sequence[Resolved],
        *,
        document_id: str,
    ) -> None:
        """Headings, field lines and table columns of one document."""
        if not self.settings.rules:
            return
        owners = self._headings(text, chunks, resolved, document_id)
        self._fields(text, chunks, resolved, owners, document_id)
        self._tables(text, chunks, resolved, document_id)

    def _headings(
        self,
        text: str,
        chunks: Sequence[Chunk],
        resolved: Sequence[Resolved],
        document_id: str,
    ) -> list[tuple[int, int, _Owner]]:
        """`(start, end, owner)` of the sections whose heading names a Record or a thing of a
        proposed Kind, innermost last."""
        found: list[tuple[int, int, _Owner]] = []
        key_patterns = [v.key_pattern for v in self.ontology.kinds.values() if v.key_pattern]
        for section in heading_sections(text):
            named = [
                r
                for r in resolved
                if r.usable and section.start <= r.mention.start < section.heading_end
            ]
            if named:
                found.append((section.start, section.end, _Owner(named[0].kind_id, named[0])))
                continue
            line = text[section.start : section.heading_end]
            for code in _CODE.finditer(line):
                token = code.group()
                if not any(c.isdigit() for c in token) or any(
                    p.fullmatch(token) for p in key_patterns
                ):
                    continue
                words = _WORDS.findall(line[: code.start()])[-_MAX_HEADING_WORDS:]
                if not words:
                    continue
                label = " ".join(words)
                if self._kind_named(label) is not None:
                    continue
                start = section.start + line.index(line.lstrip("#").strip())
                quote = text[start : section.heading_end]
                self.add(
                    Observation(
                        type=SuggestionType.KIND,
                        label=label,
                        document_id=document_id,
                        confidence=self.settings.heading_confidence,
                        source=RULES,
                        value=token,
                        evidence=Evidence(
                            document_id,
                            chunk_of(chunks, start),
                            start,
                            section.heading_end,
                            quote,
                        ).as_dict(),
                    )
                )
                found.append((section.start, section.end, _Owner(proposed_kind(label))))
                break
        return found

    def _field(
        self,
        owner: _Owner,
        label: str,
        value_start: int,
        value_end: int,
        evidence: Evidence,
        text: str,
        resolved: Sequence[Resolved],
        document_id: str,
    ) -> None:
        """One label and value about `owner`: a Relation when the label names a Kind or the
        value names Records of another Kind, an Attribute otherwise."""
        label = label.strip()
        if not label:
            return
        others = [
            r
            for r in resolved
            if r.usable
            and value_start <= r.mention.start < value_end
            and (owner.mention is None or r.key != owner.mention.key or r.kind_id != owner.ref)
        ]
        named = self._kind_named(label)
        targets = [named] if named is not None else list(dict.fromkeys(r.kind_id for r in others))
        targets = [t for t in targets if t != owner.ref]
        confidence = self.settings.field_confidence
        if owner.mention is not None:
            confidence *= owner.mention.confidence
        if targets:
            for target in targets[:1]:
                self.add(
                    Observation(
                        type=SuggestionType.RELATION,
                        label=label,
                        document_id=document_id,
                        confidence=confidence,
                        source=RULES,
                        kind=owner.ref,
                        target=target,
                        evidence=evidence.as_dict(),
                    )
                )
            return
        if named is not None or any(r.kind_id == owner.ref for r in others):
            return  # a field naming the owner's own Kind or Record
        self.add(
            Observation(
                type=SuggestionType.ATTRIBUTE,
                label=label,
                document_id=document_id,
                confidence=confidence,
                source=RULES,
                kind=owner.ref,
                value=self._sample(text[value_start:value_end]),
                evidence=evidence.as_dict(),
            )
        )

    def _fields(
        self,
        text: str,
        chunks: Sequence[Chunk],
        resolved: Sequence[Resolved],
        owners: Sequence[tuple[int, int, _Owner]],
        document_id: str,
    ) -> None:
        for match in _FIELD.finditer(text):
            label = match.group("a") or match.group("b") or ""
            value_group = "v" if match.group("a") is not None else "w"
            enclosing = [o for s, e, o in owners if s <= match.start() < e]
            if not enclosing:
                continue
            start, end = match.start(), match.end()
            while start < end and text[start].isspace():
                start += 1
            evidence = Evidence(document_id, chunk_of(chunks, start), start, end, text[start:end])
            self._field(
                enclosing[-1],
                label,
                match.start(value_group),
                match.end(value_group),
                evidence,
                text,
                resolved,
                document_id,
            )

    def _tables(
        self,
        text: str,
        chunks: Sequence[Chunk],
        resolved: Sequence[Resolved],
        document_id: str,
    ) -> None:
        lines = [(m.start(), m.end()) for m in _TABLE_LINE.finditer(text)]
        index = 0
        while index + 1 < len(lines):
            header, separator = lines[index], lines[index + 1]
            contiguous = text[header[1] : separator[0]].strip() == ""
            if not (contiguous and _SEPARATOR.match(text[separator[0] : separator[1]])):
                index += 1
                continue
            labels = [c.strip() for c in _cells(text, *header)]
            rows: list[tuple[int, int]] = []
            previous = separator[1]
            index += 2
            while index < len(lines) and not text[previous : lines[index][0]].strip():
                rows.append(lines[index])
                previous = lines[index][1]
                index += 1
            for start, end in rows:
                self._row(text, chunks, resolved, labels, start, end, document_id)

    def _row(
        self,
        text: str,
        chunks: Sequence[Chunk],
        resolved: Sequence[Resolved],
        labels: Sequence[str],
        start: int,
        end: int,
        document_id: str,
    ) -> None:
        inside = sorted(
            (r for r in resolved if r.usable and start <= r.mention.start < end),
            key=lambda r: r.mention.start,
        )
        if not inside:
            return
        first = inside[0]
        owner = _Owner(first.kind_id, first)
        evidence = Evidence(document_id, chunk_of(chunks, start), start, end, text[start:end])
        cells = _cell_bounds(text, start, end)
        for label, (cell_start, cell_end) in zip(labels, cells, strict=False):
            if cell_start <= first.mention.start < cell_end:
                continue
            if not text[cell_start:cell_end].strip():
                continue
            self._field(owner, label, cell_start, cell_end, evidence, text, resolved, document_id)

    # Tier 1 -----------------------------------------------------------------------------

    def span_labels(self) -> list[str]:
        """Extra span labels, those that name no Kind."""
        labels = self.settings.span_labels
        return [name for name in labels if self.ontology.kind_by_label(name) is None]

    def spans(self, spans: Iterable[Span], chunk: Chunk, *, document_id: str, model: str) -> None:
        wanted = {name.casefold() for name in self.span_labels()}
        for span in spans:
            if span.label.casefold() not in wanted:
                continue
            start, end = chunk.start + span.start, chunk.start + span.end
            self.add(
                Observation(
                    type=SuggestionType.KIND,
                    label=span.label,
                    document_id=document_id,
                    confidence=span.score,
                    source=f"span:{model}",
                    value=self._sample(span.text),
                    evidence=Evidence(document_id, chunk.seq, start, end, span.text).as_dict(),
                )
            )

    async def relations(
        self,
        extractor: RelationExtractor,
        resolved: Sequence[Resolved],
        chunk: Chunk,
        *,
        document_id: str,
        model: str,
    ) -> None:
        """Relations with `relation_labels` between the Records named in one chunk."""
        known = {r.api_name for r in self.ontology.relations.values()}
        labels = [RelationLabel(name=n) for n in self.settings.relation_labels if n not in known]
        inside = [
            r
            for r in resolved
            if r.usable and chunk.start <= r.mention.start and r.mention.end <= chunk.end
        ]
        if not labels or len(inside) < 2:
            return
        by_offsets: dict[tuple[int, int], Resolved] = {}
        spans: list[Span] = []
        for item in inside:
            start, end = item.mention.start - chunk.start, item.mention.end - chunk.start
            by_offsets[(start, end)] = item
            label = self.ontology.kinds[item.kind_id].api_name
            spans.append(Span(label, chunk.text[start:end], start, end))
        names = {label.name for label in labels}
        for relation in await extractor.extract(chunk.text, spans, labels):
            source = by_offsets.get((relation.head.start, relation.head.end))
            target = by_offsets.get((relation.tail.start, relation.tail.end))
            if relation.label not in names or source is None or target is None:
                continue
            if source.kind_id == target.kind_id and source.key == target.key:
                continue
            lo = min(relation.head.start, relation.tail.start)
            hi = max(relation.head.end, relation.tail.end)
            self.add(
                Observation(
                    type=SuggestionType.RELATION,
                    label=relation.label,
                    document_id=document_id,
                    confidence=relation.score * min(source.confidence, target.confidence),
                    source=f"relation:{model}",
                    kind=source.kind_id,
                    target=target.kind_id,
                    evidence=Evidence(
                        document_id,
                        chunk.seq,
                        chunk.start + lo,
                        chunk.start + hi,
                        chunk.text[lo:hi],
                    ).as_dict(),
                )
            )

    # Tier 2 -----------------------------------------------------------------------------

    def extend(self, observations: Iterable[Observation]) -> None:
        for observation in observations:
            self.add(observation)

    # Result -----------------------------------------------------------------------------

    def _noise(self, name: str, fields_known: Sequence[str]) -> bool:
        """Whether the proposed Kind `name` is noise (`kind_is_filtered`), unless the job's
        observations show an entity of its own: several distinct codes seen for it, or
        several Attributes it owns."""
        instances = {
            o.value for o in self.observations if o.type is SuggestionType.KIND and o.name == name
        }
        owner = f"{PROPOSED}{name}"
        fields = {
            o.name
            for o in self.observations
            if o.type is SuggestionType.ATTRIBUTE and o.kind == owner
        }
        instances.discard(None)
        return kind_is_filtered(
            name, fields_known, self.options, instances=len(instances), fields=len(fields)
        )

    def evidence_texts(self) -> list[str]:
        """Distinct quotes and sample values of the observations, for the check against
        vaulted values (`finish` takes the ones that hold one as `vaulted`)."""
        texts: dict[str, None] = {}
        for observation in self.observations:
            for text in ((observation.evidence or {}).get("quote"), observation.value):
                if text:
                    texts[str(text)] = None
        return list(texts)

    def finish(
        self,
        records: Mapping[tuple[str, str], Mapping[str, Any]],
        vaulted: Collection[str] = (),
    ) -> list[Observation]:
        """The observations to aggregate: new to the schema, never about a sensitive
        Attribute, without evidence holding a sensitive value of the Records in `records`
        (what the job read of the target branch) or one of the `vaulted` texts (those of
        `evidence_texts` that hold a vault value)."""
        tags = self.ontology.tags
        secrets = _sensitive_values(records, tags)
        kept: list[Observation] = []
        fields = dangling_reference_names(self.schema)
        verdicts: list[tuple[Observation, str | None]] = []
        for observation in self.observations:
            if observation.type is SuggestionType.KIND and self._noise(observation.name, fields):
                verdicts.append((observation, "suggestions_noise"))
                continue
            match = existing_match(
                self.schema,
                observation.type,
                observation.label,
                kind=observation.kind,
                target=observation.target,
                options=self.options,
                tags=tags,
            )
            if match is not None:
                verdicts.append(
                    (
                        observation,
                        "suggestions_withheld_sensitive"
                        if match.sensitive
                        else "suggestions_known",
                    )
                )
                continue
            verdicts.append((observation, None))
        # An Attribute owned by a proposed Kind that was filtered out has no owner to join.
        kept_owners = {f"{PROPOSED}{o.name}" for o, why in verdicts if why is None}
        lost_owners = {
            f"{PROPOSED}{o.name}"
            for o, why in verdicts
            if why is not None and o.type is SuggestionType.KIND
        } - kept_owners
        for observation, why in verdicts:
            if why is not None:
                self.count(why)
                continue
            if observation.type is SuggestionType.ATTRIBUTE and observation.kind in lost_owners:
                self.count("suggestions_orphaned")
                continue
            quote = str((observation.evidence or {}).get("quote") or "")
            value = observation.value or ""
            if (
                quote in vaulted
                or value in vaulted
                or any(s in quote.casefold() or s in value.casefold() for s in secrets)
            ):
                self.evidence_withheld += 1
                observation = Observation(
                    type=observation.type,
                    label=observation.label,
                    document_id=observation.document_id,
                    confidence=observation.confidence,
                    source=observation.source,
                    kind=observation.kind,
                    target=observation.target,
                )
            kept.append(observation)
        limit = self.settings.max_observations_per_job
        if len(kept) > limit:
            self.count("suggestion_observations_dropped", len(kept) - limit)
            kept = kept[:limit]
        self.count("suggestion_observations", len(kept))
        return kept


def _sensitive_values(
    records: Mapping[tuple[str, str], Mapping[str, Any]],
    tags: Mapping[str, frozenset[Sensitivity]],
) -> set[str]:
    """Text of the stored values of sensitive Attributes (vault references excluded)."""
    found: set[str] = set()
    for values in records.values():
        for attribute_id, value in values.items():
            if not tags.get(attribute_id) or isinstance(value, dict | list) or value is None:
                continue
            text = str(value).strip().casefold()
            if len(text) >= 3:
                found.add(text)
    return found


def _cell_bounds(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """`(start, end)` of the cells of a table row, pipes excluded."""
    line = text[start:end]
    pipes = [i for i, c in enumerate(line) if c == "|"]
    return [(start + left + 1, start + right) for left, right in pairwise(pipes)]


def _cells(text: str, start: int, end: int) -> list[str]:
    return [text[s:e] for s, e in _cell_bounds(text, start, end)]
