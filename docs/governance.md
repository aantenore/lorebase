# Governance and ingestion

How changes reach a protected branch: who has to approve them, which ones may merge without a
person, how merge Conflicts are decided by policy, and where disagreements between Sources wait.
Each rule has a page of its own; this one ties them together and ends with a worked
configuration.

## Where the rules live

| Rule | Set in | Default |
|------|--------|---------|
| Protected default branch | `keeper.domains.protect_default_branch` | on: the default branch changes only through Proposals |
| Approvals count | `keeper.proposals.required_approvals`, Domain setting `required_approvals` | 1 |
| Approvals from stewards | `keeper.proposals.required_approvals_from_stewards`, Domain setting of that name | 0 (off) |
| Roles that must approve | `keeper.proposals.required_roles`, Domain setting of that name | none (off) |
| Stewards | schema: `domain.stewards`, `kinds.{Kind}.stewards` | none |
| Ingestion mode of extraction | `scribe.extraction.mode`, Domain setting `extraction` | `doubts_only` |
| Merge of a Source run | the Source's `target_branch_policy.merge_policy` and `local_edits` (schema) | `proposal`, `review` |
| Automatic merges into protected branches | `scribe.sources.auto_merge_protected`, `scribe.extraction.direct_merge_protected` | on |
| Merge policies (who wins a Conflict) | Domain setting `merge_policy` | text merge on, nothing else |
| Open contradictions at merge | `keeper.reconciliation.merge_check`, `scribe.reconciliation.merge_check`, Domain setting `reconciliation_check` | `warn` |
| Automatic merges of agent writes | `keeper.agent_writes`, Domain setting `agent_writes` | off |
| Vocabulary learned from resolution misses | `store.resolve_misses`, `scribe.consolidation` | off: proposals only, always reviewed |

Installation settings are in the configuration file ([reference](reference/configuration.md));
give Keeper and Scribe the same file, since both evaluate the approval rules and the extraction
bounds. Domain settings are given when the Domain is created (`POST /api/v1/domains` with
`settings`); afterwards `PATCH /api/v1/domains/{d}` replaces only `extraction`. A Domain's
`required_approvals`, `required_approvals_from_stewards` and `required_roles` replace the
installation's values; its `extraction` may tighten them and loosen only what
`scribe.extraction.domain_may_loosen` allows; its `reconciliation_check` can only be stricter.
Stewards and Source policies are part of the schema, so they change through a reviewed schema
change like the rest of it ([modeling guide](modeling.md#stewards-and-required-roles)).

## Approvals

A Proposal into a protected branch merges when it has no open Conflict and its approvals are in
([design 10.3](design.md#103-proposals-as-governance)):

- **Count.** `required_approvals` counted approvals; authors' own approvals are not counted
  unless `keeper.proposals.allow_self_approval` is on, and an outstanding `request_changes`
  review blocks the merge (`changes_requested_blocks`).
- **Stewards.** With `required_approvals_from_stewards` above 0, that many of the approvals
  must come from the Proposal's assignees: the principals holding a steward role of the Kinds
  it touches who may read those Kinds and review it ([design 10.7](design.md#107-stewards)).
  The Proposal then needs the larger of the two counts in all. When steward roles are declared
  but too few principals can approve, the merge answers 409 `stewards-unavailable`.
- **Roles.** Each role in `required_roles` needs one counted approval from a principal holding
  it who may review the Proposal; it does not raise the count. A merge without one answers 409
  `approvals-required` with `missing_roles`.

Reviewers find their work with `assigned=me` on `GET .../proposals` and
`GET .../reconciliation` (CLI `lorebase proposal list --mine`, `lorebase reconcile list --mine`;
"Assigned to me" in the web app).

## Automatic merges

Three kinds of merge can happen without a person pressing Merge:

- **Source runs** with `merge_policy: auto_when_clean` (merged when no Conflict remains) or
  `auto_with_authority` (the same, and value Conflicts on Attributes whose `authority` is the
  Source go to the Source) ([Scribe](scribe.md#sources)).
- **Extraction jobs** in `direct` mode: only jobs whose items all came from the rules tier
  (tier 0), with nothing pending ([extraction](extraction.md#ingestion-modes)).
- **Agent writes** with `auto_merge: true`, when a role the agent holds is in
  `allowed_roles_for_auto_merge` and the target's approvals are in
  ([agents](agents.md#writes-that-need-review)).

Into a protected branch, Source runs and direct extraction merge only with
`auto_merge_protected` and `direct_merge_protected` (both on by default). That trust waives the
plain approvals count, not the rules that name who must approve: when steward approvals or
required roles apply, the merge is not performed and the Proposal waits for review (the run's
stats say `merge_blocked: approvals`, the job's result `direct_blocked: approvals`, with
`blocking_rules`) ([operations](operations.md#approval-rules-and-automatic-merges)). Open
Conflicts, pending items and, under `block`, open contradictions stop an automatic merge too.

A Source never overwrites a correction silently: it writes only what changed upstream since it
last wrote it, and an upstream change over a value someone else set is a Conflict of the
Source's Proposal under `local_edits: review` (the default), the person's value under `keep`,
or upstream's under `source_wins` ([Scribe](scribe.md#corrections-that-stick)).

## Merge policies

The Domain setting `merge_policy` holds the options of the merge engine
([design 6.10](design.md#610-resolution-flows)). Keeper copies it into each Proposal when the
Proposal is created; Source runs and extraction jobs read it at each merge. Per-element options
are keyed by Kind or Relation id and Attribute id (the `id` members of the schema document,
`lorebase schema get`), not by api name.

| Option | Effect on a Conflict |
|--------|----------------------|
| `prefer`, `prefer_elements`, `prefer_attributes` | `ours` or `theirs` wins value and schema Conflicts, globally, per Kind or Relation, or per Attribute |
| `authority`, `authority_attributes` | a ranking of origins (`source:{id}`, `principal:{id}`, `operation:{name}`, `actor:{id}`, `origin:{type}`): the side whose last change of the Attribute comes from the higher ranked one wins |
| `schema_authority` | an Attribute's schema `authority` (a Source) ranks first for it |
| `newest_wins`, `newest_wins_attributes` | the side whose last change is the more recent wins |
| `text_merge` and its options | long text edited in different places merges ([below](#text-merge)) |

Rules are tried from the most specific to the least: per Attribute `prefer`, `authority`,
`newest_wins`, then per Kind or Relation `prefer`, then global `authority`, `newest_wins`,
`prefer`. The first that decides resolves the Conflict, recorded as a resolution with resolver
`policy:{name}`; when none decides, a person does. A resolution a person stored always comes
first.

### Text merge

Two different values of a `text` Attribute that both started from the same base value are
merged by sentence (or paragraph) before any rule is tried, so two branches editing different
parts of a long description, note or clause do not conflict. It is on by default.

| Option | Default | Meaning |
|--------|---------|---------|
| `text_merge` | `true` | on or off for every `text` Attribute |
| `text_merge_elements` | none | `{kind or relation id: true or false}`, for one Kind or Relation |
| `text_merge_attributes` | none | `{attribute id: true or false}`, for one Attribute; the most specific setting wins |
| `text_merge_granularity` | `sentence` | `sentence` (split after `.`, `!`, `?` or `…` followed by whitespace) or `paragraph` (split at blank lines) |
| `text_merge_max_chars` | `200000` | characters of the three values together above which text is not merged |

- **Clean.** Edits to different sentences are all kept; two sentences next to each other, one
  rewritten on each side, merge too. The merged value is stored as a resolution with resolver
  `policy:text_merge`, and the Review screen lists it under "Resolved by policy".
- **Conflict.** When both sides changed the same sentence differently, or inserted text at the
  same place, the Conflict stays a `DV1` (same fingerprint as without text merge) with
  `details.text_hunks`: the whole text in order, each hunk `clean` (with its merged text) or
  `conflict` (base, ours and theirs). A conflicting hunk is narrowed to the words that differ.
  The policies above then try to decide it; otherwise the Conflict resolver shows it passage
  by passage ([tour](tour.md#merge-attempt-and-conflict-resolver)), or a resolution with
  `choice: custom` and the full text as `value` settles it over the API.
- **Not merged.** Other types (`string`, `uri`, enums, numbers, `json`, arrays), values added
  on both sides (no base), and text above the caps: `text_merge_max_chars`, and two fixed
  bounds on the alignment (20,000 sentences or paragraphs over the three values, and the
  product of the base's units and the larger side's at most 4,000,000). Those stay plain
  `DV1` Conflicts.
- **Constraints.** `min_length`, `max_length` and `pattern` are checked on the merged text, and
  a value that breaks them is a Conflict like any other.

Before and after, on `Contract.notes` with the default `sentence` granularity:

```text
base    Payment within 60 days of invoice. Deliveries go to the Turin plant. The contract renews every year.
ours    Payment within 45 days of invoice. Deliveries go to the Turin plant. The contract renews every year.
theirs  Payment within 60 days of invoice. Deliveries go to the Turin and Bilbao plants. The contract renews every year.
merged  Payment within 45 days of invoice. Deliveries go to the Turin and Bilbao plants. The contract renews every year.
```

If theirs had also changed the first sentence (`within 30 days`) and the last one
(`every two years`), the last one would still merge and the first would conflict, narrowed to
the number:

```json
[{"status": "clean", "base": "Payment within ", "merged": "Payment within ", ...},
 {"status": "conflict", "base": "60", "ours": "45", "theirs": "30", "merged": null},
 {"status": "clean", "base": " days of invoice. Deliveries go to the Turin plant. ", ...},
 {"status": "clean", "base": "The contract renews every year.",
  "merged": "The contract renews every two years.", ...}]
```

To turn it off for one Attribute, keep it elsewhere and merge by paragraph:

```json
{"merge_policy": {"text_merge_granularity": "paragraph",
                  "text_merge_attributes": {"att_01KEZF7C00...": false}}}
```

## Reconciliation queue

A merge Conflict is about two branches; a contradiction between Sources is about values already
merged. Scribe raises those as candidates in the reconciliation queue (contradictions,
superseded and stale values, probable duplicates), and nothing changes until a person decides
([design 10.6](design.md#106-reconciliation-of-source-contradictions),
[Scribe](scribe.md#reconciliation)). The merge check ties the two together: under `warn` a
Proposal lists its open contradictions, under `block` they stop its merge with 409
`reconciliation-open`, automatic merges included. Candidates are routed to the same stewards
as Proposals. Operator settings: [operations](operations.md#reconciliation-queue).

## Schema suggestions

Schema suggestions are proposals a person accepts or rejects; none reaches the schema on its
own. Extraction proposes new Kinds, Attributes and Relations found in documents
([extraction](extraction.md#schema-suggestions)). With `store.resolve_misses.enabled`, Scribe
also proposes vocabulary from the words agents asked `lorebase_resolve` for that matched
nothing ([design 8.10](design.md#810-consolidation-from-use-vocabulary-learned-from-resolution-misses)):
an alias of a stored value (`value_alias`), of an Attribute or a Kind, and, with
`scribe.consolidation.llm.enabled`, a description of an Attribute agents often missed. Each
shows the missed phrases with their misses, distinct callers and the similarity that linked
them. They are listed and decided in the same place (web app **Review > Schema suggestions**,
`lorebase schema suggestions`, `GET .../schema/suggestions`, which needs `document.read`):
accepting opens a schema change Proposal on a new branch, reviewed and merged like any schema
change, so the approval rules above apply. Rejected suggestions stay rejected however often
the phrase is missed again. Suggestions for an Attribute the reviewer may not read values of
are listed without their phrases and value; Attributes that are sensitive, kept in the vault,
derived or `resolvable: false` never receive one.

## A worked configuration

The `supply` Domain takes nightly ERP data and contract documents. Procurement owns suppliers,
risk analysts own risk flags, and nothing reaches `main` without a supply steward; deterministic
feeds should flow without waiting for a person.

The schema names the stewards and the Source policy (excerpt):

```yaml
domain:
  id: supply
  stewards: [supply-steward]
kinds:
  RiskFlag:
    stewards: [risk-analyst]
sources:
  erp.purchase_orders:
    # type, connection, mappings ...
    local_edits: review
    target_branch_policy: {merge_policy: auto_when_clean}
```

The installation keeps one approval and lets automation into protected branches:

```yaml
keeper:
  proposals:
    required_approvals: 1
  reconciliation:
    merge_check: warn
scribe:
  sources:
    auto_merge_protected: true
  extraction:
    mode: doubts_only
    domain_may_loosen: [mode]
    direct_merge_protected: true
```

The Domain is created with its own rules, and its extraction settings set afterwards:

```http
POST /api/v1/domains
Content-Type: application/json

{"id": "supply", "display_name": "Supply chain",
 "settings": {"required_approvals_from_stewards": 1,
              "reconciliation_check": "block"}}
```

```http
PATCH /api/v1/domains/supply
Content-Type: application/json

{"settings": {"extraction": {"mode": "doubts_only",
  "rules": [{"source_key_prefix": "feeds/erp/", "mode": "direct"}]}}}
```

What happens:

- A purchase order sync whose run meets no Conflict would merge on its own, but the Domain
  requires a steward approval, so its Proposal waits with `merge_blocked: approvals` and a
  supply steward finds it under `assigned=me`. With `required_approvals_from_stewards: 0` it
  would merge at once.
- A run that changes a status someone corrected by hand opens a `DV1` instead of overwriting
  it. A Domain `merge_policy` with an `authority` ranking that lists the ERP Source would
  decide such Conflicts for the ERP, since an origin missing from a ranking ranks below every
  listed one: rank only the origins that should win over people.
- A contract uploaded under `feeds/erp/` whose items all come from rules is a `direct` job, held
  the same way by the steward rule; one under any other key, or one that used a model, waits as
  `doubts_only` with its doubtful items pending.
- A Proposal that would merge a value an open contradiction is about stops with
  `reconciliation-open` until the candidate is decided or dismissed.
- Two analysts editing different sentences of a contract's notes merge cleanly; the same
  sentence edited twice waits for one of them in the passage view.
