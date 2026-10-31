# Reddit

One variant per community. Read each subreddit's rules first: most limit self-promotion and some require an established account or a specific flair. Post at most one per day, never cross-post the same text, and answer every comment. State pre-alpha status in the first paragraph of each post.

## r/selfhosted

Angle: what it costs to run, what it needs, what you get.

**Title:** Lorebase: an open-source, Postgres-only knowledge graph with git-style branches and merges (pre-alpha)

**Body:**

Release 0.3.0 (pre-alpha) is out: https://github.com/aantenore/lorebase/releases/tag/v0.3.0. With Docker and uv, `make demo` in a clone builds a stack under its own compose project, runs a five-day scenario against it and removes it.

I am building Lorebase, a self-hosted ontology platform: you describe what your organization knows as a typed schema in YAML, store it in Postgres, and change it through branches, reviews and merges instead of editing it in place. It is meant as the knowledge base that AI agents and applications read from, under Apache-2.0.

What running it looks like today: `docker compose` starts Postgres 16, a migration job, one stateless API service (Keeper) and a worker (Scribe) for Source syncs, document extraction and webhooks. The job queue lives in Postgres: there is no second datastore, no graph database, no search cluster and no queue broker. A compose profile adds a second API instance, a Postgres streaming replica and an nginx in front; another adds an OpenTelemetry Collector, Prometheus with alert rules and Grafana with dashboards. Authentication supports API keys and any OIDC provider, with role grants per Domain, and writes land in a hash-chained audit log. Outbound calls happen only for what you configure (OIDC signing keys, webhook targets, Sources, a model endpoint for extraction or embeddings), and nothing in it needs an API key.

It is pre-alpha, so please do not put it in front of anything you care about yet. Missing: the design's performance targets at 10M Records on reference hardware, and API stability guarantees, which bind from 1.0. The merge engine, the REST API, the CLI, file, HTTP JSON, Postgres, MySQL/MariaDB and SQL Server Source syncs and the web app work; `examples/merge_demo.py` shows a three-way merge with conflicts in a few seconds without any services. New in 0.3.0, and the part that matters if you sync from other systems: a sync no longer puts back a value someone corrected by hand, and values on which Sources disagree go to a reconciliation queue instead of the newest write winning. If you run 0.2.x, it changes behavior in places; read the breaking changes in the release notes before upgrading. Documentation: https://aantenore.github.io/lorebase/. I would like to hear which sources you would want to sync first and how you back up and monitor what you self-host.

## r/MachineLearning

Angle: the systems problem of trustworthy knowledge for agents, not a model. Use the `[P]` (project) flair and keep it factual.

**Title:** [P] Lorebase: versioned, typed knowledge for agents with three-way merge of schema and data and a model-free read path

**Body:**

Release 0.3.0 (pre-alpha): https://github.com/aantenore/lorebase/releases/tag/v0.3.0. `make demo` runs a five-day scenario against a real stack in a clone.

Lorebase is an open-source system of record for the knowledge that agents consume. The problem it addresses: agents answer from retrieved text or from a graph that a model wrote, and when an answer is wrong there is no history to inspect and no review step that could have caught it.

The design makes four choices. Knowledge is typed (an ontology of Kinds, Attributes and Relations) and enforced in every Revision. It is versioned with branches and immutable content-addressed Revisions, and a merge is a three-way merge of schema and data together, with attribute-level conflicts and consistency checks under the merged schema (type coercion, cardinality, required relations, uniqueness). The read path contains no model at all, which makes reads deterministic, reproducible and cheap; semantic search takes a vector supplied by the caller. And models are confined to offline work: Record embeddings, and extraction that produces reviewable proposals with verified evidence and never writes to a protected branch.

Status: pre-alpha. The engine, the Postgres store, a REST API for reads and governed writes, an MCP server and tiered extraction (rules, local GLiNER and GLiREL, then any LLM with JSON schema output) exist. Extraction quality has not been measured on a benchmark, so I will not claim any numbers. New in 0.3.0: `POST /resolve` grounds the words of a question in the schema and the stored values (exact names, aliases, trigrams, and nearest Records only when the caller passes a vector) without interpreting the question, and query answers carry coverage signals (share of Records missing each Attribute used, Source freshness, phrases unknown in the Domain), so an agent can tell "none" from "not in the data". Code, design and the conflict taxonomy: https://github.com/aantenore/lorebase. Interested in critique of the merge semantics and in related work I should compare against beyond Dolt, TerminusDB, Graphiti and Cognee.

## r/LocalLLaMA

Angle: a knowledge layer for local agents that needs no model and no key. Be careful with embeddings: Scribe embeds Records offline, but Lorebase never embeds the query; semantic search takes a vector from the caller (`lorebase-inference embed` computes one with the same model).

**Title:** A versioned knowledge base for local agents: no model in the read path, one Postgres, Apache-2.0 (pre-alpha)

**Body:**

Release 0.3.0 (pre-alpha): https://github.com/aantenore/lorebase/releases/tag/v0.3.0. `make demo` runs a five-day scenario, including an agent reading and writing through the MCP server, against a real stack.

If you run agents on local models, the knowledge they read from is usually a vector store of chunks or a pile of files. Lorebase is a different shape: a typed knowledge graph in Postgres with git-style branches and merges, where an agent asks a precise question (query, traverse, neighborhood, search) and gets deterministic rows back in milliseconds. No model sits in that path, so it costs nothing per question and a small local model gets exactly the same answers as a large hosted one.

Writes go through governed Operations with validation and dry run, so an agent can propose a change without being able to corrupt the base; protected branches take changes through reviewed proposals. It runs from a single `docker compose` on a laptop and needs no API keys.

Agents connect through the REST API or through an MCP server (Herald, stdio or Streamable HTTP). Document extraction can run on a local model through Ollama or any OpenAI-compatible endpoint, with local GLiNER and GLiREL for spans and relations, and every extracted fact lands in a proposal for review. Scribe can embed Records offline with a local model; the query vector comes from the caller. New in 0.3.0, and useful for small models: `lorebase_resolve` says which Kinds, Attributes and stored values the words of a question stand for, with a ready-made filter, so the model does not have to guess field names. Repository: https://github.com/aantenore/lorebase. Which local models and extraction tasks would you want it to support first?
