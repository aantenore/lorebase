# Scribe

Scribe is the Lorebase worker. It runs the jobs queued in Postgres and schedules the periodic
ones: Source synchronization, document extraction ([extraction.md](extraction.md)), webhook
delivery, queued index builds, garbage collection and embeddings
([below](#embeddings)). Design references:
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
lorebase-scribe embeddings supply                 # embed what lacks a vector now (backfill)
lorebase-scribe embeddings supply --reindex --prune
lorebase-scribe modes clear                       # forget the remembered structured output modes
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
| `store.materialize` | creation of a copy branch of a large source (`store.materialize.async_copy_min_rows`); commits that move a base branch past an overlay's `overlay_max_base_lag_revisions`; the deletion of a large base | Copies the base rows of the overlay branch in batches, then makes it a full copy; resumes from its cursor ([store schema notes](store-schema-notes.md#large-branches)). Payload `{"branch": name}` |
| `store.branch_delete` | deletion of a large branch (`store.materialize.async_delete_min_rows`) | Deletes the rows of the hidden branch in batches, then its ref; waits for the copies of overlays based on it ([store schema notes](store-schema-notes.md#large-branches)). Payload `{"branch": name}` |
| `gc` | the scheduler, at `gc_schedule` per Domain; `python -m lorebase_store.gc --enqueue` | Garbage collection of a Domain ([operations](operations.md)) |
| `extraction` | `{"documents": [ids], "tiers": [..] or null, "requested_by"}` | Documents into a Proposal of items ([extraction.md](extraction.md)) |
| `embeddings` | the scheduler, every `embeddings_interval_s` per Domain; a job that used its row budget (continuation) | One embedding pass over the Domain's materialized branches ([below](#embeddings)). Payload `{"branches"?, "reindex"?, "resume"?}`; the scheduled one is `{}` |
| `audit_verify` | the scheduler, every `audit_verify_interval_s` | Recomputes every audit hash chain; the result names the first broken entry, and `lorebase.audit.verifications` counts the outcome ([operations](operations.md#audit-chain-verification)) |

### Worker loop

Each process runs `scribe.worker.concurrency` slots. A slot claims one due job with
`SELECT ... FOR UPDATE SKIP LOCKED`, so replicas never run the same job, runs it outside any
transaction and records the outcome. A failure is retried after `store.jobs.retry_delay_seconds`
until `store.jobs.max_attempts`; failures that a retry cannot fix (an unknown Source, an invalid
mapping, rows the branch rejects, more failed rows than allowed) fail the job at once.

Jobs run at least once, so every handler tolerates running the same job again. Running jobs
refresh their lock every `heartbeat_interval_s` (30 s). A job without heartbeat for
`store.jobs.stale_after_seconds` (90 s, three missed heartbeats) belongs to a worker that is gone
(killed, out of memory, cut off from the database): the scheduler leader reclaims it at its next
tick, queued again, or failed when it has used `store.jobs.max_attempts` (a job that keeps
killing its worker is not run forever). A worker that lost a job this way cannot record its
outcome over the next run. `lorebase.jobs.reclaimed` counts reclaimed jobs by type and outcome.

On SIGTERM (Scribe runs as PID 1 of its container and handles the signal itself) the slots stop
claiming and running jobs get `shutdown_grace_s` to finish. The rest are cancelled and go back to
the queue without counting the attempt; a job whose handler has not stopped `shutdown_cancel_s`
later is released anyway, so a worker that stops cleanly leaves no job `running`
(`lorebase.jobs.released`). Keep `shutdown_grace_s` plus `shutdown_cancel_s` below the time the
platform waits before it kills the process (`SCRIBE_STOP_GRACE` in compose,
`scribe.terminationGracePeriodSeconds` in the chart).

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
  Domain's default branch;
- an `embeddings` job per Domain every `embeddings_interval_s` (0 disables; an identical queued
  job is reused);
- an `audit_verify` job every `audit_verify_interval_s` (one day; 0 disables; an identical queued
  job is reused). The interval is kept in memory, so a process that becomes leader queues one at
  its first tick.

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

MySQL, MariaDB and SQL Server Sources take the same fields as Postgres ones (`table`, `query`,
`scope_filter`, `watermark_column`, `tombstone_column`):

```yaml
sources:
  erp.shipments:
    id: src_01KEZF7C00SHIPMENTSMYSQL01
    type: mysql_table                     # mysql_query with `query:`; MariaDB works too
    connection: secret://env/ERP_MYSQL_DSN     # mysql://reader:...@db:3306/erp
    table: shipments
    target_kind: Shipment
    key_mapping: [shipment_no]
    sync_mode: incremental
    watermark_column: updated_at
    target_branch_policy: {merge_policy: auto_when_clean}

  finance.invoices:
    id: src_01KEZF7C00INVOICESMSSQL001
    type: mssql_query                     # mssql_table reads `table: dbo.invoices`
    connection: secret://env/FINANCE_MSSQL_DSN # mssql://reader:...@db:1433/finance
    query: SELECT invoice_no, supplier_code, total, modified_at FROM dbo.invoices
    target_kind: Invoice
    key_mapping: [invoice_no]
    sync_mode: incremental
    watermark_column: modified_at
    target_branch_policy: {merge_policy: proposal}
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
Parquet a local path, for `http_json` the URL of the first page, for Postgres a DSN, for MySQL
and SQL Server a URL (`mysql://user:password@host:3306/database`,
`mssql://user:password@host:1433/database`). Only
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
- MySQL, MariaDB and SQL Server Sources (`mysql_table`, `mysql_query`, `mssql_table`,
  `mssql_query`) are read the same way, fetched `fetch_rows` rows at a time, with their own
  settings (`scribe.sources.mysql`, `scribe.sources.mssql`: `statement_timeout_ms`,
  `connect_timeout_s`, `fetch_rows`). Their drivers are optional extras and the run fails with a
  message naming the extra when it is missing: `lorebase-scribe[mysql]` (asyncmy, async, wheels
  for the usual platforms) and `lorebase-scribe[mssql]` (pymssql, run in worker threads; FreeTDS
  is bundled, so no ODBC driver is needed). MySQL and MariaDB sessions are `READ ONLY` and
  bounded by `max_execution_time` (MariaDB: `max_statement_time`). SQL Server has no read-only
  transaction: the login must be able to read only. `query` is wrapped as
  `SELECT * FROM (<query>) AS lorebase_source`; SQL Server refuses an `ORDER BY` in a derived
  table unless it has `TOP` or `OFFSET`, so a query whose top level ends in `ORDER BY` gets
  `OFFSET 0 ROWS` appended (the rows and their order are unchanged). `datetime2` watermarks keep
  their microseconds and a `rowversion` watermark is binary: it is stored in the run as
  hexadecimal text and bound back as bytes. Locators are `<table>:<row>` or `query:<row>`.

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
than `max_row_errors` such rows fail the run without committing anything. An Attribute mapped
by `expression` instead of `column` starts from a CEL expression over the row: each column
whose name is an identifier is a variable of unknown type (`dyn`, so CSV text needs
`int(price)`), and `row["Legal name"]` reads any column. Variables are the columns of the
Source (the header, or every key of the JSON records read, whose rows are then mapped once all
are read): a column a row lacks is null. The expression is bounded by
`schema.expression_limits`; its result then goes through the transforms and the coercion like a
column's, and the Modeler previews it (`POST /domains/{d}/sources:preview`).

The Source owns its mapped Attributes and its mapped Relations. Other Attributes of an
existing Record are kept, and Links of other Relations are left alone.

## A synchronization run

1. Scribe resolves the Source (by id or api name) in the schema of the Domain's default branch,
   the target branch, and records the run in `source_runs`.
2. It reads the rows (incremental Sources only those whose watermark column is greater than the
   highest watermark of the last successful run) and maps them. Postgres, MySQL and SQL Server
   compare watermarks in SQL with the column's type and CSV as text (use ISO timestamps or zero-padded numbers); JSON,
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

## Embeddings

Semantic and hybrid search rank stored vectors against a query vector the caller computes
([design 8.4](design.md#84-search)); Scribe computes the stored ones
([design 9.3](design.md#93-embedding-jobs)) with the model of `inference.embeddings`
([inference providers](inference-providers.md)).

A Kind opts in with an embedding template in the schema; nothing is embedded otherwise, and a
Domain without templates never creates the model:

```yaml
kinds:
  Supplier:
    search:
      embedding_template: "{legal_name}, {city} ({country}). Tier {tier}."
      # embedding_sensitive: true   # needed to name a pii/confidential/restricted Attribute
```

Placeholders are Attribute api names, plus `{_kind}` (the Kind's display name) and `{_key}`;
`{{` and `}}` are literal braces. Derived Attributes cannot be named. Schema validation reports
`schema.embedding_template_invalid` for a template it cannot use.

Each `embeddings` job runs one pass:

- branches with no template, and branches whose head has not moved since the last complete pass
  for the model, are skipped;
- otherwise the branch's own rows that lack a vector of the model under their Kind's template
  are rendered and embedded, plus every row of the Kinds whose template changed (the template
  fingerprint is recorded per branch in `embedding_cursors`);
- a text whose hash equals the stored one is left alone; a text already embedded for another
  Record content or another template is copied, not sent to the model;
- vectors are keyed by content hash and template fingerprint, so overlay and copied branches
  share the vectors of their unchanged Records and only add work for the Records they change,
  while a branch whose template differs keeps vectors of its own and Keeper searches each
  branch with its own template's vectors;
- the templates of a schema are resolved once per schema hash and kept for the last
  `template_cache_size` schemas, so branches sharing a schema do not read and parse it again.

Garbage collection ([operations](#operations)) deletes the vectors no branch reads any more:
those whose content no branch holds, and those of templates no branch uses for that content.

| Setting (`scribe.embeddings`) | Default | Meaning |
|-------------------------------|---------|---------|
| `page_rows` | 500 | rows read and rendered per query |
| `batch_texts` | 64 | texts per model call |
| `max_records_per_job` | 20000 | rows one job reads; the rest goes to a queued continuation |
| `max_calls_per_second` | 0 | pacing of model calls (a paid endpoint's rate limit); 0: none |
| `max_text_chars` | 8000 | rendered texts are cut to this length |
| `template_cache_size` | 64 | schemas whose resolved templates a process keeps, by schema hash |
| `retry.attempts`, `backoff_s`, `backoff_max_s` | 2, 2.0, 60.0 | retries of a failed call inside the job |

`store.embeddings.dims` must equal `inference.embeddings.dims`: the job checks the model's
dimension and embeds a probe text before its first call, and fails without retries on a
mismatch. One pass per Domain runs at a time.

**Switching model.** Set the new `inference.embeddings` and run
`lorebase-scribe embeddings DOMAIN` (or let the schedule do it): vectors are stored under the
new model name next to the old ones. Then point Keeper's `search.semantic.model` at the new
name (`fake:<model>` for the fake provider) and run `lorebase-scribe embeddings DOMAIN --prune`
to delete the old vectors. `--reindex` renders every text again and re-embeds those that changed.

**Query vectors.** Keeper and Herald never call a model. Compute the query vector with the same
settings: `lorebase-inference embed --query "late deliveries" | lorebase search "late deliveries"
--vector-file -` (hybrid), or `create_embedding_model(settings).embed([text])` in Python.

## Operations

- Trigger a run: `POST /api/v1/domains/{d}/sources/{s}/sync` with an optional
  `{"dry_run": true}` (permission `source.sync`: `editor`, `maintainer`, `admin`; audited as
  `source.sync`; a Source that maps vault Attributes also needs `pii.import` Domain-wide, as
  do schema changes and merges that add or change such a Source, design 13.5). The answer is
  202 with the job id; a second request while the job is pending returns the same job.
- Follow runs: `GET /api/v1/domains/{d}/sources/{s}/runs` (Domain-wide `data.read`), newest
  first, with status, counts, rows read and failed, row error samples, Revision, Proposal, merge
  status and duration.
- Scale: run more Scribe processes (or raise `concurrency`); claiming and scheduling are safe
  with replicas. Restrict a process to some job types with `scribe.worker.types`.
- Inspect the queue: `SELECT type, status, attempts, run_after, result FROM jobs ORDER BY id DESC`.
  Failed jobs keep their last error in `result`.
