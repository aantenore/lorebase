"""Ingestion modes of extraction (design 9.2 "Ingestion modes", AC-36).

How much of an extraction a person sees is a Domain choice within bounds the installation
sets. `extraction.mode` is one of:

- `review_all`: nothing is accepted at once (as an `auto_accept_threshold` above 1);
- `doubts_only` (the installation default): complete, confident, unambiguous items that are
  not held are accepted at once;
- `direct`: as `doubts_only`, and Scribe merges the Proposal itself when nothing is held and
  every item came from tier 0. Never an installation value.

A Domain's `settings.extraction` overrides `mode`, `auto_accept_threshold` and
`min_confidence`, and adds `rules` keyed by `source_key_prefix` (the longest matching prefix
wins; members a rule leaves out come from the Domain). Tightening is always applied: a
stricter mode (`review_all` > `doubts_only` > `direct`), a higher threshold, a lower
`min_confidence`. Loosening below the installation's value is applied only for the members
the installation lists in `domain_may_loosen`; any other loosening value, and any invalid
value, is ignored on its own and reported (`ignored`). Both Keeper (to report
`ignored_settings`) and Scribe (to run jobs) resolve settings with this module.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

SETTING_KEY = "extraction"
"""Key of the extraction setting in a Domain's settings."""
RULES_KEY = "rules"
PREFIX_KEY = "source_key_prefix"

REVIEW_ALL = "review_all"
DOUBTS_ONLY = "doubts_only"
DIRECT = "direct"
MODES = (DIRECT, DOUBTS_ONLY, REVIEW_ALL)
"""Modes from the loosest to the strictest."""
INSTALLATION_MODES = (DOUBTS_ONLY, REVIEW_ALL)
"""Modes the installation may set: `direct` is per Domain only."""

MODE = "mode"
THRESHOLD = "auto_accept_threshold"
MIN_CONFIDENCE = "min_confidence"
MEMBERS = (MODE, THRESHOLD, MIN_CONFIDENCE)
"""Members a Domain and its rules may set."""
DOMAIN_KEYS = frozenset({*MEMBERS, RULES_KEY})
RULE_KEYS = frozenset({*MEMBERS, PREFIX_KEY})

MAX_THRESHOLD = 1.01
"""Largest `auto_accept_threshold` (above 1 accepts nothing); also the threshold of
`review_all`."""

# Blocked reasons of `direct` (`direct_blocked` in a job's result), in the order checked.
BLOCKED_TIER = "tier"
BLOCKED_PENDING = "pending_items"
BLOCKED_CONFLICTS = "conflicts"
BLOCKED_RECONCILIATION = "reconciliation"
BLOCKED_PROTECTED = "protected"
DIRECT_MERGE_MODE = "direct"
"""`mode` of the `proposal.merge` audit entry of a merge Scribe ran in `direct` mode."""


@dataclass(frozen=True, slots=True)
class Ingestion:
    """The effective members of one document or job."""

    mode: str = DOUBTS_ONLY
    auto_accept_threshold: float = 0.9
    min_confidence: float = 0.3

    @property
    def accept_threshold(self) -> float:
        """The threshold items are accepted at once with: above any confidence for
        `review_all`."""
        return MAX_THRESHOLD if self.mode == REVIEW_ALL else self.auto_accept_threshold


@dataclass(frozen=True, slots=True)
class Installation:
    """The installation's bounds: `scribe.extraction.mode`, `auto_accept_threshold`,
    `min_confidence` and `domain_may_loosen`."""

    base: Ingestion = field(default_factory=Ingestion)
    domain_may_loosen: frozenset[str] = frozenset({MODE})

    @classmethod
    def from_section(cls, raw: Mapping[str, Any] | None) -> Installation:
        """Read the bounds from a raw `scribe.extraction` mapping, defaults for what is
        missing or invalid (Keeper reads Scribe's section this way, without owning it)."""
        values = raw or {}
        default = Ingestion()
        mode = values.get(MODE)
        threshold: Any = values.get(THRESHOLD)
        minimum: Any = values.get(MIN_CONFIDENCE)
        loosen = values.get("domain_may_loosen")
        return cls(
            base=Ingestion(
                mode=str(mode) if mode in INSTALLATION_MODES else default.mode,
                auto_accept_threshold=float(threshold)
                if _valid(THRESHOLD, threshold)
                else default.auto_accept_threshold,
                min_confidence=float(minimum)
                if _valid(MIN_CONFIDENCE, minimum)
                else default.min_confidence,
            ),
            domain_may_loosen=frozenset(str(m) for m in loosen if m in MEMBERS)
            if isinstance(loosen, list | tuple)
            else frozenset({MODE}),
        )


@dataclass(frozen=True, slots=True)
class Resolved:
    """The effective members and the paths of the values that were ignored."""

    ingestion: Ingestion
    ignored: tuple[str, ...] = ()


def _number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _valid(member: str, value: object) -> bool:
    if member == MODE:
        return value in MODES
    if member == THRESHOLD:
        return _number(value) and 0 <= float(value) <= MAX_THRESHOLD  # type: ignore[arg-type]
    if member == MIN_CONFIDENCE:
        return _number(value) and 0 <= float(value) <= 1  # type: ignore[arg-type]
    return False


def looser(member: str, value: Any, base: Any) -> bool:
    """Whether `value` of `member` lets more through without a person than `base`."""
    if member == MODE:
        return MODES.index(value) < MODES.index(base)
    if member == THRESHOLD:
        return float(value) < float(base)
    return float(value) > float(base)


def _apply(
    installation: Installation,
    current: dict[str, Any],
    raw: Mapping[str, Any],
    prefix: str,
    ignored: list[str],
) -> None:
    base = installation.base
    for member in MEMBERS:
        if member not in raw:
            continue
        value = raw[member]
        if not _valid(member, value) or (
            looser(member, value, getattr(base, member))
            and member not in installation.domain_may_loosen
        ):
            ignored.append(f"{prefix}.{member}")
            continue
        current[member] = float(value) if member != MODE else str(value)


def _rules(raw: Mapping[str, Any]) -> list[tuple[int, Mapping[str, Any]]]:
    rules = raw.get(RULES_KEY)
    if not isinstance(rules, list):
        return []
    return [
        (index, rule)
        for index, rule in enumerate(rules)
        if isinstance(rule, Mapping)
        and isinstance(rule.get(PREFIX_KEY), str)
        and rule.get(PREFIX_KEY)
    ]


def _setting(domain_settings: Mapping[str, Any] | None) -> Mapping[str, Any]:
    raw = (domain_settings or {}).get(SETTING_KEY)
    return raw if isinstance(raw, Mapping) else {}


def _ingestion(values: Mapping[str, Any]) -> Ingestion:
    return Ingestion(
        mode=str(values[MODE]),
        auto_accept_threshold=float(values[THRESHOLD]),
        min_confidence=float(values[MIN_CONFIDENCE]),
    )


def resolve(
    installation: Installation,
    domain_settings: Mapping[str, Any] | None,
    source_key: str | None = None,
) -> Resolved:
    """The effective members for a document of `source_key` (`None`: keyless, the Domain's
    own members), with the ignored values of the Domain level and of the rule applied."""
    raw = _setting(domain_settings)
    base = installation.base
    current: dict[str, Any] = {
        MODE: base.mode,
        THRESHOLD: base.auto_accept_threshold,
        MIN_CONFIDENCE: base.min_confidence,
    }
    ignored: list[str] = []
    _apply(installation, current, raw, SETTING_KEY, ignored)
    if source_key is not None:
        matching = [
            (index, rule)
            for index, rule in _rules(raw)
            if source_key.startswith(str(rule[PREFIX_KEY]))
        ]
        if matching:
            index, rule = max(
                matching, key=lambda found: (len(str(found[1][PREFIX_KEY])), -found[0])
            )
            _apply(installation, current, rule, f"{SETTING_KEY}.{RULES_KEY}[{index}]", ignored)
    return Resolved(_ingestion(current), tuple(ignored))


def ignored_settings(
    installation: Installation, domain_settings: Mapping[str, Any] | None
) -> list[str]:
    """Paths of every value of the Domain's extraction setting that does not apply: invalid
    values, unknown keys, malformed rules and loosening values of members the installation
    does not let Domains loosen (`GET /domains/{d}`)."""
    raw = (domain_settings or {}).get(SETTING_KEY)
    if raw is None:
        return []
    if not isinstance(raw, Mapping):
        return [SETTING_KEY]
    ignored: list[str] = [f"{SETTING_KEY}.{key}" for key in sorted(set(raw) - DOMAIN_KEYS)]
    _apply(installation, {}, raw, SETTING_KEY, ignored)
    rules = raw.get(RULES_KEY)
    if rules is not None and not isinstance(rules, list):
        ignored.append(f"{SETTING_KEY}.{RULES_KEY}")
    valid = {index for index, _ in _rules(raw)}
    for index, rule in enumerate(rules if isinstance(rules, list) else []):
        path = f"{SETTING_KEY}.{RULES_KEY}[{index}]"
        if index not in valid:
            ignored.append(path)
            continue
        ignored.extend(f"{path}.{key}" for key in sorted(set(rule) - RULE_KEYS))
        _apply(installation, {}, rule, path, ignored)
    return ignored


def combine(ingestions: Iterable[Ingestion]) -> Ingestion:
    """The members of a job over several documents: the strictest mode, the highest
    threshold and the lowest `min_confidence` (fewest candidates dropped unseen)."""
    found = list(ingestions)
    if not found:
        return Ingestion()
    return Ingestion(
        mode=max((i.mode for i in found), key=MODES.index),
        auto_accept_threshold=max(i.auto_accept_threshold for i in found),
        min_confidence=min(i.min_confidence for i in found),
    )


def setting_problems(raw: object) -> list[tuple[str, str]]:
    """Structural problems of a Domain's `extraction` setting as `(pointer, message)`, for
    writes: an unknown key, a value of the wrong type or range, a rule without a prefix or a
    prefix given twice. Loosening values are not problems (they are reported as ignored)."""
    if raw is None:
        return []
    if not isinstance(raw, Mapping):
        return [("", "must be an object")]
    problems: list[tuple[str, str]] = [
        (f"/{key}", "unknown member") for key in sorted(set(raw) - DOMAIN_KEYS)
    ]
    problems.extend(_member_problems(raw, ""))
    rules = raw.get(RULES_KEY)
    if rules is None:
        return problems
    if not isinstance(rules, list):
        return [*problems, (f"/{RULES_KEY}", "must be a list")]
    seen: set[str] = set()
    for index, rule in enumerate(rules):
        pointer = f"/{RULES_KEY}/{index}"
        if not isinstance(rule, Mapping):
            problems.append((pointer, "must be an object"))
            continue
        problems.extend(
            (f"{pointer}/{key}", "unknown member") for key in sorted(set(rule) - RULE_KEYS)
        )
        prefix = rule.get(PREFIX_KEY)
        if not isinstance(prefix, str) or not prefix:
            problems.append((f"{pointer}/{PREFIX_KEY}", "must be a non-empty string"))
        elif prefix in seen:
            problems.append((f"{pointer}/{PREFIX_KEY}", f"prefix {prefix!r} is given twice"))
        else:
            seen.add(prefix)
        problems.extend(_member_problems(rule, pointer))
    return problems


def _member_problems(raw: Mapping[str, Any], pointer: str) -> list[tuple[str, str]]:
    messages = {
        MODE: f"must be one of {', '.join(MODES)}",
        THRESHOLD: f"must be a number from 0 to {MAX_THRESHOLD}",
        MIN_CONFIDENCE: "must be a number from 0 to 1",
    }
    return [
        (f"{pointer}/{member}", messages[member])
        for member in MEMBERS
        if member in raw and not _valid(member, raw[member])
    ]


def tier_capped(tiers: Sequence[int | None]) -> bool:
    """Whether items of these tiers cap `direct` at `doubts_only`: any item a model produced
    (tier 1 or 2), or whose tier is not recorded."""
    return any(tier is None or tier != 0 for tier in tiers)
