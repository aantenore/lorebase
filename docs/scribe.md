# Scribe

Scribe is the Lorebase worker. It runs the jobs queued in Postgres and schedules the periodic
ones: Source synchronization, document extraction ([extraction.md](extraction.md)), webhook
delivery, queued index builds and garbage collection. Embeddings (design 9.3) come later.
Design references:
[5.9](design.md#59-source) (Source), [9.1](design.md#91-structured-sources) (structured
Sources), [6.6](design.md#66-the-commit-pipeline) (commit pipeline), AC-18 (no external broker).

Scribe writes through the same commit pipeline as Keeper, under a service principal
(`scribe.worker.principal`, default `svc_scribe`): every change it makes has a Revision,
Lineage and an audit entry.

## Running it

```bash
lorebase-scribe                                   # work and schedule until SIGTERM or SIGINT
lorebase-scribe once [--limit N]                  # run the due jobs, then exit
lorebase-scribe sync supply approved.suppliers    # synchronize one Source now, print the report
lorebase-scribe sync supply approved.suppliers --dry-run
```

Configuration comes from the same layered settings as Keeper (`LOREBASE_CONFIG`, then
`LOREBASE__SCRIBE__...` variables); every key with its default is in
[config/lorebase.example.yaml](../config/lorebase.example.yaml) under `scribe`. Retries and
stale locks use `store.jobs`, webhook delivery uses `webhooks`, collections use `store.gc`.

In docker compose the `scribe` service runs the backend image with `lorebase-scribe`. It mounts
`examples/supply/data` read-only at `/data/sources/supply` and points the example CSV Source at
it, so `POST /api/v1/domains/supply/sources/approved.suppliers/sync` (or `lorebase sources sync
approved.suppliers -d supply`, then `lorebase sources runs approved.suppliers -d supply`) works
once the `supply` Domain exists (see the [quickstart](quickstart.md)). The runs listing needs a
Domain-wide `data.read` grant.

## Job types

| Type | Queued by | Does |
|------|-----------|------|
| `source_sync` | Keeper (`POST .../sources/{s}/sync`), the scheduler (Source `schedule`) | Synchronizes one Source (below). Payload `{"source": id, "dry_run": bool}` |
| `webhook_delivery` | the scheduler, every `webhook_interval_s` | One pass of `lorebase_store.webhooks.deliver_pending`: fan-out of outbox events, signed POSTs, retries with backoff |
| `store.index` | commits whose per-Attribute index plan changed (large Kinds, or roles that do not own `mat_records`) | Builds or drops one per-Attribute index (`CONCURRENTLY`), as `database.maintenance_dsn` when it is set ([store schema notes](store-schema-notes.md#physical-layout)) |
| `store.materialize` | commits that move a base branch past an overlay's `overlay_max_base_lag_revisions` | Promotes the overlay branch to a full copy ([store schema notes](store-schema-notes.md#overlay-branches)). Payload `{"branch": name}` |
| `gc` | the scheduler, at `gc_schedule` per Domain; `python -m lorebase_store.gc --enqueue` | Garbage collection of a Domain ([operations](operations.md)) |
| `extraction` | `{"documents": [ids], "tiers": [..] or null, "requested_by"}` | Documents into a Proposal of items ([extraction.md](extraction.md)) |

### Worker loop

Each process runs `scribe.worker.concurrency` slots. A slot claims one due job with
`SELECT ... FOR UPDATE SKIP LOCKED`, so replicas never run the same job, runs it outside any
transaction and records the outcome. A failure is retried after `store.jobs.retry_delay_seconds`
until `store.jobs.max_attempts`; failures that a retry cannot fix (an unknown Source, an invalid
mapping, rows the branch rejects, more failed rows than allowed) fail the job at once. Running
jobs refresh their lock every `heartbeat_interval_s`; a job whose lock is older than
`store.jobs.stale_after_seconds` is taken for lost and requeued. On SIGTERM the slots stop
claiming, running jobs get `shutdown_grace_s` to finish and the rest go back to the queue without
counting the attempt.

Spans (`scribe.job <type>`, `scribe.source_sync`, `scribe.scheduler.tick`) and metrics
(`lorebase.scribe.jobs`, `lorebase.scribe.job.duration`, `lorebase.scribe.scheduled`,
`lorebase.scribe.source.rows`, `lorebase.scribe.source.changes`) are exported when
`telemetry.otlp_endpoint` is set. Logs are structured JSON with the job, type, Domain, status
and duration.

### Scheduler and leader election

Every process may run the scheduler; the one holding a Postgres advisory lock (key derived from
`scheduler.lock_name`, held on a dedicated connection) is the leader, so several replicas never
schedule twice. If the leader dies, its connection and lock go with it and another process takes
over at its next tick (`tick_s`). The leader requeues stale jobs and queues:

- a `webhook_delivery` job every `webhook_interval_s` (an identical queued job is reused, so a
  backlog never piles up);
- a `gc` job per Domain at each firing of `gc_schedule` (cron, UTC);
- a `source_sync` job for each Source whose `schedule` fired, read from the schema of the
  Domain's default branch.

Cron jobs are queued with `run_after` set to the firing time; a firing is due when it is later
than the newest such job or, without one, than the moment the process became leader. Missed
firings are not replayed one by one: a single job covers them.

## Sources

A Source (design 5.9) maps an external table into one Kind. It lives in the schema:

```yaml
sources:
  approved.suppliers:
    id: src_01KEZF7C00EDDEFRGSAXSHCWJ2
    type: csv
    connection: secret://env/SUPPLY_APPROVED_SUPPLIERS_CSV   # a path or an https URL
    target_kind: Supplier
    key_mapping: [supplier_code]
    attribute_mapping:
      legal_name: {column: legal_name, transforms: [{name: trim}]}
      country: {column: country, transforms: [{name: trim}, {name: upper}]}
      risk_score: {column: risk_score, transforms: [{name: cast, args: {to: integer}}]}
      onboarded_on: {column: onboarded_on, transforms: [{name: parse_date, args: {format: '%Y-%m-%d'}}]}
    sync_mode: snapshot
    target_branch_policy: {branch_template: 'source/{source}', merge_policy: auto_when_clean}

  erp.purchase_orders:
    id: src_01KEZF7C00PMKRJJ98PAYYB4SX
    type: postgres_table                  # or postgres_query with `query: SELECT ...`
    connection: secret://env/ERP_DSN      # DSN of a read-only role
    table: erp.purchase_orders            # optional; default: last segment of the api name
    target_kind: PurchaseOrder
    key_mapping: [po_no]
    attribute_mapping:
      supplier: {column: vendor_id}
      status: {column: state, transforms: [{name: lower}]}
    relation_mapping:
      - {relation: for_part, columns: [part_no]}
      - {relation: delivered_to, columns: [plant_code]}
    scope_filter: "state <> 'archived'"
    sync_mode: incremental
    watermark_column: updated_at
    tombstone_column: deleted_at
    schedule: '0 2 * * *'
    target_branch_policy: {merge_policy: auto_with_authority}
```

JSON documents, JSON Lines and Parquet files are local files, like local CSV files:

```yaml
sources:
  plm.parts:
    type: json
    connection: secret://env/PLM_PARTS_JSON     # a path, e.g. /data/sources/plm/parts.json
    records_path: data.parts                    # the array of records; omit when the document is one
    flatten_separator: '.'                      # optional: {"spec": {"weight": 2}} -> spec.weight
    target_kind: Part
    key_mapping: [part_no]
    attribute_mapping:
      name: {column: name}
      weight_kg: {column: spec.weight}
      dimensions: {column: dimensions}          # a nested object, kept as a json value

  mes.events:
    type: jsonl                                 # one object per line, streamed
    connection: secret://env/MES_EVENTS_JSONL
    target_kind: ProductionEvent
    key_mapping: [event_id]
    attribute_mapping:
      status: {column: status}
    sync_mode: incremental
    watermark_column: recorded_at

  finance.invoices:
    type: parquet                               # needs lorebase-scribe[parquet]
    connection: secret://env/FINANCE_INVOICES_PARQUET
    flatten_separator: '_'                      # struct columns: billing_country, billing_city
    target_kind: Invoice
    key_mapping: [invoice_no]
    attribute_mapping:
      amount: {column: amount}
      country: {column: billing_country}
```

An `http_json` Source reads a JSON API with GET requests, page by page:

```yaml
sources:
  crm.accounts:
    type: http_json
    connection: secret://env/CRM_ACCOUNTS_URL   # https://crm.example.com/api/v2/accounts
    records_path: data                          # omit when each response is the array
    target_kind: Account
    key_mapping: [id]
    attribute_mapping:
      name: {column: name}
      owner: {column: owner.email}
    flatten_separator: '.'
    http:
      headers:
        Authorization: secret://env/CRM_TOKEN   # credential headers take secret references only
        X-Tenant: acme                          # other headers may be literal
      pagination:
        type: cursor                            # none | cursor | page | offset | link
        cursor_path: meta.next_cursor           # cursor: where the next cursor is in a response
        cursor_param: cursor                    # cursor: query parameter that carries it
        # page:   page_param (default page), start_page (default 1)
        # offset: offset_param (default offset), page_size (required)
        # link:   follows the rel="next" URL of the Link header
        size_param: limit                       # optional, with page_size: asks for the page size
        page_size: 200
      since_param: updated_since                # incremental runs send the previous watermark
      max_pages: 500                            # optional, below scribe.sources.http_json.max_pages
    sync_mode: incremental
    watermark_column: updated_at
```

**Connections** are secret references, never literals. Scribe resolves them in its own
environment: for CSV the value is a local path or an `http(s)` URL, for JSON, JSON Lines and
Parquet a local path, for `http_json` the URL of the first page, for Postgres a DSN. Only
the references listed in `scribe.sources.allowed_connections` are resolved (empty by default),
and so are secret header values of `http_json` Sources: Sources are written by schema authors,
and without the list any of them could name a reference meant for something else, such as the
DSN of the Lorebase database. Errors name the reference, never the value behind it.

- CSV files must lie under `scribe.sources.csv.allowed_roots`; URLs need a scheme in
  `allowed_schemes` and a host in `allowed_hosts`, are fetched without following redirects and
  stop at `max_bytes`. Both lists are empty by default, which refuses every CSV location. The
  delimiter is sniffed, the first encoding of `encodings` that decodes the file wins and the
  first row is the header.
- A local path is resolved, links included, before the roots check, so links inside a root
  that point inside a root still work. The file is then opened from the root one component at
  a time without following links: a component replaced by a link after the check is refused.
  It must be a regular file (not a directory, FIFO or device) and is read from that descriptor,
  at most `max_bytes`.
- A URL host must resolve to global unicast addresses only, checked as for webhook targets;
  the download connects to those addresses, never to a second lookup, and `timeout_s` bounds
  it from lookup to last byte. Hosts on private networks need `allow_private_targets: true`
  (off by default; earlier versions did not check addresses).
- JSON and JSON Lines files must lie under `scribe.sources.json_files.allowed_roots`, Parquet
  files under `scribe.sources.parquet.allowed_roots` (both empty by default), and are opened
  with the same link and file type checks as CSV files. A `json` document is parsed whole, at
  most `json_files.max_bytes`; its records are the array at `records_path` (dotted keys) or the
  document itself. A `jsonl` file is streamed line by line (blank lines skipped, lines at most
  `max_line_bytes`). A `parquet` file is streamed in batches of `parquet.batch_rows` rows and
  needs pyarrow: install `lorebase-scribe[parquet]` (the run fails with a message naming the
  extra otherwise). Locators are `file.json:<record>`, `file.jsonl:<line>` and
  `file.parquet:<row>`.
- `http_json` URLs, next pages included, follow the CSV URL rules with their own settings
  (`scribe.sources.http_json`): a scheme in `allowed_schemes`, a host in `allowed_hosts`,
  global unicast addresses only unless `allow_private_targets`, looked up once per host and
  run and pinned, no redirects, at most `max_bytes` per response and `timeout_s` per request.
  429 and 5xx answers (`retry_statuses`), network errors and timeouts are retried up to
  `max_attempts` times with exponential backoff (`backoff_base_s`, `backoff_factor`, at most
  `backoff_max_s`, a longer `Retry-After` honored up to that cap); other statuses fail the run.
  Reading stops at a page without records or without a next page; a Source that needs more
  than `max_pages` pages (or its own lower `http.max_pages`) fails instead of committing part
  of itself, since a partial snapshot would remove the Records it did not see. Headers whose
  name suggests a credential (`Authorization`, `Cookie`, names containing `key`, `token`,
  `secret`, `auth`...) must be secret references, checked when the schema is written, so a
  credential never lands in the schema or its history; `Host` and framing headers cannot be
  set. Locators are `<last URL path segment>:<record number>`.
- Records keep their JSON (or Arrow) types: nested objects and arrays are JSON values, which a
  `json` Attribute stores as they are and textual Attributes receive as canonical JSON text.
  `flatten_separator` turns nested objects into columns named `parent<separator>child`
  instead (arrays stay values). Records have no header: a key missing from a record is null
  there, and a mapped column that no record of the run has fails the run.
- Postgres Sources are read in one `READ ONLY` transaction with
  `scribe.sources.postgres.statement_timeout_ms`, `fetch_rows` rows per round trip. The table
  name is checked as an identifier and quoted; `scope_filter` and `query` are SQL written by
  the schema's modelers and reviewed with the schema, so give the DSN a role that can only
  read what the Source needs.

**Mapping.** Each mapped Attribute reads one column through a list of transforms: `trim`,
`lower`, `upper`, `null_if` (`value` or `values`), `constant` (`value`), `concat` (`columns`,
`separator`), `map` (`values`, optional `default`), `parse_date` (`format`, strptime syntax)
and `cast` (`to`: `string`, `integer`, `decimal`, `float`, `boolean`, `date`, `timestamp`).
The result is coerced to the Attribute type through the core canonical encoding: text is cast
with the core cast rules (`'true'` to a boolean, `'12'` to an integer), native Postgres values
are encoded directly, empty text is an absent value and timestamps without a zone are read as
UTC. The key comes from `key_mapping`, one column per primary key Attribute, and sets those
Attributes. Each Relation mapping names the columns holding the primary key of the `to`
Record; empty columns mean no Link. A row whose values do not fit is skipped and reported; more
than `max_row_errors` such rows fail the run without committing anything. Expression mappings
are not evaluated yet: a Source using one is refused.

The Source owns its mapped Attributes and its mapped Relations. Other Attributes of an
existing Record are kept, and Links of other Relations are left alone.

## A synchronization run

1. Scribe resolves the Source (by id or api name) in the schema of the Domain's default branch,
   the target branch, and records the run in `source_runs`.
2. It reads the rows (incremental Sources only those whose watermark column is greater than the
   highest watermark of the last successful run) and maps them. Postgres compares watermarks in
   SQL with the column's type and CSV as text (use ISO timestamps or zero-padded numbers); JSON,
   JSON Lines, Parquet and `http_json` Sources compare natively (numbers, Parquet timestamps)
   or as text when the types differ, so string watermarks should be ISO timestamps too.
3. It prepares the Source branch, `source/<api name>` by default: created from the target
   branch the first time, and recreated at the target head when no Proposal from it is open
   and the heads differ. Every run therefore compares the Source with what the target holds now
   (a manual edit of an owned Attribute on the target is put back by the next run).
4. It computes the delta against the branch head: `added` keys, `changed` keys (the content
   hash of the new Record differs from the stored one; Lineage is not content, so rewriting the
   same values is no change) and `removed` keys. Snapshot Sources remove the Records they own
   (their Lineage names this Source) that the run did not see, unless a row failed; incremental
   Sources remove the Records of rows whose tombstone column is set.
5. It commits the delta on the Source branch in Revisions of at most `commit_batch` changes
   (the changes of one row stay together), with origin `source_sync` and one provenance entry
   per changed row: Source id, run id and row locator (`file.csv:17`, `orders:42`).
6. It applies the merge policy of `target_branch_policy` to the Source's Proposal into the
   target branch, opened on the first changes and updated by later runs (its title carries the
   counts of the last run):

| Policy | Effect |
|--------|--------|
| `proposal` | The Proposal stays open with its merge preview, for review in Keeper or the web app |
| `auto_when_clean` | Merged at once when no Conflict remains; otherwise left open with its Conflicts |
| `auto_with_authority` | As `auto_when_clean`, and value Conflicts on Attributes whose `authority` is this Source are resolved in the Source's favor |

Every Source merge also applies the Domain's `merge_policy` setting, so its `authority`
ranking (for example `["source:<erp source id>", "principal:usr_ops"]`) and `newest_wins`
decide the remaining value Conflicts from the Lineage of both branches
([design 6.10](design.md#610-resolution-flows)).

Automatic merges into a protected target happen only with `auto_merge_protected` (default
true): the policy is part of the reviewed schema. With it off, those Proposals stay open. A run
with no changes leaves an open Proposal untouched; with an automatic policy it retries its merge.

A dry run (`dry_run: true`) reads, maps and computes the delta against the state step 3 would
produce, records the counts in the run and writes nothing else.

## Operations

- Trigger a run: `POST /api/v1/domains/{d}/sources/{s}/sync` with an optional
  `{"dry_run": true}` (permission `source.sync`: `editor`, `maintainer`, `admin`; audited as
  `source.sync`). The answer is 202 with the job id; a second request while the job is pending
  returns the same job.
- Follow runs: `GET /api/v1/domains/{d}/sources/{s}/runs` (Domain-wide `data.read`), newest
  first, with status, counts, rows read and failed, row error samples, Revision, Proposal, merge
  status and duration.
- Scale: run more Scribe processes (or raise `concurrency`); claiming and scheduling are safe
  with replicas. Restrict a process to some job types with `scribe.worker.types`.
- Inspect the queue: `SELECT type, status, attempts, run_after, result FROM jobs ORDER BY id DESC`.
  Failed jobs keep their last error in `result`.
