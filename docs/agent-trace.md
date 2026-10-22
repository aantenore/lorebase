# Agent trace

The agent trace records how an agent session moves through the graph: which tools it called,
the queries it sent, what came back (counts, not values), which Records and Links each answer
touched, how long it took and whether the response cache answered. An agent can attach notes
(its reasoning between two tool calls) to the same session. The web app shows a session as a
timeline with the touched subgraph on the Canvas (**Agent activity**).

![Agent activity](images/agent-activity.png)

Tracing is debugging, not compliance: it is off by default, opt-in per request or per
principal, written best effort off the request path and deleted after a retention period. For
evidence of who read what, use the [read audit](operations.md) (`keeper.auth.audit.reads`).
Keeper stays model-free: the trace only observes requests.

## Contract

These names are what agents, Herald and the example agent ([examples/agent](../examples/agent/README.md))
rely on. The header names are configuration (`keeper.trace.*_header`); the values below are the
defaults.

| What | Value |
| --- | --- |
| Session header | `X-Lorebase-Session: <session id>`, 1 to 128 characters of `A-Z a-z 0-9 . _ : -`. Explicit session ids always win. Without one, a traced request joins an inferred session (below); with a malformed one it is not traced. |
| Tool header | `X-Lorebase-Tool: <tool name>` (optional), for example the MCP tool that made the call. |
| Opt-in header | `X-Lorebase-Trace: on`, or `debug` to add query plans (needs `trace.explain`). |
| Trace context | W3C `traceparent` (optional); its trace id is stored with each step. |
| Notes endpoint | `POST /api/v1/trace/sessions/{session_id}/notes` with `{"text": "...", "step": 3, "domain_id": "supply"}` (`step` and `domain_id` optional). Answers `202 {"recorded": true}`, or `{"recorded": false}` when tracing is off. |
| Sessions | `GET /api/v1/trace/sessions?actor=&domain_id=&limit=&cursor=` |
| Session timeline (API) | `GET /api/v1/trace/sessions/{session_id}` (the caller's session; `?actor=<principal>` for another principal's) |
| Session timeline (web app) | `/{domain}/agent-activity?session={session_id}` |

A note is opaque text: stored as sent (at most `keeper.trace.max_note_chars` characters),
never interpreted and shown as plain text. Notes go through the same queue as calls, so a note
and the call it describes keep their order within a few milliseconds. Notes and calls are kept
per session and principal: post notes with the credential that makes the calls (through Herald,
the credential Herald forwards).

### Herald

Herald sends the session header, the tool header (the MCP tool name) and a `traceparent` on
every Keeper call. Each tool call is a span and each Keeper request a child span of it with its
own span id, in the caller's trace (or, when an OpenTelemetry span is active in Herald, that
span's context). The session id is the one the MCP client sends in `X-Lorebase-Session`, else
`herald.trace.session_id`, else the MCP session (`mcp-session-id`, stateful Streamable HTTP).
When none applies (stateless HTTP, stdio without `session_id`) Herald sends no session id and
Keeper infers the session. Herald forwards an incoming `X-Lorebase-Trace` header, and sends
`on` itself with `herald.trace.opt_in: true`. See [MCP server](mcp.md) and `herald.trace` in
`config/lorebase.example.yaml`.

### Guide and the example agent

[Guide](guide.md) traces each exploration as one session (`guide-<hex>`) with these header
names and posts the model's reasoning to the notes endpoint (`guide.trace`). The
[example agent](../examples/agent/README.md) does the same with `agent-<hex>` sessions,
through Herald. Both default to the values above; a test keeps Guide's defaults equal to
Keeper's routes and settings.

### Inferred sessions

A traced request without a session id is not dropped and not given a session of its own: Keeper
groups it with the previous calls of the same client. The client is the principal plus a
fingerprint of its credential (the API key id, else a digest of the web session, else the
authentication method) and of its `User-Agent`; only digests are stored. A call joins the
inferred session of its client that saw a call at most `keeper.trace.inferred_session_idle_s`
(300 s) earlier; after a longer gap a new one starts. Inferred sessions have ids
`inferred-<16 hex>`, are marked `inferred: true` in the API and with a badge in the web app
(grouped heuristically: two agents behind the same key and user agent land in one session), and
are never shared across principals. An explicit session id always wins. Notes need an explicit
session id, so an agent that wants its reasoning interleaved sends one.

### Python client and CLI

```python
from lorebase_client import LorebaseClient

client = LorebaseClient(base_url, api_key=key, session_id="agent-42", trace="on", tool="my-agent")
client.search("supply", q="bearing")              # traced under agent-42
client.trace.add_note("agent-42", "checking suppliers next", domain_id="supply")
timeline = client.trace.session("agent-42")
```

```sh
lorebase trace list
lorebase trace show agent-42
```

## What a step holds

| Field | Content |
| --- | --- |
| `step`, `at`, `seq` | Position in the timeline, request start, write order. |
| `tool`, `method`, `route` | Tool header, HTTP method, route template (values unexpanded). |
| `status`, `latency_ms`, `cache` | Answer status, time in Keeper, response cache `hit`, `miss`, `bypass` or `not_modified`. |
| `query` | Reads only: the request body (`body`) and query parameters (`params`) as structured data: filters, traversal spec, search text, mode, limits. |
| `result` | Reads only: counts of the lists in the answer, search `mode` and `corrections`, context pack `budget`, `truncated`, `has_more`. |
| `refs` | Records (`kind`, `key`) and Links (`relation`, `from`, `to`) the answer touched, at most `max_refs`. |
| `explain` | With `X-Lorebase-Trace: debug` and `trace.explain`: plan summaries (node types, indexes and relations scanned, estimated rows and cost) of the query and search statements. `EXPLAIN` only plans; nothing runs twice. |
| `proposal_id` | Writes: the Proposal the answer names (opened or acted on). The timeline links it. |
| `note` | Notes only: the text. |

Never stored: values of Records, titles and snippets, response bodies, credentials, request
bodies of writes. In the query, values compared by `attr` and `derived` predicates are replaced
by their type (`{"redacted": "string"}`) unless `capture_literals: true`, query vectors by
their dimension, cursors are dropped and long strings cut (`max_text_chars`). Search text
(`q`, `search`, `match`) is kept, since it is what an agent asked; `capture_search_text: false`
keeps only its length and drops the search corrections, which name the query words. Members named `values` are never read when refs are collected, so
references held in values are not refs.

## Who sees what

A session id names one session per principal: `GET /trace/sessions/{id}` reads the caller's own
session unless `actor` names another principal (the sessions list gives it), so another
principal reusing the id cannot slip steps or notes into the timeline you open.

- Every principal sees its own steps and notes in full (an API key limited to Domains: in those
  Domains, and the notes).
- Steps of other principals in a Domain: principals holding `trace.read` there through a
  Domain-wide grant (the built-in `admin` role holds it; custom roles can grant it). Refs are
  filtered to the Kinds the reader may read. The query, the search corrections and the text of
  notes are what another principal typed and may hold values (a name searched for, a filter
  value with `capture_literals`, a model's reasoning), so they are shown only to a reader with
  Domain-wide `data.read` and every sensitivity clearance (`pii.read`, `confidential.read`,
  `restricted.read`); for others the step is marked `withheld: true` and keeps its counts and
  refs only.
- Notes without `domain_id` and steps outside a Domain of other principals: installation
  administrators only. A note with `domain_id` is visible like a step of that Domain; posting it
  needs a grant in that Domain. Notes are writes: rate limits count them like other writes.
- Query plans are recorded only for callers holding `trace.explain` in the Domain, and shown
  to other readers only when they hold it too.

## Settings

`keeper.trace` in `config/lorebase.example.yaml` ([reference](reference/configuration.md)).
A request is traced when `enabled` is true and one of: it opts in with
the opt-in header (`allow_header_opt_in`), its principal is in `principals`, its API key id is
in `api_keys`, or `trace_all` is set.

| Setting | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Record traces (needs the `database` section). |
| `capture_literals` | `false` | Keep filter values. |
| `capture_search_text` | `true` | Keep search text. |
| `max_query_bytes`, `max_text_chars` | 16384, 500 | Bounds of a recorded query. |
| `max_response_bytes` | 1 MiB | Larger answers are recorded without counts and refs. |
| `max_refs` | 500 | Refs kept per step. |
| `max_note_chars` | 4000 | Longest note. |
| `max_steps_per_session` | 2000 | Steps kept per session and principal; later ones are dropped. |
| `inferred_session_idle_s` | 300 | Gap after which calls without a session id start a new inferred session. |
| `retention_days` | 7 | Steps older than this are deleted every `prune_interval_s`. |
| `queue_size`, `batch_size`, `flush_interval_s`, `write_timeout_s` | 10000, 200, 1 s, 5 s | Off-path writing, like the read audit. |

Steps live in `agent_trace_steps` (migration `0023`, row-level security like every table with
a `domain_id`). Dropped steps are counted in `lorebase.trace.steps.dropped` by reason
(`queue_full`, `write_failed`, `session_full`, `shutdown`), written ones in
`lorebase.trace.steps.recorded` ([metrics](metrics.md)).

## Cost

An untraced request pays one header lookup. A traced read keeps its request body (up to
`max_query_bytes`) and, when it is a successful JSON answer to a traced principal, its answer
(up to `max_response_bytes`; 64 KiB for writes, read only for the Proposal they name) in memory
until it is sent, then parses it once for counts and refs; the write happens in a background
batch. A read Keeper rejects as invalid (422) is recorded as `{"rejected": true}`, without its
query. Listing sessions groups the visible steps of the retention period, and the per-session
cap is checked per write (concurrent Keepers may pass it by a few steps). Turn on `trace_all`
only for short investigations on busy installations.
