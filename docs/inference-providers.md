# Inference providers

Lorebase talks to models through one package, `lorebase_inference`, used by Scribe only
([design 9.4](design.md#94-model-agnostic-inference)). Nothing else in the product calls a model.
There is no vendor SDK: one HTTP client speaks the chat completions and embeddings protocol,
and everything that differs between a laptop and an enterprise is configuration.

| Need | Provider | Runs | Selected by |
|------|----------|------|-------------|
| Chat with schema-valid JSON (tier 2) | `openai_compatible` | any server or proxy speaking the protocol | `inference.llm` |
| Embeddings | `fastembed` (default) | in-process, CPU, ONNX | `inference.embeddings` |
| Embeddings | `openai_compatible` | same endpoints as chat | `inference.embeddings` |
| Entity spans (tier 1) | `gliner` | in-process, CPU | `inference.local_extractors` |
| Relation candidates (tier 1) | `glirel`, or `cooccurrence` fallback | in-process, CPU / pure Python | `inference.local_extractors` |
| Tests, offline demos | `fake` | pure Python, no network | `provider: fake` |

Provider names live in `inference` settings and in the registry only; an architecture test
fails if they leak into other modules.

## Run it free, locally

Ollama for chat, fastembed for embeddings, GLiNER and GLiREL for tier 1. No API keys, no
per-token cost.

```bash
# 1. a chat model served locally (any model Ollama has; this is the default)
ollama pull qwen2.5:7b-instruct

# 2. the optional local models (large download: PyTorch for the extractors)
uv sync --all-packages --extra fastembed --extra local-extractors
#    or, outside the workspace:  pip install 'lorebase-inference[fastembed,local-extractors]'

# 3. verify the chat and embedding providers (the only command that touches the network)
python -m lorebase_inference check
```

The defaults already point there (`http://localhost:11434/v1`, `qwen2.5:7b-instruct`,
`BAAI/bge-small-en-v1.5`, 384 dimensions), so a fresh checkout needs no configuration. Output
of `check` (here with `LLM_MODEL=qwen3:4b`; the first fastembed call also downloads the model):

```
chat       ok     provider=openai_compatible model=qwen3:4b latency_ms=5304 tokens=218/6 structured_output=json_schema
embeddings ok     provider=fastembed model=BAAI/bge-small-en-v1.5 dims=384 latency_ms=591
```

It exits 1 when a provider fails, and `--no-chat`, `--no-embeddings`, `--task NAME` and `--json`
narrow or reshape it. Model files are downloaded on first use into the library's cache
directory (set `inference.embeddings.cache_dir` and
`inference.local_extractors.<name>.cache_dir` to choose another, for example a volume).

**Ollama for embeddings too**, instead of fastembed (no extra to install):

```yaml
inference:
  embeddings:
    provider: openai_compatible
    base_url: http://localhost:11434/v1
    model: nomic-embed-text      # ollama pull nomic-embed-text
    dims: 768                    # must match; a mismatch fails fast
```

**With Docker Compose**, `docker compose --profile local-models up` starts Ollama and pulls
`OLLAMA_MODEL` from the environment file. Services reach it at `http://ollama:11434/v1`
(`LLM_BASE_URL` in `.env.example`). Probe it with:

```bash
docker compose --profile local-models run --rm inference-check
```

The stock image has no local models. To run fastembed and the extractors in containers, build
with `UV_SYNC_ARGS="--all-packages --extra fastembed --extra local-extractors"` (set it in
`.env`; see `.env.example`).

## Switch to a paid provider: configuration only

The default chat model is selected by three variables. Change them, restart, done:

```bash
export LLM_BASE_URL=https://api.example-provider.com/v1   # any OpenAI-compatible endpoint
export LLM_MODEL=<model name>
export LLM_API_KEY=secret-or-reference                    # or secret://env/NAME, secret://file//path
```

The same in YAML, with a price so that job budgets can count cost:

```yaml
inference:
  llm:
    base_url: https://api.example-provider.com/v1
    model: <model name>
    api_key: secret://env/LLM_API_KEY
    price_per_million_tokens: {input: <price>, output: <price>}
```

Per-task routing sends bulk extraction to a small local model and schema discovery to a larger
hosted one, each with its own endpoint, key and price:

```yaml
inference:
  llm:
    model: qwen2.5:7b-instruct          # default: local
    tasks:
      schema_discovery:
        base_url: https://api.example-provider.com/v1
        model: <model name>
        api_key: secret://env/HOSTED_KEY
        price_per_million_tokens: {input: <price>, output: <price>}
```

Hosted embeddings work the same way (`embeddings.provider: openai_compatible`, `base_url`,
`api_key`, `model`, `dims`). The embedding model name keys the stored vectors, so switching
model creates new vectors next to the old ones ([design 9.3](design.md#93-embedding-jobs)).

## LiteLLM for routing and non-compatible providers

Providers that do not speak the protocol natively (Anthropic, Google, Amazon Bedrock, and
others), or an organisation that wants central keys, budgets and fallbacks, put a LiteLLM
proxy in front. Lorebase treats it as one more compatible endpoint and never imports a
provider library.

```bash
# .env
LITELLM_MASTER_KEY=sk-<48 hex characters>     # printf 'sk-%s\n' "$(openssl rand -hex 24)"
LITELLM_UPSTREAM_MODEL=<provider>/<model>      # LiteLLM notation
LITELLM_UPSTREAM_API_KEY=<provider key>
docker compose --profile litellm up -d litellm
```

The proxy does not start while `LITELLM_MASTER_KEY` is empty, still `change-me` or shorter
than 16 characters: the one-shot `litellm-secrets` exits with the reason
(`docker compose logs litellm-secrets`). Grafana of the `observability` profile has the same
guard for `GRAFANA_ADMIN_PASSWORD` (`grafana-secrets`).

```yaml
inference:
  llm:
    base_url: http://litellm:4000/v1
    model: default                      # the alias in deploy/compose/litellm.yaml
    api_key: secret://env/LITELLM_MASTER_KEY
```

`deploy/compose/litellm.two-providers.yaml` is a second example with a primary and a fallback
provider, each with its own model and key from the environment; select it with
`LITELLM_CONFIG_FILE=./litellm.two-providers.yaml`. When `default` keeps failing, the proxy
answers from `fallback`. Retries, timeouts and fallbacks are the proxy's; Lorebase's own
retries (below) apply on top for 429 and 5xx answers of the proxy itself.

To probe the proxy: `docker compose --profile litellm run --rm --no-deps -e
LLM_BASE_URL=http://litellm:4000/v1 -e LLM_MODEL=default -e LLM_API_KEY=$LITELLM_MASTER_KEY
inference-check`.

## Endpoints

The client uses `POST {base_url}/chat/completions` and `POST {base_url}/embeddings`, nothing
else, with `Authorization: Bearer <key>` only when a key is configured.

| Endpoint | Chat | Embeddings | Verified |
|----------|------|------------|----------|
| Ollama (0.5 or later for `json_schema`) | yes | yes | live: chat with `json_schema` and embeddings, Ollama 0.32 |
| LiteLLM proxy | yes | yes | protocol level (mock server), not live |
| vLLM, llama.cpp server, LM Studio | yes | where the server implements `/embeddings` | protocol level, not live |
| Hosted OpenAI-compatible endpoints | yes | yes | protocol level, not live |

"Protocol level" means the behaviours below are covered by tests against a scripted HTTP
server, including servers that reject `response_format`. Provider endpoints evolve; run
`python -m lorebase_inference check` against yours before relying on it.

### Structured output

The answer is JSON conforming to the schema the caller passes. The client asks for it in
decreasing order of strength and validates the result locally, always:

1. `json_schema`: `response_format` carrying the schema (`structured_output: json_schema`, the default).
2. `json_mode`: `response_format: {type: json_object}` with the schema in the system prompt.
3. prompt only: no `response_format`, schema and a strict instruction in the system prompt.

When the server answers HTTP 400 or 422 with an error that names the response format, the
client steps down one mode and keeps it for that process (an unrelated 400, for example a
context overflow, is not mistaken for missing support). An answer that is not JSON, or does not
satisfy the schema, triggers up to `repair_attempts` (default 1) repair calls that show the
model what was wrong; if the answer is still invalid, `InvalidOutputError` is raised and the
caller discards and counts the item ([design 9.2](design.md#92-unstructured-extraction), step 6).
Reasoning blocks (`<think>...</think>`) and code fences around the JSON are tolerated.
Results carry the summed token usage of all calls; when a server reports none, usage is
estimated from text length.

### Retries

HTTP 429, 500, 502, 503, 504, connection errors and timeouts are retried up to
`retry.max_retries` times. A `Retry-After` header (seconds or date) sets the wait, capped at
`retry.backoff_max_s`; otherwise the wait doubles from `backoff_base_s` with jitter. Other 4xx
errors are raised at once. Each model instance runs at most `max_concurrency` requests at a
time.

### Logging

Operational events (retries, mode step-downs) are logged. Prompts, documents, answers and keys
are never logged; error messages carry the status and the server's error text only.

## Tier 1: GLiNER and GLiREL

`gliner` finds entity spans for the labels passed on each call; `glirel` proposes relations
between spans that were already found, for relation labels passed on each call. Thresholds,
checkpoint names and cache directories come from `inference.local_extractors`. Both run on CPU
in the process, read a few hundred tokens at a time (longer texts are cut into windows of
`window_chars` at sentence ends, so pass chunks), and return exact character offsets.

**Weight licenses.** The libraries are Apache-2.0; checkpoints are separate artifacts with
their own terms. `urchade/gliner_multi-v2.1` declares Apache-2.0 on its model card.
`jackboyla/glirel-large-v0` declares no license on its card at the time of writing, and
checkpoints in this family are often trained on data with non-commercial terms. Read the card
and the training data terms of every checkpoint before using it commercially, and pin the
checkpoint you vetted in configuration.

**GLiREL is optional.** It shares the heavy extra with GLiNER, and `glirel` 1.2 does not load
checkpoints through `from_pretrained` with `huggingface_hub` 1.x. The provider detects that and
calls the library's own loader directly, which works with the releases tested but may break
with a future one. When GLiREL is missing, unusable or not worth its weight,
`inference.local_extractors.relation_provider: cooccurrence` selects a deterministic fallback
that needs no model: it pairs spans that share a sentence and emits each relation label whose
head and tail span labels satisfy the label's constraints. Its score is proximity,
`1 / (1 + words between the spans)`, not a probability, and it over-proposes by design:
downstream validation, evidence checks and review decide, as for every tier 1 candidate.
Sentences end at `.`, `!`, `?` followed by whitespace, and at blank lines.

## Settings reference

All keys live under `inference` in [config/lorebase.example.yaml](../config/lorebase.example.yaml);
every one can be set as `LOREBASE__INFERENCE__...` in the environment.

| Key | Default | Notes |
|-----|---------|-------|
| `llm.base_url` / `model` / `api_key` | `http://localhost:11434/v1` / `qwen2.5:7b-instruct` / none | aliases `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` |
| `llm.structured_output` | `json_schema` | `json_schema` or `json_mode`; steps down on rejection |
| `llm.repair_attempts` | 1 | 0 to 3 |
| `llm.temperature`, `llm.max_tokens` | 0, unset | per call overridable through `ChatOptions` |
| `llm.max_concurrency`, `llm.timeout_s` | 2, 120 | |
| `llm.retry.max_retries` / `backoff_base_s` / `backoff_max_s` | 3 / 0.5 / 30 | |
| `llm.tasks.<name>.*` | none | any of the `llm` keys above, per task; a task with another `base_url` uses only its own `api_key` |
| `embeddings.provider` / `model` / `dims` | `fastembed` / `BAAI/bge-small-en-v1.5` / 384 | |
| `embeddings.batch_size` | 32 | texts per model call or request |
| `embeddings.cache_dir`, `threads` | library defaults | fastembed |
| `embeddings.base_url`, `api_key`, `timeout_s`, `retry` | none | `openai_compatible` |
| `local_extractors.provider` / `relation_provider` | `gliner` / `glirel` | `fake` for tests; `cooccurrence` fallback |
| `local_extractors.gliner`, `.glirel` | see example | `model`, `threshold`, `cache_dir`, `window_chars` |
| `local_extractors.cooccurrence.max_pairs_per_sentence` | 200 | nearest pairs kept |

## Limits

- Chat has no tools and no streaming; one request per call.
- Vectors are fixed-size: `embeddings.dims` must equal what the model returns. A mismatch
  raises `DimensionMismatchError`; `check` reports it at deploy time.
- `json_schema` is not strict mode: schemas are sent as given, and the local validation is the
  guarantee. A server that rejects a particular schema feature in its message pins the process
  to a lower mode until restart.
- `Retry-After` longer than `backoff_max_s` is cut to it; raise the cap to wait longer.
- Usage is estimated (about four characters per token) when an endpoint reports none, so
  budgets from such endpoints are approximate.
- GLiNER and GLiREL truncate long inputs and miss entities that straddle a window cut. GLiREL
  scores are lower than a threshold of 0.5 suggests (a correct pair scored 0.44 in a manual
  run): tune `local_extractors.glirel.threshold` on your own text.
- The optional extras pull large dependencies (PyTorch and transformers for the extractors).
  Weights download on first use and need network access once; set the cache directories to
  mounted volumes in containers.
