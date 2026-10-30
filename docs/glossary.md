# Lorebase Glossary

Lorebase uses one vocabulary everywhere: code, API, CLI, UI and documentation. When a term is capitalized in the docs, it carries the meaning below. Examples use the demo Domain `supply` of a fictional industrial distributor.

## Modeling

### Domain
An ontology workspace for one company area. It is the isolation boundary for data, permissions and storage, and it has its own Branches and history.

Example: the Domain `supply` holds suppliers, parts, plants and contracts; the Domain `hr` is separate, with its own grants, and nothing in `supply` can link into it.

### Kind
A type of thing that exists in the Domain. A Kind has Attributes, a primary key, a title Attribute and may implement Traits.

Example: `Supplier`, with primary key `supplier_code` and title Attribute `legal_name`.

### Attribute
A typed field on a Kind (or on a Relation, as a link Attribute). It has a type, constraints, sensitivity tags and search settings.

Example: `Supplier.risk_score`, type `integer`, `min: 0`, `max: 100`, indexed, not required.

### Relation
A typed, directional connection between two Kinds, with a forward name, an inverse name and a cardinality. It may carry its own Attributes.

Example: `Part supplied_by Supplier` (inverse `supplies`), `many_to_many`, with link Attribute `is_primary: boolean`.

### Trait
A shared set of Attributes that several Kinds implement by mapping their own Attributes to it. Queries, search, context packs and Operations can target a Trait instead of a single Kind.

Example: the Trait `Located` (`country`, `city`) is implemented by `Supplier` and `Plant`; a query on `Located` with `country = "IT"` returns both Italian suppliers and Italian plants.

### Lens
A named, schema-validated preset of a read: which Kinds and Traits it is about, which Relations to follow, how deep, which optional sections to include and a budget. A request through a lens (`lens` on search and context packs) takes the lens's members as defaults; what the request names wins. A lens is a preset, not a permission: grants apply to everything it names.

Example: the lens `supplier_risk` (Supplier, Contract, Part, RiskFlag; depth 1) turns "what should I know about this supplier" into `{"lens": "supplier_risk", "refs": ["Supplier/S-1001"]}`.

### Operation
A governed write with parameters, preconditions, steps, permissions, review policy and audit. Operations are the only way users and agents change data. Lorebase provides built-in Operations per Kind and Relation; teams define custom ones for business intent.

Example: `flag_supplier_risk(supplier, severity, reason)` creates a `RiskFlag` Record linked to the Supplier; when `severity` is `critical`, the edits go to a Proposal instead of committing.

### Derivation
A computed Attribute or Relation, defined declaratively and recomputed from its inputs. Derived values are never edited directly and never conflict in merges.

Example: `Part.single_sourced` is `true` when the Part has exactly one `supplied_by` Link.

### Source
External backing data mapped into a Kind: a database table or query, a CSV or Parquet file, or an API. A Source defines key mapping, Attribute mapping with transforms, Relation mapping, scope, sync mode, merge policy and what an upstream change does to a local edit (`local_edits`). A Source writes only what changed upstream since it last wrote it, so a correction made on the target stays until upstream changes that value.

Example: the Source `erp.vendors` maps the ERP Postgres table `vendors` to the Kind `Supplier`, with `vendor_id` as key, nightly `snapshot` sync and the policy `auto_with_authority` for Attributes the ERP owns.

### Record
An instance of a Kind, identified by its Kind and key.

Example: `Supplier/S-1001` with `legal_name = "Brevik Bearings S.p.A."`, `country = "IT"`, `tier = "2"`.

### Link
An instance of a Relation between two Records, with optional link Attribute values.

Example: `Part/P-77812 supplied_by Supplier/S-1001` with `is_primary = true`.

### Alias

Another word for a Kind, an Attribute (`aliases`) or a stored value (`value_aliases`, keyed by
the value), written in the schema and versioned with it. Resolution matches them at its
`synonym` level.

### Value alias
Another word for one stored value of an Attribute, written in the schema as `value_aliases`, keyed by the value. On an enum the keys must be values of the enum. Resolution turns the word into a predicate on the stored value ([modeling](modeling.md#words-for-resolution)).

Example: `country: {value_aliases: {IT: [Italy, Italia]}}` makes "suppliers in Italy" resolve to a predicate on `country` with the stored value `IT`.

### Resolution

Matching the words of a text against the schema and the value index: ranked candidate Kinds,
Attributes and values, each with the words matched, a level and, for values, a ready-to-use
predicate (`POST /resolve`, `lorebase_resolve`). It never interprets the question or runs a
query.

### Value index

Per branch, the distinct short values of selected Attributes and how many Records hold each
(`mat_values`), kept by triggers on every write. Enums are always selected, plain strings while
their distinct values stay few; `resolvable` in the schema forces an Attribute in or out.

### Resolvable
The schema flag that forces an Attribute into the Value index (`resolvable: true`) or keeps it out (`false`). Unset, enums are held and plain strings while their distinct values stay few; sensitive, vault, derived and link Attributes never are ([operations](operations.md#value-index)).

Example: `Supplier.city` has too many distinct values for the automatic selection; `resolvable: true` makes "suppliers in Turin" resolve anyway.

### Vault
Encrypted storage, outside history, for the values of `pii` Attributes declared `vault: true`. Record objects hold a token instead of the value, so `lorebase admin erase` removes a person's data while every Revision keeps its hash; erased values read as `[erased]` for cleared callers ([design 13.5](design.md#135-personal-data-and-erasure)).

Example: `Supplier.contact_email` is a vault Attribute; after an erasure request the old Revisions still verify, and the address is gone from all of them.

## Versioning

### Branch
A named, movable pointer to a Revision. Branches isolate work in progress; protected Branches change only through Proposals and permitted Operations.

Example: `model/risk` holds Marco's schema changes to the risk model while `main` keeps serving agents.

### Overlay
A Branch that stores only the rows it changed and reads everything else from its base Branch as of its fork point, instead of a full copy of the rows. Agent task Branches and Source and extraction Branches are overlays by default; an overlay that grows past its limits becomes a full copy ([store schema notes](store-schema-notes.md#overlay-branches)).

Example: `agent/svc_risk/q4-audit` holds the two rows its risk flag wrote, and reads the other 1M Records from `main`.

### Revision
An immutable snapshot of schema and data together, identified by the hash of its content, with parent Revisions, author, message and origin.

Example: Revision `9f2c41...` "Sync erp.vendors run 7" changed 1,840 Records and has the previous `main` head as its parent.

### Proposal
A request to merge one Branch into another, with diff, checks, reviews and Conflict resolution. Extraction jobs and Source syncs also produce Proposals, with per-item evidence when the content comes from documents.

Example: Proposal #42 "Contracts batch October" proposes 280 new `Contract` Records, each with a quoted evidence span from a PDF.

### Local edit
A value someone other than a Source (a person through an Operation, a Conflict resolution, another Source) set on the target since that Source last wrote it. When upstream changes the same value, the Source's Proposal holds a Conflict whose base is the Source's previous value (`local_edits: review`), the person's value wins (`keep`) or the upstream value overwrites it (`source_wins`) ([design 9.1](design.md#91-structured-sources)).

Example: Sofia corrects `Supplier/S-1003.risk_score` to `10`; the nightly sync keeps it while the CSV still says `32`, and opens a `DV1` (base `32`, ours `10`, theirs `71`) the night the CSV says `71`.

### Last-written state
Per Source and target Branch, the hash of each value the Source last wrote (`source_written`), never the value. A run compares the fresh upstream value with it, not with the current value, which is how a Local edit is told from an upstream change. It advances only when the Source's Proposal merges ([Scribe](scribe.md#corrections-that-stick)).

### Stale after
A Source setting (`stale_after`, an ISO 8601 duration; incremental Sources only) after which a Record the Source wrote but no run has read again is raised as a `stale` Reconciliation candidate with signal `source_unseen`. Nothing is deleted on that signal ([Scribe](scribe.md#incremental-sources-and-upstream-deletions)).

Example: `erp.purchase_orders` sets `stale_after: P30D`; an order the ERP query stopped returning in March is raised for review in April.

### Conflict
A disagreement found while merging. Attribute-level Conflicts arise when both sides changed the same value differently; structural Conflicts arise when the merged schema and data would break a rule (removed Kind with new Records, failed type cast, cardinality violation, and others). A long text edited in different sentences on each side is merged, not a Conflict; when the same sentence changed on both sides, the resolver shows the text passage by passage.

Example: on `cleanup/tiers` Sofia set `Supplier/S-1001.tier` to `"1"` while the nightly sync set it to `"3"` on `main`: Conflict `DV1`, resolved by taking the ERP value under the authority policy.

### Text merge
The merge of two different values of a `text` Attribute by sentence (or paragraph): edits to different sentences are all kept, recorded as resolver `policy:text_merge`; only the passages both sides changed conflict. On by default, settable per Domain, Kind, Relation or Attribute in the Domain's `merge_policy` ([governance](governance.md#text-merge)).

Example: Sofia shortens the first sentence of a Contract's notes while Luca adds a sentence at the end; the merged notes hold both edits.

### Hunk
One region of a text Conflict, in order: `clean` (merged text included) or `conflict` (base, ours and theirs). A text Conflict carries the whole text as hunks in `details.text_hunks`, and a conflicting hunk is narrowed to the words that differ.

Example: in "Payment within 60 days", one side wrote 45 and the other 30: the conflicting hunk is `60` / `45` / `30`, the rest of the sentence is clean.

### Passage
A conflicting hunk as the Conflict resolver shows it: base, ours and theirs with the changed words marked, and a choice (ours, theirs, both in either order, base, or an edit). Once every passage has a choice, the merged text is the resolution ([tour](tour.md#merge-attempt-and-conflict-resolver)).

### Claim
One value for one Attribute of one Record together with where it comes from: the provenance of a value that is or was on a Branch, or a Proposal item with its evidence when it is only proposed. Its source identity (`source:{id}`, `document:{id}`, `principal:{id}`, `import:{batch}`) says who claims it.

Example: the ERP sync claims `Supplier/S-1025.risk_score = 30` (`source:erp.vendors`), the Q4 supplier review claims `74` (`document:doc_supplier_review_q4`).

### Reconciliation candidate
A group of claims that disagree, waiting for a person in the reconciliation queue. Types: `contradiction` (different values from different sources, periods overlapping or unknown), `superseded` (the same on a valid time Attribute, with periods that do not overlap), `stale` (the current value is older than its maximum age, its period ended, its source has a newer claim pending, or its evidence quote is gone from a new version of its document or the document was retired: `evidence_gone`) and `duplicate` (two Records that probably describe one entity). Statuses: `open`, `resolved`, `dismissed`, `obsolete` (the disagreement left the data). Unlike a Conflict it concerns values already merged and survives the Proposal that raised it; nothing is changed because a candidate exists ([design 10.6](design.md#106-reconciliation-of-source-contradictions)).

Example: Sofia takes the document's claim for `S-1025.risk_score` with the reason "Q4 review supersedes the ERP score"; the value is written by `update_supplier` with a `reconciliation` provenance.

### Valid time
An optional validity period (`valid_from`, `valid_to`) on the provenance of a value of an Attribute declared `valid_time: true`. It lives outside content, so hashes and merges ignore it; reconciliation uses it to tell a superseded value from a contradiction.

Example: `Contract/C-2231.payment_terms_days` is 60 from April 2022 to March 2025 and 45 from April 2025, after an amendment: two disjoint periods, a `superseded` candidate, not a contradiction.

### Document version
A document uploaded under a stable `source_key` (a path, URL or external id) is one version of the document of that key: new bytes under the key become version n + 1, which `replaces` the previous one (now `superseded`); the same bytes are a no-op. A document is `active`, `superseded`, `retired` (kept and readable, never extracted again) or `purged` (bytes, text and quotes erased, provenance kept). A new version or a retirement checks, word for word, whether the evidence quotes of the values that came from the earlier version are still there; where one is gone, a `stale` Reconciliation candidate asks a person ([extraction](extraction.md#document-versions-and-lifecycle)).

Example: `contracts/brevik-msa.pdf` version 2 no longer says "payment within 60 days": `Contract/C-2231.payment_terms_days = 60` gets a `stale` candidate with signal `evidence_gone`, and stays 60 until Sofia decides.

### Ingestion mode
How much of a document extraction a person sees, chosen per Domain (and per `source_key` prefix) within bounds the installation sets: `review_all` (nothing is accepted at once), `doubts_only` (the default: confident items are accepted, the doubts wait) or `direct` (Scribe merges the Proposal itself when every item came from deterministic rules and none is pending).

Example: the Domain `supply` sets `direct` for documents keyed `feeds/erp/` and keeps `doubts_only` for everything else; a feed document whose job used a model is capped at `doubts_only`.

### Steward
A role that looks after part of a Domain: the schema names steward roles on the Domain and per Kind, and the Proposals and reconciliation candidates touching those Kinds are routed to the principals holding them (the assignees), who must also be able to read and act on the work. A steward role grants nothing by itself.

Example: `Supplier` declares `stewards: [risk-analyst]`; a Proposal changing suppliers lists the risk analysts as assignees, `GET .../proposals?assigned=me` shows it to them, and a protected branch may require one of their approvals (`required_approvals_from_stewards`).

### Required roles
A protection rule of protected Branches (`required_roles`, in `keeper.proposals` or a Domain setting): each listed role needs one approval from a principal holding it before a Proposal merges. Automatic merges stop while it applies ([governance](governance.md#approvals)).

Example: `required_roles: [legal]` holds every Proposal into `main` until someone with the `legal` role approves it.

### Lineage
Where a Record and each of its values came from: Source row, document span, Operation invocation, merge resolution or schema migration, with the Revision that introduced it.

Example: the Lineage of `Contract/C-2231.expiry_date` shows extraction job 18, document `brevik-msa-2025.pdf`, page 4, the quoted sentence, the model name, and the Proposal where Sofia accepted it.

## User interface areas

### Explorer
Search and navigation: Domain home, search results, Kind tables with filters, Record pages with Lineage and history.

Example: Luca types "Brevik" and opens the Supplier page, where linked Parts are grouped under `supplies`.

### Canvas
Graph visualization of Records and Links, of the schema itself, and of differences between two refs.

Example: Luca expands two hops from `Plant/TRN` and sees which Suppliers feed it, colored by Kind; switching to diff mode highlights Links added since last week.

### Modeler
The schema editor: Kinds, Attributes, Relations, Traits, Operations, Derivations and Source mappings, always edited on a Branch.

Example: Marco changes `risk_score` from `string` to `integer` in the Modeler and sees how many existing values would fail the cast before opening the Proposal.

### Review
Where Proposals are reviewed, extraction items accepted or rejected with evidence, and Conflicts resolved. The Reconciliation screen next to it holds the candidates of contradictions between Sources.

Example: Sofia accepts all extracted items with confidence above 0.9, inspects 40 ambiguous matches side by side with the document, then resolves the remaining Conflicts and merges.

## Components

### Keeper
The API service. It owns authentication, authorization, the commit pipeline, merges, the read path and the REST API.

Example: an agent's query for single-sourced parts reaches Keeper, which answers from the materialized head of `main` in a few milliseconds.

### Scribe
The ingestion and extraction worker. It runs Source syncs, document parsing and extraction, embedding computation, webhook delivery and maintenance jobs from the Postgres job queue.

Example: at 02:00 Scribe runs the `erp.vendors` sync, commits on `source/erp.vendors` and opens a Proposal into `main`.

### Herald
The MCP server. It exposes Lorebase tools, resources and prompts to agents and calls Keeper with the agent's credentials.

Example: an agent connected to Herald calls `lorebase_execute_operation` with `dry_run: true` to preview a risk flag before committing it.
