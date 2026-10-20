"""Shared pytest configuration: tests never reach the network.

Unit tests fail fast on any outbound socket connection. Tests marked `integration` may reach
services started for them (for example Postgres in a container) and are exempt.
"""

from __future__ import annotations

import socket
from typing import Any, NoReturn

import pytest

# Fixtures that run a real Keeper on Postgres (client and CLI integration tests).
pytest_plugins = ["keeper_harness"]


class NetworkAccessError(RuntimeError):
    """Raised when a test tries to open a network connection."""


def _deny(*_args: Any, **_kwargs: Any) -> NoReturn:
    raise NetworkAccessError("network access is disabled in tests")


@pytest.fixture(autouse=True)
def _block_network(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    if request.node.get_closest_marker("integration") is None:
        original_connect = socket.socket.connect

        def guarded_connect(self: socket.socket, address: Any) -> None:
            if self.family in (socket.AF_INET, socket.AF_INET6):
                _deny()
            original_connect(self, address)

        monkeypatch.setattr(socket.socket, "connect", guarded_connect)
        monkeypatch.setattr(socket.socket, "connect_ex", _deny)
        monkeypatch.setattr(socket, "getaddrinfo", _deny)

