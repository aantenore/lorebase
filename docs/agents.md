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
   overwriting a newer state.
5. **Follow changes** with the event stream (`GET /branches/{b}/events`), the polling fallback
   (`GET /branches/{b}/changes?since=`) or a webhook, and refresh only what changed.

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
| Records matching a filter | `POST /query` (design 8.2) |
| a path of Relations | `POST /traverse` |
| the graph around some Records | `POST /neighborhood` |
| Records by text or vector | `POST /search` |
| what changed between two Revisions | `GET /diff?from=&to=` |

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

The answer holds `secret`, shown only this once. Each delivery is a POST of the event document
above with the headers `Lorebase-Event`, `Lorebase-Delivery` (delivery id) and
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

Answer 2xx to acknowledge. Other answers and timeouts are retried with exponential backoff
(`webhooks.backoff_*`) up to `webhooks.max_attempts`, then the delivery is marked dead.
Deliveries are made by `lorebase_store.webhooks.deliver_pending`, which a worker runs
periodically with an HTTP sender (`lorebase_keeper.services.webhooks.HttpxSender`); targets
must use `webhooks.allowed_schemes` and, unless `webhooks.allow_private_targets`, must not
resolve to private, loopback or link-local addresses. `GET /webhooks` lists them (never the
secret) and `DELETE /webhooks/{id}` removes one with its pending deliveries.

## Settings

All limits are configuration (`config/lorebase.example.yaml`): `agents.context` (budgets,
estimator, caps, trim order), `agents.explain` (listed Links and Proposals), `agents.feed`
(poll interval, heartbeat, connection time, page sizes, scan depth) and `webhooks` (targets,
headers, timeouts, retries, retention).
