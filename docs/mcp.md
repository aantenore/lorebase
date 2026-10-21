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

Streamable HTTP on `http://127.0.0.1:8090/mcp`:

```bash
LOREBASE_URL=http://127.0.0.1:8080 LOREBASE_DOMAIN=supply \
LOREBASE__HERALD__TRANSPORT=streamable_http \
uv run lorebase-herald
```

The API key belongs to a service principal created for the agent
(`POST /api/v1/domains/{domain}/api-keys`), with only the grants it needs: for example role `agent-reader` for reads plus
an Operation grant such as `flag_supplier_risk` for writes. Never give an agent an admin key.

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

Credentials: with stdio, every Keeper call carries `herald.keeper.api_key`. With Streamable
HTTP, every call forwards the caller's `Authorization: Bearer <api key>` header unchanged and
fails with `[unauthorized]` without one; the configured key is never used, so one Herald can
serve many agents, each with its own grants.

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
| `lorebase_context` | `records?: [ref]` or `question?`, `depth?`, `relations?`, `token_budget?` | Keeper's context pack: seed Records, neighbors, Links, schema of the Kinds present, recent changes and lineage, trimmed to the budget (markdown for `text`) | `POST .../context` (`POST .../search` first for a question) |
| `lorebase_schema` | `kinds?`, `detail?: summary or full` | Kinds with attribute types (`!` = required), key, title, built-in Operations; Relations; custom Operations with parameters and review policy | `GET .../schema` |
| `lorebase_get` | `ref`, `links?`, `at?` | values and Link counts per Relation | `GET .../kinds/{kind}/records/{key}` |
| `lorebase_query` | `query` (DSL JSON), `cursor?`, `at?` | rows `ref "title" \| attr=value`, included neighbors, aggregate, `next_cursor` | `POST .../query` |
| `lorebase_search` | `query?`, `kinds?`, `where?`, `query_vector?`, `limit?` | ranked hits with snippet and score; lexical unless a vector is given | `POST .../search` |
| `lorebase_neighborhood` | `seeds: [ref]`, `depth`, `relations?`, `max_nodes?`, `max_edges?`, `values?` | nodes and edges, `truncated` when Keeper caps were reached | `POST .../neighborhood` |
| `lorebase_explain` | `ref`, `attributes?` | how the Record was created; per attribute: value, origin type, actor, time and Revision of its last change; without `attributes`, its Links and the Proposals that changed it | `GET .../records/{key}/explain` |
| `lorebase_changes` | `since?`, `limit?`, `author?`, `origin_type?`, `cursor?` | Revisions newest first back to `since` (id or 8+ char prefix) | `GET .../branches/{b}/log` |
| `lorebase_diff` | `from_revision`, `to_revision`, `kinds?`, `relations?`, `limit?`, `cursor?` | counts per Kind and Relation, then `+`/`~`/`-` elements with old -> new values | `GET .../diff` |
| `lorebase_execute` | `operation`, `parameters`, `dry_run` (default true), `expected_head?`, `idempotency_key?`, `message?`, `task?`, `intent?`, `auto_propose?`, `auto_merge?` | status, edits, violations, warnings, new Revision, and the next step; with a `task` on a write that needs review: the Proposal (id, status `proposed`, `merged` or `conflicts`) and `next` steps | `POST .../operations/{op}/execute` |
| `lorebase_branches` | `limit?`, `cursor?` | branches with head, default and protected flags | `GET .../branches` |

Read tools are annotated read-only; `lorebase_execute` is the only tool that can write.

**Resources.** `lorebase://{domain}/schema` (schema summary) and
`lorebase://{domain}/{kind}/{key}` (one Record), for hosts that attach resources to the context.

**Prompt.** `lorebase_guide(domain?, task?)`: the workflow below as a template; it contains no
model calls.

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
2. **Then targeted reads.** `lorebase_get` for one Record, `lorebase_query` for filtered lists,
   `lorebase_search` to find Records by words, `lorebase_neighborhood` for more links,
   `lorebase_explain` before trusting a value, `lorebase_changes` and `lorebase_diff` to catch
   up with what changed since a Revision seen earlier.
3. **Writes through Operations.** Call `lorebase_execute` with `dry_run` (the default), check
   edits and violations, then commit with `dry_run: false` and `expected_head` set to the head
   the dry run reported. Operations with a review policy, and protected branches, need a
   Proposal: add `task` (a short slug) and `intent`. Keeper then runs the Operation on the
   branch `agent/{agent}/{task}`, opens or updates the Proposal and answers (HTTP 202) with its
   id and status (`proposed`, `merged` or `conflicts`) and the `next` steps. `auto_merge: true`
   asks for an immediate merge; it only happens when the Domain allows it for the agent's role
   and no approval or Conflict is pending, otherwise the Proposal waits for a reviewer. Without
   a `task` the answer stays `review-required`. Keep an `idempotency_key` per intended change
   so retries commit once (and find the same Proposal). Details in [agents](agents.md).

## Implementation notes

- Herald calls Keeper through the typed `lorebase_client` package.
- `lorebase_context` and `lorebase_explain` call Keeper's agent endpoints, described in
  [agents](agents.md); the pack is trimmed by Keeper to `token_budget`
  (`agents.context.chars_per_token` there sets the token estimate).
- `lorebase_changes` pages through the branch log until it reaches `since`, bounded by
  `herald.changes.max_pages`.
- Import contracts: `lorebase_herald` may import `lorebase_platform` only; it never imports
  `lorebase_inference` (enforced by `.importlinter`).
