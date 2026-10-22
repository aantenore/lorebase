# The five-day demo

The proof scenario of [design 3.2](design.md#32-the-5-day-proof-scenario) as a script. A fictional pilot at Corvane Industries, an industrial components distributor, runs for five days against a real stack: Postgres, Keeper, Scribe, and the `lorebase` command line as the client. Every step is a command you can read; the script prints each one before its output.

```bash
make demo                # stack up, five days, stack removed
make demo KEEP=1         # leave the stack running; run it again with examples/five_day_demo/run.sh
make demo-down           # remove a kept stack and its data
```

It needs Docker with compose, [uv](https://docs.astral.sh/uv/) and `uv sync --all-packages` run once. The stack is the compose project `lorebase-demo`, with Keeper on `127.0.0.1:18080` and Postgres on `127.0.0.1:15432` (`DEMO_KEEPER_PORT`, `DEMO_POSTGRES_PORT`, `DEMO_PROJECT`), so it does not collide with a stack you run for development. Timings of the run below, on a laptop: building the image and starting the stack 374 s (the first build, dominated by pulling base images), the five days 45 s, document and checks included. With the image cached, only Postgres, the migrations and Keeper have to start.

| File | What it is |
|------|------------|
| `examples/five_day_demo/stack.sh` | `up`, `down`, `demo`: the compose stack under its own project name; writes its passwords and the API key pepper to `build/demo` (git-ignored); creates the first administrator key with `python -m lorebase_keeper bootstrap` (in `api_key` mode nobody can issue the first key over the API) and the service principal `agent-risk` with `principal create` |
| `examples/five_day_demo/run.sh` | The five days, in bash with strict mode; idempotent |
| `examples/five_day_demo/demo.py` | The parts that are awkward in shell: schema inference, rebasing the curated schema, conflict tables, the webhook receiver, the MCP client, rate-limit probes, and the assertions (`assert-*`) and the timing table |
| `examples/five_day_demo/documents/` | The contract (Markdown) that day 2 extracts |
| `examples/five_day_demo/lorebase.demo.yaml` | The stack's settings: `api_key` authentication, self-approval, a small context bucket, private webhook targets, short polling intervals |

`run.sh` works against any Keeper. Its parameters, all environment variables:

| Variable | Meaning | Default |
|----------|---------|---------|
| `LOREBASE_URL` | Keeper | `http://127.0.0.1:8080` |
| `LOREBASE_API_KEY` | Administrator key; leave it out for a Keeper with `keeper.auth.mode: none` (then the agent has no quota to show) | none |
| `LOREBASE_DOMAIN` | Domain created by the demo | `supply` |
| `DEMO_RESET` | `1` deletes the Domain left by an earlier run, but only one named "Lorebase five-day demo" | `1` |
| `DEMO_SOURCES_DIR` | Directory Scribe reads CSV Sources from; without it day 4's Source step is skipped | unset |
| `DEMO_STORE_EXEC`, `DEMO_POSTGRES_EXEC` | Command prefixes that run a command in the backend and Postgres containers (`docker compose ... exec -T keeper`); without them the backup and garbage collection steps are skipped | unset |
| `DEMO_NO_BUILD` (`stack.sh`) | `1` starts the stack from the image `lorebase-demo:local` without building it; the CI workflow builds that image first, with a layer cache | unset |
| `DEMO_RECEIVER_PORT`, `DEMO_RECEIVER_BIND`, `DEMO_WEBHOOK_HOST` | The webhook receiver of day 4 and how Scribe reaches it | `19090`, `127.0.0.1`, `host.docker.internal` |

## Assertions and CI

Every day ends with assertions, printed as `check ok: ...` lines, so the transcript doubles as a test. A failed one prints `assertion failed: ...` and stops the script with exit code 1; a command of the `lorebase` command line that fails stops it too. The run ends with a table of the seconds each day took, also when it fails (then it names the day).

| Day | What is asserted |
|-----|------------------|
| 1 | `main` holds the curated schema (Kinds, Relations, Traits, Operations, Derivations and Sources by name), reached it through the merged Proposal of `model/initial`, and is protected |
| 2 | every CSV row is a Record or a Link on `main` (counts per Kind and Relation); the derived value `single_sourced` answers a query; search finds Parts; the extraction Proposal has exactly the Contract and its Link, from tier 0, pending; after the decision the Contract, its Link and their `extraction` Lineage are on `main` |
| 3 | the Proposal merged, its Conflicts were raised and none is open; the value decided by hand and the non-conflicting changes of both teams are on `main`; the suppliers added on branches exist; time travel shows day 2 |
| 4 | the Source ran twice and succeeded; the second run found two changed rows and its Proposal merged by policy; the two cells changed on `main`; the change feed lists them; the webhook delivered with a valid signature |
| 5 | Herald lists its tools; the query returns rows with their suppliers; the context pack describes the Record; the dry run reports the head and no violation; the commit lands on the work branch; a critical flag is refused with `review-required`, and with a task opens a Proposal; the rate limit serves the burst and then answers 429 with `Retry-After`; both flags reach `main` through merged Proposals, no Proposal is left open, the audit log has the writes, and a direct write by the agent to `main` is refused; the restored database has the head and the Record count of the live one |

The End-to-end workflow (`.github/workflows/e2e.yaml`) runs it in CI: on pushes to `main`, nightly, on demand and on pull requests that change `examples/`, `packages/`, `deploy/compose/` or `deploy/docker/`. It builds the backend image from the checkout (Docker layers cached in the GitHub Actions cache), starts the stack with `stack.sh up` (`DEMO_NO_BUILD=1`), runs `run.sh`, writes the timing table into the job summary, and on failure uploads the compose logs, the run transcript and the work files (without keys and secrets) as the artifact `five-day-demo-logs` before `stack.sh down` removes the stack. The stack uses the deterministic fake provider for every model task (`inference.llm`, `embeddings` and `local_extractors` are `fake` in `lorebase.demo.yaml`), so CI downloads no model and uses no secret. The job is limited to 20 minutes.

## How the days map to the design

The demo follows what is built. Document extraction (design day 3) is shown at the end of day 2, with the deterministic tiers, so the days are: model, load and explore (with a first document), version and merge, sync a Source, agents.

| Demo | Design 3.2 | Shown |
|------|-----------|-------|
| Day 1 | Day 1 | schema inferred from CSV files, curated, reviewed in a Proposal |
| Day 2 | Day 2 (files), Day 3 | batch import, derived values, query, search, context pack, explain, health; a contract document extracted into a Proposal of items that a reviewer accepts |
| Day 3 | Day 4 | two teams, schema and data Conflicts, resolution, time travel |
| Day 4 | Day 2 (Sources) | CSV Source synced twice, a Proposal that merges itself, change feed and webhook |
| Day 5 | Day 5 | agent key, Herald over MCP, a governed write, rate limits and quotas, audit log, backup, garbage collection |

The transcripts below are condensed: repeated tables are cut, ids shortened, the repository path replaced by `<repo>`. `$` lines are the commands, `#` lines the narration.

## Day 1: install and model from zero

Priya has the stack up and creates the Domain; `main` is protected from the start. Marco does not start from a blank page: `schema:infer` reads the client's twelve CSV files and proposes an ontology without writing anything.

```text
$ lorebase domain create supply --name 'Lorebase five-day demo' --description '...'
id              supply
default branch  main
$ lorebase branch list
main    4ab4f509d484  true (protected)

$ demo.py infer build/demo/work/inferred.yaml
read 12 CSV files: 6 Kinds, 6 Relations, 426 rows
  kind           key              attributes
  Contract       contract_number  12
  Part           part_number      8
  Plant          plant_code       6
  PurchaseOrder  po_number        8
  RiskFlag       (minted)         4
  Supplier       supplier_code    9

  relation       ends                       cardinality   link attrs
  supplier       PurchaseOrder -> Supplier  many_to_one   0
  stocked_at     Part -> Plant              many_to_many  1
  supplied_by    Part -> Supplier           many_to_many  2
  ...
63 decisions, lowest confidence 0.85; 1 issue(s):
  infer.ambiguous_primary_key: 2 key candidates in 'supplier': supplier_code, vat_number; 'supplier_code' was chosen
```

He applies the proposal on a branch, then curates it with what only a modeler knows: a Trait, two Derivations, two Operations and two Sources. The curated document (`examples/supply/schema.yaml`) carries other ids than the inferred elements, so `demo.py curate` drops the ids of elements that exist and lets the loader keep the inferred ones; the diff is then an update, not a replacement. The inference named the order-to-supplier Relation `supplier`; the curated schema calls it `issued_to`.

```text
$ lorebase branch create model/initial
$ lorebase schema apply build/demo/work/inferred.yaml -b model/initial -m 'Apply the inferred schema'
status           committed
element changes  14
$ demo.py curate build/demo/work/curated.yaml --branch model/initial
$ lorebase schema diff build/demo/work/curated.yaml -b model/initial
update  kinds        Supplier             knd_0000000000BPGHZWMBB1S5R8H7
create  relations    issued_to            rel_01KEZF7C0046ZA6Z11PH3MARM5
create  traits       Located              trt_01KEZF7C00E1SNG0SW6QRM5904
create  operations   flag_supplier_risk   opr_01KEZF7C0038VRE1JRZAKM367W
create  derivations  single_sourced       drv_01KEZF7C00DFJP2E5C2F29QVHQ
create  sources      approved.suppliers   src_01KEZF7C00EDDEFRGSAXSHCWJ2
delete  relations    supplier             rel_0000000000KYHPEKGPX0P4YQ1C
...
$ lorebase schema apply build/demo/work/curated.yaml -b model/initial -m 'Curate the schema'
element changes  22
```

Sofia reviews the change in a Proposal and merges it. Required approvals apply to `main`; the demo allows self-approval (`keeper.proposals.allow_self_approval`) because one operator plays every role.

```text
$ lorebase --json proposal create model/initial main --title 'Model the supply Domain'
$ lorebase proposal get prp_01M5E0SEE1ZQ8YW2AP708EF4QJ
branches   model/initial -> main
status     open
checks     merge: passed, approvals: pending
$ lorebase proposal review prp_01M5E0SEE1ZQ8YW2AP708EF4QJ approve --comment Reviewed
approvals  1/1 (satisfied)
$ lorebase proposal merge prp_01M5E0SEE1ZQ8YW2AP708EF4QJ -m 'Model the supply Domain'
result     fast_forward
$ lorebase log main -n 3
2e2a6cbef748  3  svc_demo_admin  schema_change  Curate the schema
312c7ac4a95e  2  svc_demo_admin  schema_change  Apply the inferred schema
ddf4dcc1f2b7  1  svc_demo_admin  domain.create  Create Domain
```

## Day 2: structured ingestion and exploration

Dana loads the CSV files with one batch import: a Record travels with its Links in one Revision, so the Records that need a Link on creation (`Contract`, `PurchaseOrder`, `RiskFlag`) pass validation. The data goes through a branch and a Proposal like the schema did.

```text
$ lorebase branch create data/initial
$ lorebase import batch -b data/initial --records Supplier=examples/supply/data/supplier.csv --records Part=... \
    --links supplied_by=examples/supply/data/supplied_by.csv --links stocked_at=... (6 Kinds, 6 Relations)
batch 1/1: rows 1-426 committed, 426 edits
$ lorebase proposal create / review / merge  (data/initial -> main, "Load the supply data")
```

Luca explores. `supplier_count` and `single_sourced` are Derivations computed by the materializer on every commit, never stored in the tree.

```text
$ lorebase record list Part -n 4 --select category --select unit_cost --select supplier_count --select single_sourced
key      title                        category  unit_cost  supplier_count  single_sourced
P-10001  Tapered roller bearing 6305  bearing   10.79      1               true
P-10002  Flat washer M10x60           fastener  0.34       2               false
$ lorebase query '{"kind": "Part", "where": {"attr": "single_sourced", "op": "eq", "value": true}, "aggregate": {"group_by": "category"}}'
Part  bearing   6
Part  filter    6
...
$ lorebase search bearing --kind Part -n 3
Part  P-10013  Needle roller bearing NK25  2.554  Needle roller <b>bearing</b> NK25 Stock item for packaging …
$ lorebase context --ref Supplier/S-1001 --budget 1500
seeds      1
neighbors  4
tokens     1169/1500
$ lorebase explain Supplier S-1001
created by  svc_demo_admin
proposals   prp_01M5E0SH3ZJ666W8C0QH35VH2M (merged)
legal_name  Kavsen Alloys Sp. z o.o.  svc_demo_admin  import  f527df6292cc
$ lorebase health
status                           ok
records                          185
invalid records                  0
orphan links                     0
duplicate groups                 11
```

Procurement also sends a framework agreement as a document (`examples/five_day_demo/documents/contract-c-3099-wexrix-filtration.md`, a contract that is not in the CSV files). Scribe reads it with the deterministic tiers: rules and the fake span extractor of the demo's configuration, no model. It opens a Proposal of two items, a new Contract found by the pattern of its key and a `with_supplier` Link to S-1013, each with its evidence and a confidence below the auto-accept threshold, so both wait for a reviewer. A new Contract needs its Link to pass validation, so Dana accepts both; the Proposal then merges like any other and the values carry `extraction` Lineage.

```text
$ lorebase documents upload examples/five_day_demo/documents/contract-c-3099-wexrix-filtration.md --tiers 0,1
$ demo.py extraction-proposal contract-c-3099-wexrix-filtration.md
$ lorebase proposal items prp_01M5H6Q6F2845N7BGP67R8JB3X
record  pending  0.425  Contract       C-3099
link    pending  0.6    with_supplier
$ lorebase proposal decide prp_01M5H6Q6F2845N7BGP67R8JB3X --where-status pending --decision accepted
items decided  2
$ lorebase proposal review / merge  (extract/exj_... -> main)
$ lorebase explain Contract C-3099
incoterm            DAP         svc_demo_admin  extraction  0ba7af213adb
```

## Day 3: versioning and conflicts

Two teams branch from `main` and change the same suppliers without knowing about each other.

Team risk (Marco) changes the schema and the data: a new required Attribute `risk_band` with a default (the schema change backfills the 30 existing Records: `data changes 30`), new scores for S-1001 and S-1002 and a new supplier S-1031. Team procurement (Sofia) flips the tier of four suppliers, scores S-1001 differently and adds S-1031 (not the same company) and S-1032. An import replaces whole rows, so `demo.py team-csv` writes the complete Supplier file each team imports.

```text
$ lorebase branch create model/risk
$ lorebase branch create cleanup/tiers
$ lorebase schema get -b main --output build/demo/work/main.yaml
$ demo.py risk-model build/demo/work/main.yaml build/demo/work/risk.yaml
$ lorebase schema apply build/demo/work/risk.yaml -b model/risk -m 'Add risk_band'
element changes  1
data changes     30
$ demo.py team-csv risk build/demo/work/team_risk.csv
team risk: rescored S-1001 to 80, S-1002 to 55, banded every supplier, added S-1031
$ lorebase import records Supplier build/demo/work/team_risk.csv -b model/risk
batch 1/1: rows 1-31 committed, 31 edits
$ demo.py team-csv tiers build/demo/work/team_tiers.csv
team procurement: flipped the tier of S-1001, S-1002, S-1003, S-1004, S-1001 score 45, added S-1031, S-1032
$ lorebase import records Supplier build/demo/work/team_tiers.csv -b cleanup/tiers
$ lorebase diff main cleanup/tiers --kind Supplier -n 4
6 changes (Supplier: +2 -0 ~4)
modified  kind  Supplier  S-1001  tier: 1 -> 2; risk_score: 32 -> 45
added     kind  Supplier  S-1032  supplier_code: (none) -> S-1032; ...
```

Team risk goes first and merges without Conflicts: schema and data travel together. Team procurement is then merged against the new `main`.

```text
$ lorebase proposal merge prp_01M5E0YCDX0TBS8RG98ZHATR3F
error: 7 Conflict(s) remain; resolve them and merge again.
(refused, exit code 2)
$ demo.py conflicts prp_01M5E0YCDX0TBS8RG98ZHATR3F
id            status  code  record  attribute     base    main         branch
cfa8913dd0dd  open    DV1   S-1001  risk_score    32      80           45
4b5ccf209bac  open    DV3   S-1031  city          (none)  Hamburg      Berlin
f5414778ca1f  open    DV3   S-1031  legal_name    (none)  Norvik GmbH  Norvik Fasteners Sp. z o.o.
...  (S-1031: six add/add Conflicts, one per Attribute)
```

S-1002 changed on both sides too (a score on one, a tier on the other) and merged without a Conflict: the merge works per Attribute. One Conflict is decided by hand, the other six in bulk from a file (the policy: keep the Record team risk added). Every resolution is stored with its resolver.

```text
$ lorebase proposal resolve prp_01M5E0YCDX0TBS8RG98ZHATR3F cfa8913d... --choice custom --value '"75"'
choice    custom
value     75
$ demo.py resolutions prp_01M5E0YCDX0TBS8RG98ZHATR3F build/demo/work/resolutions.json
6 Conflicts take 'ours' (keep the Record team risk added)
$ lorebase proposal resolve prp_01M5E0YCDX0TBS8RG98ZHATR3F --file build/demo/work/resolutions.json
status              resolved
open conflicts      0
resolved conflicts  7
$ lorebase proposal merge prp_01M5E0YCDX0TBS8RG98ZHATR3F -m 'Reclassify supplier tiers'
result     merged
```

Time travel compares `main` today with `main` as loaded on day 2:

```text
$ lorebase record get Supplier S-1001              $ lorebase record get Supplier S-1001 --at 1592765bdd13...
tier        2                                      tier        1
risk_score  75                                     risk_score  32
risk_band   high                                   (no risk_band yet)
$ lorebase diff 1592765bdd13... main --kind Supplier -n 5
32 changes (Supplier: +2 -0 ~30)
```

## Day 4: a Source that keeps syncing, and a change feed

Priya registers a webhook for `main`; `demo.py receiver` is a tiny HTTP server on the host that verifies the `Lorebase-Signature` of each delivery. Dana's Source `approved.suppliers` is declared in the schema (a CSV, mapped to Suppliers, merge policy `auto_when_clean`); procurement exports its list to the directory Scribe reads, and Scribe syncs it.

```text
$ demo.py webhook-register http://host.docker.internal:19090/hook build/demo/work/webhook.secret --branch main
webhook whk_4561...: ['revision.committed'] on ['main'] -> http://host.docker.internal:19090/hook
$ demo.py export-suppliers build/demo/sources/supplier.csv
procurement exported 32 approved suppliers to build/demo/sources/supplier.csv
$ lorebase sources sync approved.suppliers
job      612
$ demo.py wait-run approved.suppliers --after none
run srn_01M5E0YKE336 succeeded: read 32 rows, added 0, changed 0, unchanged 32, removed 0
no changes: no branch commit, no Proposal
```

The first run finds nothing to do. Procurement changes two cells; the second run finds them, commits on `source/approved.suppliers` and opens a Proposal; the merge policy merges it because nothing conflicts.

```text
$ demo.py csv-set build/demo/sources/supplier.csv S-1005 city=Madrid
  S-1005.city: Valencia -> Madrid
$ demo.py csv-set build/demo/sources/supplier.csv S-1008 tier=1
  S-1008.tier: 3 -> 1
$ lorebase sources sync approved.suppliers
$ demo.py wait-run approved.suppliers --after srn_01M5E0YKE33698JF386GTM7X6S
run srn_01M5E0YND82R succeeded: read 32 rows, added 0, changed 2, unchanged 30, removed 0
proposal prp_01M5E0YNEFZX8JVH36YF688363 merged (merge policy result: merged)
$ lorebase proposal list --status merged -n 2
prp_01M5E0YNEFZX8JVH36YF688363  Sync approved.suppliers: 0 added, 2 changed, 0 removed  source/approved.suppliers  main  merged
$ lorebase explain Supplier S-1005
city          Madrid        svc_scribe      source_sync       79d120c60008  2026-10-21T01:24:31Z
risk_band     medium        svc_demo_admin  schema_migration  79d120c60008
```

The change feed (polling form; `GET .../branches/{branch}/events` is the Server-Sent Events form) and the delivery the receiver verified:

```text
$ demo.py changes main --since caf0755551653d4b...
gen  revision      author      kinds       message
8    79d120c60008  svc_scribe  Supplier:2  Sync approved.suppliers: +0 ~2 -0
$ demo.py wait-lines build/demo/work/receiver.log signature=ok --count 1 --timeout 30
delivery 3 revision.committed signature=ok branch=main revision=79d120c60008 author=svc_scribe kinds=Supplier:2 message='Sync approved.suppliers: +0 ~2 -0'
```

## Day 5: agents

Alex gets a key for his agent: role `agent-reader` on the Domain, a grant of role `operator` on one Operation and the role `editor` on the branches `agent/*` (its work branches and the Proposals it opens; `main` stays out of reach). The Operation `flag_supplier_risk` is restricted to the subject `agent-risk`, so the key is issued on that service principal (`stack.sh` created it with `python -m lorebase_keeper principal create agent-risk`: the API creates service principals only with generated ids, and groups not at all). The key authenticates as the agent and has its own quota.

```text
$ lorebase branch create agent/flags
$ demo.py agent-setup agent-alex build/demo/work/agent.key
service principal agent-risk, key lff4qmdxzf... (7 days)
grants: agent-reader on the Domain, operator on Operation flag_supplier_risk,
        editor on branches agent/*
the secret is in build/demo/work/agent.key and is not printed
$ LOREBASE_API_KEY=<the agent key> lorebase whoami
id                    agent-risk
method                api_key
requests today        1 of 100000
context tokens today  0 of 2000000
```

The agent is a scripted MCP client (the `mcp` SDK) that starts Herald over stdio with the agent's key and calls its tools. No model runs anywhere: the times are Herald and Keeper.

```text
$ demo.py agent build/demo/work/agent.key
Herald over stdio exposes 13 tools: lorebase_branches, lorebase_changes, lorebase_context, lorebase_diff, lorebase_execute, ...

Question: which active contracts end by 2026-04-15, and who is the supplier?
> lorebase_query {"query":{"kind":"Contract","where":{"and":[{"attr":"end_date","op":"lte","value":"2026-04-15"},{"attr":"status","op":"eq","value":"active"}]},"include":{"with_supplier":{}},"limit":3}}
  Contract/C-3001 "Framework supply agreement 2023 - Kavsen Alloys Sp. z o.o." | ...
    with_supplier: Supplier/S-1001 "Kavsen Alloys Sp. z o.o."
  ...
  [ok, 38 ms through Herald and Keeper]

> lorebase_context {"records":["Supplier/S-1001"],"depth":1,"token_budget":1200}
  # Context: supply/main
  revision 79d120c6...; estimated 886 of 1200 tokens
  ### Supplier/S-1001: Kavsen Alloys Sp. z o.o.
  - risk_band: high
  - risk_score: 75
  [ok, 13 ms through Herald and Keeper]

Governed write on a work branch, dry run first (the default of lorebase_execute):
> lorebase_execute {"operation":"flag_supplier_risk","parameters":{"supplier":"S-1001","severity":"high","reason":"..."},"branch":"agent/flags","dry_run":true}
  dry_run flag_supplier_risk on agent/flags (head 79d120c6...): 2 edits, 0 violations
  next: call again with dry_run=false and expected_head=79d120c6... to commit
  [ok, 21 ms through Herald and Keeper]
> lorebase_execute {... "dry_run":false,"expected_head":"79d120c6...","idempotency_key":"demo-flag_supplier_risk-S-1001"}
  committed flag_supplier_risk on agent/flags ...: 2 edits, 0 violations
  [ok, 20 ms through Herald and Keeper]
> lorebase_execute {... "severity":"critical","dry_run":false}
  Error executing tool lorebase_execute: [review-required] Edits of 'flag_supplier_risk' on branch 'agent/flags' need review ...
  next: this change needs review: call again with task and intent so that Keeper runs it on a task branch and opens the Proposal, or run it o...
  [error, 9 ms through Herald and Keeper]
```

A high flag commits on the agent's work branch. The Operation's review policy refuses a critical one on any branch. The call names no `task`, so it is not turned into a Proposal: the refusal lists the next steps. The agent then repeats it with a `task` and an `intent`, and Keeper runs it on the task branch `agent/agent-risk/critical-flag-s-1001` and opens a Proposal that needs a person's approval; the agent cannot merge it.

Rate limits are per principal and request class. The demo's configuration gives the Domain a small bucket for context packs, so the limit is visible in a few calls; the quota counts per UTC day.

```text
$ demo.py limits build/demo/work/agent.key
GET records -> 200
  RateLimit-Limit: 200
  RateLimit-Remaining: 199
  RateLimit-Reset: 1
8 context packs in a row (this Domain allows a burst of 4 per class):
  1: 200 remaining 2
  2: 200 remaining 1
  3: 200 remaining 0
  4: 429 rate-limited, Retry-After 2 s, class context
  ...
whoami for the agent key: agent that flags supplier risk (service, api_key)
  requests today: 11 of 100000
  context_tokens today: 3765 of 2000000
```

Sofia takes the agent's flag through a Proposal like any other change and approves the Proposal the agent opened for the critical flag. Priya shows the audit log (the refused requests are in it too). Last, the checks of the day: both flags are on `main` only through merged Proposals, and a direct write by the agent to `main` is refused:

```text
$ lorebase proposal create / review / merge  (agent/flags -> main, "Risk flags raised by the agent")
$ demo.py audit --limit 8
230  proposal.merge   prp_01M5E0YSQZJ6XWZBTCJNZRJHVH   svc_demo_admin
227  agent-risk       POST /api/v1/domains/{domain_id}/context
223  agent-risk       operation.execute                  flag_supplier_risk
$ demo.py wait-lines build/demo/work/receiver.log signature=ok --count 1 --timeout 30
delivery 3 revision.committed signature=ok branch=main ... author=svc_scribe kinds=Supplier:2 ...
delivery 4 revision.committed signature=ok branch=main ... author=agent-risk kinds=RiskFlag:1 message='flag_supplier_risk (inv_...)'
```

Finally, maintenance. A branch that was thrown away leaves Revisions that nothing reaches; the dry run of the garbage collector counts them (`--grace-seconds 0` ignores the one-hour protection of young Revisions, which the demo would otherwise always hit). A bundle is a portable copy of the Domain; it is restored into an empty database of the same Postgres server. A Revision id includes the Domain id, so a bundle keeps its Domain id: it moves between installations, and the demo restores it into a second database rather than a second Domain.

```text
$ lorebase branch create scratch/experiment
$ lorebase import records Supplier build/demo/work/team_risk.csv -b scratch/experiment
$ lorebase branch delete scratch/experiment
$ docker compose exec keeper python -m lorebase_store.gc --domain supply --dry-run --grace-seconds 0
{"domain_id": "supply", "dry_run": true, "revisions": 1, "changesets": 6, "provenance": 1, "nodes": 2, "node_bytes": 2091, ...}

$ docker compose exec keeper python -m lorebase_store.bundle export --domain supply --out /tmp/demo-bundle.tar
{"domain_id": "supply", "revisions": 10, "changesets": 526, "objects": 525, "refs": 7, ...}
$ psql -c 'CREATE DATABASE lorebase_restore'
$ alembic upgrade head   (on lorebase_restore)
$ docker compose exec keeper python -m lorebase_store.bundle import --in /tmp/demo-bundle.tar   (on lorebase_restore)
{"domain_id": "supply", "revisions": 10, ..., "rebuilt_branches": ["agent/flags", "cleanup/tiers", "data/initial", "main", ...]}

$ psql -c "select head of main, Revisions, Records of main"
live      ebb1f99fb8e1ed755bf53b10782d4fa57bf0a7d022ded5c97b5c8af8b891d99c 11 188
restored  ebb1f99fb8e1ed755bf53b10782d4fa57bf0a7d022ded5c97b5c8af8b891d99c 10 188
```

The head of `main` and the Records are identical. The live database counts one Revision more: the one of the deleted scratch branch, which no branch reaches and the bundle leaves out, so a restore also compacts.

## What the demo showed about the product

Things the script works around or states, found while writing it:

- A required Attribute with a `default` is backfilled by the schema change that adds it, but imports and Source syncs do not apply the default: a row without the value is rejected (`'risk_band' is required`). Day 3 therefore writes `risk_band` into the rows it imports, and day 4 syncs existing Suppliers only.
- Imports replace whole rows; there is no partial update through `lorebase import`.
- An Operation restricted by `permissions` is open to the principal ids and groups named there. API-key principals have no groups and the API cannot create them, so the demo issues the agent's key on a service principal named like the permitted subject.
- The first administrator key comes from `python -m lorebase_keeper bootstrap`, run inside the Keeper container with its settings; it refuses a second key unless `--force` is given.
- A critical `flag_supplier_risk` is refused with `review-required` even on a work branch: its review policy counts on every branch. A call that names a `task` gets a task branch and a Proposal instead ([agents](agents.md#writes-that-need-review)).
- Webhooks deliver events committed after the webhook was registered.
