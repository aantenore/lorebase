from __future__ import annotations

from typing import Any

import pytest

from lorebase_keeper import __main__ as entrypoint


@pytest.fixture(autouse=True)
def no_global_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entrypoint, "configure_logging", lambda *_a, **_k: None)


def test_main_serves_the_app_on_host_and_port_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served: dict[str, Any] = {}
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda app, **kwargs: served.update(kwargs))
    monkeypatch.setenv("LOREBASE__KEEPER__HOST", "0.0.0.0")
    monkeypatch.setenv("LOREBASE__KEEPER__AUTH__ALLOW_INSECURE", "true")
    monkeypatch.setenv("LOREBASE__KEEPER__PORT", "9191")

    entrypoint.main()

    assert (served["host"], served["port"]) == ("0.0.0.0", 9191)
    assert served["log_config"] is None


def test_main_exits_with_the_configuration_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOREBASE__KEEPER__PORT", "not-a-port")
    with pytest.raises(SystemExit) as caught:
        entrypoint.main()
    assert "keeper.port" in str(caught.value)
