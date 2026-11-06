"""`lorebase-herald --help` and `--version` answer before any setting is read; the start line
says which transport and Keeper the calls go to."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest

from lorebase_herald import __main__ as entry
from lorebase_herald import __version__
from lorebase_herald.__main__ import main

VARIABLES = (
    "LOREBASE_URL",
    "LOREBASE_API_KEY",
    "LOREBASE_DOMAIN",
    "LOREBASE_BRANCH",
    "LOREBASE__HERALD__TRANSPORT",
)


@pytest.fixture
def root_logger_restored() -> Iterator[None]:
    """`main` installs the service log handler on the root logger; take it off again."""
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    yield
    for handler in [h for h in root.handlers if h not in before]:
        root.removeHandler(handler)
    root.setLevel(level)


def test_help_names_the_variables_and_the_documentation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["--help"])
    assert stopped.value.code == 0
    out = capsys.readouterr().out
    for name in VARIABLES:
        assert name in out, name
    assert "stdio (default) or streamable_http" in out
    assert "https://aantenore.github.io/lorebase/mcp/" in out


def test_version_prints_the_version_and_exits(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["--version"])
    assert stopped.value.code == 0
    assert capsys.readouterr().out.strip() == f"lorebase-herald {__version__}"


def test_an_unknown_argument_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["--transport", "stdio"])
    assert stopped.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_the_start_line_names_the_transport_and_the_keeper(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    root_logger_restored: None,
) -> None:
    served: list[tuple[str, str]] = []

    async def serve(herald: Any, clients: Any) -> None:
        served.append((herald.transport, herald.keeper.base_url))

    monkeypatch.setattr(entry, "serve", serve)
    monkeypatch.setenv("LOREBASE_URL", "http://keeper.test:8080")
    monkeypatch.setenv("LOREBASE_DOMAIN", "supply")
    caplog.set_level(logging.INFO, logger="lorebase.herald")

    assert main([]) == 0

    assert served == [("stdio", "http://keeper.test:8080")]
    record = next(r for r in caplog.records if r.name == "lorebase.herald")
    assert (record.transport, record.keeper, record.domain) == (  # type: ignore[attr-defined]
        "stdio",
        "http://keeper.test:8080",
        "supply",
    )
    err = capsys.readouterr().err
    assert "stdio transport, Keeper at http://keeper.test:8080, Domain supply" in err
    assert capsys.readouterr().out == ""  # stdout is the protocol's


def test_without_a_domain_the_start_line_says_each_call_names_one(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    root_logger_restored: None,
) -> None:
    async def serve(herald: Any, clients: Any) -> None:
        return None

    monkeypatch.setattr(entry, "serve", serve)
    monkeypatch.setenv("LOREBASE__HERALD__TRANSPORT", "streamable_http")
    caplog.set_level(logging.INFO, logger="lorebase.herald")

    assert main([]) == 0

    record = next(r for r in caplog.records if r.name == "lorebase.herald")
    assert "Domain none (each call names one)" in record.getMessage()
    assert record.endpoint == "http://127.0.0.1:8090/mcp"  # type: ignore[attr-defined]
