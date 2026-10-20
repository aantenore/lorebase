from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from lorebase_inference.fake import FakeChatModel, prompt_hash
from lorebase_inference.interfaces import ChatModel, Message
from lorebase_inference.settings import FakeSettings, LlmSettings

SCHEMAS: dict[str, dict[str, Any]] = {
    "object": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 3, "maxLength": 12},
            "count": {"type": "integer", "minimum": 5, "maximum": 9},
            "score": {"type": "number", "exclusiveMinimum": 0.5, "maximum": 1.5},
            "active": {"type": "boolean"},
            "note": {"type": ["string", "null"]},
            "kind": {"enum": ["a", "b"]},
            "fixed": {"const": {"x": 1}},
        },
        "required": ["name", "count"],
        "additionalProperties": False,
    },
    "array": {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 3,
        "maxItems": 4,
        "uniqueItems": True,
    },
    "nested": {
        "type": "object",
        "properties": {
            "facts": {
                "type": "array",
                "minItems": 2,
                "items": {
                    "type": "object",
                    "properties": {
                        "subject": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "evidence": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["subject", "confidence", "evidence"],
                },
            }
        },
        "required": ["facts"],
    },
    "refs": {
        "$defs": {"Quote": {"type": "object", "properties": {"text": {"type": "string"}}}},
        "type": "object",
        "properties": {"quote": {"$ref": "#/$defs/Quote"}},
        "required": ["quote"],
    },
    "any_of": {"anyOf": [{"type": "null"}, {"type": "integer", "minimum": 10}]},
    "all_of": {
        "allOf": [
            {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
            {"type": "object", "properties": {"b": {"type": "integer"}}, "required": ["b"]},
        ]
    },
    "formats": {
        "type": "object",
        "properties": {
            "day": {"type": "string", "format": "date"},
            "mail": {"type": "string", "format": "email"},
        },
        "required": ["day", "mail"],
    },
    "recursive": {
        "$defs": {
            "Node": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "children": {"type": "array", "items": {"$ref": "#/$defs/Node"}},
                },
                "required": ["label", "children"],
            }
        },
        "$ref": "#/$defs/Node",
    },
    "negative_range": {"type": "integer", "minimum": -20, "maximum": -10},
    "multiple_of": {"type": "integer", "minimum": 7, "multipleOf": 5},
}

PROMPT = [
    Message("system", "Extract facts."),
    Message("user", 'Text: "ACME Corp supplies bolts" and "Berlin plant".'),
]


def model(**fake: Any) -> FakeChatModel:
    return FakeChatModel(LlmSettings(provider="fake"), FakeSettings(**fake))


@pytest.mark.parametrize("name", sorted(SCHEMAS))
async def test_output_validates_against_the_requested_schema(name: str) -> None:
    schema = SCHEMAS[name]
    result = await model().complete_json(PROMPT, schema)
    Draft202012Validator(schema).validate(result.data)


@pytest.mark.parametrize("name", sorted(SCHEMAS))
async def test_same_input_gives_byte_identical_output(name: str) -> None:
    first = await model().complete_json(PROMPT, SCHEMAS[name])
    second = await model().complete_json(PROMPT, SCHEMAS[name])
    assert json.dumps(first.data, sort_keys=True) == json.dumps(second.data, sort_keys=True)
    assert first == second


async def test_strings_are_built_from_quoted_prompt_text() -> None:
    result = await model().complete_json(PROMPT, {"type": "array", "items": {"type": "string"}})
    assert result.data == ["ACME Corp supplies bolts"]


async def test_prompt_without_quotes_uses_its_words() -> None:
    result = await model().complete_json(
        [Message("user", "summarize the contract")], {"type": "string"}
    )
    assert result.data in {"summarize", "the", "contract"}


async def test_different_prompts_give_different_text() -> None:
    schema = {"type": "string"}
    first = await model().complete_json([Message("user", 'say "alpha"')], schema)
    second = await model().complete_json([Message("user", 'say "beta"')], schema)
    assert (first.data, second.data) == ("alpha", "beta")


async def test_reports_model_id_and_usage() -> None:
    result = await model().complete_json(PROMPT, {"type": "string"})
    assert result.model == "fake:qwen2.5:7b-instruct"
    assert result.usage.input_tokens > 0
    assert result.usage.output_tokens > 0


async def test_fixture_directory_takes_precedence(tmp_path: Path) -> None:
    (tmp_path / f"{prompt_hash(PROMPT)}.json").write_text('{"canned": true}')
    result = await model(fixtures_dir=tmp_path).complete_json(PROMPT, SCHEMAS["object"])
    assert result.data == {"canned": True}


async def test_in_memory_fixtures_are_copied_on_use() -> None:
    chat = FakeChatModel(
        LlmSettings(provider="fake"), FakeSettings(), fixtures={prompt_hash(PROMPT): {"k": [1]}}
    )
    first = await chat.complete_json(PROMPT, {})
    first.data["k"].append(2)
    assert (await chat.complete_json(PROMPT, {})).data == {"k": [1]}


def test_prompt_hash_depends_on_roles_and_content() -> None:
    assert prompt_hash(PROMPT) == prompt_hash(list(PROMPT))
    assert prompt_hash(PROMPT) != prompt_hash(PROMPT[:1])
    assert prompt_hash([Message("user", "x")]) != prompt_hash([Message("assistant", "x")])


async def test_remote_references_are_rejected() -> None:
    with pytest.raises(ValueError, match="only local references"):
        await model().complete_json(PROMPT, {"$ref": "https://example.org/schema.json"})


def test_fake_model_satisfies_the_protocol() -> None:
    assert isinstance(model(), ChatModel)
