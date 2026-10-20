# How Lorebase compares

This page positions Lorebase against products people evaluate for similar problems. It aims to be fair: every product below is good at what it was built for, and several are the right choice in cases where Lorebase is not. Facts about other products reflect their public documentation at the time of writing; check their current docs before deciding. Corrections are welcome through a pull request.

Lorebase in one line: an open-source, Postgres-only knowledge platform where a typed ontology and its data are versioned together (branches, Revisions, three-way merge with attribute-level and structural Conflicts), filled from structured and unstructured sources through reviewable Proposals, and exposed to external agents through REST and MCP with governed Operations and an LLM-free read path.

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

### LinkML
LinkML is an excellent way to write schemas as YAML and convert them into many formats.

Lorebase differs by being a **running system** with data, versions, UI and APIs. The roadmap includes LinkML schema import and export (M5) so teams can keep LinkML as their modeling source or publish Lorebase schemas to the LinkML ecosystem.

### Protégé
Protégé is the reference editor for OWL ontologies and description logic reasoning.

Lorebase differs by targeting **operational knowledge** (records, links, writes, agents) rather than formal axioms and reasoning. Lorebase validates constraints; it does not perform OWL entailment.

Choose Protégé when you need formal ontology engineering, reasoning and OWL interoperability.

## What Lorebase deliberately does not compete on

- Graph algorithms and arbitrary pattern queries at very large scale (graph databases do this better).
- General-purpose versioned SQL (Dolt does this better).
- Automated, unreviewed memory construction for a single agent (memory engines do this faster).
- Formal reasoning over axioms (OWL tools do this).
- Full data platform capabilities (pipelines, BI, app builders).

## Summary

Pick Lorebase when you need a shared, typed, versioned knowledge base that business teams can review and govern, that structured and unstructured sources feed through Proposals, and that external agents read in milliseconds and change only through governed Operations, all on a Postgres you already know how to run.
