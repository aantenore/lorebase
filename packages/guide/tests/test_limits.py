from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from lorebase_guide.app import create_app
from lorebase_guide.embed import Embedder
from lorebase_guide.limits import Gate, SlidingWindow

from .support import (
    ALL_KINDS,
    DOMAIN,
    FakeKeeper,
    ScriptedChat,
    bearer,
    guide_settings,
    inference_settings,
    open_app,
)

QUESTION = {"domain": DOMAIN, "question": "anything?"}
NOT_FOUND = {"thought": "t", "action": "answer", "answer": "", "citations": [], "found": False}


def test_window_map_is_bounded_and_refuses_new_callers_while_full() -> None:
    now = [0.0]
    window = SlidingWindow(5, lambda: now[0], max_keys=2)
    assert window.take("a") is None
    assert window.take("b") is None
    refusal = window.take("c")
    assert refusal is not None
    assert refusal.reason == "busy"
    assert window.take("a") is None  # known callers go on
    now[0] = 61.0
    assert window.take("c") is None  # idle callers were pruned
    assert len(window) == 1


def test_gate_never_waits() -> None:
    gate = Gate(1)
    assert gate.enter()
    assert not gate.enter()
    gate.leave()
    assert gate.enter()


async def test_unverified_credentials_take_no_slot_and_no_rate(keeper: FakeKeeper) -> None:
    chat = ScriptedChat([NOT_FOUND])
    async with open_app(keeper, chat, limits={"explorations_per_minute": 1}) as http:
        for n in range(5):
            refused = await http.post("/explore", json=QUESTION, headers=bearer(f"lbk_bad_{n}"))
            assert refused.status_code == 401
        accepted = await http.post("/explore", json=QUESTION, headers=bearer(ALL_KINDS))
    assert accepted.status_code == 200


async def test_limits_follow_the_principal_not_the_credential(
    keeper: FakeKeeper, monkeypatch: pytest.MonkeyPatch
) -> None:
    keeper.grants["lbk_test_second_key"] = keeper.grants[ALL_KINDS]
    monkeypatch.setattr(keeper, "_principal", lambda request: "same-person")
    chat = ScriptedChat([NOT_FOUND, NOT_FOUND])
    async with open_app(keeper, chat, limits={"explorations_per_minute": 1}) as http:
        first = await http.post("/explore", json=QUESTION, headers=bearer(ALL_KINDS))
        second = await http.post("/explore", json=QUESTION, headers=bearer("lbk_test_second_key"))
    assert first.status_code == 200
    assert second.status_code == 429


async def test_client_disconnect_releases_the_slot(keeper: FakeKeeper) -> None:
    app = create_app(
        guide_settings(limits={"max_concurrent": 1}),
        inference_settings(),
        chat=lambda: ScriptedChat([NOT_FOUND, NOT_FOUND]),
        embedder=Embedder(inference_settings()),
        keeper_transport=keeper.transport(),
    )
    runtime = app.state.runtime
    body = b'{"domain": "supply", "question": "anything?"}'
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/explore",
        "raw_path": b"/explore",
        "query_string": b"",
        "root_path": "",
        "server": ("guide.test", 80),
        "client": ("127.0.0.1", 1234),
        "headers": [
            (b"host", b"guide.test"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"authorization", f"Bearer {ALL_KINDS}".encode()),
        ],
    }
    sent: list[str] = []
    requested = asyncio.Event()

    async def receive() -> dict[str, Any]:
        if not requested.is_set():
            requested.set()
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()  # the peer is gone: nothing arrives any more
        raise AssertionError("not reached")

    async def send(message: dict[str, Any]) -> None:
        sent.append(message["type"])
        if message["type"] == "http.response.start":
            raise OSError("connection reset by peer")

    with pytest.raises(OSError, match="connection reset"):
        await app(scope, receive, send)
    assert sent == ["http.response.start"]
    assert runtime.slots.running == 0
    assert runtime.verifying.held == 0

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://guide.test") as http:
        again = await http.post("/explore", json=QUESTION, headers=bearer(ALL_KINDS))
    assert again.status_code == 200


async def test_malformed_session_cookie_is_refused(keeper: FakeKeeper) -> None:
    async with open_app(keeper, ScriptedChat([])) as http:
        response = await http.post(
            "/explore",
            json=QUESTION,
            headers={"Cookie": "lorebase_session=short", "X-CSRF-Token": "t"},
        )
    assert response.status_code == 401
    assert keeper.requests == []


async def test_notes_can_be_switched_off(keeper: FakeKeeper) -> None:
    chat = ScriptedChat([NOT_FOUND])
    async with open_app(keeper, chat, trace={"notes": False}) as http:
        response = await http.post("/explore", json=QUESTION, headers=bearer(ALL_KINDS))
    assert response.status_code == 200
    assert keeper.notes == []
    assert all(r.headers.get("x-lorebase-session") for r in keeper.requests)


async def test_dot_segments_in_refs_never_reach_keeper(keeper: FakeKeeper) -> None:
    chat = ScriptedChat(
        [{"thought": "t", "action": "get", "args": {"ref": "Supplier/.."}}, NOT_FOUND]
    )
    async with open_app(keeper, chat) as http:
        response = await http.post("/explore", json=QUESTION, headers=bearer(ALL_KINDS))
    assert '"summary": "invalid arguments"' in response.text
    assert not any("/kinds/" in r.url.path for r in keeper.requests)
