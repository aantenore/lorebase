"""Fixtures of the CLI tests: configuration never leaks in from the machine running them."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No LOREBASE_* variable, and a home directory without a config file."""
    import os

    for name in [n for n in os.environ if n.startswith("LOREBASE_")]:
        monkeypatch.delenv(name)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return home
