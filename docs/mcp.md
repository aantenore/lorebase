# Herald: the Lorebase MCP server

Herald exposes a Lorebase Domain to any MCP host as a small set of tools, two resources and one
prompt. It is a thin, stateless process over Keeper's REST API (design 11.2, AC-20): every call
goes to Keeper with the calling agent's own API key, so authorization, masking, audit and rate
limits are enforced in one place. Herald has no database credentials and never calls a model.

- Package: `packages/herald` (`lorebase_herald`), console script `lorebase-herald`.
- Transports: stdio (one Herald process per agent, started by the host) and Streamable HTTP
  (one shared Herald; each caller sends its own key).
- Built on the official Python MCP SDK (`mcp`).
- The REST endpoints behind the tools, for agents that call Keeper directly: [agents](agents.md).

## Run it

```bash
uv sync --all-packages
LOREBASE_URL=http://127.0.0.1:8080 \
LOREBASE_API_KEY=lbk_xxxxxxxxxx_yyyyyyyy \
LOREBASE_DOMAIN=supply \
uv run lorebase-herald              # stdio: the host starts this command
```

Outside a clone, install Herald from PyPI (`uv tool install lorebase-herald`, or `pip install lorebase-herald`) and run `lorebase-herald` without `uv run`.

Streamable HTTP on `http://127.0.0.1:8090/mcp`:

```bash
LOREBASE_URL=http://127.0.0.1:8080 LOREBASE_DOMAIN=supply \
LOREBASE__HERALD__TRANSPORT=streamable_http \
uv run lorebase-herald
```

The API key belongs to a service principal created for the agent
(`POST /api/v1/domains/{domain}/api-keys`), with only the grants it needs: for example role `agent-reader` for reads plus
an Operation grant such as `flag_supplier_risk` for writes. Never give an agent an admin key.

### Docker compose

The compose stack has Herald as the optional profile `mcp`, on the backend image, over
Streamable HTTP and wired to the stack's Keeper (`http://keeper:8080`):

```bash
make up PROFILES=mcp
# or: docker compose --env-file .env -f deploy/compose/docker-compose.yaml --profile mcp up -d --wait herald
```

It listens on port 8090 in the container and is published on `127.0.0.1:8091`
(`HERALD_BIND`, `HERALD_HOST_PORT`; 8090 on the host is the web app's), so agents on the host use
`http://127.0.0.1:8091/mcp` and containers on the compose network `http://herald:8090/mcp`. DNS
rebinding protection accepts the Host values `127.0.0.1`, `localhost` and `herald` on any port
and the Origins `http://127.0.0.1` and `http://localhost` (`HERALD_ALLOWED_HOSTS`,
`HERALD_ALLOWED_ORIGINS`, JSON lists; add the name agents use when they reach it another way).
`HERALD_DOMAIN` and `HERALD_BRANCH` set the defaults of calls that name none, and
`HERALD_ALLOW_ANONYMOUS=true` lets calls without a credential through to a Keeper in auth mode
none (below). The variables are listed in `.env.example`.

## Configuration

Herald reads the `herald` section of the Lorebase settings (YAML file named by
`LOREBASE_CONFIG`, then `LOREBASE__HERALD__...` variables). Every key and its default is in
[`config/lorebase.example.yaml`](../config/lorebase.example.yaml). MCP host configurations
usually set only the short variables:

| Variable | Setting | Notes |
|----------|---------|-------|
| `LOREBASE_URL` | `herald.keeper.base_url` | Keeper origin, default `http://127.0.0.1:8080` |
| `LOREBASE_API_KEY` | `herald.keeper.api_key` | stdio only; a literal or `secret://env/NAME`, `secret://file/PATH` |
| `LOREBASE_DOMAIN` | `herald.domain` | Domain used when a call names none |
| `LOREBASE_BRANCH` | `herald.branch` | default: the Domain's default branch |
| `LOREBASE__HERALD__TRANSPORT` | `herald.transport` | `stdio` (default) or `streamable_http` |
| `LOREBASE__HERALD__HTTP__PORT` | `herald.http.port` | default `8090`; also `host`, `path`, `allowed_hosts` |
| `LOREBASE__HERALD__OUTPUT__MAX_CHARS` | `herald.output.max_chars` | longest tool output, default 12000 |
| `LOREBASE__HERALD__KEEPER__MAX_RETRIES` | `herald.keeper.max_retries` | resends after 429, 503 or an unreachable Keeper, default `2`; `0` turns retries off |
| `LOREBASE__HERALD__KEEPER__RETRY_MAX_WAIT_S` | `herald.keeper.retry_max_wait_s` | longest pause before a retry, default `5`; `Retry-After` is capped to it |
| `LOREBASE__HERALD__KEEPER__TRUST_ENV` | `herald.keeper.trust_env` | default `false`: `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY` are ignored; `true` honors them (not with `verify_tls: false`) |

Credentials: with stdio, every Keeper call carries `herald.keeper.api_key`. With Streamable
HTTP, every call forwards the caller's `Authorization: Bearer <api key>` header unchanged and
fails with `[unauthorized]` without one; the configured key is never used, so one Herald can
serve many agents, each with its own grants.

**Local stacks without authentication.** When Keeper runs with `keeper.auth.mode: none` (the
example configuration of the compose quickstart), set `herald.http.allow_anonymous: true`
(`LOREBASE__HERALD__HTTP__ALLOW_ANONYMOUS=true`) and agents may call Streamable HTTP without a
header: Herald forwards those calls to Keeper without a credential, as the web app does, and
Keeper accepts them as it accepts every request in that mode. Calls that carry a header still
forward it. Herald does not decide on its own: under any other Keeper mode the same calls fail
with `[unauthorized]`, so the setting never opens a secured Keeper. It is off by default; keep
it off for anything but a laptop, because whoever reaches Herald then reaches Keeper as its
anonymous administrator.

## MCP host configuration

### Generic stdio configuration

Most hosts accept this shape (`mcpServers`, one entry per server):

```json
{
  "mcpServers": {
    "lorebase": {
      "command": "uv",
      "args": ["--directory", "/path/to/lorebase", "run", "lorebase-herald"],
      "env": {
        "LOREBASE_URL": "http://127.0.0.1:8080",
        "LOREBASE_API_KEY": "lbk_xxxxxxxxxx_yyyyyyyy",
        "LOREBASE_DOMAIN": "supply"
      }
    }
  }
}
```

With the backend image (`deploy/docker/backend.Dockerfile`), use `"command": "docker"` and
`"args": ["run", "-i", "--rm", "-e", "LOREBASE_URL", "-e", "LOREBASE_API_KEY", "-e",
"LOREBASE_DOMAIN", "lorebase", "python", "-m", "lorebase_herald"]`.

Keep the key out of shared files: point `LOREBASE_API_KEY` at a reference such as
`secret://file//run/secrets/lorebase_agent_key`, or let the host prompt for it (VS Code below).

### Claude desktop app

Add the generic entry under `mcpServers` in `claude_desktop_config.json` (Settings, Developer,
Edit Config), then restart the app. `uv` must be on the app's `PATH`; otherwise give its
absolute path in `command`.

### Cursor

Put the generic entry in `.cursor/mcp.json` in the project (or `~/.cursor/mcp.json` for every
project). The tools appear under the `lorebase` server in the MCP settings.

### VS Code

VS Code reads `.vscode/mcp.json`, with `servers` instead of `mcpServers` and an explicit
`type`; `inputs` prompts for the key once and stores it securely:

```json
{
  "inputs": [
    {"type": "promptString", "id": "lorebase-key", "description": "Lorebase agent API key", "password": true}
  ],
  "servers": {
    "lorebase": {
      "type": "stdio",
      "command": "uv",
      "args": ["--directory", "/path/to/lorebase", "run", "lorebase-herald"],
      "env": {
        "LOREBASE_URL": "http://127.0.0.1:8080",
        "LOREBASE_API_KEY": "${input:lorebase-key}",
        "LOREBASE_DOMAIN": "supply"
      }
    }
  }
}
```

### Agent frameworks over Streamable HTTP

Agent frameworks with an MCP client, including those driving any OpenAI-compatible model
endpoint, connect to a shared Herald over Streamable HTTP. Configure the client with the URL
and the agent's own key as a header:

```json
{
  "url": "https://herald.example.com/mcp",
  "headers": {"Authorization": "Bearer lbk_xxxxxxxxxx_yyyyyyyy"}
}
```

The same works for desktop hosts that accept remote servers (`"type": "http"` in VS Code,
`"url"` entries in Cursor). Herald runs stateless with JSON responses by default
(`herald.http.stateless`, `herald.http.json_response`), so any replica can serve any call
behind a load balancer. When it is reachable under a public name, list that name in
`herald.http.allowed_hosts` (DNS rebinding protection) and terminate TLS in front of it.

## Tools

Every tool takes optional `domain` (default `LOREBASE_DOMAIN`) and, where it applies, `branch`;
reads also take `at` (Revision id or tag) where Keeper supports it, and every tool takes
`format`: `text` (default, compact lines) or `json` (stable shapes). Refs are `Kind/key`, for
example `Supplier/S-1001`.

| Tool | Input (besides domain, branch, format) | Output | Keeper calls |
|------|-----------------------------------------|--------|--------------|
| `lorebase_context` | `records?: [ref]` or `question?`, `depth?`, `relations?`, `traits?`, `lens?`, `token_budget?` | Keeper's context pack: seed Records, neighbors, Links, schema of the Kinds present, recent changes and lineage, trimmed to the budget (markdown for `text`) | `POST .../context` (`POST .../search` first for a question) |
| `lorebase_schema` | `kinds?`, `detail?: summary or full` | Kinds with attribute types (`!` = required), key, title, built-in Operations; Relations; custom Operations with parameters and review policy | `GET .../schema` |
| `lorebase_get` | `ref`, `links?`, `at?` | values and Link counts per Relation | `GET .../kinds/{kind}/records/{key}` |
| `lorebase_resolve` | `text`, `kinds?`, `levels?`, `query_vector?`, `limit?`, `at?` | ranked candidates the words may stand for: Kinds, Attributes (with type) and stored values with a ready-to-use `where` predicate and Record count, each with its level (`exact`, `synonym`, `trigram`, `embedding`) and the words matched; `unmatched` words; skipped levels; `coverage:` lines (phrases unknown in the Domain, Records missing the Attributes found) | `POST .../resolve` |
| `lorebase_query` | `query` (DSL JSON), `cursor?`, `at?` | rows `ref "title" \| attr=value`, included neighbors, aggregate (`"aggregate": {"group_by": attr}` counts Records per value), `next_cursor`, `coverage:` lines (missing values of the Attributes used, Source freshness; `"coverage": false` in the query leaves them out) | `POST .../query` |
| `lorebase_search` | `query?`, `kinds?`, `traits?`, `lens?`, `where?`, `query_vector?`, `limit?` | ranked hits with snippet and score; lexical unless a vector is given; `truncated_candidates` when every term was so common that only a sample of the matches was ranked, `truncated` when the vector list stopped short of its size while more Records match the filters | `POST .../search` |
| `lorebase_neighborhood` | `seeds: [ref]`, `depth`, `relations?`, `max_nodes?`, `max_edges?`, `values?` | nodes and edges, `truncated` when Keeper caps were reached; without `values` (the default) Keeper returns Record headers and Link endpoints only | `POST .../neighborhood` (`values: false` unless `values`) |
| `lorebase_explain` | `ref`, `attributes?` | how the Record was created; per attribute: value, origin type, actor, time and Revision of its last change; without `attributes`, its Links and the Proposals that changed it | `GET .../records/{key}/explain` |
| `lorebase_history` | `ref`, `values?`, `limit?`, `cursor?`, `at?` | changes of the Record newest first: Revision, time, author, created/updated/deleted, changed attributes (with before -> after when `values`), `next_cursor` | `GET .../records/{key}/history` |
| `lorebase_lineage` | `ref`, `at?` | per attribute: value, origin type, actor, Revision, and the Source, Operation, extraction evidence or Proposal behind it | `GET .../records/{key}/lineage` |
| `lorebase_changes` | `since?`, `limit?`, `author?`, `origin_type?`, `cursor?` | Revisions newest first back to `since` (id or 8+ char prefix) | `GET .../branches/{b}/log` |
| `lorebase_diff` | `from_revision`, `to_revision`, `kinds?`, `relations?`, `limit?`, `cursor?` | counts per Kind and Relation, then `+`/`~`/`-` elements with old -> new values | `GET .../diff` |
| `lorebase_execute` | `operation`, `parameters`, `dry_run` (default true), `expected_head?`, `idempotency_key?`, `message?`, `task?`, `intent?`, `auto_propose?`, `auto_merge?` | status, edits, violations, warnings, new Revision, and the next step; with a `task` on a write that needs review: the Proposal (id, status `proposed`, `merged` or `conflicts`) and `next` steps | `POST .../operations/{op}/execute` |
| `lorebase_branches` | `limit?`, `cursor?` | branches with head, default and protected flags | `GET .../branches` |

Read tools are annotated read-only; `lorebase_execute` is the only tool that can write.

Herald never embeds text. For semantic or hybrid `lorebase_search`, the agent computes
`query_vector` on its side with the model Scribe uses for the Domain (for example
`lorebase-inference embed --query "text"` with the same `inference.embeddings` settings,
[inference providers](inference-providers.md#embeddings-for-search)); a vector of another
dimension is refused. The output of `lorebase_schema` ends with a `SEARCH` line (`search` in
the JSON format): the search modes of the
Domain and, when a Kind has an `embedding_template`, the embedding model name and dimension to
embed queries with. Without an embedding model the only mode is lexical.

`traits` are Trait api names (each stands for the Kinds implementing it that the agent may
read) and `lens` is the api name of a read preset of the schema; the `LENSES` lines of
`lorebase_schema` list them. A lens gives `lorebase_context` its Relations, depth, sections and
budget unless the call passes them, and limits neighbors and searched Kinds
([lenses and Traits](agents.md#lenses-and-traits)).

**Resources.** `lorebase://{domain}/schema` (schema summary),
`lorebase://{domain}/lenses/{lens}` (a lens with its resolved members) and
`lorebase://{domain}/{kind}/{key}` (one Record), for hosts that attach resources to the context.

**Prompt.** `lorebase_guide(domain?, task?)`: the workflow below as a template; it contains no
model calls.

### History and lineage

`lorebase_history` lists the changes of one Record, newest first; `values: true` adds each
changed value before and after. `lorebase_lineage` says, per current value, which Revision,
actor and origin set it, with the Source, Operation, document span or Proposal behind it. Both
take `at` to answer as of an earlier Revision. Revision ids are 64 hex digits; they are cut
short here:

```json
{"ref": "Supplier/S-1001", "values": true, "limit": 2}
```

```text
Supplier/S-1001 rev f4d229169a37... on main: 2 changes
f4d229169a37... 2026-10-01T08:00:04Z usr_editor [operation] updated: Raise the risk of S-1001
  risk_score: 32 -> 90
97ce9a2ce272... 2026-10-01T08:00:03Z usr_loader [import] created: Import the supply example data
  supplier_code: null -> S-1001
  legal_name: null -> Kavsen Alloys Sp. z o.o.
  risk_score: null -> 32
```

```json
{"ref": "Supplier/S-1001"}
```

```text
Supplier/S-1001 rev f4d229169a37... on main (2 changes)
created <- usr_loader at 2026-10-01T08:00:03Z in 97ce9a2ce272...
supplier_code = S-1001 <- import by usr_loader at 2026-10-01T08:00:03Z in 97ce9a2ce272...
legal_name = Kavsen Alloys Sp. z o.o. <- import by usr_loader at 2026-10-01T08:00:03Z in 97ce9a2ce272...
risk_score = 90 <- operation by usr_editor at 2026-10-01T08:00:04Z in f4d229169a37... operation update_supplier
```

A value that came from a document adds a line `extracted from document <id> [start:end] by
<model>`; the answer ends with `SOURCES:` (the Sources behind the values) and one `PROPOSAL`
line per Proposal that brought a value to the branch. `lorebase_explain` gives the same per
value in one call, plus the Links; use history for the sequence of changes and lineage for the
evidence of one value.

### Output size

Outputs are cut at `herald.output.max_chars`, lists at `max_items`, single values at
`max_value_chars`. Every cut is marked (`... N more`, `[truncated: ...]`, `next_cursor: ...`),
never silent. `lorebase_context` passes `token_budget` to Keeper, which trims the pack to it
and lists what it trimmed (`agents.context`, see [agents](agents.md)).

### Errors

Failures come back as tool errors: `[code] message`, an optional list of issues, and a
`next:` line with the action that resolves them. Codes are Keeper's problem codes:

| Code | Meaning | Next step given |
|------|---------|-----------------|
| `stale-head` | the branch moved since `expected_head` | the current head; re-read, then retry with it |
| `review-required`, `protected-branch` | the change needs a Proposal | repeat with `task` and `intent`, or run it on a work branch and open a Proposal |
| `agent-branch-limit`, `already-exists` | too many task branches, or the branch is someone else's | merge or close a Proposal, or use another `task` |
| `validation-failed` | parameters or data rules failed | the issues, one per line with their path |
| `not-found` | no such Domain, branch, Kind or Record | check refs with `lorebase_search` or `lorebase_schema` |
| `forbidden`, `unauthorized` | the agent's key lacks the grant, or is invalid | do not retry |
| `rate-limited`, `branch-busy`, `keeper-unavailable` | transient | retry after the given delay |

## Agent workflow

1. **Context first.** Call `lorebase_context` with the Records the task is about, or with a
   short `question`. One call returns the seeds, their links, nearby Records and the schema of
   the Kinds involved, within a budget.
2. **Ground the words, then read.** When a question names things in its own words ("red
   shoes", "stores in Milan"), `lorebase_resolve` says which Kinds, Attributes and stored
   values they stand for and gives the `where` fragments; it never runs the query. Then
   `lorebase_query` with those fragments for filtered lists and counts
   (`"aggregate": {"group_by": attr}`), `lorebase_get` for one Record,
   `lorebase_search` to find Records by words, `lorebase_neighborhood` for more links,
   `lorebase_explain` (or `lorebase_history` and `lorebase_lineage`) before trusting a value,
   `lorebase_changes` and `lorebase_diff` to catch up with what changed since a Revision seen
   earlier. `lorebase_query` and `lorebase_resolve` end with `coverage:` lines, one per signal
   ("color missing on 25% of Product: a filter on color excludes them", a stale Source, a phrase
   unknown in the Domain): read them before answering "none" (design 8.11). Words that matched
   nothing may be kept for vocabulary suggestions a person reviews (design 8.10).
3. **Writes through Operations.** Call `lorebase_execute` with `dry_run` (the default), check
   edits and violations, then commit with `dry_run: false` and `expected_head` set to the head
   the dry run reported. Operations with a review policy, and protected branches, need a
   Proposal: add `task` (a short slug) and `intent`. Keeper then runs the Operation on the
   branch `agent/{agent}/{task}`, opens or updates the Proposal and answers (HTTP 202) with its
   id and status (`proposed`, `merged` or `conflicts`) and the `next` steps. `auto_merge: true`
   asks for an immediate merge; it only happens when the Domain allows it for the agent's role
   and no approval or Conflict is pending, otherwise the Proposal waits for a reviewer. Without
   a `task` the answer stays `review-required`, and `auto_propose: true` is refused as
   `validation-failed`; `intent`, `auto_propose` and `auto_merge` are always passed on and
   Keeper's problem text is returned unchanged. Keep an `idempotency_key` per intended change
   so retries commit once (and find the same Proposal). Details in [agents](agents.md).

Parameters that shape every read: `lens` (a read preset of the schema, on `lorebase_context`
and `lorebase_search`) and `traits` (Kinds by the Trait they implement) narrow what is read;
`at` reads a past Revision or tag; `domain` and `branch` default to `LOREBASE_DOMAIN` and
`LOREBASE_BRANCH`, else the Domain's default branch. Herald has no Proposal tools: an agent that reviews as a steward lists
its work over REST with `assigned=me` ([agents](agents.md#the-loop)).

### From words to counts

"How many red shoes per brand?" on a retail Domain. Resolve the words first:

```json
{"text": "red shoes per brand", "kinds": ["Product"]}
```

```text
rev 5b0c41e2...: 3 candidates
1. value Product.color (string) = Rosso, red | exact 1.0 for 'red', 120 records
   where: {"attr":"color","op":"in","value":["Rosso","red"]}
2. value Product.category (string) = Calzature | synonym 0.95 for 'shoes', 100 records
   where: {"attr":"category","op":"eq","value":"Calzature"}
3. attribute Product.brand (string) | exact 1.0 for 'brand'
unmatched: how, many, per
```

Then write the query from the `where` fragments, grouped by the Attribute the third candidate
named, with `lorebase_query`:

```json
{"query": {"kind": "Product",
           "where": {"and": [{"attr": "color", "op": "in", "value": ["Rosso", "red"]},
                             {"attr": "category", "op": "eq", "value": "Calzature"}]},
           "aggregate": {"group_by": "brand"}}}
```

The answer starts with one `count Product <brand>: <n>` line per value, then the rows. Before
quoting a surprising Record, `lorebase_explain` (or `lorebase_lineage`) says where its values
came from. Resolution only suggests: when no candidate fits, the agent falls back to
`lorebase_schema` and `lorebase_search`.

## Implementation notes

- Herald calls Keeper through the typed `lorebase_client` package.
- `lorebase_context` and `lorebase_explain` call Keeper's agent endpoints, described in
  [agents](agents.md); the pack is trimmed by Keeper to `token_budget`
  (`agents.context.chars_per_token` there sets the token estimate).
- `lorebase_changes` pages through the branch log until it reaches `since`, bounded by
  `herald.changes.max_pages`.
- Import contracts: `lorebase_herald` may import `lorebase_platform` only; it never imports
  `lorebase_inference` (enforced by `.importlinter`).
