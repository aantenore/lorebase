"""The shared fixture blocks outbound connections in unit tests."""

from __future__ import annotations

import socket

import pytest


def test_tcp_connection_is_refused() -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(RuntimeError, match="network access is disabled"),
    ):
        sock.connect(("127.0.0.1", 9))


def test_name_resolution_is_refused() -> None:
    with pytest.raises(RuntimeError, match="network access is disabled"):
        socket.getaddrinfo("example.org", 443)


def test_local_socket_pair_still_works() -> None:
    left, right = socket.socketpair()
    with left, right:
        left.sendall(b"x")
        assert right.recv(1) == b"x"
