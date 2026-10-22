"""The `scribe` settings section."""

from __future__ import annotations

from pathlib import Path

import pytest

from lorebase_platform.settings import CONFIG_ENV_VAR, ConfigError, Settings, load_settings
from lorebase_scribe.settings import ScribeSettings, WorkerSettings, scribe_settings
from lorebase_scribe.worker import Worker
from lorebase_store.settings import JobSettings

EXAMPLE = Path(__file__).resolve().parents[3] / "config" / "lorebase.example.yaml"


def test_defaults() -> None:
    scribe = scribe_settings(Settings())
    assert scribe.worker.concurrency == 2
    assert scribe.scheduler.gc_schedule == "0 3 * * *"
    assert scribe.sources.commit_batch == 5000
    assert scribe.sources.csv.allowed_roots == ()


def test_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOREBASE__SCRIBE__WORKER__CONCURRENCY", "4")
    monkeypatch.setenv("LOREBASE__SCRIBE__SOURCES__CSV__ALLOWED_ROOTS", '["/data/supply"]')
    scribe = scribe_settings(load_settings())
    assert scribe.worker.concurrency == 4
    assert scribe.sources.csv.allowed_roots == ("/data/supply",)


def test_invalid_gc_schedule_is_refused() -> None:
    with pytest.raises(ValueError, match="5 fields"):
        ScribeSettings.model_validate({"scheduler": {"gc_schedule": "daily"}})


def test_heartbeat_must_exceed_the_poll_interval() -> None:
    with pytest.raises(ValueError, match="heartbeat"):
        ScribeSettings.model_validate({"worker": {"heartbeat_interval_s": 1, "poll_interval_s": 2}})


def test_unknown_keys_fail_at_load() -> None:
    with pytest.raises(ConfigError):
        scribe_settings(Settings(scribe={"worker": {"threads": 3}}))


def test_example_file_matches_code_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    defaults = scribe_settings(load_settings())
    monkeypatch.setenv(CONFIG_ENV_VAR, str(EXAMPLE))
    assert scribe_settings(load_settings()) == defaults


def test_heartbeat_must_fit_twice_in_the_stale_threshold() -> None:
    async def handler(job: object) -> dict[str, object]:
        return {}

    with pytest.raises(ValueError, match="at most half"):
        Worker(
            None,  # type: ignore[arg-type]
            {"test": handler},  # type: ignore[dict-item]
            settings=WorkerSettings(heartbeat_interval_s=60),
            jobs=JobSettings(stale_after_seconds=90),
        )
    defaults = Worker(None, {"test": handler}, settings=WorkerSettings(), jobs=JobSettings())  # type: ignore[arg-type, dict-item]
    assert defaults.types == ("test",)
