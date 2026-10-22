"""The dedicated schema discovery pass: a chat call routed through the `schema_discovery` task."""

from __future__ import annotations

from typing import Any

import pytest

from lorebase_core.infer.suggestions import Observation, SuggestionOptions, SuggestionType
from lorebase_core.model import Schema
from lorebase_inference import InferenceSettings
from lorebase_inference.fake import FakeChatModel, prompt_hash
from lorebase_inference.interfaces import ChatOptions, ChatResult, Message, Usage
from lorebase_inference.settings import FakeSettings, LlmSettings, LlmTaskOverride
from lorebase_scribe.extraction.discovery import Discovery
from lorebase_scribe.extraction.llm_discovery import TASK, build_messages, run_llm_discovery
from lorebase_scribe.extraction.ontology import Ontology, build_ontology
from lorebase_scribe.extraction.pipeline import build_models
from lorebase_scribe.extraction.settings import DiscoverySettings, ExtractionSettings
from lorebase_scribe.extraction.text import Chunk

SETTINGS = ExtractionSettings()
TEXT = (
    "Carrier Beta (CR-1) and Carrier Gamma (CR-2) deliver to the plant. "
    "Supplier S-1025 has a payment term of 45 days."
)
CHUNK = Chunk(0, 100, 100 + len(TEXT), TEXT)


@pytest.fixture
def ontology(supply_schema: Schema) -> Ontology:
    return build_ontology(supply_schema, None)


ANSWER: dict[str, Any] = {
    "kinds": [
        {"name": "Carrier", "example": "CR-1", "evidence": "Carrier Beta (CR-1)"},
        {"name": "Carrier", "example": "CR-2", "evidence": "Carrier Gamma (CR-2)"},
        {"name": "Invented", "example": "X", "evidence": "not in the text"},
    ],
    "attributes": [
        {
            "kind": "supplier",
            "name": "payment term",
            "value": "45 days",
            "evidence": "payment term of 45 days",
        },
        {"kind": "Carrier", "name": "region", "value": "EU", "evidence": "Carrier Beta (CR-1)"},
        {"kind": "Nowhere", "name": "x", "value": "S-1025", "evidence": "Supplier S-1025 has"},
    ],
}


def fake_chat(ontology: Ontology, answer: dict[str, Any]) -> FakeChatModel:
    key = prompt_hash(build_messages(ontology, CHUNK))
    return FakeChatModel(LlmSettings(provider="fake"), FakeSettings(), fixtures={key: answer})


class Cache:
    def __init__(self) -> None:
        self.items: dict[bytes, tuple[Any, Usage, str]] = {}

    async def get(self, key: bytes) -> tuple[Any, Usage, str] | None:
        return self.items.get(key)

    async def put(self, key: bytes, data: Any, usage: Usage, model: str) -> None:
        self.items[key] = (data, usage, model)


async def discover(ontology: Ontology, chat: Any, cache: Cache | None = None) -> Any:
    return await run_llm_discovery(
        chat,
        ontology,
        CHUNK,
        document_id="doc_1",
        schema_hash=b"\0" * 32,
        model_key="fake:test",
        settings=SETTINGS,
        cache=cache,
    )


async def test_proposals_become_observations_with_verified_evidence(
    ontology: Ontology, supply_schema: Schema
) -> None:
    result = await discover(ontology, fake_chat(ontology, ANSWER))
    supplier = supply_schema.kind_by_api_name("Supplier")
    assert supplier is not None
    found = {(o.type, o.name, o.value): o for o in result.observations}
    assert (SuggestionType.KIND, "Carrier", "CR-1") in found
    assert (SuggestionType.KIND, "Carrier", "CR-2") in found
    term = found[(SuggestionType.ATTRIBUTE, "payment_term", "45 days")]
    assert term.kind == supplier.id
    assert term.evidence is not None
    assert TEXT[term.evidence["start"] - 100 : term.evidence["end"] - 100] == (
        "payment term of 45 days"
    )
    assert len(result.observations) == 3
    assert result.discarded == {
        "unverified_evidence": 1,  # the Kind whose quote is not in the text
        "ungrounded_value": 1,  # "EU" is not in its quote
        "unknown_kind": 1,  # an Attribute of a Kind neither in the schema nor proposed
    }


async def test_the_call_uses_the_schema_discovery_task_and_the_cache(ontology: Ontology) -> None:
    class Spy:
        def __init__(self) -> None:
            self.tasks: list[str | None] = []

        async def complete_json(
            self, messages: Any, schema: Any, options: ChatOptions | None = None
        ) -> ChatResult:
            self.tasks.append(options.task if options else None)
            return ChatResult(data={"kinds": [], "attributes": []}, usage=Usage(5, 2), model="m")

    spy, cache = Spy(), Cache()
    first = await discover(ontology, spy, cache)
    second = await discover(ontology, spy, cache)
    assert spy.tasks == [TASK]
    assert TASK == "schema_discovery"
    assert (first.cached, second.cached) == (False, True)
    assert (first.usage.input_tokens, second.usage.input_tokens) == (5, 0)


async def test_known_elements_are_dropped_and_independent_kinds_survive(
    ontology: Ontology,
) -> None:
    answer = {
        "kinds": [
            {"name": "Supplier", "example": "S-1025", "evidence": "Supplier S-1025"},
            {"name": "Carrier", "example": "CR-1", "evidence": "Carrier Beta (CR-1)"},
        ],
        "attributes": [],
    }
    result = await discover(ontology, fake_chat(ontology, answer))
    discovery = Discovery(ontology, DiscoverySettings(), SuggestionOptions())
    discovery.extend(result.observations)
    kept = discovery.finish({})
    assert [o.name for o in kept] == ["Carrier"]
    assert discovery.stats["suggestions_known"] == 1


def test_a_kind_named_like_a_free_text_field_needs_evidence_of_its_own(
    ontology: Ontology, supply_schema: Schema
) -> None:
    def kinds(*codes: str | None) -> Discovery:
        discovery = Discovery(ontology, DiscoverySettings(), SuggestionOptions())
        for code in codes:
            discovery.add(
                Observation(SuggestionType.KIND, "Country", "doc_1", 0.8, "model", value=code)
            )
        return discovery

    lone = kinds(
        "PL"
    )  # a Kind `Country` beside the free-text Attribute `country`: a restated field
    assert lone.finish({}) == []
    assert lone.stats["suggestions_noise"] == 1
    several = kinds("PL", "TR")  # two distinct countries seen as things of their own
    assert [o.name for o in several.finish({})] == ["Country", "Country"]
    strict = Discovery(
        ontology, DiscoverySettings(), SuggestionOptions(kind_independent_instances=0)
    )
    strict.extend(several.observations)
    assert strict.finish({}) == []


def test_the_pass_builds_its_model_from_the_task_route() -> None:
    inference = InferenceSettings(
        llm=LlmSettings(
            provider="fake",
            model="small",
            tasks={"schema_discovery": LlmTaskOverride(model="large")},
        )
    )
    off = build_models(inference, ExtractionSettings(), (0,))
    assert off.discovery_chat is None
    on = build_models(
        inference, ExtractionSettings(discovery=DiscoverySettings(llm_pass=True)), (0,)
    )
    assert on.discovery_chat is not None
    assert on.discovery_model == "fake:large"
    assert on.chat is None  # no tier 2 needed
    disabled = build_models(
        inference,
        ExtractionSettings(discovery=DiscoverySettings(enabled=False, llm_pass=True)),
        (0,),
    )
    assert disabled.discovery_chat is None


def test_messages_list_the_kinds_without_records(ontology: Ontology) -> None:
    system, user = build_messages(ontology, CHUNK)
    assert isinstance(system, Message)
    assert "Kind Supplier:" in user.content
    assert TEXT in user.content
