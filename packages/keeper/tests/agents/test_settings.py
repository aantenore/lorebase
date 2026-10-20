from __future__ import annotations

from pathlib import Path

import pytest

from lorebase_keeper.agents.budget import DEFAULT_TRIM_ORDER, TrimStep
from lorebase_keeper.agents.settings import AgentSettings, agent_settings
from lorebase_platform.settings import CONFIG_ENV_VAR, ConfigError, Settings, load_settings

EXAMPLE = Path(__file__).resolve().parents[4] / "config" / "lorebase.example.yaml"


def test_example_file_matches_code_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONFIG_ENV_VAR, str(EXAMPLE))
    assert agent_settings(load_settings()) == AgentSettings()


def test_default_trim_order_lists_every_step() -> None:
    assert AgentSettings().context.trim_order == DEFAULT_TRIM_ORDER
    assert set(DEFAULT_TRIM_ORDER) == set(TrimStep)


@pytest.mark.parametrize(
    ("values", "needle"),
    [
        ({"context": {"default_budget_tokens": 9, "max_budget_tokens": 8}}, "agents.context"),
        ({"context": {"chars_per_token": 0}}, "agents.context.chars_per_token"),
        ({"context": {"trim_order": ["schema", "schema"]}}, "agents.context"),
        ({"context": {"trim_order": ["everything"]}}, "agents.context.trim_order"),
    ],
)
def test_invalid_values_name_the_key(values: dict[str, object], needle: str) -> None:
    with pytest.raises(ConfigError) as caught:
        agent_settings(Settings(agents=values))
    assert needle in str(caught.value)
