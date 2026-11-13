# How Lorebase compares

This page positions Lorebase against products people evaluate for similar problems. It aims to be fair: every product below is good at what it was built for, and several are the right choice in cases where Lorebase is not. Facts about other products come from their public documentation, linked in each section and last checked on 2026-11-06; products change fast, so check the linked page before deciding. Corrections are welcome through a pull request.

Lorebase in one line: an open-source, Postgres-only knowledge platform where a typed ontology and its data are versioned together (branches, Revisions, three-way merge with attribute-level and structural Conflicts), filled from structured and unstructured sources through reviewable Proposals, and exposed to external agents through REST and MCP with governed Operations and an LLM-free read path.

## Where Lorebase is different

Many products now give agents a context layer over MCP. The combination below is what Lorebase is for, and none of the products on this page offers all four together:

- **Governed instance data, not metadata.** Lorebase holds the business Records themselves (suppliers, parts, contracts) with their Links, under grants per Domain, Kind, Operation and branch, a hash-chained audit log and a PII vault. Data catalogs govern descriptions of datasets; Lorebase governs the facts agents act on.
- **Schema and data merged together, three-way.** A branch can change the ontology and the Records at once; the merge checks the merged data against the merged schema and returns typed Conflicts ([design 6.9](design.md#69-conflict-taxonomy)) instead of a state the schema forbids.
- **Reconciliation of Sources that disagree.** When an ERP feed and a contract say different things about one value, the disagreement goes to a review queue; nothing picks the newest write silently ([design 10.6](design.md#106-reconciliation-of-source-contradictions)).
- **LLM-free reads on stock Postgres.** Agent reads are SQL over materialized branch heads, measured in milliseconds ([performance](performance.md)), each answer stamped with its Revision; models are optional and run only in Scribe and Guide. There is no new storage engine to operate.

## Summary table

| Product | What it is | Versioning of knowledge | Schema / ontology | Unstructured ingestion | Agent interface | License and hosting |
|---------|-----------|-------------------------|-------------------|------------------------|-----------------|---------------------|
| **Lorebase** | Ontology and knowledge system of record for agents | Branches, immutable Revisions, three-way merge of schema and data together, Conflict taxonomy, Lineage per value | Kinds, Attributes, Relations with cardinality, Traits, Operations, Derivations, Sources | Offline LLM extraction into Proposals with verified evidence; never direct writes | REST and MCP; governed writes via Operations; no LLM on reads | Apache-2.0, self-hosted, Postgres only |
| Palantir Foundry Ontology | Operational ontology layer inside a proprietary data and application platform | Platform-level branching and change management within Foundry | Rich and mature: typed objects, links, shared shapes, governed write actions, functions, backing datasets | Through the wider platform (pipelines, AIP features) | Platform SDKs and APIs, AIP agent tooling | Proprietary, commercial, vendor-operated or vendor-supported |
| TerminusDB | Document and graph database with git-like revision control | Branch, merge, diff, clone, push and pull on a versioned store | Schema language with classes, properties, inheritance | None built in | Query languages (WOQL, GraphQL), HTTP API, clients | Apache-2.0, self-hosted or hosted offering |
| Dolt | SQL database with git semantics | Branch, commit, merge with cell-level conflicts, diff, clone | Relational tables; no ontology layer | None built in | SQL (MySQL-compatible; a Postgres-compatible variant exists) | Apache-2.0, self-hosted or hosted offering |
| Neo4j | Property graph database | No native branching of graph content | Optional constraints and indexes; schema is not enforced as an ontology | GraphRAG libraries and builder tools that use LLMs to populate graphs | Cypher, drivers; MCP servers available | Community edition GPLv3; Enterprise and cloud commercial |
| Cognee | Memory and knowledge engine for agents | Not a versioning system | Optional ontology grounding; graph built by pipelines | LLM pipelines turn documents into graph and vectors | Python SDK, API, MCP | Apache-2.0, self-hosted or hosted offering |
| Graphiti / Zep | Temporal knowledge graph for agent memory (Graphiti) and a memory service built on it (Zep) | Bi-temporal validity of facts, no branches or reviewed merges | Optional custom entity and edge types | Incremental LLM extraction from episodes (messages, text, JSON) | Python library, MCP server; Zep service APIs | Graphiti Apache-2.0 on graph databases; Zep commercial service |
| Mem0 | Memory layer for personalized agents | Memory updates managed by the engine; no branches | User, session and agent memories; optional graph | LLM extracts and updates memories from conversations | SDKs, API, MCP | Apache-2.0 open source plus managed platform |
| DataHub (with `mcp-server-datahub`) | Metadata catalog: datasets, lineage, glossary, ownership | Versions of metadata such as glossary terms; proposals for metadata changes | Glossary terms, domains, tags, data products over the metadata graph | Metadata ingestion from data platforms | MCP server with search, lineage and opt-in mutation and proposal tools; GraphQL and REST | Apache-2.0, self-hosted, or DataHub Cloud |
| Microsoft Fabric IQ ontology (MCP, preview) | Ontology over OneLake data inside Microsoft Fabric | Within Fabric | Entity types with properties bound to Fabric data | Through Fabric | MCP tools `list_ontology_entity_types` and `search_ontology` (natural-language questions) | Proprietary, needs paid Fabric capacity |
| Semantica | Python framework that builds knowledge graphs and ontologies for agents | Entity merge history; no branches or reviewed merges of the graph | OWL generation, SHACL validation, PROV-O provenance | LLM and rule pipelines over documents and data sources | Python API, MCP server (`semantica-mcp`) | MIT, a library over pluggable stores |
| Doltgres (with the Dolt MCP server) | Postgres-compatible SQL database with git semantics | Branch, commit, merge with cell-level conflicts, on its own storage engine | Relational tables; no ontology layer | None built in | SQL over the Postgres wire protocol; MCP server (`MCP_DIALECT=doltgres`) | Apache-2.0, self-hosted or hosted offering |
| TypeDB (with TypeDB MCP) | Strongly typed database with entities, relations and attributes | No branches or merges of content | TypeQL schema with types, inheritance and rules | None built in | TypeQL; official MCP server for read, write and schema queries | MPL-2.0, self-hosted or cloud |
| mnem | Local, embedded knowledge graph for one project's agents | Commits, branches and three-way graph merge in a single-writer local store | No typed ontology | None built in | 22 MCP tools; `mnem integrate` wires it into desktop hosts | Apache-2.0, local files, no server |
| Omnigraph | Typed graph database on Lance, with branches and review | Branches with a row-level three-way merge, according to its README | Typed schema | Not stated in its README | MCP, according to its README | Open source, self-hosted; does not reuse Postgres |
| RDF triple stores (Stardog, GraphDB, Neptune) | Graph databases for RDF with SPARQL | Named graphs and store-specific versioning; no three-way merge of schema and data | RDFS/OWL ontologies, SHACL validation, reasoning (varies by product) | Store-specific connectors and virtualization | SPARQL endpoints and APIs | Commercial products, with free editions or as a managed cloud service |
| LinkML | Modeling language and toolkit | Schemas versioned as files in git | YAML schemas, converters to JSON Schema, SQL, Pydantic, OWL and more | None (modeling only) | None (not a runtime) | Open source, a toolkit rather than a server |
| Protégé | Ontology editor for OWL (desktop and web) | Files in version control; collaborative editing in the web version | OWL classes, properties, axioms, reasoners | None | None (editor, not a runtime) | Open source (BSD-style), desktop or self-hosted web |

## Where Lorebase differs, product by product

### Palantir Foundry Ontology
Foundry is the conceptual reference for an operational ontology: typed things, links, governed write actions, computed logic, backing data, a schema manager, an explorer and graph views. It is mature, broad, and tightly integrated with a full data platform.

Lorebase differs by being:
- **Open source and small.** One service set on one Postgres database, deployable with docker compose, no platform purchase.
- **Focused.** Lorebase does not do data pipelines, BI or application building. It maps data in, it does not transform it at scale.
- **Version-first.** Schema and data are versioned together with explicit three-way merge semantics and a published Conflict taxonomy.
- **Agent-neutral.** Agents live in any framework and connect through MCP or REST.

Choose Foundry when you need the full platform, have the budget, and want a vendor to operate it.

### TerminusDB
The closest open-source relative in spirit: versioned, schema-aware graph data with branching and merging.

Lorebase differs by:
- running on **Postgres only** instead of a dedicated storage engine, so teams reuse their existing backup, HA and security practices;
- merging **schema and data together** with consistency rules (type casts, cardinality, required Relations, Trait conformance) and recording resolutions as Lineage;
- providing **governed Operations** (preconditions, permissions, review policies, audit) as the only write path;
- including **ingestion** (Sources, schema inference, document extraction into Proposals) and an **agent interface** (MCP) out of the box;
- shipping **Review** and **Canvas** UIs aimed at business stewards.

Choose TerminusDB when you want a versioned document graph database as a general building block and plan to build the rest yourself.

### Dolt
Dolt brings git semantics to SQL tables with excellent cell-level merge and a mature storage engine.

Lorebase differs by modeling an **ontology** (Kinds, Relations with cardinality, Traits, Operations) rather than tables, by enforcing **cross-element consistency rules** during merge, and by serving **agent-shaped reads** (search, traversal, neighborhoods, Lineage). Dolt is a database; Lorebase is a knowledge platform with a database inside.

Choose Dolt when your data is naturally tabular and you want versioned SQL with existing SQL tooling.

### Neo4j
Neo4j is a fast, mature property graph database with a rich query language and a large ecosystem, including GraphRAG tooling.

Lorebase differs by:
- **enforced schema**: every Record conforms to its Kind in every Revision;
- **branches, Revisions and merges** of the whole knowledge base;
- **reviewed ingestion**: LLM output is a Proposal, never a direct write;
- **governed writes** for agents instead of raw query access;
- **one store**: relational Postgres with graph-shaped reads, no separate graph database to operate.

Choose Neo4j when you need deep graph algorithms, arbitrary pattern matching at scale, or already run it.

### Cognee
Cognee turns documents and data into a knowledge graph plus vectors for agent memory, with pluggable backends.

Lorebase differs by putting **humans and policies between the model and the knowledge**: extraction runs offline, every item carries verified evidence, and nothing reaches a protected Branch without review. Lorebase also keeps **structured sources** first-class, versions everything, and keeps the **read path free of LLM calls**.

Choose Cognee when you want fast, automated memory construction for an agent and do not need reviewed, versioned company records.

### Graphiti and Zep
Graphiti builds a temporally aware graph from agent interactions and documents, tracking when facts were valid; Zep offers it as a memory service.

Lorebase differs in purpose: it is a **system of record** for company knowledge maintained by teams, not an episodic memory that an agent writes as it goes. Lorebase versions with **Branches and Revisions** (what the knowledge base said at any point, and why it changed) rather than per-fact validity intervals, and uses **reviewed Proposals** instead of incremental model writes.

Choose Graphiti or Zep for conversational and episodic agent memory with temporal reasoning.

### Mem0
Mem0 gives agents a personal memory: facts about users and sessions, extracted and updated by an LLM.

Lorebase differs by scope (shared company knowledge, not per-user memory), by **typed schema**, **governance**, **versioning** and **review**. The two are complementary: an agent can use Mem0 for user preferences and Lorebase for company facts.

### DataHub
[DataHub](https://docs.datahub.com/docs/features/feature-guides/mcp.md) is a metadata platform many data teams already run: search, lineage, glossary and ownership over the datasets of their platforms. Its MCP server (`uvx mcp-server-datahub@latest`, or the hosted endpoint with OAuth2 and dynamic client registration) exposes search and lineage, and with `TOOLS_IS_MUTATION_ENABLED=true` also mutation and proposal tools such as `propose_create_glossary_term` and `accept_or_reject_proposals`.

Lorebase differs in what it governs: the Records and Links themselves, not metadata about datasets. It versions schema and data together with branches and three-way merge, writes through typed Operations with preconditions, and reconciles conflicting Source values per Attribute.

Choose DataHub when the question is "which dataset, owned by whom, derived from what" across a data platform. The two fit together: DataHub describes the tables a Lorebase Source maps in.

### Microsoft Fabric IQ ontology
The [Fabric IQ Ontology MCP server](https://learn.microsoft.com/en-us/microsoft-copilot-studio/mcp-fabric-iq-ontology) (preview) exposes an ontology bound to OneLake data to Copilot Studio and other MCP clients, with two tools: `list_ontology_entity_types` and `search_ontology`, which answers natural-language questions. It needs a paid Fabric capacity.

Lorebase differs by being open source and self-hosted on Postgres, with branches, Proposals and Conflict resolution on the ontology and the data, and reads that are deterministic queries rather than natural-language answers.

Choose Fabric IQ when your data already lives in Fabric and your agents run in the Microsoft stack.

### Semantica
[Semantica](https://github.com/semantica-agi/semantica) is an MIT-licensed Python framework that builds knowledge graphs and ontologies from documents and data sources, with OWL generation, SHACL validation, W3C PROV-O provenance, many graph and vector backends and an MCP server (`semantica-mcp`).

Lorebase differs by being one running system on one database with a governed write path: branches and three-way merge of ontology and data, Proposals with approvals, row-level security, an audit chain and a PII vault, and published read latencies. Semantica's page describes no branching or review workflow for graph changes.

Choose Semantica when you want a standards-heavy toolkit to build a graph inside your own application, and the semantic-web formats matter more than a governed system of record.

### Doltgres and the Dolt MCP server
[Doltgres](https://www.dolthub.com/blog/2026-04-23-doltgres-mcp-server/) is Dolt with the Postgres wire protocol; since 2026-04-23 the Dolt MCP server works with it (`MCP_DIALECT=doltgres`). Everything said about Dolt above applies: mature cell-level merge on tables, on its own storage engine.

Lorebase differs by running on stock Postgres rather than a Postgres-compatible engine, and by adding an ontology (Kinds, Relations with cardinality, Traits, Operations), cross-element checks at merge, governed Operations instead of raw SQL for agents, and reconciliation and extraction into Proposals.

Choose Doltgres when you want versioned tables that Postgres tools can talk to.

### TypeDB
[TypeDB](https://typedb.com/docs/tools/mcp-server) has a strongly typed schema language (entities, relations, attributes, inheritance) and an official MCP server, shipped as a Docker image, that runs TypeQL read, write and schema queries.

Lorebase differs by versioning schema and data with branches and merges, reviewing changes through Proposals, and giving agents governed Operations rather than direct write queries.

Choose TypeDB when you need its type system and query language as the database of your application.

### mnem
[mnem](https://github.com/Uranid/mnem) calls itself "Git for AI Agent Knowledge": an embedded, local-first graph with commits, branches and three-way merge, 22 MCP tools and an `mnem integrate` command for desktop hosts. Its store allows one writer at a time; teams share it by committing it with the code.

Lorebase differs by being a multi-user server: a typed ontology, grants, Proposals with approvals, row-level security and PII handling, and Sources that keep it current.

Choose mnem for the memory of one project's agents on one machine.

### Omnigraph
[Omnigraph](https://github.com/ModernRelay/omnigraph), from ModernRelay, is an open-source graph database written in Rust on the Lance format. According to its own README it has a typed schema, branches with a row-level three-way merge, review of changes and an MCP interface. It keeps its own storage rather than reusing Postgres.

Lorebase differs by running on stock Postgres, by adding Operations, grants, Proposals with approvals and PII handling around the ontology, and by reconciling Sources that disagree.

Choose Omnigraph when you want a graph database with its own storage engine and branching; compare the two on your own workload, as this page does not.

### RDF triple stores
Stardog, GraphDB and Amazon Neptune store RDF, answer SPARQL and, depending on the product, validate SHACL shapes and reason over OWL. They are the home of semantic-web ontologies.

Lorebase differs by targeting operational knowledge with governed writes, branches and three-way merge of schema and data, and by not speaking RDF natively (see [Standards](#standards)).

Choose a triple store when you need SPARQL, OWL reasoning or linked data published to others.

### LinkML
LinkML is an excellent way to write schemas as YAML and convert them into many formats.

Lorebase differs by being a **running system** with data, versions, UI and APIs. It imports and exports LinkML schemas (`lorebase schema import --from linkml`, `lorebase schema export --format linkml`, [LinkML](linkml.md)), so teams can keep LinkML as their modeling source or publish Lorebase schemas to the LinkML ecosystem.

### Protégé
Protégé is the reference editor for OWL ontologies and description logic reasoning.

Lorebase differs by targeting **operational knowledge** (records, links, writes, agents) rather than formal axioms and reasoning. Lorebase validates constraints; it does not perform OWL entailment.

Choose Protégé when you need formal ontology engineering, reasoning and OWL interoperability.

## Standards

- **LinkML: supported.** Schemas import from and export to LinkML YAML; an export imported again gives back the same schema, element ids included ([LinkML](linkml.md)).
- **RDF, OWL, SHACL, JSON-LD: not native.** Lorebase does not store triples, answer SPARQL, run an OWL reasoner or validate SHACL shapes; its schema is checked by its own rules. The route to these formats goes through LinkML: export the schema with `lorebase schema export --format linkml`, then use LinkML's generators, for example `gen-owl` for OWL, `gen-shacl` for SHACL shapes and `gen-jsonld-context` for a JSON-LD context. Records leave as JSON through the REST API and the Python client.

## What Lorebase deliberately does not compete on

- Graph algorithms and arbitrary pattern queries at very large scale (graph databases do this better).
- General-purpose versioned SQL (Dolt does this better).
- Automated, unreviewed memory construction for a single agent (memory engines do this faster).
- Formal reasoning over axioms and SPARQL over linked data (OWL tools and triple stores do this).
- Metadata cataloging of a data platform (data catalogs do this).
- Full data platform capabilities (pipelines, BI, app builders).

## Summary

Pick Lorebase when you need a shared, typed, versioned knowledge base that business teams can review and govern, that structured and unstructured sources feed through Proposals, and that external agents read in milliseconds and change only through governed Operations, all on a Postgres you already know how to run.
