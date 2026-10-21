"""Chat completions with schema-valid JSON over any compatible HTTP endpoint.

One client covers every server and proxy, local or hosted, that speaks the chat completions
protocol (see docs/inference-providers.md for the tested list): `POST {base_url}/chat/completions`
with `Authorization: Bearer <key>` when a key is configured. The model has no tools and
nothing is streamed.

Structured output is requested in decreasing order of strength and the answer is always
validated against the schema locally:

1. `json_schema`: `response_format` of type `json_schema` carrying the schema;
2. `json_mode`: `response_format` of type `json_object`, with the schema in the system prompt;
3. `prompt`: no `response_format`, schema and a strict instruction in the system prompt.

The first mode is chosen by `structured_output`. When the server rejects a mode with HTTP
400 or 422 and an error text that names the response format, the client steps down one mode
and remembers it for the lifetime of the instance. A parse or validation failure triggers up
to `repair_attempts` repair calls that show the model its mistakes; usage of all calls is
summed in the result.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from jsonschema import exceptions as schema_exceptions
from jsonschema import validators

from lorebase_inference.errors import EndpointError, InvalidOutputError
from lorebase_inference.interfaces import ChatOptions, ChatResult, JsonSchema, Message, Usage
from lorebase_inference.settings import LlmSettings
from lorebase_inference.transport import EndpointClient, Sleep

logger = logging.getLogger("lorebase.inference")

MODES = ("json_schema", "json_mode", "prompt")
_UNSUPPORTED_STATUS = frozenset({400, 422})
_FORMAT_HINT = re.compile(
    r"response_format|json_schema|json_object|structured|grammar|schema", re.IGNORECASE
)
_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?```\s*$", re.DOTALL)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
_ECHO_LIMIT = 4000
_MAX_PROBLEMS = 5
_CHARS_PER_TOKEN = 4

_SCHEMA_INSTRUCTION = (
    "Answer with a single JSON value that conforms to the JSON Schema below. "
    "Output only the JSON: no prose, no comments, no code fences.\n\nJSON Schema:\n"
)
_REPAIR_INSTRUCTION = (
    "Your previous answer is not valid: {problems}\n"
    "Answer again with only the corrected JSON value that conforms to the schema."
)


@dataclass(frozen=True, slots=True)
class _Problem:
    path: str
    keyword: str
    message: str


class EndpointChatModel:
    """`ChatModel` for an HTTP chat completions endpoint."""

    def __init__(
        self,
        llm: LlmSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep | None = None,
    ) -> None:
        self._llm = llm
        self._transport = transport
        self._sleep = sleep
        self._mode_index = MODES.index(llm.structured_output)
        self._client = EndpointClient(
            base_url=llm.base_url,
            api_key=llm.api_key,
            timeout_s=llm.timeout_s,
            retry=llm.retry,
            max_concurrency=llm.max_concurrency,
            transport=transport,
            sleep=sleep,
        )
        self._routes: dict[str, EndpointChatModel] = {}

    @property
    def model(self) -> str:
        """Configured model name."""
        return self._llm.model

    @property
    def output_mode(self) -> str:
        """Structured output mode in use; lower than configured after a server rejection."""
        return MODES[self._mode_index]

    async def complete_json(
        self,
        messages: Sequence[Message],
        schema: JsonSchema,
        options: ChatOptions | None = None,
    ) -> ChatResult:
        """Answer `messages` with JSON that conforms to `schema`.

        Raises `InvalidOutputError` when no attempt satisfies the schema, `EndpointError` when
        the endpoint keeps failing, and `ValueError` when `schema` is not a valid JSON Schema.
        """
        options = options or ChatOptions()
        if options.task and options.task in self._llm.tasks:
            return await self._route(options.task).complete_json(messages, schema, options)
        validator = _validator(schema)
        conversation = list(messages)
        usage = Usage()
        problems: list[_Problem] = []
        attempts = self._llm.repair_attempts + 1
        for attempt in range(1, attempts + 1):
            text, call_usage, model, truncated = await self._call(conversation, schema, options)
            usage = Usage(
                usage.input_tokens + call_usage.input_tokens,
                usage.output_tokens + call_usage.output_tokens,
            )
            data, problems = _check(text, validator)
            if not problems:
                return ChatResult(data=data, usage=usage, model=model)
            if truncated:
                break
            if attempt < attempts:
                conversation = [
                    *messages,
                    Message("assistant", text[:_ECHO_LIMIT]),
                    Message("user", _REPAIR_INSTRUCTION.format(problems=_describe(problems))),
                ]
        reason = " (answer cut off by the token limit)" if truncated else ""
        raise InvalidOutputError(
            f"model {self._llm.model!r} did not produce JSON satisfying the schema after "
            f"{attempt} attempt(s){reason}: "
            + "; ".join(f"{p.path or '/'} [{p.keyword}]" for p in problems[:_MAX_PROBLEMS]),
            problems=[f"{p.path or '/'}: {p.keyword}" for p in problems],
            attempts=attempt,
        )

    async def aclose(self) -> None:
        """Release connections, including those of per-task routes."""
        await self._client.aclose()
        for route in self._routes.values():
            await route.aclose()

    def _route(self, task: str) -> EndpointChatModel:
        if task not in self._routes:
            self._routes[task] = EndpointChatModel(
                self._llm.for_task(task), transport=self._transport, sleep=self._sleep
            )
        return self._routes[task]

    async def _call(
        self, conversation: list[Message], schema: JsonSchema, options: ChatOptions
    ) -> tuple[str, Usage, str, bool]:
        """One completion; steps down the structured output mode when the server rejects it."""
        while True:
            payload = self._payload(conversation, schema, options, MODES[self._mode_index])
            try:
                body = await self._client.post("/chat/completions", payload)
            except EndpointError as exc:
                if (
                    exc.status in _UNSUPPORTED_STATUS
                    and self._mode_index < len(MODES) - 1
                    and _FORMAT_HINT.search(exc.detail)
                ):
                    previous = MODES[self._mode_index]
                    self._mode_index += 1
                    logger.info(
                        "endpoint rejected structured output mode %s; using %s",
                        previous,
                        MODES[self._mode_index],
                    )
                    continue
                raise
            return self._read(body, conversation)

    def _payload(
        self, conversation: list[Message], schema: JsonSchema, options: ChatOptions, mode: str
    ) -> dict[str, Any]:
        messages = [{"role": m.role, "content": m.content} for m in conversation]
        payload: dict[str, Any] = {
            "model": self._llm.model,
            "messages": messages,
            "temperature": self._llm.temperature
            if options.temperature is None
            else options.temperature,
            "stream": False,
        }
        max_tokens = options.max_tokens or self._llm.max_tokens
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if options.seed is not None:
            payload["seed"] = options.seed
        if mode == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": dict(schema)},
            }
        else:
            _add_instruction(messages, schema)
            if mode == "json_mode":
                payload["response_format"] = {"type": "json_object"}
        return payload

    def _read(
        self, body: dict[str, Any], conversation: list[Message]
    ) -> tuple[str, Usage, str, bool]:
        choices = body.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else None
        message = choice.get("message") if isinstance(choice, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise EndpointError("/chat/completions: response has no text content")
        reported = body.get("usage")
        raw_usage: dict[str, Any] = reported if isinstance(reported, dict) else {}
        usage = Usage(
            input_tokens=_count(
                raw_usage.get("prompt_tokens"), sum(len(m.content) for m in conversation)
            ),
            output_tokens=_count(raw_usage.get("completion_tokens"), len(content)),
        )
        model = body.get("model")
        truncated = isinstance(choice, dict) and choice.get("finish_reason") == "length"
        return (
            content,
            usage,
            model if isinstance(model, str) and model else self._llm.model,
            truncated,
        )


def _count(reported: object, chars: int) -> int:
    """Reported token count; estimated from the text length when the server omits it."""
    if isinstance(reported, int) and not isinstance(reported, bool) and reported >= 0:
        return reported
    return math.ceil(chars / _CHARS_PER_TOKEN)


def _add_instruction(messages: list[dict[str, Any]], schema: JsonSchema) -> None:
    """Put the schema instruction into the first system message, creating it if needed."""
    instruction = _SCHEMA_INSTRUCTION + json.dumps(schema, ensure_ascii=False)
    if messages and messages[0]["role"] == "system":
        messages[0] = {"role": "system", "content": f"{messages[0]['content']}\n\n{instruction}"}
    else:
        messages.insert(0, {"role": "system", "content": instruction})


def _validator(schema: JsonSchema) -> Any:
    document = dict(schema)
    cls = validators.validator_for(document)
    try:
        cls.check_schema(document)
    except schema_exceptions.SchemaError as exc:
        raise ValueError(f"invalid JSON Schema: {exc.message}") from None
    return cls(document)


def parse_json_text(text: str) -> Any:
    """Parse a model answer as JSON, tolerating reasoning blocks, code fences and prose.

    Raises `ValueError` when no JSON value can be found.
    """
    text = _THINK.sub("", text).strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[{\[]", text):
        try:
            value, _end = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        return value
    raise ValueError("no JSON value found in the answer")


def _check(text: str, validator: Any) -> tuple[Any, list[_Problem]]:
    try:
        data = parse_json_text(text)
    except ValueError as exc:
        return None, [_Problem("", "json", str(exc))]
    errors = sorted(validator.iter_errors(data), key=lambda e: [str(p) for p in e.absolute_path])
    problems = [
        _Problem("/" + "/".join(str(p) for p in e.absolute_path), str(e.validator), e.message)
        for e in errors
    ]
    return data, problems


def _describe(problems: list[_Problem]) -> str:
    return "; ".join(f"{p.path or '/'}: {p.message[:200]}" for p in problems[:_MAX_PROBLEMS])
