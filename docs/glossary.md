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
A shared set of Attributes that several Kinds implement by mapping their own Attributes to it. Queries and Operations can target a Trait instead of a single Kind.

Example: the Trait `Located` (`country`, `city`) is implemented by `Supplier` and `Plant`; a query on `Located` with `country = "IT"` returns both Italian suppliers and Italian plants.

### Operation
A governed write with parameters, preconditions, steps, permissions, review policy and audit. Operations are the only way users and agents change data. Lorebase provides built-in Operations per Kind and Relation; teams define custom ones for business intent.

Example: `flag_supplier_risk(supplier, severity, reason)` creates a `RiskFlag` Record linked to the Supplier; when `severity` is `critical`, the edits go to a Proposal instead of committing.

### Derivation
A computed Attribute or Relation, defined declaratively and recomputed from its inputs. Derived values are never edited directly and never conflict in merges.

Example: `Part.single_sourced` is `true` when the Part has exactly one `supplied_by` Link.

### Source
External backing data mapped into a Kind: a database table or query, a CSV or Parquet file, or an API. A Source defines key mapping, Attribute mapping with transforms, Relation mapping, scope, sync mode and merge policy.

Example: the Source `erp.vendors` maps the ERP Postgres table `vendors` to the Kind `Supplier`, with `vendor_id` as key, nightly `snapshot` sync and the policy `auto_with_authority` for Attributes the ERP owns.

### Record
An instance of a Kind, identified by its Kind and key.

Example: `Supplier/S-1001` with `legal_name = "Brevik Bearings S.p.A."`, `country = "IT"`, `tier = "2"`.

### Link
An instance of a Relation between two Records, with optional link Attribute values.

Example: `Part/P-77812 supplied_by Supplier/S-1001` with `is_primary = true`.

## Versioning

### Branch
A named, movable pointer to a Revision. Branches isolate work in progress; protected Branches change only through Proposals and permitted Operations.

Example: `model/risk` holds Marco's schema changes to the risk model while `main` keeps serving agents.

### Revision
An immutable snapshot of schema and data together, identified by the hash of its content, with parent Revisions, author, message and origin.

Example: Revision `9f2c41...` "Sync erp.vendors run 7" changed 1,840 Records and has the previous `main` head as its parent.

### Proposal
A request to merge one Branch into another, with diff, checks, reviews and Conflict resolution. Extraction jobs and Source syncs also produce Proposals, with per-item evidence when the content comes from documents.

Example: Proposal #42 "Contracts batch October" proposes 280 new `Contract` Records, each with a quoted evidence span from a PDF.

### Conflict
A disagreement found while merging. Attribute-level Conflicts arise when both sides changed the same value differently; structural Conflicts arise when the merged schema and data would break a rule (removed Kind with new Records, failed type cast, cardinality violation, and others). A long text edited in different sentences on each side is merged, not a Conflict; when the same sentence changed on both sides, the resolver shows the text passage by passage.

Example: on `cleanup/tiers` Sofia set `Supplier/S-1001.tier` to `"1"` while the nightly sync set it to `"3"` on `main`: Conflict `DV1`, resolved by taking the ERP value under the authority policy.

### Claim
One value for one Attribute of one Record together with where it comes from: the provenance of a value that is or was on a Branch, or a Proposal item with its evidence when it is only proposed. Its source identity (`source:{id}`, `document:{id}`, `principal:{id}`, `import:{batch}`) says who claims it.

Example: the ERP sync claims `Supplier/S-1025.risk_score = 30` (`source:erp.vendors`), the Q4 supplier review claims `74` (`document:doc_supplier_review_q4`).

### Reconciliation candidate
A group of claims that disagree, waiting for a person in the reconciliation queue. Types: `contradiction` (different values from different sources, periods overlapping or unknown), `superseded` (the same on a valid time Attribute, with periods that do not overlap), `stale` (the current value is older than its maximum age, its period ended, or its source has a newer claim pending) and `duplicate` (two Records that probably describe one entity). Statuses: `open`, `resolved`, `dismissed`, `obsolete` (the disagreement left the data). Unlike a Conflict it concerns values already merged and survives the Proposal that raised it; nothing is changed because a candidate exists ([design 10.6](design.md#106-reconciliation-of-source-contradictions)).

Example: Sofia takes the document's claim for `S-1025.risk_score` with the reason "Q4 review supersedes the ERP score"; the value is written by `update_supplier` with a `reconciliation` provenance.

### Valid time
An optional validity period (`valid_from`, `valid_to`) on the provenance of a value of an Attribute declared `valid_time: true`. It lives outside content, so hashes and merges ignore it; reconciliation uses it to tell a superseded value from a contradiction.

Example: `Contract/C-2231.payment_terms_days` is 60 from April 2022 to March 2025 and 45 from April 2025, after an amendment: two disjoint periods, a `superseded` candidate, not a contradiction.

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
