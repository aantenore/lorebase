# Lorebase for agents

How an external agent should read from, write to and follow a Lorebase Domain through Keeper's
REST API. Every path below is relative to `/api/v1/domains/{domain}`; the examples use the
`supply` sample Domain ([examples/supply](../examples/supply)).

Three properties hold on every read an agent makes:

- **No model is called.** Context packs, explanations and the change feed are computed from
  the materialized branch head and the history. The same request at the same Revision returns
  the same bytes.
- **Every answer names its Revision.** In the body (`revision`) and in the `Lorebase-Revision`
  header. Cacheable answers carry an `ETag`; send it back in `If-None-Match` and an unchanged
  answer costs a `304`.
- **Authorization is the one of the REST reads.** `data.read` on the branch; a grant narrowed
  to a Kind sees that Kind only (other Kinds are left out of neighborhoods, an explicit request
  for them gets `403`). History reads (`changes`, `events`) need a grant not narrowed to a Kind.

## The loop

1. **Start with one context pack** for the Records the task is about (`POST /context`). It
   holds the Records, their neighborhood, the schema of what is in it, recent changes and
   lineage, within a token budget you choose.
2. **Then targeted reads** when the pack is not enough: one Record, one Relation, a DSL query,
   a traversal, a search (design 8 and 11.1).
3. **Ask why** with `GET .../explain` before acting on a surprising value.
4. **Write through Operations** (`POST /operations/{op}/execute`), with `dry_run` first and an
   `idempotency_key`; pass `expected_head` (the `revision` of your pack) to fail instead of
   overwriting a newer state. When the change needs review, add a `task` and an `intent`:
   Keeper opens the Proposal for you (section 4).
5. **Follow changes** with the event stream (`GET /branches/{b}/events`), the polling fallback
   (`GET /branches/{b}/changes?since=`) or a webhook, and refresh only what changed.

To see how an agent moved through the graph, send a session id with its calls and turn on the
[agent trace](agent-trace.md): every call becomes a step (query, counts, touched Records and
Links, latency), the agent can attach notes, and the web app replays the session on the
Canvas. To tune what an agent finds, compare search parameters and context packs in the
[retrieval lab](retrieval-lab.md).

## 1. Context packs

`POST /context` takes exactly one seed selector: `refs` (`Kind/key` references), `query` (the
DSL of design 8.2) or `search` (lexical search text).

| Member | Default | Meaning |
|--------|---------|---------|
| `refs` / `query` / `search` | | the seed Records (at most `agents.context.max_seeds`) |
| `branch` | the default branch | branch read, at its head |
| `budget_tokens` | `agents.context.default_budget_tokens` | estimated token budget |
| `depth` | `agents.context.default_depth` | neighborhood depth; `0` for the seeds alone |
| `relations` | all | Relation api names to follow (forward or inverse) |
| `include_schema`, `include_recent_changes`, `include_lineage` | `true` | optional sections |
| `since` | none | Revision id or tag: recent changes after it (default: the latest ones) |
| `format` | `json` | `markdown` answers `text/markdown`, ready to paste into a prompt |

```http
POST /api/v1/domains/supply/context
Content-Type: application/json

{"refs": ["Supplier/S-1001"], "depth": 1, "budget_tokens": 1500, "format": "json"}
```

The JSON pack (values in canonical form: integers and decimals are strings):

```json
{
  "domain": "supply",
  "branch": "main",
  "revision": "f4d229169a37fa73e83df368ea2c035fead80d809b908cf236c013a0dd23066b",
  "etag": "\"f4d229169a37fa73e83df368ea2c035fead80d809b908cf236c013a0dd23066b.3c1f0e7a9b2d4c55\"",
  "seeds": [
    {
      "ref": "Supplier/S-1001",
      "title": "Kavsen Alloys Sp. z o.o.",
      "values": {"supplier_code": "S-1001", "legal_name": "Kavsen Alloys Sp. z o.o.",
                 "city": "Poznan", "country": "PL", "risk_score": "90", "status": "active",
                 "tier": "1"},
      "updated_revision": "f4d229169a37fa73e83df368ea2c035fead80d809b908cf236c013a0dd23066b"
    }
  ],
  "neighbors": [
    {"ref": "Contract/C-3001", "title": "Framework supply agreement 2023 - Kavsen Alloys Sp. z o.o.",
     "depth": 1, "values": {"contract_number": "C-3001", "status": "active", "currency": "PLN"}},
    {"ref": "Part/P-10028", "title": "Radial shaft seal 25x42x7", "depth": 1,
     "values": {"part_number": "P-10028", "category": "seal", "unit_cost": "24.01"}}
  ],
  "links": {
    "supplied_by": [{"from": "Part/P-10028", "to": "Supplier/S-1001",
                     "values": {"is_primary": true, "lead_time_days": "21"}}],
    "with_supplier": [{"from": "Contract/C-3001", "to": "Supplier/S-1001"}]
  },
  "schema": {
    "kinds": [{"name": "Supplier", "description": "A company that supplies parts to Corvane Industries.",
               "key": ["supplier_code"], "title": "legal_name",
               "attributes": [{"name": "risk_score", "type": "integer",
                               "description": "Supplier risk score from 0 (safe) to 100 (critical), maintained by risk analysts."}]}],
    "relations": [{"name": "supplied_by", "inverse": "supplies", "from": "Part", "to": "Supplier",
                   "cardinality": "many_to_many", "description": "A supplier can supply the part."}]
  },
  "recent_changes": [
    {"revision": "f4d229169a37fa73e83df368ea2c035fead80d809b908cf236c013a0dd23066b",
     "at": "2026-10-01T08:00:04Z", "ref": "Supplier/S-1001", "change": "modified",
     "attributes": ["risk_score"], "actor": "usr_editor", "origin": "operation",
     "message": "Raise the risk of S-1001"}
  ],
  "lineage": [
    {"ref": "Supplier/S-1001", "attributes": ["risk_score"], "origin": "operation",
     "actor": "usr_editor", "at": "2026-10-01T08:00:04Z"}
  ],
  "budget": {
    "limit_tokens": 1500, "used_tokens": 1412, "chars_per_token": 4.0,
    "sections": {"seeds": 61, "neighbors": 402, "links": 120, "schema": 690,
                 "recent_changes": 88, "lineage": 51},
    "trimmed": [], "omitted": {}, "over_budget": false, "capped": false
  }
}
```

(Abbreviated: a real pack lists every value of each Record and every Attribute of each Kind.)

With `"format": "markdown"` the same pack reads:

```markdown
# Context: supply/main
revision f4d229169a37fa73e83df368ea2c035fead80d809b908cf236c013a0dd23066b; estimated 1487 of 1500 tokens; trimmed: lineage(2), recent_changes(6)

## Records
### Supplier/S-1001: Kavsen Alloys Sp. z o.o.
- supplier_code: S-1001
- legal_name: Kavsen Alloys Sp. z o.o.
- risk_score: 90
- (last changed in f4d229169a37)

## Neighbors
| ref | title | depth | values |
| --- | --- | --- | --- |
| Part/P-10028 | Radial shaft seal 25x42x7 | 1 | part_number=P-10028; name=Radial shaft seal 25x42x7; category=seal; unit_cost=24.01 |

## Links
### supplied_by (Part -> Supplier)
- Part/P-10028 -> Supplier/S-1001 (is_primary=true; lead_time_days=21)

## Schema
### Kind Supplier
A company that supplies parts to Corvane Industries.
key: supplier_code; title: legal_name
- risk_score (integer): Supplier risk score from 0 (safe) to 100 (critical), maintained by risk analysts.
```

Revisions inside markdown sections are shortened to 12 hex digits; the header carries the full
id, which is what `since` and `expected_head` take.

### Budget

Tokens are estimated as `ceil(characters / agents.context.chars_per_token)` over the exact
text each section renders to in the requested format; the `budget` block reports the estimate
per section. When the pack is over budget, sections are trimmed in the order of
`agents.context.trim_order`, each step removing as little as needed:

1. `lineage` entries, last first;
2. `recent_changes`, oldest first;
3. `far_neighbor_values`: neighbors beyond depth 1 keep only reference and title;
4. `far_neighbors`: neighbors beyond depth 1 and their Links, deepest first;
5. `schema_descriptions`: names and types stay, descriptions go;
6. `near_neighbor_values`, then 7. `near_neighbors`;
8. `schema`.

Seed Records are never trimmed; when they alone exceed the budget the pack says
`over_budget: true`. `capped: true` means the graph caps (`agents.context.max_neighbors`,
`max_links`, `read.traverse`, `store.traverse`, whose `max_fanout` also bounds the Links
followed from one Record per hop) cut the neighborhood before any trimming. Ask for a smaller
`depth`, fewer `relations` or a larger budget accordingly.

### Caching

The `ETag` names the head Revision and a digest of the request. Repeat the request with
`If-None-Match: <etag>` to get `304` while nothing changed; the pack is also cached in Keeper,
keyed by Revision, so commits never serve a stale pack.

## 2. Targeted reads

| Need | Call |
|------|------|
| one Record, with Link counts and lineage | `GET /kinds/{kind}/records/{key}?links=true&lineage=true` |
| the Links of a Record along a Relation | `GET /kinds/{kind}/records/{key}/links/{relation}` |
| every change of one Record on a branch | `GET /kinds/{kind}/records/{key}/history` (section 3) |
| where each current value of a Record came from | `GET /kinds/{kind}/records/{key}/lineage` (section 3) |
| Records matching a filter | `POST /query` (design 8.2) |
| a path of Relations | `POST /traverse` |
| the graph around some Records | `POST /neighborhood` |
| Records by text or vector | `POST /search` |
| what changed between two Revisions | `GET /diff?from=&to=` |
| whether the data is sound | `GET /health`, `GET /kinds/{kind}/health` (section 6) |

## 3. Explain a Record

`GET /kinds/{kind}/records/{key}/explain` (or `?format=markdown`) answers why a Record exists
and why its values are what they are: the Revision that created it with its origin
(Operation, import, merge, extraction), per Attribute the provenance and Revision of its last
change, its Links with the Revision that made each, the number of changes on the branch, and
merged Proposals that changed it.

```markdown
# Explain: Supplier/S-1001: Kavsen Alloys Sp. z o.o.
supply/main at revision f4d229169a37fa73e83df368ea2c035fead80d809b908cf236c013a0dd23066b; 2 changes on this branch

## Created
In 97ce9a2ce272 at 2026-10-01T08:00:03Z by usr_loader (import examples/supply): Import the supply example data

## Attributes
| attribute | value | last change | at | by | origin | message |
| --- | --- | --- | --- | --- | --- | --- |
| risk_score | 90 | f4d229169a37 | 2026-10-01T08:00:04Z | usr_editor | operation | Raise the risk of S-1001 |
```

### History and lineage

`GET /kinds/{kind}/records/{key}/history` lists the Revisions that changed the Record on the
branch, newest first: author, time, message and origin of the Revision, whether it `created`,
`updated` or `deleted` the Record, and the Attributes it changed with the provenance of each.
`values=true` adds the values before and after. Pages hold `read.history.default_limit`
changes (at most `max_limit`); follow `next_cursor`. A deleted Record keeps its history.

`GET /kinds/{kind}/records/{key}/lineage` answers, per Attribute holding a value, where that
value came from: the provenance entry (origin type, origin, actor, time), the Revision of its
last change with its author and message, and what the provenance names: the Source
(`source_sync`), the Operation (`operation`), the extraction evidence (document, span, model and
prompt version) and the Proposal that brought it to the branch (merged in that Revision, or
whose Conflict resolution set the value). A value merged from another branch keeps its original
provenance; a resolved Conflict shows `merge_resolution`.

Both read the branch history table, one index range per Record, so their cost follows the
number of changes of the Record, not the length of the branch; lineage reads at most
`read.history.lineage_max_changes` of the newest changes to find the Revision behind each
value. Both are served at branch heads only (no `at`), authorized like a Record read (the
Kind of the path must be readable; only that Kind's Attributes appear) and cached by Revision
with an `ETag`. CLI: `lorebase record history|lineage KIND KEY`; MCP: `lorebase_history`,
`lorebase_lineage`.

## 4. Write through Operations

Agents never write rows; they execute Operations, which validate, commit a Revision (or open a
Proposal when policy says so) and record provenance:

```http
POST /api/v1/domains/supply/operations/update_supplier/execute
Content-Type: application/json
Idempotency-Key: risk-review-2026-10-21-S-1001

{"parameters": {"key": "S-1001", "risk_score": 75},
 "dry_run": true,
 "expected_head": "f4d229169a37fa73e83df368ea2c035fead80d809b908cf236c013a0dd23066b",
 "message": "Lower the risk after the audit of 2026-10-20"}
```

Repeat without `dry_run` once the edits and violations look right. A retry with the same
idempotency key returns the original result; a moved head answers `409`.

### Writes that need review

An Operation needs review when its branch is protected (the default branch of a Domain is) or
its review policy says so. Without more, that is `409 review-required` (problem details with
`branch`, `operation`, `proposals`, `reason`, either `protected-branch` or `review-policy`, and
`next` hints).

The review policy counts on every branch, protected or not. A policy that evaluates to true (the
supply schema's `flag_supplier_risk` with `severity: critical`, for example) refuses the write on
an unprotected work branch too: `409 review-required` with `reason: review-policy` and the `next`
hints of the agent flow (`name_a_task` first). With a `task`, the task branch is cut from that work
branch, the Proposal targets it, and merging it needs an actual approval even when the work branch
requires none (`review-policy-needs-approval`).

Name a `task` and Keeper does the rest in one call:

| Member | Default | Meaning |
|--------|---------|---------|
| `task` | none | short slug naming the piece of work; normalized to lower case (`Audit Q4` is `audit-q4`), at most `max_task_length` characters. Required for this flow |
| `intent` | none | what you are after, in a sentence or two; the start of the Proposal description |
| `auto_propose` | `keeper.agent_writes.auto_propose` for agent principals, `false` for others | `true` needs a `task`; `false` keeps the plain `409` |
| `auto_merge` | `keeper.agent_writes.auto_merge` | ask for an immediate merge; honored only as described below |

```http
POST /api/v1/domains/supply/operations/flag_supplier_risk/execute
Content-Type: application/json

{"parameters": {"supplier": "S-1001", "severity": "high", "reason": "Two late deliveries"},
 "task": "q4-audit",
 "intent": "Flag S-1001 after the Q4 delivery audit",
 "idempotency_key": "q4-audit-S-1001-flag",
 "auto_merge": true}
```

```http
HTTP/1.1 202 Accepted

{"status": "proposed",
 "operation": "flag_supplier_risk",
 "branch": "agent/svc_01j9x4k2/q4-audit",
 "target": "main",
 "invocation_id": "inv_01J9X4M7",
 "revision": "9be1…", "revisions": ["9be1…"], "edit_count": 2, "replayed": false,
 "proposal": {"id": "prp_01J9X4M8", "title": "Agent svc_01j9x4k2: q4-audit", "status": "open",
              "source_ref": "agent/svc_01j9x4k2/q4-audit", "target_ref": "main",
              "url": "/api/v1/domains/supply/proposals/prp_01J9X4M8", "created": true},
 "approval": {"required": 1, "approvals": 0, "satisfied": false},
 "attempt": {"id": "mat_01J9X4M9", "result": "fast_forward", "open_conflicts": 0,
             "resolved_conflicts": 0, "counts": {}},
 "merge": {"requested": true, "allowed": true, "performed": false,
           "blockers": ["approvals-pending"]},
 "merged_revision": null, "branch_created": true, "branch_deleted": false, "warnings": [],
 "next": [{"action": "await_review",
           "detail": "Waiting for approval (0 of 1 required).",
           "method": "POST", "href": "/api/v1/domains/supply/proposals/prp_01J9X4M8/reviews",
           "permission": "proposal.review"},
          {"action": "get_proposal", "detail": "Read the state of the Proposal.",
           "method": "GET", "href": "/api/v1/domains/supply/proposals/prp_01J9X4M8"}]}
```

What Keeper does, in order, and what it refuses before any of it is created:

1. The request is checked as usual (permission, Operation, expected head). A stale
   `expected_head` is `409 stale-head`; edits that would be rejected are `422` with their
   issues, exactly as for a direct write: a dry run happens first and creates nothing.
2. The task branch `agent/{principal}/{task}` (`keeper.agent_writes.branch_prefix`; the
   principal id in lower case) is created from the target head as an overlay branch (it
   stores only the rows the task changes; `branch_mode: copy` for full rows), or reused when
   it exists and this principal created it (another creator: `409 already-exists`). A principal holds at most
   `max_open_branches_per_principal` task branches (`409 agent-branch-limit`, listing them);
   reusing one of them as the `task` is always possible.
3. The Operation runs on the task branch, with your `idempotency_key`, `message` and the usual
   provenance. Its review policy does not refuse it there: the Proposal is the review.
4. One open Proposal per task branch and target: created with the title
   `Agent {principal}: {task}` (`title_template`) and a description made of your `intent` and
   the list of executions (`- flag_supplier_risk(reason="...", severity="high", ...) [inv_...]`;
   `summary_parameter_values: false` lists names only). When it exists and you opened it, the
   new execution is added to the list, and the `intent` replaced if you sent a new one.
5. The merge, only when all of this holds: `auto_merge` is true (request or settings); a role
   you hold in the Domain (a grant covering the task branch) is in
   `allowed_roles_for_auto_merge`; the Proposal has no open Conflict; the approvals the target
   requires are in (`required_approvals` of a protected target: an automatic merge never skips
   them); and, when the review came from the Operation's own policy, someone approved. Merging
   needs no `proposal.merge`: the Domain's list is the grant.

`status` says where it stands: `proposed` (open, waiting), `merged` (`merged_revision` is the
new target head and the task branch is gone) or `conflicts` (open Conflicts with the target:
`attempt.counts` per code, details in `GET .../proposals/{p}/conflicts`). `merge.blockers`
lists what keeps the Proposal from merging (`approvals-pending`,
`review-policy-needs-approval`), `next` the calls that move it forward. The response carries
`Idempotent-Replayed: true` when the execution had already been done.

Send an `idempotency_key` with every such write. The steps above are separate transactions, and
a retry with the same key finds the commit, the branch and the Proposal (even one that merged
and lost its branch) instead of executing again. An Operation with nothing to change answers
like a direct write (`committed`, no Revision) and creates no branch.

**Permissions.** The agent needs `operation.execute` (or `operation.execute_custom` and the
Operation's own `permissions`) and `data.read` on the target branch, and `branch.create` and
`proposal.create` for the task branch. Grants narrowed to `agent/*` cover the task branch.

**Settings** (`keeper.agent_writes`, see `config/lorebase.example.yaml`): `enabled`,
`principal_types` (the principals that count as agents, default service principals),
`auto_propose`, `auto_merge`, `allowed_roles_for_auto_merge` (default none),
`max_open_branches_per_principal`, `branch_prefix`, `branch_mode`, `delete_branch_on_merge`,
and the text limits `max_task_length`, `title_template`, `summary_parameter_values`,
`summary_value_chars` and `max_summary_operations`. A Domain overrides any of them with its own `agent_writes`
setting, for example `{"agent_writes": {"allowed_roles_for_auto_merge": ["editor"],
"auto_merge": true}}` when it is created.

**Cleanup.** When the Proposal of a task branch is merged or closed (by anyone), Keeper deletes
the branch (`delete_branch_on_merge`, default on) unless another open Proposal still has it as
its source. The Revisions stay in the history.

**Audit.** Each request writes `operation.propose`; the steps write `branch.create`,
`proposal.create` or `proposal.update`, `proposal.merge` (automatic merges) and
`operation.invoke`. Rate limiting counts the request once, in the `writes` class.

## 5. Follow changes

### Event stream

`GET /branches/{branch}/events` is a Server-Sent Events stream of the Revisions committed on
the branch. `kinds=Supplier,Part` keeps only Revisions changing those Kinds.

```text
retry: 3000

id: 42
event: revision
data: {"sequence":42,"type":"revision.committed","domain":"supply","branch":"main","revision":"5b0c…","parents":["f4d2…"],"generation":5,"author":"usr_editor","message":"Lower the risk after the audit of 2026-10-20","origin":{"type":"operation","ref":"inv_…"},"stats":{"partitions_changed":1,"changes":1},"created_at":"2026-10-21T09:14:03.120044Z","kinds":{"Supplier":1},"relations":{},"schema_changed":false,"keys":{"Supplier":["S-1001"]},"keys_truncated":false}

: keep-alive
```

- The `id` is the event sequence. Reconnect with `Last-Event-ID: 42` (or `?last_event_id=42`)
  to resume after it; without it the stream starts with the next commit.
- A comment line is sent after `agents.feed.heartbeat_s` of silence; the server closes the
  stream after `agents.feed.max_connection_s`, and clients reconnect.
- Every `agents.feed.reauthorize_interval_s` the stream checks the credential and the grants
  again and closes when they no longer allow the read: a revoked key or an expired token ends it
  within that interval, a withdrawn grant within that interval plus
  `keeper.auth.authz.grants_cache_ttl_s`. The reconnection then answers `401` or `403`.
- `keys` lists at most `agents.feed.changed_keys_max` changed Record keys (`keys_truncated`).

### Polling fallback

`GET /branches/{branch}/changes?since=<revision>&kinds=&limit=` returns the same events for
the first-parent Revisions after `since`, oldest first, with `next_since` to continue and
`more` when a page was cut. Without `since` it returns the latest Revisions. A `since` that is
not on the first-parent history of the branch, or more than `agents.feed.max_scan` Revisions
behind, answers `409`: take a new context pack and follow from its `revision`.

### Webhooks

Domain administrators (`domain.settings`) register endpoints; every change is in the audit log.

```http
POST /api/v1/domains/supply/webhooks
Content-Type: application/json

{"url": "https://agents.example.com/lorebase", "events": ["revision.committed"], "branches": ["main"]}
```

`events` defaults to `revision.committed` and `operation.*` (every Operation event, see
[Operation events](#operation-events)); `operation.<side effect>` names one.
The answer holds `secret`, shown only this once. Each delivery is a POST of the event document
above with the headers `Lorebase-Event`, `Lorebase-Delivery` (delivery id),
`Idempotency-Key` (`evt_<sequence>`, the same on every attempt of an event) and
`Lorebase-Signature: t=<unix seconds>,v1=<hex>`, where the hex digest is HMAC-SHA256 over
`"<t>." + body` keyed with SHA-256 of the secret (Lorebase stores only that digest):

```python
import hashlib, hmac, time


def verified(secret: str, header: str, body: bytes, tolerance_s: int = 300) -> bool:
    parts = dict(item.split("=", 1) for item in header.split(","))
    timestamp = int(parts["t"])
    if abs(time.time() - timestamp) > tolerance_s:
        return False
    key = hashlib.sha256(secret.encode()).digest()
    expected = hmac.new(key, f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts["v1"])
```

Answer 2xx to acknowledge. Delivery is at least once: a worker that dies between sending and
recording sends again, so drop repeats by `Idempotency-Key`. Deliveries are not ordered; order by
`sequence` in the body when it matters.
Other answers and timeouts are retried with exponential backoff
(`webhooks.backoff_*`) up to `webhooks.max_attempts`, then the delivery is marked dead.
Each attempt, host lookup included, is cut off after `webhooks.timeout_s` (10 s), and a
connection to one address after `webhooks.connect_timeout_s` (5 s), so a receiver that is gone
(connection refused), accepts and never answers, drops packets, or closes midway only fails
that attempt. A pass sends `webhooks.concurrency` deliveries at a time and starts none after
`webhooks.pass_timeout_s`; the deliveries it did not reach go to the next pass without counting
an attempt. Answer quickly and do the work afterwards: the response body is never read.
Deliveries are made by `lorebase_store.webhooks.deliver_pending`, which Scribe runs
periodically (`webhook_delivery` jobs, see [scribe.md](scribe.md)) with its HTTP sender
(`lorebase_scribe.webhooks.HttpxSender`); targets
must use `webhooks.allowed_schemes` and, unless `webhooks.allow_private_targets`, must not
resolve to private, loopback or link-local addresses. The host is resolved once per attempt,
every address is checked, and the sender connects to those addresses only (the `Host` header
and TLS keep the host name), so a DNS answer that changes after the check cannot reach an
internal service; redirects are never followed. Give the sender an `httpx.AsyncClient` built
with `trust_env=False`, so no proxy from the environment sits in between. `GET /webhooks` lists them (never the
secret) and `DELETE /webhooks/{id}` removes one with its pending deliveries.

The stored digest is enough to sign deliveries, so set `webhooks.encryption_key` (a `secret://`
reference to at least 32 characters, the same for Keeper and Scribe) to keep it encrypted in the
database (AES-256-GCM). Keeper then stores new keys encrypted, and each delivery pass encrypts up
to `webhooks.batch` keys stored before, so existing webhooks keep working and receivers change
nothing. To change the key, move the old value to `webhooks.previous_encryption_keys` and set the
new one; the worker re-encrypts, and the old value can go once no webhook uses it. A key
encrypted under a value that is no longer configured cannot sign: its deliveries fail with an
error naming the setting until the value is configured again.

### Operation events

An Operation declares the events its committed executions emit:

```yaml
operations:
  flag_supplier_risk:
    side_effects: [risk_flag_raised]
```

Each committed execution writes one `operation.risk_flag_raised` event in its commit
transaction; dry runs, refusals, executions that need review (a `task` write that opens a
Proposal included: follow `revision.committed` on the target for the merge) and replays of an
`idempotency_key` write none. A webhook subscribed to it (or to `operation.*`) receives:

```json
{"type": "operation.risk_flag_raised", "sequence": 812, "side_effect": "risk_flag_raised",
 "domain": "supply", "branch": "main", "operation": "flag_supplier_risk",
 "operation_id": "opr_...", "invocation_id": "inv_...", "principal": "agt_risk",
 "revisions": ["..."], "head": "...", "parameters": {"supplier": "S-1001", "severity": "high"},
 "edit_count": 2, "edits": [{"op": "upsert_record", "kind": "RiskFlag", "key": "...",
 "values": {"severity": "high"}}, {"op": "upsert_link", "relation": "flags", "from": "...",
 "to": "S-1001", "values": {}}], "edits_truncated": false}
```

`parameters` are as the invocation records them: values that may be sensitive are `***` for
everyone. In `edits` (at most `keeper.operations.side_effect_max_edits`), values of Attributes
tagged `pii`, `confidential` or `restricted` are `***` unless the webhook was created with
`"clearances": ["pii", ...]` covering every tag of the value; vault values are read from the
vault when the delivery is sent, so an erased one arrives as `[erased]`. Creating a webhook with
a clearance needs that clearance permission (`pii.read`, ...) on the whole Domain; the
clearances of a webhook are fixed when it is created, so delete and recreate it to change them.

## 6. Check the health of the data

`GET /health?branch=` and `GET /kinds/{kind}/health?branch=` (add `format=markdown` for text
to paste into a prompt) say how sound the head of a branch is before you build on it. They are
computed when asked, from the materialized rows, and cached under the Revision they describe.
Per Kind:

- **records** and the **last change** (the latest Revision that touched the Kind);
- **violations**: Records re-validated against the current schema (types, canonical form,
  constraints, required values, keys, Trait conformance), grouped by rule and Attribute with
  example keys. Past `read.health.max_records_validated` a fixed, evenly spread sample is
  checked and `sampled` is true;
- **duplicates**: Records whose unique or title Attribute is equal once case and spacing are
  ignored. A duplicate on a unique Attribute is a violation (`unique: true`); on a title it is
  a candidate for a merge. Values of Attributes with sensitivity tags are not printed;
- **derived**: derived Attributes whose materialized value is missing (`stale`). The check
  reads the stored `derived` values only: a value the materializer could not compute (an
  evaluation error) is absent and counted too, and a stored value that differs from what the
  Derivation computes now is not detected. `lorebase_store.materialize.verify` with
  `derived=True` recomputes the values and compares them.

Per Relation: stored **links** (for a foreign-key backed Relation, the references), **orphan
links** whose `from` or `to` Record does not exist, and for a Relation with `required_from`
the Records **missing** the required Link. `summary.ok` is false when there are violations,
orphans, gaps or duplicates of unique Attributes; duplicate titles and stale derived values
are reported without failing it. A grant narrowed to Kinds gets the report for those Kinds and
for Relations between them only. The CLI prints the same with `lorebase health [KIND]`
(`--markdown`, `--json`). An excerpt of the Markdown report for a damaged copy of the example:

```markdown
# Health of supply on broken
revision 97ce9a2ce272

needs attention: 6 kinds, 184 records, 289 links
- invalid records: 3; orphan links: 10; required-relation gaps: 2; duplicate groups: 12; kinds with stale derived values: 0

## Kinds
| kind | records | invalid | duplicate groups | derived | last change |
| --- | --- | --- | --- | --- | --- |
| Part | 40 | 0 | 4 |  | 97ce9a2ce272 |
| PurchaseOrder | 50 | 1 | 0 |  | 97ce9a2ce272 |
| Supplier | 30 | 2 | 2 |  | 97ce9a2ce272 |

### Supplier: violations (30 of 30 Records checked)
- value.pattern on country: 1 (e.g. S-1003)
- value.required on legal_name: 1 (e.g. S-1004)
### Supplier: duplicates (2 groups)
- vat_number [unique]: S-1002, S-1006 (2)

## Relations
| relation | ends | links | orphans | gaps | side |
| --- | --- | --- | --- | --- | --- |
| for_part | PurchaseOrder -> Part | 49 | 0 | 1 |  |
| stocked_at | Part -> Plant | 48 | 3 |  |  |
- for_part required, missing for: PO-200001
- stocked_at orphans: P-10003->PL-01, P-10029->PL-01, P-10032->PL-01
```

## Settings

All limits are configuration (`config/lorebase.example.yaml`): `agents.context` (budgets,
estimator, caps, trim order), `agents.explain` (listed Links and Proposals), `agents.feed`
(poll interval, heartbeat, connection time, page sizes, scan depth), `keeper.agent_writes`
(task branches and automatic Proposals, section 4), `read.health` (records
validated, examples, duplicate and orphan caps, statement timeout) and `webhooks` (targets,
headers, timeouts, retries, retention).

## Example agent

[`examples/agent`](../examples/agent/README.md) is a process agent that connects through Herald ([MCP](mcp.md)) to any OpenAI-compatible chat model (Ollama by default), works on the `supply` Domain with ready-made tasks and writes only through Operations and Proposals.
