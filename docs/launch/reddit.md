# Reddit

One variant per community. Read each subreddit's rules first: most limit self-promotion and some require an established account or a specific flair. Post at most one per day, never cross-post the same text, and answer every comment. State pre-alpha status in the first paragraph of each post.

## r/selfhosted

Angle: what it costs to run, what it needs, what you get.

**Title:** Lorebase: an open-source, Postgres-only knowledge graph with git-style branches and merges (pre-alpha)

**Body:**

I am building Lorebase, a self-hosted ontology platform: you describe what your organization knows as a typed schema in YAML, store it in Postgres, and change it through branches, reviews and merges instead of editing it in place. It is meant as the knowledge base that AI agents and applications read from, under Apache-2.0.

What running it looks like today: `docker compose` starts Postgres 16, a migration job and one stateless API service (Keeper). There is no second datastore, no graph database, no search cluster and no queue broker. A compose profile adds a second API instance, a Postgres streaming replica and an nginx in front. Authentication supports API keys and any OIDC provider, with role grants per Domain, and writes land in a hash-chained audit log. It makes no outbound calls apart from fetching signing keys from your OIDC provider if you enable that mode, and nothing in it needs an API key.

It is pre-alpha, so please do not put it in front of anything you care about yet. Missing: the CLI, the Python client, the MCP server and ingestion from CSV, Parquet or databases. The merge engine and the REST API for reading and changing the model work, and `examples/merge_demo.py` shows a three-way merge with conflicts in a few seconds without any services. Quickstart: https://github.com/aantenore/lorebase/blob/main/docs/quickstart.md. I would like to hear which sources you would want to sync first and how you back up and monitor what you self-host.

## r/MachineLearning

Angle: the systems problem of trustworthy knowledge for agents, not a model. Use the `[P]` (project) flair and keep it factual.

**Title:** [P] Lorebase: versioned, typed knowledge for agents with three-way merge of schema and data and a model-free read path

**Body:**

Lorebase is an open-source system of record for the knowledge that agents consume. The problem it addresses: agents answer from retrieved text or from a graph that a model wrote, and when an answer is wrong there is no history to inspect and no review step that could have caught it.

The design makes four choices. Knowledge is typed (an ontology of Kinds, Attributes and Relations) and enforced in every Revision. It is versioned with branches and immutable content-addressed Revisions, and a merge is a three-way merge of schema and data together, with attribute-level conflicts and consistency checks under the merged schema (type coercion, cardinality, required relations, uniqueness). The read path contains no model at all, which makes reads deterministic, reproducible and cheap; semantic search takes a vector supplied by the caller. And models are confined to offline extraction that produces reviewable proposals with evidence, never writes to a protected branch (this part is not built yet).

Status: pre-alpha. The engine, the Postgres store and a REST API for reads and governed writes exist; the MCP server and extraction do not, so there are no extraction quality numbers to report and I will not claim any. Code, design and the conflict taxonomy: https://github.com/aantenore/lorebase. Interested in critique of the merge semantics and in related work I should compare against beyond Dolt, TerminusDB, Graphiti and Cognee.

## r/LocalLLaMA

Angle: a knowledge layer for local agents that needs no model and no key. Be careful not to imply that local-model extraction exists, because it is planned.

**Title:** A versioned knowledge base for local agents: no model in the read path, one Postgres, Apache-2.0 (pre-alpha)

**Body:**

If you run agents on local models, the knowledge they read from is usually a vector store of chunks or a pile of files. Lorebase is a different shape: a typed knowledge graph in Postgres with git-style branches and merges, where an agent asks a precise question (query, traverse, neighborhood, search) and gets deterministic rows back in milliseconds. No model sits in that path, so it costs nothing per question and a small local model gets exactly the same answers as a large hosted one.

Writes go through governed Operations with validation and dry run, so an agent can propose a change without being able to corrupt the base; protected branches take changes through reviewed proposals. It runs from a single `docker compose` on a laptop and needs no API keys.

What does not exist yet: the MCP server (planned, a thin layer over the REST API) and the document extraction that would use a local model through Ollama or any OpenAI-compatible endpoint, with local-first defaults (fastembed, GLiNER, GLiREL). Today you can run the merge demo and the REST API. Repository: https://github.com/aantenore/lorebase. Which local models and extraction tasks would you want it to support first?
