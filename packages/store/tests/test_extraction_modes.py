"""Resolution of the ingestion mode of extraction (design 9.2 "Ingestion modes")."""

from __future__ import annotations

from typing import Any

from lorebase_store.extraction.modes import (
    Ingestion,
    Installation,
    combine,
    ignored_settings,
    resolve,
    setting_problems,
    tier_capped,
)

DEFAULT = Installation()


def domain(**extraction: Any) -> dict[str, Any]:
    return {"extraction": extraction}


def test_no_setting_is_the_installation() -> None:
    assert resolve(DEFAULT, {}).ingestion == Ingestion()
    assert resolve(DEFAULT, None, "any/key").ignored == ()
    assert Ingestion().accept_threshold == 0.9
    assert Ingestion(mode="review_all").accept_threshold > 1


def test_tightening_always_applies() -> None:
    found = resolve(
        Installation(domain_may_loosen=frozenset()),
        domain(mode="review_all", auto_accept_threshold=0.95, min_confidence=0.1),
    )
    assert found.ingestion == Ingestion("review_all", 0.95, 0.1)
    assert found.ignored == ()


def test_loosening_needs_the_installation() -> None:
    loose = domain(mode="direct", auto_accept_threshold=0.5, min_confidence=0.6)
    found = resolve(DEFAULT, loose)
    assert found.ingestion == Ingestion("direct", 0.9, 0.3)
    assert found.ignored == ("extraction.auto_accept_threshold", "extraction.min_confidence")
    none = resolve(Installation(domain_may_loosen=frozenset()), loose)
    assert none.ingestion == Ingestion()
    everything = Installation(
        domain_may_loosen=frozenset({"mode", "auto_accept_threshold", "min_confidence"})
    )
    assert resolve(everything, loose).ingestion == Ingestion("direct", 0.5, 0.6)


def test_rules_take_the_longest_prefix_and_fall_back_to_the_domain() -> None:
    settings = domain(
        auto_accept_threshold=0.95,
        rules=[
            {"source_key_prefix": "feeds/", "mode": "direct"},
            {"source_key_prefix": "feeds/manual/", "mode": "review_all"},
            {"source_key_prefix": "feeds/loose/", "auto_accept_threshold": 0.1},
        ],
    )
    assert resolve(DEFAULT, settings, "feeds/x.csv").ingestion == Ingestion("direct", 0.95, 0.3)
    assert resolve(DEFAULT, settings, "feeds/manual/a").ingestion.mode == "review_all"
    loose = resolve(DEFAULT, settings, "feeds/loose/a")
    assert loose.ingestion.auto_accept_threshold == 0.95
    assert loose.ignored == ("extraction.rules[2].auto_accept_threshold",)
    assert resolve(DEFAULT, settings, "other").ingestion.mode == "doubts_only"
    assert resolve(DEFAULT, settings).ingestion.mode == "doubts_only"


def test_a_job_runs_at_the_strictest() -> None:
    combined = combine(
        [Ingestion("direct", 0.5, 0.2), Ingestion("review_all", 0.9, 0.3), Ingestion()]
    )
    assert combined == Ingestion("review_all", 0.9, 0.2)
    assert combine([]) == Ingestion()


def test_ignored_settings_lists_every_value_that_does_not_apply() -> None:
    settings = domain(
        mode="sideways",
        auto_accept_threshold=0.5,
        extra=1,
        rules=[{"mode": "direct"}, {"source_key_prefix": "a/", "min_confidence": 0.9, "x": 1}],
    )
    assert ignored_settings(DEFAULT, settings) == [
        "extraction.extra",
        "extraction.mode",
        "extraction.auto_accept_threshold",
        "extraction.rules[0]",
        "extraction.rules[1].x",
        "extraction.rules[1].min_confidence",
    ]
    assert ignored_settings(DEFAULT, {}) == []
    assert ignored_settings(DEFAULT, {"extraction": 3}) == ["extraction"]


def test_setting_problems_are_structural() -> None:
    assert setting_problems({"mode": "direct", "auto_accept_threshold": 0.2}) == []
    assert setting_problems(None) == []
    assert setting_problems([]) == [("", "must be an object")]
    found = setting_problems(
        {
            "mode": "fast",
            "min_confidence": True,
            "other": 1,
            "rules": [
                {"source_key_prefix": "a/"},
                {"source_key_prefix": "a/", "auto_accept_threshold": 2},
                {"mode": "direct"},
                "x",
            ],
        }
    )
    assert [pointer for pointer, _ in found] == [
        "/other",
        "/mode",
        "/min_confidence",
        "/rules/1/source_key_prefix",
        "/rules/1/auto_accept_threshold",
        "/rules/2/source_key_prefix",
        "/rules/3",
    ]


def test_installation_bounds_read_leniently() -> None:
    bounds = Installation.from_section(
        {"mode": "direct", "auto_accept_threshold": 0.8, "domain_may_loosen": []}
    )
    assert bounds.base == Ingestion("doubts_only", 0.8, 0.3)
    assert bounds.domain_may_loosen == frozenset()
    assert Installation.from_section(None) == Installation()


def test_any_model_tier_caps_direct() -> None:
    assert tier_capped([0, 0]) is False
    assert tier_capped([0, 1]) is True
    assert tier_capped([2]) is True
    assert tier_capped([None]) is True
    assert tier_capped([]) is False
