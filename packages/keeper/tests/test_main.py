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

    entrypoint.main([])

    assert (served["host"], served["port"]) == ("0.0.0.0", 9191)
    assert served["log_config"] is None
    assert served["proxy_headers"] is False


def test_main_exits_with_the_configuration_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOREBASE__KEEPER__PORT", "not-a-port")
    with pytest.raises(SystemExit) as caught:
        entrypoint.main([])
    assert "keeper.port" in str(caught.value)


def test_bootstrap_exits_with_the_reason_when_api_keys_are_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOREBASE__KEEPER__AUTH__MODE", "none")
    with pytest.raises(SystemExit) as caught:
        entrypoint.main(["bootstrap"])
    assert "API keys are not accepted" in str(caught.value)


@pytest.mark.parametrize(
    ("maintenance", "expected"),
    [
        (None, None),
        ("postgresql://owner:pw@db:5432/lorebase", "postgresql://owner:pw@db:5432/lorebase"),
    ],
)
def test_installation_commands_connect_as_the_maintenance_role_when_set(
    monkeypatch: pytest.MonkeyPatch, maintenance: str | None, expected: str | None
) -> None:
    monkeypatch.setenv("LOREBASE__DATABASE__DSN", "postgresql://app:pw@db:5432/lorebase")
    if maintenance is not None:
        monkeypatch.setenv("LOREBASE__DATABASE__MAINTENANCE_DSN", maintenance)
    else:
        monkeypatch.delenv("LOREBASE__DATABASE__MAINTENANCE_DSN", raising=False)
        monkeypatch.delenv("LOREBASE_DB_MAINTENANCE_DSN", raising=False)
    used: dict[str, Any] = {}

    class _Engine:
        async def dispose(self) -> None:
            return None

    def fake_engine(database: Any, *, url: str | None = None, pooled: bool = True) -> _Engine:
        used.update(url=url, pooled=pooled)
        return _Engine()

    monkeypatch.setattr(entrypoint, "create_engine", fake_engine)
    entrypoint._store(entrypoint.load_settings())
    assert used == {"url": expected, "pooled": False}
