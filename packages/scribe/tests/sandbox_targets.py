"""Parser stand-ins run in the sandbox child by `test_parser_sandbox.py`: a document that never
finishes, one that spins the CPU, one that allocates without bound, one whose text is huge, one
that reaches for the network and one that kills its process."""

from __future__ import annotations

import os
import signal
import socket
import time

from lorebase_scribe.extraction.parsing import ParsedDocument

GIB = 1024 * 1024 * 1024


def sleeps(data: bytes, media_type: str) -> ParsedDocument:
    time.sleep(600)
    return ParsedDocument("", media_type)


def spins(data: bytes, media_type: str) -> ParsedDocument:
    while True:
        pass


def allocates(data: bytes, media_type: str) -> ParsedDocument:
    hoard = [bytearray(GIB) for _ in range(8)]
    return ParsedDocument(str(len(hoard)), media_type)


def huge_text(data: bytes, media_type: str) -> ParsedDocument:
    return ParsedDocument("x" * 5_000_000, media_type, pages=(0,))


def connects(data: bytes, media_type: str) -> ParsedDocument:
    outcomes = []
    for attempt in (
        lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM),
        lambda: socket.create_connection(("127.0.0.1", 9), timeout=1),
        lambda: socket.getaddrinfo("example.org", 443),
    ):
        try:
            attempt()
            outcomes.append("open")
        except OSError:
            outcomes.append("refused")
    proxies = sorted(name for name in os.environ if "proxy" in name.lower())
    return ParsedDocument(" ".join(outcomes + proxies), media_type)


def dies(data: bytes, media_type: str) -> ParsedDocument:
    os.kill(os.getpid(), signal.SIGKILL)
    return ParsedDocument("", media_type)
