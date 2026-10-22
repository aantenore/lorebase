"""The `inference` settings section (design 16), registered with `lorebase_platform`.

Environment aliases override the YAML values of the default chat model:
`LLM_BASE_URL` (`inference.llm.base_url`), `LLM_MODEL` (`inference.llm.model`) and
`LLM_API_KEY` (`inference.llm.api_key`, optional, handled as a secret).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from lorebase_platform.secrets import SecretValue
from lorebase_platform.settings import SectionModel, Settings, register_section

SECTION = "inference"
ENV_ALIASES = {
    "LLM_BASE_URL": "llm.base_url",
    "LLM_MODEL": "llm.model",
    "LLM_API_KEY": "llm.api_key",
}

ChatProvider = Literal["openai_compatible", "fake"]
EmbeddingProvider = Literal["fastembed", "openai_compatible", "fake"]
SpanProvider = Literal["gliner", "fake"]
RelationProvider = Literal["glirel", "cooccurrence", "fake"]

RESERVED_HEADERS = frozenset({"authorization", "content-type", "accept", "content-length", "host"})
"""Headers the client sets itself; `headers` cannot replace them."""
RESERVED_BODY_FIELDS = frozenset({"model", "messages", "stream", "response_format"})
"""Request fields the client sets itself; `extra_body` cannot replace them."""


def _check_headers(headers: dict[str, Any] | None) -> None:
    for name in headers or {}:
        if not name.strip() or name.strip().lower() in RESERVED_HEADERS:
            raise ValueError(f"header {name!r} cannot be set in `headers`")


def _check_extra_body(extra: dict[str, Any] | None) -> None:
    for name in extra or {}:
        if name in RESERVED_BODY_FIELDS:
            raise ValueError(f"field {name!r} cannot be set in `extra_body`")


class RetrySettings(SectionModel):
    """Retries of HTTP calls on 429, 5xx and connection errors, with exponential backoff.

    A `Retry-After` header is honored (capped at `backoff_max_s`); otherwise the wait before
    retry `n` (from 0) is `min(backoff_max_s, backoff_base_s * 2**n)` with full jitter.
    """

    max_retries: int = Field(default=3, ge=0, description="Retries after the first attempt.")
    backoff_base_s: float = Field(default=0.5, gt=0)
    backoff_max_s: float = Field(default=30, gt=0)


class Price(SectionModel):
    """Price per million tokens in the operator's currency; zero for local models."""

    input: float = Field(default=0, ge=0)
    output: float = Field(default=0, ge=0)


class LlmTaskOverride(SectionModel):
    """Per-task override of the chat model; unset fields inherit from `llm`."""

    provider: ChatProvider | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: SecretValue | None = None
    structured_output: Literal["json_schema", "json_mode"] | None = None
    json_schema_strict: bool | None = None
    temperature: float | None = Field(default=None, ge=0)
    max_concurrency: int | None = Field(default=None, ge=1)
    timeout_s: float | None = Field(default=None, gt=0)
    trust_env: bool | None = None
    price_per_million_tokens: Price | None = None
    headers: dict[str, SecretValue] | None = None
    extra_body: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check_request_options(self) -> Self:
        _check_headers(self.headers)
        _check_extra_body(self.extra_body)
        return self


class LlmSettings(SectionModel):
    """Chat model (tier 2). Any OpenAI-compatible endpoint, local or hosted."""

    provider: ChatProvider = Field(
        default="openai_compatible", description="openai_compatible or fake (tests)."
    )
    base_url: str = Field(
        default="http://localhost:11434/v1",
        min_length=1,
        description="OpenAI-compatible endpoint. Default: local Ollama. Hosted providers and "
        "a LiteLLM proxy are selected by changing this value (alias LLM_BASE_URL).",
    )
    model: str = Field(
        default="qwen2.5:7b-instruct",
        min_length=1,
        description="Model name served by the endpoint (alias LLM_MODEL).",
    )
    api_key: SecretValue | None = Field(
        default=None,
        description="Literal or secret reference; omitted for local servers (alias LLM_API_KEY).",
    )
    structured_output: Literal["json_schema", "json_mode"] = Field(
        default="json_schema",
        description="How the endpoint is asked for schema-valid JSON. json_schema sends the "
        "schema as response_format and steps down to json_mode when the server rejects it; "
        "json_mode asks for a JSON object with the schema in the prompt. Answers are always "
        "validated against the schema, with up to `repair_attempts` repair retries.",
    )
    json_schema_strict: bool | None = Field(
        default=None,
        description="Value of response_format.json_schema.strict in json_schema mode; null "
        "leaves it out. Some endpoints require true and then refuse schemas outside their "
        "strict subset, which steps the client down like any other rejection.",
    )
    repair_attempts: int = Field(
        default=1, ge=0, le=3, description="Repair retries after an invalid answer."
    )
    temperature: float | None = Field(
        default=0,
        ge=0,
        description="Sampling temperature; null leaves it out of the request, for models that "
        "refuse the parameter.",
    )
    max_tokens: int | None = Field(
        default=None, ge=1, description="Completion token cap; unset leaves it to the server."
    )
    max_concurrency: int = Field(default=2, ge=1, description="Parallel requests per process.")
    timeout_s: float = Field(default=120, gt=0)
    trust_env: bool = Field(
        default=False,
        description="Honor HTTP_PROXY, HTTPS_PROXY and NO_PROXY (and other httpx environment "
        "settings) for this endpoint; false ignores them and connects directly.",
    )
    retry: RetrySettings = Field(default_factory=RetrySettings)
    price_per_million_tokens: Price = Field(default_factory=Price)
    headers: dict[str, SecretValue] = Field(
        default_factory=dict,
        description="Extra HTTP headers sent with every request, for example a workspace or "
        "organization id a hosted provider requires. Values accept secret references.",
    )
    extra_body: dict[str, Any] = Field(
        default_factory=dict,
        description="Extra fields merged into every request body, for server-specific options "
        'such as {"reasoning_effort": "none"}. model, messages, stream and response_format '
        "are set by the client and cannot be overridden.",
    )
    tasks: dict[str, LlmTaskOverride] = Field(
        default_factory=dict,
        description="Optional overrides per task name, e.g. extraction, schema_discovery.",
    )

    @model_validator(mode="after")
    def _check_request_options(self) -> Self:
        _check_headers(self.headers)
        _check_extra_body(self.extra_body)
        return self

    def for_task(self, task: str | None) -> Self:
        """Effective settings for a task: this model with the task's overrides applied.

        A task that names another `base_url` uses only its own `api_key` and `headers`: the main
        key and headers are never sent to an endpoint they were not configured for.
        """
        override = self.tasks.get(task) if task else None
        changes: dict[str, Any] = (
            {
                name: value
                for name in type(override).model_fields
                if (value := getattr(override, name)) is not None
            }
            if override
            else {}
        )
        if override and override.temperature is None and "temperature" in override.model_fields_set:
            changes["temperature"] = None  # an explicit null leaves the parameter out
        if override and override.base_url not in (None, self.base_url):
            changes["api_key"] = override.api_key
            changes["headers"] = override.headers or {}
        return self.model_copy(update={**changes, "tasks": {}})


class EmbeddingSettings(SectionModel):
    """Embedding model; its name and dimension define the vector index."""

    provider: EmbeddingProvider = Field(
        default="fastembed", description="fastembed (in-process), openai_compatible or fake."
    )
    model: str = Field(default="BAAI/bge-small-en-v1.5", min_length=1)
    dims: int = Field(default=384, ge=1, description="Vector dimension produced by the model.")
    base_url: str | None = Field(
        default=None,
        description="Endpoint for openai_compatible embeddings (Ollama or hosted); "
        "for example http://localhost:11434/v1.",
    )
    api_key: SecretValue | None = None
    batch_size: int = Field(default=32, ge=1, description="Texts per model call or request.")
    query_prefix: str = Field(
        default="",
        description="Text put before a search query when it is embedded "
        "(`lorebase-inference embed --query`); models such as E5 and BGE expect one, for "
        'example "query: " or "Represent this sentence for searching relevant passages: ".',
    )
    document_prefix: str = Field(
        default="",
        description="Text put before every Record text Scribe embeds, for models that expect "
        'one (E5: "passage: "). Part of the stored text hash and of the template fingerprint: '
        "after changing it the next embedding pass embeds every text again.",
    )
    cache_dir: Path | None = Field(
        default=None, description="fastembed: where model files are stored; unset: library default."
    )
    threads: int | None = Field(
        default=None, ge=1, description="fastembed: CPU threads; unset: library default."
    )
    timeout_s: float = Field(default=60, gt=0, description="openai_compatible request timeout.")
    trust_env: bool = Field(
        default=False,
        description="Honor HTTP_PROXY, HTTPS_PROXY and NO_PROXY (and other httpx environment "
        "settings) for the openai_compatible endpoint; false ignores them and connects directly.",
    )
    retry: RetrySettings = Field(default_factory=RetrySettings)

    @model_validator(mode="after")
    def _endpoint_required(self) -> Self:
        if self.provider == "openai_compatible" and not self.base_url:
            raise ValueError("base_url is required when provider is openai_compatible")
        return self


class ExtractorSettings(SectionModel):
    """One tier-1 local extractor."""

    enabled: bool = True
    model: str = Field(min_length=1, description="Checkpoint name; see the weight license note.")
    threshold: float = Field(default=0.5, ge=0, le=1)
    cache_dir: Path | None = Field(
        default=None, description="Where checkpoints are stored; unset: library default."
    )
    window_chars: int = Field(
        default=1500,
        ge=100,
        description="Longer texts are cut at whitespace into windows of at most this size, "
        "because the models read a few hundred tokens at a time.",
    )


class SpacySettings(SectionModel):
    """Optional spaCy pipeline."""

    enabled: bool = False
    pipeline: str = Field(default="en_core_web_sm", min_length=1)


class CooccurrenceSettings(SectionModel):
    """Limits of the deterministic relation fallback (`relation_provider: cooccurrence`)."""

    max_pairs_per_sentence: int = Field(
        default=200, ge=1, description="Candidate pairs kept per sentence, nearest first."
    )


class LocalExtractorSettings(SectionModel):
    """Tier-1 extractors; check each checkpoint's weight license before enabling."""

    provider: SpanProvider = Field(default="gliner", description="gliner or fake (tests).")
    relation_provider: RelationProvider = Field(
        default="glirel",
        description="glirel, or cooccurrence (deterministic pairing of spans that share a "
        "sentence, no model), or fake (same as cooccurrence, for tests; the default when "
        "`provider` is fake).",
    )
    gliner: ExtractorSettings = ExtractorSettings(model="urchade/gliner_multi-v2.1")
    glirel: ExtractorSettings = ExtractorSettings(model="jackboyla/glirel-large-v0")
    cooccurrence: CooccurrenceSettings = Field(default_factory=CooccurrenceSettings)
    spacy: SpacySettings = Field(default_factory=SpacySettings)

    @model_validator(mode="before")
    @classmethod
    def _fake_provider_implies_fake_relations(cls, data: Any) -> Any:
        """`provider: fake` selects the offline relation provider too, unless one is named."""
        if isinstance(data, dict) and data.get("provider") == "fake":
            return {"relation_provider": "fake", **data}
        return data


class FakeSettings(SectionModel):
    """Behavior of `provider: fake`, used by tests and offline demos."""

    fixtures_dir: Path | None = Field(
        default=None,
        description="Directory of `<prompt-hash>.json` files holding canned chat answers.",
    )
    dictionary: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Label name -> surface forms matched by the fake span extractor.",
    )
    max_schema_depth: int = Field(
        default=6, ge=1, description="Nesting depth beyond which optional properties are omitted."
    )


class InferenceSettings(SectionModel):
    """All model-related configuration. Used by Scribe only."""

    llm: LlmSettings = Field(default_factory=LlmSettings)
    embeddings: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    local_extractors: LocalExtractorSettings = Field(default_factory=LocalExtractorSettings)
    fake: FakeSettings = Field(default_factory=FakeSettings)
    mode_memory_key: SecretValue | None = Field(
        default=None,
        description="Secret that keys the digests under which the structured output mode of "
        "each chat endpoint is remembered across restarts, e.g. secret://env/LOREBASE_MODE_KEY; "
        "without it the digests are keyed by a fixed label only. Changing it forgets the modes.",
    )
    mode_memory_ttl_s: float = Field(
        default=7 * 24 * 3600,
        gt=0,
        description="Seconds a remembered structured output mode counts; after that the "
        "endpoint is tried with the configured mode again.",
    )


register_section(SECTION, InferenceSettings, env_aliases=ENV_ALIASES)


def inference_settings(settings: Settings) -> InferenceSettings:
    """The validated `inference` section of loaded settings."""
    return settings.section(SECTION, InferenceSettings)
