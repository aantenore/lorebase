"""The exploration loop: model turn, read-only tool call, repeat; bounded on every axis.

Budgets (from `guide.agent`, lowered per request, never raised):

- `max_steps` tool calls; when they are used up the model gets one last turn to answer;
- `max_tokens_per_call` completion tokens per model call;
- `max_total_tokens` input plus output tokens over the whole exploration; before each call the
  input is estimated from the text length, and the call is only made when that estimate fits in
  what remains, with a completion cap of at most what is left after it;
- `timeout_s` wall-clock time from the start, covering model calls and Keeper reads alike.

The loop yields `Event`s that the HTTP layer streams as server-sent events. Logs and metrics
carry counts, tool names and outcomes only: never the question, data or answers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import secrets
import time
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass, field
from typing import Any

from lorebase_client import AsyncLorebaseClient
from lorebase_client.errors import LorebaseError
from lorebase_guide import prompts
from lorebase_guide.embed import Embedder
from lorebase_guide.keeper import tool_scope
from lorebase_guide.settings import AgentSettings
from lorebase_guide.telemetry import STEPS, TOKENS, tracer
from lorebase_guide.tools import TOOLS, ToolContext, ToolResult, run_tool
from lorebase_guide.trace import TraceSession
from lorebase_inference import ChatModel, ChatOptions, InferenceError, Message

logger = logging.getLogger("lorebase.guide.agent")

_CHARS_PER_TOKEN = 4  # input estimate before a call; same ratio as the inference package


@dataclass(frozen=True)
class Event:
    """One server-sent event: `name` and a JSON object."""

    name: str
    data: dict[str, Any]


@dataclass(frozen=True)
class Budget:
    max_steps: int
    max_total_tokens: int
    max_tokens_per_call: int
    timeout_s: float

    @classmethod
    def of(cls, settings: AgentSettings, *, max_steps: int | None, budget: int | None) -> Budget:
        return cls(
            max_steps=min(max_steps or settings.max_steps, settings.max_steps),
            max_total_tokens=min(budget or settings.max_total_tokens, settings.max_total_tokens),
            max_tokens_per_call=settings.max_tokens_per_call,
            timeout_s=settings.timeout_s,
        )


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class Exploration:
    """Outcome counters, read by the HTTP layer for logs and metrics."""

    steps: int = 0
    outcome: str = "running"
    usage: _Usage = field(default_factory=_Usage)


class _StopError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Explorer:
    """Runs explorations with one chat model; the Keeper client is per exploration."""

    def __init__(self, chat: ChatModel, settings: AgentSettings, embedder: Embedder | None) -> None:
        self._chat = chat
        self._settings = settings
        self._embedder = embedder

    async def run(
        self,
        *,
        client: AsyncLorebaseClient,
        trace: TraceSession,
        domain: str,
        branch: str | None,
        question: str,
        budget: Budget,
        state: Exploration | None = None,
    ) -> AsyncIterator[Event]:
        """Events of one exploration, ending with `answer` (or `error`) and `done`."""
        state = state or Exploration()
        started = time.monotonic()
        deadline = asyncio.get_running_loop().time() + budget.timeout_s
        marker = secrets.token_hex(12)
        ctx = ToolContext(
            client=client,
            domain=domain,
            branch=branch,
            settings=self._settings,
            embedder=self._embedder,
        )
        messages = [
            Message("system", prompts.system_prompt(marker)),
            Message("user", prompts.question_prompt(domain, branch, question.replace(marker, ""))),
        ]
        yield Event(
            "session",
            {
                "session_id": trace.session_id,
                "domain": domain,
                "branch": branch,
                "model": getattr(self._chat, "model", None),
                "max_steps": budget.max_steps,
            },
        )
        final: dict[str, Any] | None = None
        try:
            while final is None:
                last_turn = state.steps >= budget.max_steps
                if last_turn:
                    messages.append(Message("user", prompts.FINAL_CALL))
                turn = await self._turn(messages, budget, state, deadline)
                thought = str(turn.get("thought") or "").strip()
                if thought:
                    yield Event("thought", {"step": state.steps, "text": thought})
                    await _bounded(trace.note(client, thought), deadline)
                action = str(turn.get("action") or "")
                if action == prompts.ANSWER or last_turn:
                    final = self._final(turn if action == prompts.ANSWER else {}, ctx)
                    break
                state.steps += 1
                raw_args = turn.get("args")
                args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
                yield Event("tool_call", {"step": state.steps, "tool": action, "args": args})
                result = await self._tool(ctx, trace, action, args, deadline)
                yield Event(
                    "tool_result",
                    {
                        "step": state.steps,
                        "tool": action,
                        "ok": result.ok,
                        "summary": result.summary,
                        "refs": result.refs[: self._settings.max_items],
                    },
                )
                messages.append(Message("assistant", json.dumps(turn, ensure_ascii=False)))
                messages.append(
                    Message(
                        "user",
                        prompts.data_block(
                            marker,
                            action,
                            result.ok,
                            result.data,
                            self._settings.max_result_chars,
                        ),
                    )
                )
        except _StopError as stop:
            state.outcome = stop.code
            yield Event("error", {"code": stop.code, "message": stop.message})
            final = {"text": prompts.NOT_FOUND, "citations": [], "found": False}
        else:
            state.outcome = "answered" if final["found"] else "not_found"
        yield Event("answer", final)
        yield Event(
            "done",
            {
                "session_id": trace.session_id,
                "steps": state.steps,
                "outcome": state.outcome,
                "tokens": {
                    "input": state.usage.input_tokens,
                    "output": state.usage.output_tokens,
                },
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            },
        )

    async def _turn(
        self, messages: list[Message], budget: Budget, state: Exploration, deadline: float
    ) -> dict[str, Any]:
        schema = prompts.turn_schema()
        chars = len(json.dumps(schema)) + sum(len(m.content) for m in messages)
        remaining = (
            budget.max_total_tokens - state.usage.total - math.ceil(chars / _CHARS_PER_TOKEN)
        )
        if remaining <= 0:
            raise _StopError("budget", "the token budget of this exploration is used up")
        options = ChatOptions(
            task=self._settings.task,
            temperature=self._settings.temperature,
            max_tokens=min(budget.max_tokens_per_call, remaining),
        )
        try:
            with tracer.start_as_current_span("guide.model"):
                result = await _bounded(
                    self._chat.complete_json(messages, schema, options), deadline
                )
        except InferenceError as exc:
            logger.warning("model call failed", extra={"error": type(exc).__name__})
            raise _StopError("model", "the model could not produce a valid step") from None
        state.usage.input_tokens += result.usage.input_tokens
        state.usage.output_tokens += result.usage.output_tokens
        TOKENS.add(result.usage.input_tokens, {"direction": "input"})
        TOKENS.add(result.usage.output_tokens, {"direction": "output"})
        if state.usage.total > budget.max_total_tokens:
            raise _StopError("budget", "the token budget of this exploration is used up")
        data = result.data
        return data if isinstance(data, dict) else {}

    async def _tool(
        self,
        ctx: ToolContext,
        trace: TraceSession,
        action: str,
        args: dict[str, Any],
        deadline: float,
    ) -> ToolResult:
        with (
            tracer.start_as_current_span("guide.tool") as span,
            tool_scope(trace.tool_headers(action)),
        ):
            span.set_attribute("guide.tool", action)
            try:
                result = await _bounded(run_tool(ctx, action, args), deadline)
            except LorebaseError as exc:
                result = ToolResult(False, f"Keeper error {exc.status}", {"error": "keeper"})
        STEPS.add(1, {"tool": action if action in TOOLS else "unknown", "ok": result.ok})
        return result

    def _final(self, turn: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        """The answer, citing only Records a tool returned; without such a citation, or when
        the model says so, the answer is that the graph does not contain it."""
        raw = turn.get("citations")
        cited: list[Any] = raw if isinstance(raw, list) else []
        citations: list[dict[str, str]] = []
        for ref in dict.fromkeys(str(c) for c in cited):
            if ref in ctx.seen:
                kind, _, key = ref.partition("/")
                citations.append({"ref": ref, "kind": kind, "key": key})
        text = str(turn.get("answer") or "").strip()[: self._settings.max_answer_chars]
        found = bool(turn.get("found", True)) and bool(citations) and bool(text)
        if not found:
            return {"text": prompts.NOT_FOUND, "citations": [], "found": False}
        return {"text": text, "citations": citations, "found": True}


async def _bounded[T](awaitable: Awaitable[T], deadline: float) -> T:
    try:
        async with asyncio.timeout_at(deadline):
            return await awaitable
    except TimeoutError:
        raise _StopError("timeout", "the exploration took longer than allowed") from None
