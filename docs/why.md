# Why Lorebase: git for knowledge

Software teams stopped losing work to overwrites when they started using branches. Nobody edits `main` directly, a change is proposed, someone reads the diff, conflicts surface before they hurt, and every line has an author and a reason. Company knowledge has none of this. This page explains why it should, and what that changes for the people and agents who depend on it.

## The state of company knowledge

Ask where the list of approved suppliers lives. It is a spreadsheet, a table in the ERP, a paragraph in a contract and the head of someone in procurement, and all four disagree a little. When an agent gets a supplier risk question wrong, the first useful question is "what did it know, and since when?". Nobody can answer it, because the knowledge has no queryable history and no review step.

Retrieval over document chunks does not fix this. It answers "what does this text say", not "which parts depend on a single supplier whose contract ends within 90 days". The second question needs typed things (suppliers, parts, contracts), typed relations between them, and values that a person has accepted as true. That is an ontology, and the closed platforms that sell one are large, expensive and tied to a data platform you may not want.

## What branches and merges change

Version control for knowledge is not a gimmick. It changes four things.

**Changes become proposals.** An analyst who wants to reclassify two hundred suppliers does it on a branch, opens a proposal and gets a review. Nothing lands on the branch agents read from until someone accepts it. A nightly ERP feed works the same way: its own branch, then a proposal.

**Disagreement is explicit.** Two people change the same supplier's risk score. In most systems the last writer wins and the other change vanishes without a trace. In Lorebase the merge produces a conflict that names the record, the attribute, the common ancestor value and both new values, and it asks for a decision. The decision is recorded. There are attribute-level conflicts for data, structural conflicts for the schema, and consistency conflicts when the two meet, for example when one branch makes a field required and the other creates records without it. The [conflict taxonomy](design.md#69-conflict-taxonomy) lists every kind.

**Schema and data move together.** This is the part most tools skip. A branch can change a field from text to integer and fix the values that no longer fit. Merging that branch while someone else adds records with the old type is exactly the case where a data-only or schema-only merge corrupts the knowledge base. Merging both at once, against the merged schema, lets the engine say "these three values cannot become integers" before anything is committed.

**History becomes a tool.** Every change is an immutable Revision with an author, a reason and provenance down to the attribute value. You can read the knowledge base as it was last Tuesday, diff it against today, and show an auditor where a value came from. When an agent was wrong, you can reproduce what it saw.

For agents the effect is a safe write path. An agent that wants to flag a supplier calls an Operation with validation, permissions and a dry run. If policy says the change needs a human, it becomes a proposal instead of a write. The agent never holds a database connection and cannot skip the review.

## Why the read path has no model in it

When an agent asks the knowledge base a question, Lorebase runs a query. It does not call a language model to plan the query, rank the results or write the answer. This is a design rule, enforced by the build: the Keeper service is forbidden from importing the inference package.

The reasons are practical. A read that needs a model call takes hundreds of milliseconds to seconds, costs money per question and returns something different next time. A read that is SQL over a materialized branch head takes milliseconds, costs nothing beyond the database and is reproducible, so a wrong answer can be debugged. Agents make many reads per task, so the cost of each one multiplies.

Models still have a place, at the edge. Offline, a model can read documents and propose facts with evidence. Those proposals go through the same review as anything else. The model never writes to a protected branch and never sits between an agent and its answer. This is also why semantic search takes a vector from the caller: Lorebase does not embed your query for you, because that would put a model back in the path.

## Why one Postgres

Every extra datastore is another thing to deploy, secure, back up and monitor. A knowledge platform seems to ask for a graph database, a search engine, a vector store and a queue. We chose to start from a single boring database that most teams already know how to run.

Postgres holds the versioned store, the materialized heads that serve reads, full-text and trigram search, optional vectors through pgvector, and the job queue. One backup covers all of it. Scaling out is the standard Postgres story: more stateless Keeper processes, and read replicas guarded by a freshness check so a client that just wrote never reads something older. If a single primary stops being enough, the Domain is the unit we would shard on, because nothing links across Domains. We have not built that and say so in the [deployment guide](deployment.md).

The trade-off is real. A dedicated graph database is better at deep graph algorithms and arbitrary pattern queries, and we do not compete there. Lorebase offers a smaller query language compiled to predictable SQL: lookups, filters, traversals and neighborhoods, which is what agents need.

## What we refuse to do

These are written down in the [design](design.md#23-anti-goals-things-lorebase-refuses-to-do) as anti-goals, and a pull request that breaks one is a bug.

- No model in the agent read path.
- No direct model writes to protected branches. Extraction produces proposals, always.
- No schemaless writes. Every record conforms to its Kind in the Revision it belongs to.
- No second required datastore.
- No crippled open core. Single sign-on, roles and audit are part of the Apache-2.0 product.
- No mandatory paid API and no vendor SDK in the core. Local open models first, hosted ones by configuration.
- No hidden mutations. Every change is a Revision with an author and a reason.

We also do not build agents, data pipelines or dashboards. Agents live outside, in any framework. Lorebase is what they read from, and the only place they may change what the company knows.

## Where it stands

Lorebase is pre-alpha. The versioning engine and the three-way merge are built and tested, the REST API for reading, schema changes, governed Operations and Proposals with merge and Conflict resolution runs on Postgres, and the web app exists on a mock API. The CLI, the MCP server and Source ingestion are still ahead of us, and the [roadmap](roadmap.md) says in what order. If this matches a problem you have, tell us which domain you would model: [open a discussion](https://github.com/aantenore/lorebase/discussions).
