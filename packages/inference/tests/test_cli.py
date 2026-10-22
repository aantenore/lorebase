from __future__ import annotations

import json

import pytest

from lorebase_inference import InferenceSettings, InvalidOutputError
from lorebase_inference import cli as cli_module
from lorebase_inference.cli import check_chat, check_embeddings, run_checks
from lorebase_inference.fake import embed_text


def fake_settings(**embeddings: object) -> InferenceSettings:
    return InferenceSettings.model_validate(
        {
            "llm": {"provider": "fake", "model": "probe"},
            "embeddings": {"provider": "fake", "dims": 8, **embeddings},
            "local_extractors": {"provider": "fake"},
        }
    )


async def test_chat_probe_reports_model_latency_and_tokens() -> None:
    result = await check_chat(fake_settings())
    assert result.ok
    assert result.details["model"] == "fake:probe"
    assert result.details["latency_ms"] >= 0
    assert "/" in result.details["tokens"]


async def test_embedding_probe_reports_dimensions() -> None:
    result = await check_embeddings(fake_settings())
    assert result.ok
    assert result.details["dims"] == 8
    assert result.details["model"] == "BAAI/bge-small-en-v1.5"


async def test_embedding_probe_fails_on_a_dimension_mismatch(monkeypatch) -> None:
    class Wrong:
        name = "m"
        dims = 8

        async def embed(self, texts):
            return [[0.0] * 5 for _ in texts]

    monkeypatch.setattr(cli_module, "create_embedding_model", lambda settings: Wrong())
    result = await check_embeddings(fake_settings())
    assert not result.ok
    assert "returned 5 dimensions, embeddings.dims is 8" in result.line()


async def test_provider_errors_become_failed_results(monkeypatch) -> None:
    class Broken:
        async def complete_json(self, messages, schema, options=None):
            raise InvalidOutputError("bad answer", problems=["/ok: type"])

    monkeypatch.setattr(cli_module, "create_chat_model", lambda settings, task=None: Broken())
    [chat, embeddings] = await run_checks(fake_settings())
    assert not chat.ok
    assert chat.error == "InvalidOutputError: bad answer"
    assert embeddings.ok, "one failing component does not hide the other"


async def test_unavailable_provider_is_a_failed_result_not_a_crash(monkeypatch) -> None:
    monkeypatch.setattr("lorebase_inference.registry.importlib.util.find_spec", lambda name: None)
    settings = InferenceSettings.model_validate(
        {"llm": {"provider": "fake"}, "embeddings": {"provider": "fastembed"}}
    )
    [chat, embeddings] = await run_checks(settings)
    assert chat.ok
    assert not embeddings.ok
    assert "lorebase-inference[fastembed]" in (embeddings.error or "")


async def test_components_can_be_skipped() -> None:
    results = await run_checks(fake_settings(), chat=False)
    assert [r.component for r in results] == ["embeddings"]


def run_main(monkeypatch, capsys, *argv: str, **env: str) -> tuple[int, str]:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit) as caught:
        cli_module.main(list(argv))
    return int(caught.value.code or 0), capsys.readouterr().out


def test_main_prints_a_line_per_component_and_exits_zero(monkeypatch, capsys) -> None:
    code, out = run_main(
        monkeypatch,
        capsys,
        "check",
        LOREBASE__INFERENCE__LLM__PROVIDER="fake",
        LOREBASE__INFERENCE__EMBEDDINGS__PROVIDER="fake",
    )
    lines = out.strip().splitlines()
    assert code == 0
    assert lines[0].startswith("chat")
    assert "ok" in lines[0]
    assert "latency_ms=" in lines[0]
    assert lines[1].startswith("embeddings")
    assert "dims=384" in lines[1]


def test_main_json_output_and_failure_status(monkeypatch, capsys) -> None:
    code, out = run_main(
        monkeypatch,
        capsys,
        "check",
        "--json",
        "--no-chat",
        LOREBASE__INFERENCE__EMBEDDINGS__PROVIDER="fake",
        LOREBASE__INFERENCE__EMBEDDINGS__DIMS="16",
    )
    assert code == 0
    [entry] = json.loads(out)
    assert entry["component"] == "embeddings"
    assert entry["details"]["dims"] == 16

    monkeypatch.setattr("lorebase_inference.registry.importlib.util.find_spec", lambda name: None)
    code, out = run_main(
        monkeypatch,
        capsys,
        "check",
        "--no-chat",
        LOREBASE__INFERENCE__EMBEDDINGS__PROVIDER="fastembed",
    )
    assert code == 1
    assert "FAILED" in out


def test_main_reports_invalid_configuration(monkeypatch) -> None:
    monkeypatch.setenv("LOREBASE__INFERENCE__LLM__RETRY__MAX_RETRIES", "-1")
    with pytest.raises(SystemExit) as caught:
        cli_module.main(["check"])
    assert "max_retries" in str(caught.value)


async def test_embed_query_returns_the_vector_of_the_configured_model() -> None:
    found = await cli_module.embed_query(fake_settings(), "late deliveries")
    assert found == {
        "model": "fake:BAAI/bge-small-en-v1.5",
        "dims": 8,
        "vector": embed_text("late deliveries", 8),
    }


async def test_embed_query_applies_the_query_prefix_only_for_queries() -> None:
    settings = fake_settings(query_prefix="query: ", document_prefix="passage: ")
    plain = await cli_module.embed_query(settings, "late deliveries")
    query = await cli_module.embed_query(settings, "late deliveries", query=True)
    assert plain["vector"] == embed_text("late deliveries", 8)
    assert query["vector"] == embed_text("query: late deliveries", 8)


def test_main_embed_query_flag_prefixes_the_text(monkeypatch, capsys) -> None:
    code, out = run_main(
        monkeypatch,
        capsys,
        "embed",
        "--query",
        "late deliveries",
        LOREBASE__INFERENCE__EMBEDDINGS__PROVIDER="fake",
        LOREBASE__INFERENCE__EMBEDDINGS__DIMS="4",
        LOREBASE__INFERENCE__EMBEDDINGS__QUERY_PREFIX="query: ",
    )
    assert code == 0
    assert json.loads(out)["vector"] == embed_text("query: late deliveries", 4)


def test_main_embed_prints_json(monkeypatch, capsys) -> None:
    code, out = run_main(
        monkeypatch,
        capsys,
        "embed",
        "late deliveries",
        LOREBASE__INFERENCE__EMBEDDINGS__PROVIDER="fake",
        LOREBASE__INFERENCE__EMBEDDINGS__DIMS="4",
    )
    assert code == 0
    body = json.loads(out)
    assert body["dims"] == 4
    assert body["vector"] == embed_text("late deliveries", 4)


def test_main_embed_fails_on_a_dimension_mismatch(monkeypatch) -> None:
    class Wrong:
        name = "m"
        dims = 8

        async def embed(self, texts):
            return [[0.0] * 5 for _ in texts]

    monkeypatch.setattr(cli_module, "create_embedding_model", lambda settings: Wrong())
    monkeypatch.setenv("LOREBASE__INFERENCE__EMBEDDINGS__PROVIDER", "fake")
    with pytest.raises(SystemExit) as caught:
        cli_module.main(["embed", "x"])
    assert "returned 5 dimensions" in str(caught.value)
