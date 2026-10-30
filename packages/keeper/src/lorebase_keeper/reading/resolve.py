"""Resolution of words to schema elements and indexed values (design 8.9, `POST /resolve`).

An agent asked "average price of red shoes" needs to know that "shoes" is
`Product.category = "Calzature"`, "red" `Product.color in ["Rosso", "red"]` and "price" the
Attribute `Product.price`. Resolution answers that and nothing more: it never interprets the
question, never aggregates and calls no model; it returns ranked candidates with the words they
matched and a ready-to-use predicate, and the agent writes the query.

**Phrases.** The text is normalized like the value index (`lorebase_store.values.normalize`:
lower case, accents stripped, blanks collapsed) and split into words at blanks and at
`search.resolve.separators`, `edges` stripped from each word (`Tokens`); other punctuation and
combining marks stay in words, so `C++`, `S/M` or Devanagari words match as written. The whole
normalized text is tried first (levels 1-2), then every run of one to
`search.resolve.max_phrase_words` words is a phrase. Longer phrases are tried first; the words
of a phrase that matched are not tried again in shorter ones. Each word is also tried with the
`plural_rules` suffixes folded both ways (shoes/shoe), so the folding is configured, not built
in for one language.

**Levels**, in order, a phrase stopping at the first that matches it:

1. *exact*: a Kind or Attribute name (api name split into words, display name) or an indexed
   value equal to the phrase; and, in the same lookup,
2. *synonym*: an alias of a Kind, an Attribute or a value (`aliases`, `value_aliases` in the
   schema). Values the same phrase reaches both ways become one candidate, so "red" gives
   `color in ["Rosso", "red"]` when `Rosso` lists `red` as an alias and `red` is stored too.
   Then, for phrases neither found,
3. *trigram*: names and indexed values similar to the phrase (trigram similarity, at least
   `trigram_threshold`; values through `lb_values_similar`), and words of Kind and Attribute
   descriptions, scaled by `scores.description`;
4. *embedding*: only when no level above matched anything, and only with the caller's
   `query_vector` (Keeper computes no embedding): the indexed values held by the Records
   nearest to the vector (semantic search, `embedding_records` of them).

**What the caller sees.** Only Kinds it may read (the read point's projection and scope) and
Attributes whose values it may read (`Masking.hidden`): a masked Attribute yields no
candidate at all, not even its name. Values come from the value index, which never holds
sensitive or vault Attributes. Counts are those of the branch read: no read of Lorebase
restricts rows within a Kind (row-level security isolates Domains), so a precomputed count
tells nothing a `lorebase_query` aggregate would not. On an overlay branch the index holds a
superset (the base's values too), so the kept candidates are counted again over the resolved
rows and those no row holds are dropped. At another Revision (`at`) the read uses the Revision's
snapshot, as search does.
"""

from __future__ import annotations

import re
from collections import OrderedDict, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from itertools import product
from threading import Lock
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Attribute, Kind, Schema
from lorebase_core.types import AttributeType
from lorebase_keeper.reading.points import ReadPoint
from lorebase_keeper.reading.problems import invalid
from lorebase_keeper.reading.requests import ResolveRequest, SearchRequest
from lorebase_keeper.reading.runtime import ReadRuntime
from lorebase_keeper.reading.search import search
from lorebase_keeper.reading.settings import DEFAULT_EDGES, DEFAULT_SEPARATORS, ResolveSettings
from lorebase_keeper.reading.snapshots import with_rows
from lorebase_keeper.reading.views import (
    ResolveCandidate,
    ResolveResponse,
    ResolveSkipped,
    served,
)
from lorebase_store import values as value_index
from lorebase_store.layout import escape_colons

Level = Literal["exact", "synonym", "trigram", "embedding"]
LEVELS: tuple[Level, ...] = ("exact", "synonym", "trigram", "embedding")

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


@dataclass(frozen=True, slots=True)
class Tokens:
    """How a text splits into words: at blanks and at `separators`, then `edges` stripped from
    each word's ends. Everything else stays in the word, so values such as `C++`, `S/M`, `50%`
    or words of scripts with combining marks match as written."""

    separators: str = DEFAULT_SEPARATORS
    edges: str = DEFAULT_EDGES

    def words(self, text_: str) -> list[str]:
        normalized = value_index.normalize(text_)
        if self.separators:
            normalized = normalized.translate({ord(c): " " for c in self.separators})
        stripped = (w.strip(self.edges) for w in normalized.split(" "))
        return [w for w in stripped if w]

    def phrase(self, text_: str) -> str:
        return " ".join(self.words(text_))


def words_of(text_: str, tokens: Tokens | None = None) -> list[str]:
    """The normalized words of a text."""
    return (tokens or Tokens()).words(text_)


def name_words(api_name: str) -> str:
    """An api name as words: `unit_price` and `UnitPrice` give `unit price`."""
    return value_index.normalize(_CAMEL.sub(" ", api_name).replace("_", " ").replace(".", " "))


def folded(word: str, rules: Sequence[tuple[str, str]]) -> list[str]:
    """`word` and its forms under the plural rules, both ways (`shoes` -> `shoe` and back)."""
    forms = [word]
    for plural, singular in rules:
        if plural and word.endswith(plural) and len(word) > len(plural) + 1:
            forms.append(word[: len(word) - len(plural)] + singular)
        if word.endswith(singular) and (singular or len(word) > 1):
            base = word[: len(word) - len(singular)] if singular else word
            if len(base) > 1:
                forms.append(base + plural)
    return list(dict.fromkeys(forms))


def variants(phrase: Sequence[str], rules: Sequence[tuple[str, str]], cap: int = 16) -> list[str]:
    """The phrase with each word in each of its folded forms, at most `cap`, as written first."""
    found: list[str] = []
    for combination in product(*(folded(w, rules) for w in phrase)):
        found.append(" ".join(combination))
        if len(found) >= cap:
            break
    return list(dict.fromkeys(found))


def trigrams(text_: str) -> frozenset[str]:
    """The trigrams pg_trgm takes from a text: per alphanumeric word, padded with two blanks
    before and one after."""
    grams: set[str] = set()
    for word in re.findall(r"[^\W_]+", text_.lower()):
        padded = f"  {word} "
        grams.update(padded[i : i + 3] for i in range(len(padded) - 2))
    return frozenset(grams)


def similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """pg_trgm's `similarity`: shared trigrams over all trigrams."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# Schema index -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Entry:
    """A name, alias or description word of a schema element, normalized."""

    kind_id: str
    attribute_id: str | None
    value: str | None
    synonym: bool
    description: bool = False


@dataclass(slots=True)
class SchemaIndex:
    """Names, aliases and description words of one schema, by normalized phrase."""

    phrases: dict[str, list[Entry]] = field(default_factory=lambda: defaultdict(list))
    grams: dict[str, frozenset[str]] = field(default_factory=dict)
    descriptions: dict[str, list[Entry]] = field(default_factory=lambda: defaultdict(list))

    def add(self, phrase: str, entry: Entry) -> None:
        if not phrase:
            return
        target = self.descriptions if entry.description else self.phrases
        if entry not in target[phrase]:
            target[phrase].append(entry)
        if not entry.description and phrase not in self.grams:
            self.grams[phrase] = trigrams(phrase)


def build_index(schema: Schema, tokens: Tokens | None = None) -> SchemaIndex:
    tokens = tokens or Tokens()
    index = SchemaIndex()
    for kind in schema.kinds.values():
        _index_element(index, tokens, kind.id, None, kind.api_name, kind.display_name, kind.aliases)
        _index_description(index, tokens, Entry(kind.id, None, None, False, True), kind.description)
        for attribute in kind.attributes.values():
            _index_element(
                index,
                tokens,
                kind.id,
                attribute.id,
                attribute.api_name,
                attribute.display_name,
                attribute.aliases,
            )
            _index_description(
                index,
                tokens,
                Entry(kind.id, attribute.id, None, False, True),
                attribute.description,
            )
            for value, aliases in (attribute.value_aliases or {}).items():
                for alias in aliases:
                    index.add(tokens.phrase(alias), Entry(kind.id, attribute.id, value, True))
    return index


def _index_element(
    index: SchemaIndex,
    tokens: Tokens,
    kind_id: str,
    attribute_id: str | None,
    api_name: str,
    display_name: str,
    aliases: Iterable[str] | None,
) -> None:
    exact = Entry(kind_id, attribute_id, None, False)
    index.add(name_words(api_name), exact)
    index.add(tokens.phrase(display_name), exact)
    for alias in aliases or ():
        index.add(tokens.phrase(alias), Entry(kind_id, attribute_id, None, True))


_DESCRIPTION_MIN_CHARS = 4
"""Description words shorter than this ("the", "of") are not indexed: they match anything."""


def _index_description(index: SchemaIndex, tokens: Tokens, entry: Entry, description: str) -> None:
    for word in set(tokens.words(description)):
        if len(word) >= _DESCRIPTION_MIN_CHARS:
            index.add(word, entry)


_INDEXES: OrderedDict[tuple[int, Tokens], tuple[Schema, SchemaIndex]] = OrderedDict()
_INDEX_ENTRIES = 64
_INDEX_LOCK = Lock()


def schema_index(schema: Schema, tokens: Tokens | None = None) -> SchemaIndex:
    """The index of `schema`, kept per schema object (compiled schemas and projections are
    cached, so one schema version keeps one index) and tokenization."""
    tokens = tokens or Tokens()
    key = (id(schema), tokens)
    with _INDEX_LOCK:
        found = _INDEXES.get(key)
        if found is not None and found[0] is schema:
            _INDEXES.move_to_end(key)
            return found[1]
    built = build_index(schema, tokens)
    with _INDEX_LOCK:
        _INDEXES[key] = (schema, built)
        while len(_INDEXES) > _INDEX_ENTRIES:
            _INDEXES.popitem(last=False)
    return built


# Matching -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Phrase:
    """A run of words; `raw` is the whole text, normalized but not split, which the exact
    levels try too (values with punctuation inside)."""

    start: int
    words: tuple[str, ...]
    raw: str | None = None

    @property
    def text(self) -> str:
        return self.raw if self.raw is not None else " ".join(self.words)

    @property
    def span(self) -> range:
        return range(self.start, self.start + len(self.words))


@dataclass(slots=True)
class Match:
    """A candidate being built: one target, the phrase it matched, the values it found."""

    kind_id: str
    attribute_id: str | None
    level: Level
    score: float
    phrase: Phrase
    values: dict[str, int] = field(default_factory=dict)
    description: bool = False
    valued: bool = False
    """A value candidate (it is dropped when no value is left after counting)."""
    uncounted: bool = False
    """Its values are not in the index: their counts are unknown."""

    @property
    def key(self) -> tuple[str, str | None, tuple[str, ...], tuple[int, ...]]:
        return (self.kind_id, self.attribute_id, tuple(self.values), tuple(self.phrase.span))


_Key = tuple[str, str | None, bool | str]
"""What `_merge` gathers hits under: Kind, Attribute, and whether values (or which one)."""


@dataclass(slots=True)
class _Context:
    point: ReadPoint
    settings: ResolveSettings
    levels: frozenset[Level]
    kinds: dict[str, Kind]
    index: SchemaIndex

    def attribute(self, kind_id: str, attribute_id: str | None) -> Attribute | None:
        kind = self.kinds.get(kind_id)
        return None if kind is None or attribute_id is None else kind.attributes.get(attribute_id)

    def readable(self, kind_id: str, attribute_id: str | None) -> bool:
        """Kinds the caller may read; Attributes whose values it may read."""
        if kind_id not in self.kinds:
            return False
        if attribute_id is None:
            return True
        attribute = self.attribute(kind_id, attribute_id)
        return attribute is not None and not self.point.masking.hidden(attribute, [kind_id])

    def resolvable(self, kind_id: str, attribute_id: str) -> bool:
        attribute = self.attribute(kind_id, attribute_id)
        return (
            attribute is not None
            and value_index.eligible(attribute) is not False
            and self.readable(kind_id, attribute_id)
        )


def phrases_of(
    words: Sequence[str], settings: ResolveSettings, raw: str | None = None
) -> list[Phrase]:
    """The raw text (when given and not a run already), then every run of words, longest
    first, then in text order."""
    found: list[Phrase] = []
    if raw and words and (raw != " ".join(words) or len(words) > settings.max_phrase_words):
        found.append(Phrase(0, tuple(words), raw))
    for size in range(min(settings.max_phrase_words, len(words)), 0, -1):
        for start in range(len(words) - size + 1):
            run = tuple(words[start : start + size])
            if size == 1 and len(run[0]) < settings.min_word_chars:
                continue
            found.append(Phrase(start, run))
    return found


def _kinds(point: ReadPoint, request: ResolveRequest) -> dict[str, Kind]:
    readable = {k.id: k for k in point.schema.kinds.values() if point.scope.allows(k.api_name)}
    if request.kinds is None:
        return readable
    point.scope.precheck(request.kinds)
    chosen: dict[str, Kind] = {}
    for position, name in enumerate(request.kinds):
        kind = point.schema.kind_by_api_name(name)
        if kind is None:
            raise invalid(f"/body/kinds/{position}", f"unknown Kind {name!r}", "unknown_kind")
        point.scope.require([kind.api_name])
        chosen[kind.id] = kind
    return chosen


def _levels(request: ResolveRequest, settings: ResolveSettings) -> frozenset[Level]:
    enabled = {level for level in LEVELS if getattr(settings.levels, level)}
    if request.levels is not None:
        enabled &= set(request.levels)
    return frozenset(enabled)


async def resolve(
    session: AsyncSession, runtime: ReadRuntime, point: ReadPoint, request: ResolveRequest
) -> ResolveResponse:
    settings = runtime.search.resolve
    if len(request.text) > settings.max_text_chars:
        raise invalid(
            "/body/text", f"must not exceed {settings.max_text_chars} characters", "too_long"
        )
    limit = request.limit or settings.results.default_limit
    if limit > settings.results.max_limit:
        raise invalid(
            "/body/limit", f"must not exceed {settings.results.max_limit}", "limit_exceeded"
        )
    tokens = Tokens(settings.separators, settings.edges)
    context = _Context(
        point,
        settings,
        _levels(request, settings),
        _kinds(point, request),
        schema_index(point.schema, tokens),
    )
    words = tokens.words(request.text)[: settings.max_words]
    skipped: list[ResolveSkipped] = []
    if not words or not context.kinds:
        return ResolveResponse(**served(point), candidates=[], unmatched=words)
    point = await with_rows(session, runtime, point, "Resolution")
    context.point = point
    phrases = phrases_of(words, settings, value_index.normalize(request.text))
    covered: set[int] = set()
    matches: list[Match] = []
    if context.levels & {"exact", "synonym"}:
        matches += await _exact(session, context, phrases, covered)
    if "trigram" in context.levels:
        matches += await _similar(session, runtime, context, phrases, covered)
    if "embedding" not in context.levels:
        if settings.levels.embedding and request.levels is not None:
            skipped.append(ResolveSkipped(level="embedding", reason="not requested"))
        elif not settings.levels.embedding:
            skipped.append(ResolveSkipped(level="embedding", reason="disabled"))
    elif matches:
        skipped.append(ResolveSkipped(level="embedding", reason="other levels matched"))
    elif request.query_vector is None:
        skipped.append(ResolveSkipped(level="embedding", reason="no query_vector"))
    else:
        matches += await _embedded(session, runtime, context, request, words, covered)
    candidates = await _candidates(session, context, matches, limit)
    unmatched = [w for i, w in enumerate(words) if i not in covered]
    return ResolveResponse(
        **served(point), candidates=candidates, unmatched=unmatched, skipped=skipped
    )


async def _exact(
    session: AsyncSession, context: _Context, phrases: Sequence[Phrase], covered: set[int]
) -> list[Match]:
    """Levels 1 and 2: names, aliases and stored values equal to a phrase (or its folded
    forms)."""
    settings = context.settings
    forms = {
        p: [p.text, " ".join(p.words)]
        if p.raw is not None
        else variants(p.words, settings.plural_rules)
        for p in phrases
    }
    hits: dict[str, list[value_index.ValueHit]] = defaultdict(list)
    if "exact" in context.levels:
        terms = sorted({form for found in forms.values() for form in found})
        for hit in await value_index.exact_values(
            session,
            context.point.layout,
            terms,
            kind_ids=sorted(context.kinds),
            per_term=settings.exact_per_phrase,
        ):
            hits[hit.term].append(hit)
    found: list[Match] = []
    for phrase in phrases:
        if covered.intersection(phrase.span):
            continue
        matched: dict[_Key, Match] = {}
        for form in forms[phrase]:
            for entry in context.index.phrases.get(form, ()):
                level: Level = "synonym" if entry.synonym else "exact"
                if level not in context.levels or not context.readable(
                    entry.kind_id, entry.attribute_id
                ):
                    continue
                if entry.value is not None and not context.resolvable(
                    entry.kind_id, entry.attribute_id or ""
                ):
                    continue
                _merge(
                    matched,
                    context,
                    phrase,
                    entry.kind_id,
                    entry.attribute_id,
                    level,
                    1.0,
                    None if entry.value is None else (entry.value, 0),
                )
            for hit in hits.get(form, ()):
                if context.resolvable(hit.kind_id, hit.attribute_id):
                    _merge(
                        matched,
                        context,
                        phrase,
                        hit.kind_id,
                        hit.attribute_id,
                        "exact",
                        1.0,
                        (hit.value, hit.records),
                    )
        if matched:
            covered.update(phrase.span)
            found.extend(matched.values())
    return found


def _merge(
    matched: dict[_Key, Match],
    context: _Context,
    phrase: Phrase,
    kind_id: str,
    attribute_id: str | None,
    level: Level,
    similar: float,
    value: tuple[str, int] | None,
    *,
    description: bool = False,
    split: bool = False,
) -> None:
    """Add a hit to the match of its target, which keeps the best level and every value
    (with `split`, each value is a target of its own)."""
    scores = context.settings.scores
    weight = scores.description if description else 1.0
    score = getattr(scores, level) * similar * weight
    key: _Key = (kind_id, attribute_id, value[0] if split and value else value is not None)
    match = matched.get(key)
    if match is None:
        match = Match(kind_id, attribute_id, level, score, phrase, description=description)
        matched[key] = match
    elif score > match.score:
        match.level, match.score = level, score
    if value is not None:
        match.valued = True
        match.values[value[0]] = max(match.values.get(value[0], 0), value[1])


async def _similar(
    session: AsyncSession,
    runtime: ReadRuntime,
    context: _Context,
    phrases: Sequence[Phrase],
    covered: set[int],
) -> list[Match]:
    """Level 3, for the phrases the exact levels left: similar names and values, and
    description words."""
    settings = context.settings
    open_ = [
        p
        for p in phrases
        if p.raw is None
        and not covered.intersection(p.span)
        and len(p.text) >= settings.trigram_min_chars
    ]
    if not open_:
        return []
    # Names of the Kinds in reach only: the work is phrases times these names.
    names = [
        (name, grams)
        for name, grams in context.index.grams.items()
        if any(e.kind_id in context.kinds for e in context.index.phrases[name])
    ]
    # An overlay's index holds its base's values too; some fall out once counted.
    per_term = settings.per_phrase * (2 if context.point.layout.overlay else 1)
    hits: dict[str, list[value_index.ValueHit]] = defaultdict(list)
    for hit in await value_index.similar_values(
        session,
        context.point.layout,
        [p.text for p in open_],
        threshold=settings.trigram_threshold,
        per_term=per_term,
        kind_ids=sorted(context.kinds),
        runtime=runtime.store,
    ):
        hits[hit.term].append(hit)
    found: list[Match] = []
    for phrase in open_:
        if covered.intersection(phrase.span):
            continue
        matched: dict[_Key, Match] = {}
        grams = trigrams(phrase.text)
        for name, name_grams in names:
            similar = similarity(grams, name_grams)
            if similar < settings.trigram_threshold:
                continue
            for entry in context.index.phrases[name]:
                if not context.readable(entry.kind_id, entry.attribute_id):
                    continue
                if entry.value is not None and not context.resolvable(
                    entry.kind_id, entry.attribute_id or ""
                ):
                    continue
                _merge(
                    matched,
                    context,
                    phrase,
                    entry.kind_id,
                    entry.attribute_id,
                    "trigram",
                    similar,
                    None if entry.value is None else (entry.value, 0),
                )
        for hit in hits.get(phrase.text, ()):
            if context.resolvable(hit.kind_id, hit.attribute_id):
                _merge(
                    matched,
                    context,
                    phrase,
                    hit.kind_id,
                    hit.attribute_id,
                    "trigram",
                    hit.similarity,
                    (hit.value, hit.records),
                )
        if len(phrase.words) == 1:
            for form in variants(phrase.words, settings.plural_rules):
                for entry in context.index.descriptions.get(form, ()):
                    if context.readable(entry.kind_id, entry.attribute_id):
                        _merge(
                            matched,
                            context,
                            phrase,
                            entry.kind_id,
                            entry.attribute_id,
                            "trigram",
                            1.0,
                            None,
                            description=True,
                        )
        if matched:
            covered.update(phrase.span)
            found.extend(matched.values())
    return found


_RECORD_VALUES = (
    'SELECT m.kind_id, m.key, m."values" FROM {rows} '
    "WHERE m.domain_id = :domain AND m.branch = :branch "
    "AND (m.kind_id, m.key) IN (SELECT * FROM unnest(CAST(:kinds AS text[]), "
    "CAST(:keys AS text[])))"
)


async def _embedded(
    session: AsyncSession,
    runtime: ReadRuntime,
    context: _Context,
    request: ResolveRequest,
    words: Sequence[str],
    covered: set[int],
) -> list[Match]:
    """Level 4: indexed values held by the Records nearest to the query vector, each value a
    candidate of its own scored by the closest Record holding it."""
    point, settings = context.point, context.settings
    names = sorted(context.kinds[k].api_name for k in context.kinds)
    hits = await search(
        session,
        runtime,
        point,
        SearchRequest(
            query_vector=request.query_vector,
            mode="semantic",
            kinds=names,
            limit=min(settings.embedding_records, runtime.search.results.max_limit),
        ),
    )
    by_name = {k.api_name: k for k in context.kinds.values()}
    nearest = [(by_name[h.kind], h.key, h.score) for h in hits.items if h.kind in by_name]
    if not nearest:
        return []
    held = await value_index.selection(session, point.domain.id)
    statement = text(_RECORD_VALUES.format(rows=escape_colons(point.layout.records_from("m"))))
    rows = await session.execute(
        statement,
        {
            "domain": point.domain.id,
            "branch": point.layout.branch,
            "kinds": [k.id for k, _, _ in nearest],
            "keys": [key for _, key, _ in nearest],
        },
    )
    score_of = {(k.id, key): score for k, key, score in nearest}
    phrase = Phrase(0, tuple(words))
    matched: dict[_Key, Match] = {}
    for kind_id, key, stored in rows.all():
        closeness = max(0.0, min(1.0, float(score_of.get((kind_id, key), 0.0))))
        kind = context.kinds[kind_id]
        for attribute in kind.attributes.values():
            if (kind_id, attribute.id) not in held or not context.resolvable(kind_id, attribute.id):
                continue
            for value in _strings(stored.get(attribute.id), held[(kind_id, attribute.id)]):
                _merge(
                    matched,
                    context,
                    phrase,
                    kind_id,
                    attribute.id,
                    "embedding",
                    closeness,
                    (value, 0),
                    split=True,
                )
    if matched:
        covered.update(phrase.span)
    return list(matched.values())


def _strings(stored: Any, max_chars: int) -> list[str]:
    """The indexed strings of a stored value (`lb_value_strings`)."""
    items = stored if isinstance(stored, list) else [stored]
    return [s for s in dict.fromkeys(items) if isinstance(s, str) and 0 < len(s) <= max_chars]


async def _candidates(
    session: AsyncSession, context: _Context, matches: Sequence[Match], limit: int
) -> list[ResolveCandidate]:
    """Best first, `limit` of them; on an overlay, values counted over the resolved rows.
    Matches are counted in rank order until `limit` of them are left, so a value match that
    counting empties gives its place to the next one."""
    ranked = sorted(
        matches,
        key=lambda m: (
            -m.score,
            LEVELS.index(m.level),
            -len(m.phrase.words),
            -sum(m.values.values()),
            m.kind_id,
            m.attribute_id or "",
        ),
    )
    kept: list[Match] = []
    seen: set[tuple[str, str | None, tuple[str, ...], tuple[int, ...]]] = set()
    held: dict[tuple[str, str], int] | None = None
    for match in ranked:
        if len(kept) >= limit:
            break
        if match.key in seen:
            continue
        seen.add(match.key)
        held = await _count(session, context, [match], held)
        if match.values or not match.valued:
            kept.append(match)
    return [_out(context, m) for m in kept]


async def _count(
    session: AsyncSession,
    context: _Context,
    chosen: Sequence[Match],
    held: dict[tuple[str, str], int] | None = None,
) -> dict[tuple[str, str], int] | None:
    """Counts of the kept values: over the resolved rows on an overlay (the index holds a
    superset there); from the index for values an alias or a nearest Record named. Values no
    Record holds are dropped; an Attribute the index does not hold keeps its values uncounted.
    """
    layout = context.point.layout
    for match in chosen:
        if not match.values or match.attribute_id is None:
            continue
        attribute = context.attribute(match.kind_id, match.attribute_id)
        assert attribute is not None
        wanted = sorted(match.values)
        if layout.overlay:
            counts = await value_index.live_counts(
                session,
                layout,
                match.kind_id,
                attribute.id,
                wanted,
                array=attribute.type.name is AttributeType.ARRAY,
            )
        elif any(n == 0 for n in match.values.values()):
            if held is None:
                held = await value_index.selection(session, context.point.domain.id)
            if (match.kind_id, attribute.id) not in held:
                match.uncounted = True
                continue
            counts = await value_index.indexed_counts(
                session, layout, match.kind_id, attribute.id, wanted
            )
        else:
            continue
        match.values = {v: n for v, n in counts.items() if n > 0}
    return held


def _out(context: _Context, match: Match) -> ResolveCandidate:
    kind = context.kinds[match.kind_id]
    attribute = context.attribute(match.kind_id, match.attribute_id)
    if attribute is None:
        return ResolveCandidate(
            target="kind",
            kind=kind.api_name,
            score=round(match.score, 4),
            level=match.level,
            matched=match.phrase.text,
        )
    if not match.values:
        return ResolveCandidate(
            target="attribute",
            kind=kind.api_name,
            attribute=attribute.api_name,
            type=attribute.type.describe(),
            score=round(match.score, 4),
            level=match.level,
            matched=match.phrase.text,
        )
    ordered = sorted(match.values, key=lambda v: (-match.values[v], v))
    records = None if match.uncounted else sum(match.values.values())
    return ResolveCandidate(
        target="value",
        kind=kind.api_name,
        attribute=attribute.api_name,
        type=attribute.type.describe(),
        values=ordered,
        predicate=predicate(attribute, ordered),
        score=round(match.score, 4),
        level=match.level,
        matched=match.phrase.text,
        records=records,
    )


def predicate(attribute: Attribute, values: Sequence[str]) -> dict[str, Any]:
    """The DSL condition selecting Records holding any of `values` (design 8.2)."""
    name = attribute.api_name
    if attribute.type.name is AttributeType.ARRAY:
        clauses = [{"attr": name, "op": "contains", "value": v} for v in values]
        return clauses[0] if len(clauses) == 1 else {"or": clauses}
    if len(values) == 1:
        return {"attr": name, "op": "eq", "value": values[0]}
    return {"attr": name, "op": "in", "value": list(values)}
