from __future__ import annotations

import json
import os
from typing import Any

import httpx
import pytest

from lorebase_inference.chat import reset_known_modes
from lorebase_inference.settings import ENV_ALIASES
from lorebase_platform import settings as platform_settings


@pytest.fixture(autouse=True)
def clean_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test without ambient LOREBASE__*, LOREBASE_CONFIG or LLM_* variables."""
    for name in list(os.environ):
        if (
            name.startswith(platform_settings.ENV_PREFIX)
            or name == platform_settings.CONFIG_ENV_VAR
            or name in ENV_ALIASES
        ):
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def fresh_output_modes() -> None:
    """Start every test without structured output modes learned by earlier tests."""
    reset_known_modes()


class ScriptedEndpoint:
    """Stands in for an HTTP model server: answers requests from a script, records them.

    Each scripted item is an `httpx.Response`, an exception to raise, or a callable taking the
    request. The last item repeats when the script runs out.
    """

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.script[min(len(self.requests), len(self.script)) - 1]
        if isinstance(item, Exception):
            raise item
        return item(request) if callable(item) else item

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]

    @staticmethod
    def completion(
        content: str,
        *,
        prompt: int | None = 5,
        output: int | None = 3,
        model: str | None = "served-model",
        finish: str = "stop",
    ) -> httpx.Response:
        """A chat completions answer carrying `content`."""
        body: dict[str, Any] = {
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish,
                }
            ]
        }
        if model:
            body["model"] = model
        if prompt is not None and output is not None:
            body["usage"] = {"prompt_tokens": prompt, "completion_tokens": output}
        return httpx.Response(200, json=body)

    @staticmethod
    def api_error(status: int, message: str, **headers: str) -> httpx.Response:
        """An error answer in the usual `{"error": {"message": ...}}` shape."""
        return httpx.Response(status, json={"error": {"message": message}}, headers=headers)


@pytest.fixture
def scripted() -> type[ScriptedEndpoint]:
    return ScriptedEndpoint


@pytest.fixture
def client_options(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Keyword arguments of every `httpx.AsyncClient` built while the test runs."""
    built: list[dict[str, Any]] = []
    real = httpx.AsyncClient

    def recording(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        built.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", recording)
    return built
