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

**Reasoning models.** Some local models (LFM 2.5, Qwen 3 and others) think before they answer
and Ollama returns the thinking in a separate `reasoning` field. It still counts against
`max_tokens`: with a low cap the answer can be empty, and the client reports "token limit
reached before any answer". Raise `max_tokens`, or turn thinking off where the server supports
it, with a field the client passes through as is:

```yaml
inference:
  llm:
    model: lfm25-bench-16k:latest
    max_tokens: 4096
    extra_body: {reasoning_effort: none}   # Ollama: no thinking
```

**Ollama for embeddings too**, instead of fastembed (no extra to install):

```yaml
inference:
  embeddings:
    provider: openai_compatible
    base_url: http://localhost:11434/v1
    model: nomic-embed-text      # ollama pull nomic-embed-text
    dims: 768                    # must match; a mismatch fails fast
```

**With Docker Compose**, the `local-models` profile starts Ollama and pulls `OLLAMA_MODEL` from
the environment file. Services reach it at `http://ollama:11434/v1` (`LLM_BASE_URL` in
`.env.example`). Probe it from the repository root with:

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.yaml \
  --profile local-models run --rm inference-check
```

An Ollama already running on the host is reached at `http://host.docker.internal:11434/v1`; add
`--no-deps` so that the Ollama container is not started. A hosted key kept in a file goes through
the overlay `deploy/compose/hosted-model.yaml` ([quickstart](quickstart.md#4-use-a-real-model)).

For `make up PROFILES=assistant` (and `make logs`, `make down`, `scripts/quickstart.sh`) to start
Scribe with that key, list the overlay in `COMPOSE_OVERLAYS` in `.env`, with any overlay of your
own after it (space separated, relative to the repository root; keep your own under the ignored
`secrets/`):

```bash
# .env
LLM_BASE_URL=https://api.example-provider.com/v1
LLM_MODEL=<model name>
LLM_API_KEY=
COMPOSE_OVERLAYS=deploy/compose/hosted-model.yaml secrets/my-model.yaml
```

```yaml
# secrets/my-model.yaml: settings of your own on top of the hosted overlay
services:
  scribe:
    environment:
      LOREBASE__INFERENCE__LLM__TIMEOUT_S: "300"
```

Compose resolves relative paths inside an overlay from the directory of the first file,
`deploy/compose`, so a `secrets:` entry of your own names `../../secrets/<file>`.

The backend image has no local models. Scribe and Guide run on a second image, `lorebase-ml`
(target `backend-ml` of `deploy/docker/backend.Dockerfile`, 391 MB larger), that carries fastembed
and the embedding model `EMBEDDING_MODEL` (default `BAAI/bge-small-en-v1.5`) downloaded at build
time: embeddings work in compose and Helm with no setting and no network at run time. A model other
than the baked one downloads on first use into `embeddings.cache_dir`, which needs a writable
volume (the root file system is read-only). The tier 1 extractors are still an extra: build with
`UV_SYNC_ARGS="--extra local-extractors"` (set it in `.env`; see `.env.example`).

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

`headers` and `extra_body` cover what a provider needs beyond the protocol: extra HTTP headers
(values may be `secret://` references) and extra request fields merged into every request.
The client keeps `Authorization`, `Content-Type`, `Accept`, `Host`, `Content-Length`, and the
`model`, `messages`, `stream` and `response_format` fields to itself; settings that try to set
them are refused at startup. A task routed to another `base_url` never inherits the default
model's headers, as it never inherits its key.

Hosted embeddings work the same way (`embeddings.provider: openai_compatible`, `base_url`,
`api_key`, `model`, `dims`). The embedding model name keys the stored vectors, so switching
model creates new vectors next to the old ones ([design 9.3](design.md#93-embedding-jobs)).

## Anthropic

Anthropic serves an OpenAI-compatible chat endpoint at `https://api.anthropic.com/v1/`, so
Claude models need no proxy and no extra code. Checked live on 2026-10-22 with
`claude-haiku-5-5` ([validation](validation.md#live-model-runs)):

```yaml
inference:
  llm:
    base_url: https://api.anthropic.com/v1/
    model: claude-haiku-5-5
    api_key: secret://env/ANTHROPIC_API_KEY          # or secret://file//run/secrets/anthropic
    headers:
      anthropic-workspace-id: secret://env/ANTHROPIC_WORKSPACE_ID   # organization-level keys only
    temperature: null                  # current models refuse the parameter
    json_schema_strict: true           # the endpoint requires strict: true
    extra_body: {thinking: {type: disabled}}
    max_tokens: 4096
    price_per_million_tokens: {input: 0.10, output: 0.50}
```

What each line is for:

- **`anthropic-workspace-id`.** A key that is not scoped to a workspace is refused with HTTP
  400 ("must include the anthropic-workspace-id header") on every endpoint. Set the header,
  or use a key created inside a workspace and leave `headers` out.
- **`temperature: null`.** Current models answer HTTP 400 "`temperature` is deprecated for
  this model"; null leaves the field out of the request.
- **`json_schema_strict: true`.** The endpoint refuses `json_schema` without `strict: true`
  and has no `json_object` mode. Strict schemas are limited to 24 optional properties: an
  extraction schema over several Kinds has more, the request is refused, and the client steps
  down to prompt-only mode for that job (two rejected requests, not billed; the answers are
  still validated locally). Schemas within the limit are answered in `json_schema` mode.
  `structured_output: json_mode` steps down to prompt-only mode at the first call.
- **`thinking: disabled`.** Thinking is on by default for Haiku 5.5 and spends `max_tokens`
  before the answer: with 1024 tokens every extraction answer was empty. With thinking off,
  extraction answers took 1,700 to 4,100 output tokens; keep `max_tokens` at 4096.

The other current models need a different `extra_body` (checked live on 2026-10-22, every
other line of the block above unchanged; all three refuse `temperature`):

| Model | `extra_body` | Why |
|-------|--------------|-----|
| `claude-haiku-5-5` | `{thinking: {type: disabled}}` | as above |
| `claude-sonnet-5-5` | `{thinking: {type: between_tools}}` | `disabled` is refused with HTTP 400; `between_tools` turns thinking off |
| `claude-opus-5-5` | `{reasoning_effort: low}` | thinking cannot be turned off (`disabled` is refused); it counts against `max_tokens`, so give Opus 8192 for extraction |

Prices per million tokens (input / output), for `price_per_million_tokens`: Haiku 5.5
0.10 / 0.50, Sonnet 5.5 2 / 10, Opus 5.5 4 / 20.

### Which model for which task

From the [live runs](validation.md#live-model-runs) (exploration questions on the `supply`
sample, the process agent, tier 2 extraction on five documents). These are recommendations for
hosted setups; the shipped defaults stay on the local Ollama model.

| Task | Model | Why | Settings |
|------|-------|-----|----------|
| Exploration (Guide, `inference.llm.tasks.explore`) | `claude-haiku-5-5` | mean score 0.83 against 0.85 for Sonnet and 0.91 for Opus, at 0.003 USD and 11 s per question (Sonnet 0.058 USD, Opus 0.10 USD) | Guide's default budgets; `max_tokens_per_call` 2048, 4096 for Sonnet or Opus |
| Exploration where completeness matters more than cost | `claude-opus-5-5` | best on the multi-hop questions, no refused tool call | `max_tokens_per_call` 4096 |
| Process agent, read-only tasks | `claude-haiku-5-5` | correct conclusions; it stops earlier than the others when the token bound is low | `--max-total-tokens 150000` |
| Process agent, tasks that run Operations | `claude-sonnet-5-5` | correct Proposals without a refused call, at a third of Opus's cost; the writes stay behind review either way | the same |
| Extraction (tier 2) | `claude-haiku-5-5` | 0.011 USD for five documents; one wrong value in 27 items | `max_tokens` 4096 |
| Extraction where a wrong value costs more than tokens | `claude-sonnet-5-5` | no wrong value, 17 times Haiku's cost | `max_tokens` 4096 |
| Extraction of a new document set, for its schema suggestions | `claude-opus-5-5` | the most Links and the most useful suggestions (notice periods with support 2, an `Amendment` Kind) | `max_tokens` 8192 |

Send each task to its model with `inference.llm.tasks.<task>` (`explore`, `extraction`); the
process agent takes `--model` and `--extra-body`. The suggestions above came from the
observations of the tier 2 calls; the dedicated discovery pass
(`scribe.extraction.discovery.llm_pass`, task `schema_discovery`) was not part of the runs.

Anthropic has no embeddings endpoint: keep `fastembed` (or another provider) for embeddings.
To send only some tasks there, put the same keys under `inference.llm.tasks.<task>`.

## Embeddings for search

Scribe embeds the Records of Kinds that have an `embedding_template` with this model
([scribe](scribe.md#embeddings)). Keeper and Herald never call a model, so the query vector of
semantic and hybrid search is computed on the caller's side with the same settings:

```bash
lorebase-inference embed --query "late deliveries"
# {"model": "BAAI/bge-small-en-v1.5", "dims": 384, "vector": [...]}
lorebase-inference embed --query "late deliveries" | lorebase search "late deliveries" --vector-file -
```

`--query` puts `inference.embeddings.query_prefix` before the text; without it the text is
embedded as given.

**Prefixes.** Some models, the E5 family and BGE with instructions among them, are trained with
different prefixes for queries and documents. Set them next to the model; both default to
empty, which changes nothing:

```yaml
inference:
  embeddings:
    model: intfloat/multilingual-e5-small
    dims: 384
    query_prefix: "query: "
    document_prefix: "passage: "
```

`document_prefix` is put before every Record text Scribe embeds and is part of the stored text
hash, so changing it makes the next embedding pass embed every text again. `query_prefix` is
applied by `lorebase-inference embed --query` only: a caller that embeds queries with its own
code has to add the prefix itself.

**Which model to use.** `GET /api/v1/domains/{domain}/schema` and the MCP tool
`lorebase_schema` report `search.embedding` (`model`, `dims`) for a Domain that has a Kind with an
`embedding_template`, taken from `search.semantic.model` and `store.embeddings.dims`. Embed
queries with that model; a vector of another dimension is refused.

From Python:

```python
from lorebase_inference import create_embedding_model, inference_settings
from lorebase_platform.settings import load_settings

model = create_embedding_model(inference_settings(load_settings()))
[vector] = await model.embed(["late deliveries"])
# client.records.search(domain, SearchRequest(q="late deliveries", query_vector=vector))
```

Agents on MCP pass the vector as `query_vector` of `lorebase_search`; Herald does not embed
text. Keeper's `search.semantic.model` must be the name Scribe stores vectors under (the
provider's model name; `fake:<model>` with the fake provider), and `store.embeddings.dims` the
dimension of both.

## LiteLLM for routing and non-compatible providers

Providers that do not speak the protocol natively (Google, Amazon Bedrock, and others), or an organisation that wants central keys, budgets and fallbacks, put a LiteLLM
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
| Ollama (0.5 or later for `json_schema`) | yes | yes | live: chat with `json_schema` and embeddings, Ollama 0.32; extraction with LFM 2.5 8B on 2026-10-22 |
| Anthropic (`https://api.anthropic.com/v1/`) | yes | no | live: extraction with `claude-haiku-5-5` on 2026-10-22, see [Anthropic](#anthropic) |
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
client steps down one mode (an unrelated 400, for example a context overflow, is not mistaken
for missing support). The mode that works is remembered per `base_url`, model, `structured_output` and
`json_schema_strict`, in the process and across restarts (Scribe: table `inference_endpoint_modes`; Guide: a file,
see [guide](guide.md)), so the next extraction job starts there instead of being refused once
more. Entries are keyed by an HMAC-SHA256 of those values (`inference.mode_memory_key` is mixed
in when set; the address is never stored in the clear) and expire after
`inference.mode_memory_ttl_s` (7 days), when the configured mode is tried again.
`lorebase-scribe modes clear` and `lorebase-guide modes clear` forget them at once, for example
after an endpoint was upgraded. An answer that is not JSON, or does not
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
are never logged; error messages carry the status and an excerpt of the server's error text
only. The excerpt is scrubbed first: the configured key, `Bearer` values, `sk-...`-style keys
and long base64 or hex runs become `[redacted]`. For 401 and 403 no server text is kept, since
providers often echo the key there.

### Proxies

Proxy variables are ignored by default: each endpoint connects directly. Set `trust_env: true`
on `inference.llm` (or a task), or on `inference.embeddings`, to have the client honor
`HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY` (and the other `httpx` environment settings, such as
`SSL_CERT_FILE`).

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
| `llm.temperature`, `llm.max_tokens` | 0, unset | per call overridable through `ChatOptions`; `temperature: null` leaves it out |
| `llm.json_schema_strict` | unset | `response_format.json_schema.strict`; unset leaves it out |
| `llm.headers`, `llm.extra_body` | none | extra HTTP headers (secret references allowed) and request fields |
| `llm.max_concurrency`, `llm.timeout_s` | 2, 120 | |
| `llm.retry.max_retries` / `backoff_base_s` / `backoff_max_s` | 3 / 0.5 / 30 | |
| `llm.tasks.<name>.*` | none | any of the `llm` keys above, per task; a task with another `base_url` uses only its own `api_key` and `headers` |
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
  raises `DimensionMismatchError`; `check` reports it at deploy time, and embedding jobs fail
  without retries when it differs from `store.embeddings.dims`.
- Query vectors use the same text path as stored ones: models that expect a query prefix
  (some BGE and E5 variants) are not given one.
- `json_schema` is not strict mode unless `json_schema_strict` is set: schemas are sent as
  given, and the local validation is the guarantee. A server that rejects a particular schema
  feature in its message pins that model instance to a lower mode.
- `Retry-After` longer than `backoff_max_s` is cut to it; raise the cap to wait longer.
- Usage is estimated (about four characters per token) when an endpoint reports none, so
  budgets from such endpoints are approximate.
- GLiNER and GLiREL truncate long inputs and miss entities that straddle a window cut. GLiREL
  scores are lower than a threshold of 0.5 suggests (a correct pair scored 0.44 in a manual
  run): tune `local_extractors.glirel.threshold` on your own text.
- The optional extras pull large dependencies (PyTorch and transformers for the extractors).
  Weights download on first use and need network access once; set the cache directories to
  mounted volumes in containers.
