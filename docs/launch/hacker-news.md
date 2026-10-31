# Hacker News

Submit as a link post to the repository, then add the body below as the first comment. Post on a weekday morning US Eastern time. Be present for the first hours to answer.

## Title

```text
Show HN: Lorebase, branch and merge a typed knowledge graph on Postgres
```

71 characters. The URL is `https://github.com/aantenore/lorebase`.

## Body (first comment)

Release 0.3.0 is out: https://github.com/aantenore/lorebase/releases/tag/v0.3.0. In a clone, `make demo` runs a five-day scenario against a real stack (Docker and uv; the first run builds the image, the five days take under a minute).

Lorebase is an open-source ontology platform: a versioned knowledge graph meant to be the system of record that AI agents read from. You model a domain as a typed schema in YAML (Kinds, Attributes, Relations, Operations), and every change to the schema or the data is an immutable Revision on a branch. Merging two branches is a three-way merge of schema and data together: conflicts are typed (same attribute changed on both sides, delete against modify, a value that no longer casts after a type change, a cardinality violation) and resolved explicitly, and the merged data is checked against the merged schema before anything is committed. Reads are deterministic SQL over a materialized branch head with no language model anywhere in the path, and writes happen only through governed Operations with validation, permissions and dry run. It runs on Postgres 16 and nothing else, under Apache-2.0.

It is pre-alpha and I want to be exact about what exists. Built and tested: the versioning and merge engine (you can watch it in `uv run python examples/merge_demo.py`, no services needed), the Postgres store, a REST API with Domains, branches, schema change sets, Operations, Proposals with merge and Conflict resolution, Records, a query DSL, traversal, search, Record history and lineage, log and diff, with API key and OIDC auth, masking of sensitive values and read replicas, the `lorebase` CLI and a Python client, an MCP server, file, HTTP JSON and database Source syncs, document extraction into Proposals that people review item by item, embeddings computed offline, and a web app tested against a live Keeper. New in 0.3.0: a Source sync no longer overwrites a value someone corrected by hand (when upstream changes it, the sync stops at a Conflict), values on which Sources disagree go to a reconciliation queue for a person to decide, and `POST /resolve` tells an agent which Kinds, Attributes and stored values the words of a question stand for, with a coverage block saying what share of Records lacks each Attribute used. Not done: the design's performance targets at 10M Records on reference hardware, and API stability guarantees, which bind from 1.0. I started it because I kept seeing agents answer from knowledge nobody could audit or roll back, and the closed ontology platforms that solve this are out of reach for most teams. I would like feedback on the model (is schema-and-data merge the right unit?) and on which Conflict cases you would want that I have not covered. Documentation: https://aantenore.github.io/lorebase/, design: [docs/design.md](https://github.com/aantenore/lorebase/blob/main/docs/design.md), rationale: [docs/why.md](https://github.com/aantenore/lorebase/blob/main/docs/why.md).

## Prepared answers

Questions that usually come up. Answer with the facts in the [FAQ](../faq.md) and the [comparison](../comparison.md).

- *Why not Dolt or TerminusDB?* They version tables or documents. Lorebase versions an ontology, merges schema and data together with consistency rules, and runs on plain Postgres. Where your data is tabular, Dolt is the better tool.
- *Why not a graph database?* No branching of content and no enforced schema, and a second datastore to run. If you need graph algorithms, use one.
- *Is it fast?* A baseline on 99,900 Records and 302,940 Links on one laptop is in [docs/performance.md](../performance.md): every agent read is within its p95 budget, under row-level security too. At 1M Records every read on `main` is within budget. At 10M, on a shared laptop with an 8 GiB Docker VM, key, page, Link and attribute reads meet the budget but the 2-hop neighborhood, full-text search, history and lineage do not, and the run on reference hardware is not done, so do not quote the design targets as results.
- *I run 0.2.x, what breaks?* It is a pre-1.0 minor that changes interfaces: Source syncs stop at a Conflict instead of overwriting a local correction, some automatic merges into protected branches now wait for steward or role approvals, `text` edits in different sentences merge on their own, and unfiltered semantic search is approximate. Each has a setting to keep the old behavior where one applies; the breaking changes and upgrade notes are in the release notes.
- *Does it need an LLM?* No. The read path never calls a model and is forbidden from importing the inference package. Document extraction can run on rules alone, on local models or on any OpenAI-compatible endpoint.
- *Where is MCP?* Herald, a thin MCP server over the REST API, over stdio and Streamable HTTP ([docs/mcp.md](../mcp.md)).
