"""Rule-based Attribute values (tier 0), sentence by sentence.

In each sentence, dates (ISO and written forms such as `8 February 2024`), numbers (with
thousands separators) and enum values are found. A value goes to an Attribute of a Record when
a cue word of that Attribute (from its api name, display name and longer description words)
precedes it by at most `value_cue_words` words in the sentence; enum values written as
upper-case codes (`DDP`, `EUR`) need no cue. The Records a sentence can describe are the ones
it names; a sentence that names none describes the Record of its section heading. The closer
the cue (and the Record), the higher the confidence, up to `confidence.value_max`. Each
Attribute of a Record takes at most one value per sentence and each occurrence goes to at most
one Attribute. Booleans and free text are left to tier 2, except the title of a Record whose
key appears in a heading, which is taken from the heading.

These rules propose; reviewers decide. Their confidence stays below the default
`auto_accept_threshold`, so nothing they find is accepted without review by default.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from lorebase_core.model import Attribute
from lorebase_core.types import AttributeType, EncodingError, encode_value
from lorebase_scribe.extraction.candidates import Evidence, Resolved, ValueCandidate
from lorebase_scribe.extraction.ontology import Ontology, humanize
from lorebase_scribe.extraction.settings import ExtractionSettings

_MONTHS = {
    name: index
    for index, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for name in names
}
_MONTH = (
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?"
    r"|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_ISO_DATE = re.compile(r"(?<![\w-])(\d{4})-(\d{2})-(\d{2})(?![\w-])")
_DMY = re.compile(rf"(?<!\w)(\d{{1,2}})\s+{_MONTH}\.?,?\s+(\d{{4}})(?!\w)")
_MDY = re.compile(rf"(?<!\w){_MONTH}\.?\s+(\d{{1,2}}),?\s+(\d{{4}})(?!\w)")
_MONTH_YEAR = re.compile(rf"(?<!\w){_MONTH}\.?,?\s+(\d{{4}})(?!\w)")
_NUMBER = re.compile(r"(?<![\w.,-])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?![\w-]|[.,]\d)")
_TOKEN = re.compile(r"\w+")
_GENERIC = frozenset(
    {
        *("date", "day", "days", "time", "first", "last", "name", "type", "kind", "number"),
        *("code", "value", "iso", "when", "units", "unit"),
    }
)
_DATE_TYPES = (AttributeType.DATE,)
_NUMBER_TYPES = (AttributeType.INTEGER, AttributeType.DECIMAL, AttributeType.FLOAT)


@dataclass(frozen=True, slots=True)
class Occurrence:
    start: int
    end: int
    kind: str  # date | number | enum
    value: Any


def find_dates(text: str, start: int, end: int) -> list[Occurrence]:
    """Full dates in `text[start:end]`, as ISO strings."""
    found: list[Occurrence] = []
    for match in _ISO_DATE.finditer(text, start, end):
        _add_date(found, match, int(match[1]), int(match[2]), int(match[3]))
    for match in _DMY.finditer(text, start, end):
        _add_date(found, match, int(match[3]), _MONTHS[match[2].lower()], int(match[1]))
    for match in _MDY.finditer(text, start, end):
        _add_date(found, match, int(match[3]), _MONTHS[match[1].lower()], int(match[2]))
    return found


def _add_date(found: list[Occurrence], match: re.Match[str], y: int, m: int, d: int) -> None:
    try:
        found.append(Occurrence(match.start(), match.end(), "date", date(y, m, d).isoformat()))
    except ValueError:
        return


def find_numbers(
    text: str, start: int, end: int, taken: Sequence[tuple[int, int]]
) -> list[Occurrence]:
    """Numbers in `text[start:end]` outside `taken` spans and month-year phrases, as strings
    without thousands separators."""
    blocked = [*taken, *((m.start(), m.end()) for m in _MONTH_YEAR.finditer(text, start, end))]
    found: list[Occurrence] = []
    for match in _NUMBER.finditer(text, start, end):
        if any(match.start() < e and match.end() > s for s, e in blocked):
            continue
        whole = match[1].replace(",", "")
        value = f"{whole}.{match[2]}" if match[2] else whole
        found.append(Occurrence(match.start(), match.end(), "number", value))
    return found


def _enums(text: str, start: int, end: int, attribute: Attribute) -> list[Occurrence]:
    found: list[Occurrence] = []
    for option in attribute.type.enum_values or ():
        if len(option) < 2:
            continue
        pattern = re.compile(rf"(?<![\w-]){re.escape(option)}(?![\w-])")
        found.extend(
            Occurrence(m.start(), m.end(), "enum", option)
            for m in pattern.finditer(text, start, end)
        )
    return found


def _cue_matches(token: str, cue: str) -> bool:
    return token == cue or (len(cue) >= 3 and token.startswith(cue))


def _encode(attribute: Attribute, occurrence: Occurrence) -> Any:
    name = attribute.type.name
    raw: Any = occurrence.value
    if occurrence.kind == "number":
        if name is AttributeType.INTEGER:
            if "." in raw:
                return None
            raw = int(raw)
        elif name is AttributeType.FLOAT:
            raw = float(raw)
    try:
        return encode_value(attribute.type, raw)
    except EncodingError:
        return None


def _compatible(attribute: Attribute, occurrence: Occurrence) -> bool:
    name = attribute.type.name
    if occurrence.kind == "date":
        return name in _DATE_TYPES
    if occurrence.kind == "number":
        return name in _NUMBER_TYPES
    return name is AttributeType.ENUM and occurrence.value in (attribute.type.enum_values or ())


@dataclass(frozen=True, slots=True)
class _Option:
    score: float
    owner_distance: int
    owner: Resolved
    attribute: Attribute
    occurrence: Occurrence


def sentence_values(
    ontology: Ontology,
    owners: Sequence[Resolved],
    text: str,
    sentence: tuple[int, int],
    *,
    excluded: Sequence[tuple[int, int]],
    settings: ExtractionSettings,
) -> list[ValueCandidate]:
    """Values the sentence `text[sentence[0]:sentence[1]]` gives the Records `owners` name."""
    start, end = sentence
    dates = find_dates(text, start, end)
    numbers = find_numbers(text, start, end, [*excluded, *((d.start, d.end) for d in dates)])
    tokens = [(m.start(), m.group().lower()) for m in _TOKEN.finditer(text, start, end)]
    limit = settings.value_cue_words
    names = {view.id: set(humanize(view.api_name).split()) for view in ontology.kinds.values()}
    options: list[_Option] = []
    for owner in owners:
        view = ontology.kinds[owner.kind_id]
        own = names[view.id]
        others = set().union(*(n for k, n in names.items() if k != view.id)) - own
        skip = {a.id for a in (view.key_attribute, view.title_attribute) if a is not None}
        for attribute in view.attributes:
            if attribute.id in skip:
                continue
            cues = [c for c in view.cues.get(attribute.id, ()) if c not in own | _GENERIC]
            pool = (
                _enums(text, start, end, attribute)
                if attribute.type.name is AttributeType.ENUM
                else [*dates, *numbers]
            )
            for occurrence in pool:
                if not _compatible(attribute, occurrence):
                    continue
                before = [
                    i for i, (position, _t) in enumerate(tokens) if position < occurrence.start
                ]
                cue_gaps = [
                    len(before) - 1 - index
                    for index in before
                    if any(_cue_matches(tokens[index][1], cue) for cue in cues)
                    # "supplier status" announces a value of a Supplier, not of this Kind
                    and not (index > 0 and tokens[index - 1][1] in others)
                ]
                gap = min((g for g in cue_gaps if g <= limit), default=None)
                if gap is None:
                    if occurrence.kind == "enum" and str(occurrence.value).isupper():
                        gap = limit
                    else:
                        continue
                score = settings.confidence.value_max * (1 - 0.5 * gap / max(limit, 1))
                distance = abs(owner.mention.start - occurrence.start)
                options.append(_Option(score, distance, owner, attribute, occurrence))
    options.sort(
        key=lambda o: (-o.score, o.owner_distance, o.occurrence.start, o.attribute.api_name)
    )
    used: set[tuple[str, str | None, str]] = set()
    places: set[int] = set()
    found: list[ValueCandidate] = []
    for option in options:
        slot = (option.owner.kind_id, option.owner.key, option.attribute.id)
        if slot in used or option.occurrence.start in places:
            continue
        value = _encode(option.attribute, option.occurrence)
        if value is None:
            continue
        used.add(slot)
        places.add(option.occurrence.start)
        mention = option.owner.mention
        found.append(
            ValueCandidate(
                subject=option.owner,
                attribute_id=option.attribute.id,
                value=value,
                evidence=Evidence(mention.document_id, mention.chunk, start, end, text[start:end]),
                confidence=option.score,
                tier=0,
                model="rules",
            )
        )
    return found


def heading_title(
    ontology: Ontology, subject: Resolved, text: str, *, settings: ExtractionSettings
) -> ValueCandidate | None:
    """The title of a Record named by key in a heading: the heading text."""
    view = ontology.kinds[subject.kind_id]
    attribute = view.title_attribute
    if attribute is None or attribute.type.name not in (AttributeType.STRING, AttributeType.TEXT):
        return None
    if view.key_attribute is not None and attribute.id == view.key_attribute.id:
        return None
    mention = subject.mention
    line_start = text.rfind("\n", 0, mention.start) + 1
    line_end = text.find("\n", mention.end)
    line_end = len(text) if line_end == -1 else line_end
    heading = text[line_start:line_end].lstrip("#").strip()
    if not heading:
        return None
    try:
        value = encode_value(attribute.type, heading)
    except EncodingError:
        return None
    offset = line_start + text[line_start:line_end].index(heading)
    return ValueCandidate(
        subject=subject,
        attribute_id=attribute.id,
        value=value,
        evidence=Evidence(
            mention.document_id, mention.chunk, offset, offset + len(heading), heading
        ),
        confidence=settings.confidence.value_max,
        tier=0,
        model="rules",
    )
