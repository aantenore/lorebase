# FAQ

Answers reflect what is on `main`. Where a feature is not built yet, the answer says so. The full picture is in the [roadmap](roadmap.md) and the [design](design.md).

**Contents:** [Basics](#basics) | [Comparisons](#comparisons) | [Agents and models](#agents-and-models) | [Versioning and conflicts](#versioning-and-conflicts) | [Operations and scale](#operations-and-scale) | [Project](#project)

## Basics

### What is Lorebase?

An open-source ontology platform: a versioned, governed knowledge graph for AI agents, self-hosted on Postgres. You model what the company knows (Kinds, Attributes, Relations, Operations) as YAML, fill it with Records and Links, change it on branches that are reviewed and merged, and expose it to agents through a REST API (and, later, an MCP server). See [Why Lorebase](why.md).

### Is it a database?

No. It is a service layer on top of Postgres 16. Postgres stores everything (the versioned objects, the materialized branch heads that serve reads, the audit log, the job queue), and Lorebase adds the ontology, the versioning engine with three-way merge, validation, permissions and the API. You do not run a second datastore.

### Is it ready to use?

No. It is pre-alpha: nothing is released and interfaces may change. The engine and a large part of the REST API work and are tested, but there is no CLI, no Python client and no MCP server yet. Try it to evaluate the model and the merge engine, not to run a business on: run `uv run python examples/merge_demo.py` for the in-memory demo, or follow the [quickstart](quickstart.md) to start Postgres and Keeper with docker compose and load the sample Domain.

## Comparisons

### How is it different from a graph database such as Neo4j?

A graph database stores nodes and edges and gives you a powerful query language, but it does not version the content, enforce an ontology or govern writes. Lorebase enforces a typed schema in every Revision, versions schema and data together with branches and merges, and offers a deliberately smaller query DSL (filters, relation predicates, traversal, neighborhood) compiled to SQL. If you need deep graph algorithms or arbitrary pattern queries at scale, use a graph database. See the [comparison](comparison.md#neo4j).

### How is it different from a vector database?

A vector database retrieves the chunks nearest to a query embedding. Lorebase stores typed Records and Links that people have reviewed, and answers questions such as "which parts depend on a single supplier" by query, not by similarity. Vectors are optional in Lorebase and used for search over Records; today nothing computes embeddings (planned for M4), and Lorebase never embeds your query itself: semantic search takes a vector from the caller.

### How is it different from a RAG framework or GraphRAG?

RAG frameworks assemble prompts from retrieved text, and GraphRAG tools use a model to build a graph from documents. Lorebase is the knowledge store such a system can read from: typed, reviewed and versioned. When documents are ingested (M4), a model proposes facts with evidence and a person or policy accepts them; the model never writes to a protected branch and never sits in the read path.

### How is it different from Palantir Foundry's Ontology?

Foundry's Ontology is the conceptual reference and is more mature and broader, inside a proprietary data platform. Lorebase is open source (Apache-2.0), one Postgres and a handful of services, focused on knowledge rather than pipelines or applications, and puts versioning with three-way merge at the center. Choose Foundry if you need the whole platform and a vendor to operate it. See the [comparison](comparison.md#palantir-foundry-ontology).

### How is it different from Dolt or TerminusDB?

They are versioned databases. Dolt versions SQL tables with cell-level merge; TerminusDB versions a document graph on its own storage engine. Lorebase models an ontology (Kinds, Relations with cardinality, Operations), merges schema and data together with consistency rules, runs on plain Postgres, and adds governed writes and agent-shaped reads. Choose Dolt for versioned SQL and TerminusDB for a general versioned graph database. See the [comparison](comparison.md).

### How is it different from agent memory (Mem0, Zep, Graphiti, Cognee)?

Memory engines let an agent remember things as it works, usually with a model writing the memory. Lorebase is shared company knowledge maintained by teams, with review. They are complementary: keep user preferences in a memory layer and company facts in Lorebase.

## Agents and models

### Does it need an LLM?

No. Nothing in Lorebase today calls a model, and the read path never will: Keeper cannot import the inference package, which the build enforces. Later milestones add optional offline extraction from documents, which can use a local model (Ollama) or any OpenAI-compatible endpoint by configuration, and always produces Proposals for review.

### How do agents read?

Through the REST API: Record reads, the query DSL, traversal, neighborhood and search, with time travel to any Revision. Every response names the Revision it was served from and supports `ETag` conditional requests. Because it is plain REST, any framework works today (an agent graph library, a vendor SDK or plain code). An MCP server (Herald) that exposes the same operations as typed tools is planned for M3; it will call Keeper over REST.

### How do agents write?

Only through Operations. An Operation has parameters, validation and permissions, supports dry run and idempotency keys, and commits a Revision with provenance. Built-in Operations (create, update, delete, link, unlink and bulk import) exist today. Custom Operations are written in CEL (preconditions, steps, review policy) in the schema and run through the same endpoint; a false precondition answers `422 precondition-failed` and a review policy that holds sends the edits to a Proposal. A write to a protected branch, such as `main` by default, goes through a Proposal: write to another branch, open a Proposal, have it reviewed (one approval by default) and merge it over REST.

## Versioning and conflicts

### How do conflicts work?

Merging a source branch into a target uses a three-way merge against the common ancestor. The schema is merged first, then the data at attribute level, then the result is checked against the merged schema. Anything that cannot be merged automatically becomes a typed Conflict with a base, an ours and a theirs value: a changed attribute, a delete against a modify, a value that no longer casts to a changed type, a cardinality violation, a dangling link and so on. Each kind offers only the resolutions that make sense (take ours, take theirs, revert to base, custom value, relax a rule). The engine is complete and shown by `examples/merge_demo.py`, and Proposals expose it over REST: the Conflict listing, resolutions one by one or in bulk, and the merge. Callers whose grants are limited to some Kinds see and resolve only the Conflicts of those Kinds. The Review UI has not been wired to these endpoints yet. See the [taxonomy](design.md#69-conflict-taxonomy).

### Can I see what the knowledge base said last month?

Yes. Every change is an immutable Revision. Reads accept `at` with a Revision id, tag or branch, and the diff endpoint compares any two Revisions. Time travel at a past Revision is served from the stored tree, so it supports key reads and Kind listings but not filters or search; those work at branch heads.

### Can I erase personal data from an immutable history?

Not yet. The design handles it with a vault indirection so that erasure does not rewrite history; it is scheduled for M5.

## Operations and scale

### How big can it get?

The design targets, measured server-side at the 95th percentile on 8 vCPU and 32 GB: key lookup under 5 ms and a filtered list under 25 ms at 10M Records. Those are targets, not results yet. What is measured is a baseline on 99,900 Records and 302,940 Links on a laptop ([performance](performance.md)): for a database role not subject to row-level security every agent read is within its budget (p95: 2.4 ms by key, 5.6 ms for a page of 50, 20.5 ms for a 2-hop neighborhood). Under row-level security, as Keeper runs in production, Links of a Record, the neighborhood and full-text search are still over budget, because Postgres cannot use non-leakproof operators as index conditions there. The 1M and 10M runs are not done. Reads scale with Postgres read replicas.

To scale out, run several Keepers behind a load balancer and add replicas. A freshness guard makes sure a client that just wrote never reads an older Revision from a lagging replica. The docker compose `scale-out` profile and the trade-offs are in [deployment](deployment.md). Writes go to one primary and are serialized per branch; the Domain is the sharding unit if one primary stops being enough, which is designed but not built.

### Is it multi-tenant?

One organization per installation. Inside it, a Domain is the isolation boundary for data, permissions and history, enforced by row-level security in Postgres, and grants can be scoped to a Domain, a Kind, an Operation or a branch. Nothing links across Domains. A managed multi-tenant offering is not a goal for v1.

### How is access controlled?

Three authentication modes: none (development only), API keys for agents and services, and OIDC bearer tokens from any provider. Roles are granted per Domain and can be narrowed to a Kind, an Operation or a branch. Writes and administrative actions go to a hash-chained audit log. Sensitivity masking of attributes is planned for M3.

## Project

### What license is it under?

Apache License 2.0. Security, roles, SSO and audit are part of the open-source product, not a paid tier.

### How can I contribute?

Design feedback, real use cases and documentation fixes are as useful as code at this stage. Open an issue or a [discussion](https://github.com/aantenore/lorebase/discussions), read [CONTRIBUTING.md](../CONTRIBUTING.md) for the workflow and the vocabulary, and pick a task from the [M1 plan](plan/m1.md). Security issues go through [SECURITY.md](../SECURITY.md), not public issues.
