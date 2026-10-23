# Guide: the optional exploration assistant

Guide answers questions about a Domain by reading it, step by step, with a small read-only agent,
and embeds query texts for semantic search. It is an optional service: Keeper, Herald, Scribe and
the web app work the same without it.

- Package: `packages/guide` (`lorebase_guide`), console script `lorebase-guide`.
- Endpoints: `POST /embed`, `POST /explore` (server-sent events), `GET /healthz`, `GET /readyz`.
- Settings: the `guide` section ([configuration reference](reference/configuration.md)); the
  models come from the `inference` section.
- Deployment: compose profile `assistant` (service `guide`), Helm value `guide.enabled`
  (off by default), runtime web configuration `guideUrl` (`VITE_GUIDE_URL`).

## Why it is separate from Keeper

Keeper and Herald never call a model ([design 2.3](design.md#23-anti-goals-things-lorebase-refuses-to-do),
AC-30): the read path is deterministic, cheap and auditable, and the build refuses an import of
`lorebase_inference` from either of them. Guide is the other side of that line. Besides Scribe it
is the only package allowed to use `lorebase_inference`, and it reaches Lorebase only through
Keeper's REST API with the Python client, exactly like an external agent:

- it has no database credential and no Keeper credential of its own;
- every read carries the caller's credential, so RBAC, sensitivity masking, row-level security,
  quotas, rate limits and the audit of reads apply to the user exactly as they would without
  Guide in between;
- it has no write tool: it cannot run Operations, open Proposals or change a branch;
- turning it off removes model cost from interactive use entirely.

The import contracts (`.importlinter`) enforce the direction: `guide` may import `platform`,
`inference` and `client`; `keeper`, `herald`, `store` and `core` may not import `guide`.

## Endpoints

### `POST /embed`

```json
{"text": "late deliveries from Italian suppliers", "mode": "query"}
```

Answers `{"model", "dims", "mode", "vector"}`. `mode: query` puts
`inference.embeddings.query_prefix` before the text; `mode: document` puts `document_prefix`, as
Scribe does before storing a Record's vector, so the same text gives the same vector. `model` is
the name vectors are stored under, which Keeper advertises in `GET .../schema` (`search.embedding`);
compare both before using the vector in `query_vector`. The text is limited to
`guide.limits.max_embed_chars`.

### `POST /explore`

```json
{"domain": "supply", "branch": null, "question": "Which suppliers of Contract C-9 are in Italy?",
 "max_steps": 6, "budget": 20000}
```

`max_steps` and `budget` (tokens) can only lower the configured budgets. The answer is a stream of
server-sent events, each a JSON object:

| Event | Data |
|-------|------|
| `session` | `session_id` of the agent trace, `domain`, `branch`, `model` (the configured chat model; null when the provider does not name one), `max_steps` (effective step budget of this run) |
| `thought` | `step`, `text`: the model's short reasoning note before its next action |
| `tool_call` | `step`, `tool`, `args`: one Keeper read |
| `tool_result` | `step`, `tool`, `ok`, `summary`, `refs` (Records the read returned) |
| `error` | `code` (`budget`, `timeout`, `model`), `message`; an `answer` follows |
| `answer` | `text`, `citations` (`ref`, `kind`, `key`), `found` |
| `done` | `session_id`, `steps`, `outcome`, `tokens` (`input`, `output`), `elapsed_ms` |

The tools are reads only: `schema`, `search` (lexical, or semantic and hybrid with a query vector
from the same embedding model when Keeper advertises it), `query` (the query DSL), `get`, `links`,
`neighborhood`, `traverse`, `history`, `lineage` and `context` (a context pack). An answer may only
cite Records that a tool returned during the exploration; when the model cites nothing it was
shown, or says the graph does not hold the answer, the answer is `Not found in the graph.` with
`found: false`.

Errors before the stream starts are problem documents (`application/problem+json`) with a `code`:
401 `unauthorized`, 403 `origin_not_allowed`, `cross_site` or `csrf`, 411, 413, 422, 429 `rate`,
`concurrent` or `busy` (with `Retry-After`), 502 `keeper_unavailable`.

## Authentication

Guide accepts the same credentials as Keeper and forwards them unchanged; it never substitutes a
credential of its own.

| Caller | Sends | Guide forwards |
|--------|-------|----------------|
| Web app in token mode, CLI, scripts | `Authorization: Bearer <OIDC token or API key>` | the same header |
| Web app in session mode, with Guide behind the web app's origin | Keeper's session cookie and the session's CSRF token in `X-CSRF-Token` | that one cookie and the token, nothing else |
| Local stack with Keeper in auth mode none | nothing, only with `guide.http.allow_anonymous: true` | nothing |

Before any model call, and before the caller counts against any limit, Guide asks Keeper who the
caller is (`GET /auth/whoami` with the forwarded credential): an unknown, revoked or expired
credential is refused with 401 and costs no token. At most
`guide.limits.max_concurrent_verifications` such checks run at once; further requests are
refused with 429 `busy`. A session cookie whose value is not shaped like a Keeper session id is
refused without being forwarded. For a
session cookie Guide also reads the session's CSRF token from Keeper (`GET /auth/session`) and
refuses the request unless it matches the header: Keeper checks CSRF only on unsafe methods, and
an exploration makes reads, so without this check a cross-site page could spend the user's model
budget with the cookie alone. The cookie name is `guide.http.session_cookie` (with
`keeper.auth.sessions.cookie_secure`, the browser holds `__Host-<cookie_name>`); session mode needs
Guide under the same site as the web app, for example behind the same reverse proxy, because the
browser sends the cookie to that origin only.

Browser requests must come from an origin listed in `guide.http.allowed_origins`, which is also the
CORS allow-list; a request whose `Origin` is not listed, or marked `Sec-Fetch-Site: cross-site`
without an `Origin`, is refused with 403. While `allowed_origins` is empty, `guide.http.default_origins`
applies instead; the compose file sets it to the origins of its `web` service, so `GUIDE_ALLOWED_ORIGINS=[]`
in `.env` behaves as unset and Guide logs a warning at start.

## Agent trace

Each exploration is one agent trace session (`guide-<hex>`). Every Keeper request it makes carries
`X-Lorebase-Session`, the opt-in `X-Lorebase-Trace: on` and `X-Lorebase-Tool: guide.<tool>`, so
Keeper's agent trace records the timeline of the reads, filtered for the viewer like any trace. The
model's reasoning notes are posted to `POST /api/v1/trace/sessions/{session}/notes` with the
caller's credential and the Domain id. Posting a note is a write (to Keeper's trace store, not to
the Domain); `guide.trace.notes: false` keeps the trace headers and posts no note. Notes are
model output shaped by graph data, so whoever displays them (the trace timeline, the Explore
panel) must render them as untrusted plain text. Notes are best effort: a Keeper with tracing off or without
the route never fails an exploration, and after a refusal no further note of that session is sent.
Header names, the notes path and the note length are in `guide.trace`; `guide.trace.enabled: false`
sends neither headers nor notes. The `session` event gives the web app the id to link to the
session's timeline.

## The Explore panel in the web app

The web app asks Guide from **Explore**, a screen of its own in the navigation
(`/<domain>/explore`); the Retrieval lab links to it ("Ask the guide"). It appears when the
runtime configuration has `guideUrl`; against the mock API it plays scripted explorations
instead, so it can be tried without Guide.

![Explore](images/explore.png)

- **Request.** `POST {guideUrl}/explore` with the Domain, the Branch on screen (`null` for the
  default one), the question and the panel's max steps (`VITE_EXPLORE_MAX_STEPS`, 6; Guide only
  ever lowers its own budget). `guideUrl` is an absolute origin (`https://guide.example.com`) or
  a path on the app's origin (`/guide` behind the web proxy); session mode needs the second.
- **Credentials.** In token mode the same `Authorization: Bearer` header Keeper gets; in session
  mode the session cookie (`credentials: include`) and the CSRF header of the session. The
  client holds no credential or setting of Guide's.
- **Streaming.** The response is read with `fetch` and a stream reader, since `EventSource`
  cannot POST. Each event is shown as it arrives: reasoning notes, tool calls with their
  arguments on one line, results with their summary and the number of Records, an `error`
  event, then the answer and the totals of `done` (steps, seconds, tokens). The model name is
  shown when an event carries `model`.
- **Stop and limits.** Stop aborts the request, which closes the stream; Guide stops working
  on the exploration. The panel stops a stream that runs longer than `VITE_EXPLORE_TIMEOUT_MS`
  (150 s, above `guide.agent.timeout_s`). A 429 shows its reason and disables asking until
  `Retry-After` has passed; other refusals show what the `code` means.
- **Untrusted text.** Notes, summaries, arguments and answers are rendered as plain text: no
  HTML, no Markdown, no links or images, whatever they contain. Only `citations` become links,
  to the Record pages of the app, when the Kind is a valid name of the Domain's schema and the
  key holds no control character; others are shown as text. "Show cited Records on the Canvas"
  draws them with their neighbors.
- **Trace.** "View trace" opens the session of the `session` event in Agent activity
  (`/<domain>/agent-activity?session=<id>`).
- **Example questions.** The panel offers the questions a Domain declares in its schema
  (`domain.examples`, see [schema as code](design.md#513-schema-as-code)) as buttons, and
  nothing when it declares none; the Retrieval lab offers the same questions as quick picks.
  The app itself holds no questions of any dataset. The `supply` example declares the
  questions of the example agent: single-source risk, tier-1 suppliers at high risk with open
  orders, what if S-1007 stops, the history of C-3013 and a misspelled search ("bering").

## Budgets and limits

| Setting | Default | Bounds |
|---------|---------|--------|
| `guide.agent.max_steps` | 8 | tool calls; then one last turn to answer |
| `guide.agent.max_tokens_per_call` | 1024 | completion cap of one model call, never above what remains of the total after the estimated input |
| `guide.agent.max_total_tokens` | 40000 | input plus output tokens of one exploration |
| `guide.agent.timeout_s` | 120 | wall clock of one exploration, model calls and Keeper reads alike |
| `guide.limits.explorations_per_minute` | 6 | per credential |
| `guide.limits.max_concurrent_per_caller` | 1 | per credential |
| `guide.limits.max_concurrent` | 4 | per process |
| `guide.limits.embeds_per_minute` | 120 | per credential |

Before each model call Guide estimates the input (prompt, accumulated messages and tool results)
from its length: 4 characters per token for the first call, then the ratio the endpoint's own
count of the previous call gives (JSON tool results measured about 2.2 characters per token on
Claude models). The call is made only if used plus estimated input still fits in
`max_total_tokens`; otherwise the exploration ends as below without calling the model. The
completion cap is cut to what remains after the estimate, so the total stays within the budget up
to the estimate's error; an answer of the final turn is kept even when the endpoint counted more
than estimated. Before each tool step Guide also checks that the budget covers that
step, the next call with a full tool result (`max_result_chars`) and an answer of
`max_tokens_per_call`; when it does not, that turn is the last one, as when `max_steps` is
reached: the model answers from what it has read, citing it, instead of the exploration
stopping with nothing. Only when even an answer no longer fits does the stream end with an
`error` event (`budget`), a `Not found in the graph.` answer and `done`. Rate windows and concurrency slots are keyed on the principal Keeper reported, so
several keys of one principal share them, and unverified credentials never enter them; the
window table holds at most `guide.limits.max_tracked_callers` callers, and while it is full of
callers active in the last minute a new caller is refused with 429 `busy`. Limits are per
process: with several replicas each enforces its own, and Keeper's rate
limits and quotas apply to the reads on top. Tool results shown to the model are bounded too
(`max_result_chars`, `max_value_chars`, `max_items`, `max_depth`, `max_nodes`,
`max_context_tokens`).

## Setup with Ollama

The default chat model is the local Ollama one of `inference.llm`; Guide uses the task named by
`guide.agent.task` (`explore`), so the exploration model can differ from the extraction one:

```yaml
inference:
  llm:
    base_url: http://ollama:11434/v1
    model: qwen2.5:7b-instruct
    tasks:
      explore: {model: qwen2.5:14b-instruct, max_concurrency: 2}
guide:
  keeper: {base_url: http://keeper:8080}
  http: {allowed_origins: ["http://localhost:5173"]}
```

```bash
make up PROFILES=assistant      # Guide and Scribe, with the web app pointed at /guide
docker compose --env-file .env -f deploy/compose/docker-compose.yaml \
  --profile local-models up -d ollama ollama-pull     # the model server, when it runs in compose
```

`make up PROFILES=assistant` (or `make up-assistant`) builds the `lorebase-ml` image for Guide and
Scribe, starts both next to the default services and adds the overlay
`deploy/compose/assistant.yaml`: the web app reads Guide at `/guide` and Guide accepts calls
without credentials, which suits only a Keeper in auth mode none ([quickstart](quickstart.md#27-guide-scribe-and-the-example-documents)).
Without the helper, add `--profile assistant` and `-f deploy/compose/assistant.yaml` to the
compose command. `make down` stops the profile too.

`/embed` and semantic search need the embedding model Scribe uses. The `lorebase-ml` image
(target `backend-ml` of `deploy/docker/backend.Dockerfile`) is the backend image plus fastembed
and one embedding model baked in at build time (`EMBEDDING_MODEL`, default
`BAAI/bge-small-en-v1.5`), so Guide and Scribe embed offline with a read-only root file system.
It is 391 MB larger than the backend image, which is why only Guide and Scribe use it; in the
Helm chart it is `imageMl`. Or point `inference.embeddings` at an OpenAI-compatible endpoint and
use the backend image. Without a usable embedding model, `/embed` answers 502 and the agent
searches lexically.

## Setup with Anthropic

Anthropic serves an OpenAI-compatible endpoint, so no vendor library is involved
([inference providers](inference-providers.md#anthropic) explains each option). Send only the
exploration task there, and keep the key in a file (a Kubernetes or compose secret) referenced
with `secret://file`; never put it in the YAML or in an environment variable:

```yaml
inference:
  llm:
    tasks:
      explore:
        base_url: https://api.anthropic.com/v1/
        model: claude-haiku-5-5
        api_key: secret://file//run/secrets/anthropic_api_key
        headers:                           # organization-level keys only
          anthropic-workspace-id: secret://file//run/secrets/anthropic_workspace_id
        temperature: null
        json_schema_strict: true
        extra_body: {thinking: {type: disabled}}
        price_per_million_tokens: {input: 0.10, output: 0.50}
guide:
  agent:
    max_tokens_per_call: 2048              # 4096 with claude-sonnet-5-5 or claude-opus-5-5
```

Which model, from the [live runs](validation.md#explore-guide) (eight questions on the `supply`
sample, three runs each, answers checked against the data):

| Model | `extra_body` | Mean score | Cost per question | Median time |
|-------|--------------|-----------|-------------------|-------------|
| `claude-haiku-5-5` | `{thinking: {type: disabled}}` | 0.83 | 0.003 USD | 11 s |
| `claude-sonnet-5-5` | `{thinking: {type: between_tools}}` | 0.85 | 0.058 USD | 16 s |
| `claude-opus-5-5` | `{reasoning_effort: low}` | 0.91 | 0.10 USD | 20 s |

Haiku 5.5 is the recommended exploration model for hosted setups: it answered every lookup and
most multi-hop questions correctly at a twentieth of Sonnet's cost. Opus 5.5 was the most
accurate on the multi-hop questions; use it where an answer's completeness matters more than
cost. Sonnet 5.5 was not better than Haiku on these questions. With `max_tokens_per_call` at
2048, 3 of 48 Sonnet and Opus runs ended with `error` `model` (an answer that did not parse
after the repair call, most likely cut off by the cap: Opus thinks within it); at 4096 none of
16 further runs did. Guide's log line `model call failed` now names the schema problems
(`/: json` for an answer that is not JSON) and counts the tokens of such calls. The default budgets fit: runs used
1 to 8 steps and at most 37,000 tokens.

A task on another `base_url` uses only its own `api_key` and `headers`: the main key and headers
are never sent to an endpoint they were not configured for. The step schema has a free-form
`args` object, which the strict mode of the endpoint may refuse; the client then steps down to
prompt-only structured output and every answer is still validated locally. The mode an endpoint
accepts is remembered per endpoint, model and output settings (a SHA-256 of them, never the
address) in `guide.mode_memory_file` under `guide.data_dir`
(`~/.local/state/lorebase/guide/structured_output_modes.json` by default), written atomically
under an exclusive lock (a `.lock` file next to it), so a restarted Guide does not try the
refused mode again. Entries carry a timestamp and expire after `inference.mode_memory_ttl_s`
(7 days); `lorebase-guide modes clear` deletes the file's entries. The compose stack mounts the volume
`guide-data` at `/var/lib/lorebase/guide` and the Helm chart an emptyDir
(`guide.persistence.enabled` keeps it on a claim); mount a volume there in other containers to
keep it; when the file cannot be read or written, Guide logs a warning and remembers the mode
for the life of the process only. Embeddings stay on fastembed or another provider: Anthropic has none.

## Security model

- **Read-only by construction.** The tool catalogue is closed and holds reads only; arguments are
  validated before a request is made; there is no URL fetching and no tool reaches anything but
  Keeper. An unknown tool name is reported to the model as an error and does nothing.
- **Caller's authority only.** Every Keeper request carries the caller's own credential, checked
  by Keeper before the first model call. A reader limited to one Kind sees, through Guide, exactly
  what that reader sees without it.
- **Graph data is untrusted.** Records, titles, snippets and change messages may come from anyone
  allowed to write, or from documents. Each tool result reaches the model inside a block delimited
  by a random marker chosen per exploration and removed from the data, so the data cannot close
  the block; the instructions say that nothing inside is an instruction. Whatever the model is led
  to do, it can only read with the caller's rights and answer: citations are limited to Records a
  tool returned data for.
- **Plain-text rendering.** `thought`, `summary` and `answer` texts are model output and may
  quote graph data: clients render them as plain text, never as HTML or Markdown with links or
  images. The web Explore panel shows answers as text and turns only the `citations` into links,
  to the Records' own pages inside the app; it never follows or renders a URL found in a text.
- **Cost.** Authentication before model work, per-credential rate and concurrency limits, a total
  concurrency cap, per-call and per-exploration token caps and a wall-clock timeout.
- **No values in logs.** Logs and metrics hold counts, tool names, outcomes and durations; never
  questions, data, answers or credentials. The access log has method, route, status and duration,
  and the HTTP client's own request log (which names URLs, so Record keys) is kept at WARNING.
- **Paths.** Record refs, relation names, Domains and branches become URL path segments only
  through the client, which percent-encodes them and refuses empty, `.` and `..` segments, so no
  value can move a request to another Keeper route.
- **Network.** Guide needs Keeper and the model endpoint only; the Helm network policy allows
  nothing else.

## Observability

Traces (`guide.model`, `guide.tool` spans and one server span per request) and metrics
`lorebase.guide.requests`, `lorebase.guide.request.duration`, `lorebase.guide.explorations`
(by outcome), `lorebase.guide.steps` (by tool and success), `lorebase.guide.tokens` (by
direction), `lorebase.guide.embeddings` and `lorebase.guide.refused` (by reason), exported when
`telemetry.otlp_endpoint` is set. `/readyz` checks that Keeper answers `/healthz`.
