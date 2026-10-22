from __future__ import annotations

import json
import logging

import httpx
import pytest

from lorebase_inference import (
    ChatModel,
    ChatOptions,
    EndpointError,
    InferenceSettings,
    InvalidOutputError,
    Message,
    create_chat_model,
)
from lorebase_inference.chat import EndpointChatModel, parse_json_text, reset_known_modes
from lorebase_inference.settings import LlmSettings, LlmTaskOverride, RetrySettings

SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}, "founded": {"type": "integer"}},
    "required": ["city", "founded"],
    "additionalProperties": False,
}
MESSAGES = [Message("system", "Extract facts."), Message("user", "Rome was founded in 753.")]
GOOD = json.dumps({"city": "Rome", "founded": 753})


def make_model(endpoint, **overrides) -> EndpointChatModel:
    llm = LlmSettings(
        base_url="http://llm.test/v1",
        model="small",
        retry=RetrySettings(max_retries=2, backoff_base_s=0.1, backoff_max_s=1),
        **overrides,
    )
    return EndpointChatModel(llm, transport=endpoint.transport, sleep=endpoint.sleep)


async def test_json_schema_response_format_when_the_server_supports_it(scripted) -> None:
    endpoint = scripted(scripted.completion(GOOD, prompt=40, output=9))
    model = make_model(endpoint, api_key="sk-secret")

    result = await model.complete_json(MESSAGES, SCHEMA, ChatOptions(seed=7, max_tokens=50))

    assert isinstance(model, ChatModel)
    assert result.data == {"city": "Rome", "founded": 753}
    assert (result.usage.input_tokens, result.usage.output_tokens) == (40, 9)
    assert result.model == "served-model"
    body = endpoint.bodies()[0]
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "answer", "schema": SCHEMA},
    }
    assert body["model"] == "small"
    assert body["messages"] == [
        {"role": "system", "content": "Extract facts."},
        {"role": "user", "content": "Rome was founded in 753."},
    ]
    assert (body["temperature"], body["seed"], body["max_tokens"], body["stream"]) == (
        0,
        7,
        50,
        False,
    )
    assert "tools" not in body
    assert endpoint.requests[0].headers["authorization"] == "Bearer sk-secret"
    assert str(endpoint.requests[0].url) == "http://llm.test/v1/chat/completions"
    assert model.output_mode == "json_schema"


async def test_server_without_json_schema_steps_down_to_json_mode(scripted) -> None:
    endpoint = scripted(
        scripted.api_error(400, "'response_format.type' must be 'json_object' or 'text'"),
        scripted.completion(GOOD),
    )
    model = make_model(endpoint)

    result = await model.complete_json(MESSAGES, SCHEMA)

    assert result.data["city"] == "Rome"
    first, second = endpoint.bodies()
    assert first["response_format"]["type"] == "json_schema"
    assert second["response_format"] == {"type": "json_object"}
    assert "JSON Schema" in second["messages"][0]["content"]
    assert second["messages"][0]["content"].startswith("Extract facts.")
    assert len(second["messages"]) == 2
    assert model.output_mode == "json_mode"

    await model.complete_json(MESSAGES, SCHEMA)
    assert len(endpoint.requests) == 3, "the stepped-down mode is remembered"


async def test_stepped_down_mode_is_shared_by_later_clients(scripted) -> None:
    endpoint = scripted(
        scripted.api_error(400, "unsupported response_format json_schema"),
        scripted.completion(GOOD),
    )
    await make_model(endpoint).complete_json(MESSAGES, SCHEMA)
    assert len(endpoint.requests) == 2

    later = make_model(endpoint)
    assert later.output_mode == "json_mode"
    await later.complete_json(MESSAGES, SCHEMA)
    assert len(endpoint.requests) == 3
    assert endpoint.bodies()[2]["response_format"] == {"type": "json_object"}


async def test_known_mode_is_per_endpoint_model_and_strictness(scripted) -> None:
    endpoint = scripted(
        scripted.api_error(400, "unsupported response_format"), scripted.completion(GOOD)
    )
    await make_model(endpoint).complete_json(MESSAGES, SCHEMA)

    other = LlmSettings(base_url="http://llm.test/v1", model="other")
    assert EndpointChatModel(other).output_mode == "json_schema"
    elsewhere = LlmSettings(base_url="http://other.test/v1", model="small")
    assert EndpointChatModel(elsewhere).output_mode == "json_schema"
    assert make_model(endpoint, json_schema_strict=False).output_mode == "json_schema"


async def test_reset_forgets_the_known_mode(scripted) -> None:
    endpoint = scripted(
        scripted.api_error(400, "unsupported response_format"), scripted.completion(GOOD)
    )
    await make_model(endpoint).complete_json(MESSAGES, SCHEMA)
    reset_known_modes()
    assert make_model(endpoint).output_mode == "json_schema"


async def test_server_without_any_response_format_falls_back_to_prompt_only(scripted) -> None:
    endpoint = scripted(
        scripted.api_error(400, "unknown field response_format"),
        scripted.api_error(400, "unknown field response_format"),
        scripted.completion("```json\n" + GOOD + "\n```"),
    )
    model = make_model(endpoint)

    result = await model.complete_json([Message("user", "Rome, 753")], SCHEMA)

    assert result.data == {"city": "Rome", "founded": 753}
    bodies = endpoint.bodies()
    assert "response_format" not in bodies[2]
    assert bodies[2]["messages"][0]["role"] == "system"
    assert json.dumps(SCHEMA) in bodies[2]["messages"][0]["content"]
    assert model.output_mode == "prompt"


async def test_configured_json_mode_never_sends_a_schema_format(scripted) -> None:
    endpoint = scripted(scripted.completion(GOOD))
    model = make_model(endpoint, structured_output="json_mode")
    await model.complete_json(MESSAGES, SCHEMA)
    assert endpoint.bodies()[0]["response_format"] == {"type": "json_object"}
    assert len(endpoint.requests) == 1


async def test_unrelated_bad_request_is_not_mistaken_for_missing_support(scripted) -> None:
    endpoint = scripted(scripted.api_error(400, "context length exceeded"))
    model = make_model(endpoint)
    with pytest.raises(EndpointError, match="context length"):
        await model.complete_json(MESSAGES, SCHEMA)
    assert model.output_mode == "json_schema"
    assert len(endpoint.requests) == 1


async def test_429_then_success_is_transparent(scripted) -> None:
    endpoint = scripted(
        scripted.api_error(429, "rate limited", **{"Retry-After": "0.5"}),
        scripted.completion(GOOD),
    )
    result = await make_model(endpoint).complete_json(MESSAGES, SCHEMA)
    assert result.data["founded"] == 753
    assert endpoint.sleeps == [0.5]


async def test_malformed_json_is_repaired_once_and_usage_is_summed(scripted) -> None:
    endpoint = scripted(
        scripted.completion('{"city": "Rome", "founded": ', prompt=30, output=5),
        scripted.completion(GOOD, prompt=60, output=8),
    )
    result = await make_model(endpoint).complete_json(MESSAGES, SCHEMA)

    assert result.data == {"city": "Rome", "founded": 753}
    assert (result.usage.input_tokens, result.usage.output_tokens) == (90, 13)
    repair = endpoint.bodies()[1]["messages"]
    assert repair[:2] == [
        {"role": "system", "content": "Extract facts."},
        {"role": "user", "content": "Rome was founded in 753."},
    ]
    assert repair[2]["role"] == "assistant"
    assert repair[3]["role"] == "user"
    assert "not valid" in repair[3]["content"]


async def test_schema_violation_is_repaired(scripted) -> None:
    endpoint = scripted(
        scripted.completion(json.dumps({"city": "Rome", "founded": "753"})),
        scripted.completion(GOOD),
    )
    result = await make_model(endpoint).complete_json(MESSAGES, SCHEMA)
    assert result.data["founded"] == 753
    assert "'753' is not of type 'integer'" in endpoint.bodies()[1]["messages"][-1]["content"]


async def test_answer_that_stays_invalid_raises_without_echoing_values(scripted) -> None:
    endpoint = scripted(scripted.completion(json.dumps({"city": "Rome", "founded": "secret-753"})))
    with pytest.raises(InvalidOutputError) as caught:
        await make_model(endpoint).complete_json(MESSAGES, SCHEMA)

    assert caught.value.attempts == 2
    assert caught.value.problems == ("/founded: type",)
    assert "secret-753" not in str(caught.value)
    assert len(endpoint.requests) == 2


async def test_repair_attempts_zero_means_no_second_call(scripted) -> None:
    endpoint = scripted(scripted.completion("not json at all"))
    with pytest.raises(InvalidOutputError, match="1 attempt"):
        await make_model(endpoint, repair_attempts=0).complete_json(MESSAGES, SCHEMA)
    assert len(endpoint.requests) == 1


async def test_answer_cut_by_the_token_limit_is_not_retried(scripted) -> None:
    endpoint = scripted(scripted.completion('{"city": "Ro', finish="length"))
    with pytest.raises(InvalidOutputError, match="token limit"):
        await make_model(endpoint).complete_json(MESSAGES, SCHEMA)
    assert len(endpoint.requests) == 1


async def test_missing_usage_is_estimated_from_text_length(scripted) -> None:
    endpoint = scripted(scripted.completion(GOOD, prompt=None, output=None, model=None))
    result = await make_model(endpoint).complete_json(MESSAGES, SCHEMA)
    assert result.usage.input_tokens > 0
    assert result.usage.output_tokens == -(-len(GOOD) // 4)
    assert result.model == "small"


async def test_endpoint_error_after_retries_propagates(scripted) -> None:
    endpoint = scripted(scripted.api_error(500, "boom"))
    with pytest.raises(EndpointError, match="HTTP 500"):
        await make_model(endpoint).complete_json(MESSAGES, SCHEMA)
    assert len(endpoint.requests) == 3


async def test_invalid_schema_is_a_value_error_before_any_request(scripted) -> None:
    endpoint = scripted(scripted.completion(GOOD))
    with pytest.raises(ValueError, match="invalid JSON Schema"):
        await make_model(endpoint).complete_json(MESSAGES, {"type": "nonsense"})
    assert endpoint.requests == []


async def test_per_task_override_routes_to_its_own_model_url_and_key(scripted) -> None:
    default = scripted(scripted.completion(GOOD))
    llm = LlmSettings(
        base_url="http://local.test/v1",
        model="small",
        tasks={
            "schema_discovery": LlmTaskOverride(
                base_url="http://hosted.test/v1", model="large", api_key="sk-hosted"
            )
        },
    )
    model = EndpointChatModel(llm, transport=default.transport, sleep=default.sleep)

    await model.complete_json(MESSAGES, SCHEMA)
    await model.complete_json(MESSAGES, SCHEMA, ChatOptions(task="schema_discovery"))
    await model.complete_json(MESSAGES, SCHEMA, ChatOptions(task="unrouted"))

    urls = [str(r.url) for r in default.requests]
    assert urls == [
        "http://local.test/v1/chat/completions",
        "http://hosted.test/v1/chat/completions",
        "http://local.test/v1/chat/completions",
    ]
    assert [b["model"] for b in default.bodies()] == ["small", "large", "small"]
    assert "authorization" not in default.requests[0].headers
    assert default.requests[1].headers["authorization"] == "Bearer sk-hosted"
    await model.aclose()


def test_registry_builds_a_task_model_from_the_override() -> None:
    settings = InferenceSettings.model_validate(
        {
            "llm": {
                "base_url": "http://local.test/v1",
                "model": "small",
                "tasks": {"extraction": {"model": "tiny"}},
            }
        }
    )
    assert isinstance(create_chat_model(settings, task="extraction"), EndpointChatModel)
    assert create_chat_model(settings, task="extraction").model == "tiny"  # type: ignore[attr-defined]
    assert create_chat_model(settings).model == "small"  # type: ignore[attr-defined]


async def test_prompts_and_keys_are_never_logged(
    scripted, caplog: pytest.LogCaptureFixture
) -> None:
    endpoint = scripted(
        scripted.api_error(429, "slow", **{"Retry-After": "0"}),
        scripted.api_error(400, "unknown response_format"),
        scripted.completion("oops"),
        scripted.completion(GOOD),
    )
    messages = [Message("user", "CONFIDENTIAL-DOCUMENT-TEXT")]
    caplog.set_level(logging.DEBUG)

    await make_model(endpoint, api_key="sk-very-secret").complete_json(messages, SCHEMA)

    assert caplog.records, "operational events are logged"
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "CONFIDENTIAL" not in logged
    assert "sk-very-secret" not in logged


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('<think>hmm {"b": 2}</think>\n{"a": 1}', {"a": 1}),
        ('Sure! Here it is: {"a": [1, 2]} Hope that helps.', {"a": [1, 2]}),
        ("[1, 2, 3]", [1, 2, 3]),
    ],
)
def test_parse_json_text_is_tolerant(text, expected) -> None:
    assert parse_json_text(text) == expected


def test_parse_json_text_rejects_prose() -> None:
    with pytest.raises(ValueError, match="no JSON"):
        parse_json_text("I cannot do that.")


@pytest.mark.parametrize(("configured", "expected"), [({}, False), ({"trust_env": True}, True)])
async def test_trust_env_follows_the_settings(
    scripted, client_options, configured, expected
) -> None:
    endpoint = scripted(scripted.completion(GOOD))
    model = make_model(endpoint, **configured)
    await model.complete_json(MESSAGES, SCHEMA)
    await model.aclose()
    assert [options["trust_env"] for options in client_options] == [expected]


async def test_configured_headers_and_body_fields_are_sent(
    scripted, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_WORKSPACE_ID", "wrk-from-env")
    llm = InferenceSettings.model_validate(
        {
            "llm": {
                "base_url": "http://llm.test/v1",
                "model": "small",
                "api_key": "sk-secret",
                "headers": {
                    "provider-workspace-id": "secret://env/TEST_WORKSPACE_ID",
                    "X-Trace": "on",
                },
                "extra_body": {"reasoning_effort": "none", "thinking": {"type": "disabled"}},
            }
        }
    ).llm
    endpoint = scripted(scripted.completion(GOOD))
    model = EndpointChatModel(llm, transport=endpoint.transport, sleep=endpoint.sleep)

    await model.complete_json(MESSAGES, SCHEMA)

    headers = endpoint.requests[0].headers
    assert headers["provider-workspace-id"] == "wrk-from-env"
    assert headers["x-trace"] == "on"
    assert headers["authorization"] == "Bearer sk-secret"
    assert headers["content-type"] == "application/json"
    body = endpoint.bodies()[0]
    assert body["reasoning_effort"] == "none"
    assert body["thinking"] == {"type": "disabled"}
    assert body["model"] == "small"


@pytest.mark.parametrize(
    "llm",
    [
        {"headers": {"Authorization": "Bearer other"}},
        {"headers": {"content-type": "text/plain"}},
        {"extra_body": {"messages": []}},
        {"extra_body": {"response_format": {"type": "text"}}},
        {"tasks": {"extraction": {"headers": {"Host": "elsewhere"}}}},
        {"tasks": {"extraction": {"extra_body": {"model": "other"}}}},
    ],
)
def test_headers_and_body_fields_set_by_the_client_cannot_be_replaced(llm) -> None:
    with pytest.raises(ValueError, match="cannot be set"):
        InferenceSettings.model_validate({"llm": llm})


async def test_task_on_another_endpoint_does_not_inherit_the_headers(scripted) -> None:
    llm = LlmSettings(
        base_url="http://local.test/v1",
        model="small",
        headers={"provider-workspace-id": "wrk-1"},  # type: ignore[dict-item]
        tasks={
            "same_endpoint": LlmTaskOverride(model="other"),
            "hosted": LlmTaskOverride(base_url="http://hosted.test/v1", model="large"),
            "hosted_with_headers": LlmTaskOverride(
                base_url="http://hosted.test/v1",
                headers={"provider-workspace-id": "wrk-2"},  # type: ignore[dict-item]
            ),
        },
    )
    endpoint = scripted(scripted.completion(GOOD))
    model = EndpointChatModel(llm, transport=endpoint.transport, sleep=endpoint.sleep)

    for task in ("same_endpoint", "hosted", "hosted_with_headers"):
        await model.complete_json(MESSAGES, SCHEMA, ChatOptions(task=task))
    await model.aclose()

    sent = [r.headers.get("provider-workspace-id") for r in endpoint.requests]
    assert sent == ["wrk-1", None, "wrk-2"]


async def test_header_values_are_scrubbed_from_error_text(scripted) -> None:
    endpoint = scripted(scripted.api_error(400, "workspace wrk-secret-value-1 is not usable"))
    model = make_model(
        endpoint,
        headers={"provider-workspace-id": "wrk-secret-value-1"},
        structured_output="json_mode",
    )
    with pytest.raises(EndpointError) as caught:
        await model.complete_json(MESSAGES, SCHEMA)
    assert "wrk-secret-value-1" not in str(caught.value)


async def test_null_temperature_is_left_out_of_the_request(scripted) -> None:
    endpoint = scripted(scripted.completion(GOOD))
    llm = LlmSettings(
        base_url="http://llm.test/v1",
        model="small",
        temperature=None,
        tasks={
            "inherits": LlmTaskOverride(model="other"),
            "sets": LlmTaskOverride(temperature=0.2),
        },
    )
    model = EndpointChatModel(llm, transport=endpoint.transport, sleep=endpoint.sleep)

    await model.complete_json(MESSAGES, SCHEMA)
    await model.complete_json(MESSAGES, SCHEMA, ChatOptions(task="inherits"))
    await model.complete_json(MESSAGES, SCHEMA, ChatOptions(task="sets"))
    await model.complete_json(MESSAGES, SCHEMA, ChatOptions(temperature=0.5))
    await model.aclose()

    assert [b.get("temperature") for b in endpoint.bodies()] == [None, None, 0.2, 0.5]
    assert "temperature" not in endpoint.bodies()[0]


def test_task_override_can_leave_the_temperature_out() -> None:
    llm = InferenceSettings.model_validate(
        {"llm": {"tasks": {"hosted": {"temperature": None}, "local": {"model": "m"}}}}
    ).llm
    assert llm.temperature == 0
    assert llm.for_task("hosted").temperature is None
    assert llm.for_task("local").temperature == 0


@pytest.mark.parametrize(("strict", "expected"), [(None, "absent"), (True, True), (False, False)])
async def test_json_schema_strict_follows_the_settings(scripted, strict, expected) -> None:
    endpoint = scripted(scripted.completion(GOOD))
    await make_model(endpoint, json_schema_strict=strict).complete_json(MESSAGES, SCHEMA)
    json_schema = endpoint.bodies()[0]["response_format"]["json_schema"]
    assert json_schema.get("strict", "absent") == expected


async def test_endpoint_requiring_strict_steps_down_when_the_schema_is_too_large(scripted) -> None:
    """Recorded answers of an endpoint that takes strict json_schema only, caps the optional
    properties of a schema and has no json_object mode."""
    endpoint = scripted(
        scripted.api_error(
            400,
            "Schemas contains too many optional parameters (28), which would make grammar "
            "compilation inefficient. Reduce the number of optional parameters in your tool "
            "schemas (limit: 24).",
        ),
        scripted.api_error(400, "response_format.type: Input should be 'json_schema'"),
        scripted.completion(GOOD),
    )
    model = make_model(endpoint, json_schema_strict=True)

    result = await model.complete_json(MESSAGES, SCHEMA)

    assert result.data == {"city": "Rome", "founded": 753}
    assert model.output_mode == "prompt"
    formats = [b.get("response_format", {}).get("type") for b in endpoint.bodies()]
    assert formats == ["json_schema", "json_object", None]


async def test_reasoning_that_uses_the_whole_budget_is_reported_with_its_usage(scripted) -> None:
    empty = scripted.completion("", prompt=1100, output=4096, finish="length")
    null_content = httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"role": "assistant", "content": None}, "finish_reason": "length"}
            ],
            "usage": {"prompt_tokens": 1100, "completion_tokens": 4096},
        },
    )
    for answer in (empty, null_content):
        endpoint = scripted(answer)
        with pytest.raises(InvalidOutputError, match="reasoning") as caught:
            await make_model(endpoint).complete_json(MESSAGES, SCHEMA)
        usage = caught.value.usage
        assert (usage.input_tokens, usage.output_tokens) == (1100, 4096)
        assert len(endpoint.requests) == 1
