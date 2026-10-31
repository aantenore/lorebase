# LinkedIn

A single post from a personal account, with the GIF (`docs/images/tour.gif`) attached as media and the release link and the demo command in the first comment, since links in the body lower reach. Keep it plain and first person. Tag no one.

## Post

Agents are only as good as the knowledge they read, and in most companies that knowledge has no history and no review. When an agent gets a supplier risk question wrong, nobody can say what it knew or since when.

I have been building Lorebase to fix that: an open-source ontology platform, a versioned knowledge graph for AI agents, on Postgres and nothing else.

The idea is to treat knowledge the way we treat code.

- Changes happen on a branch and arrive as a proposal.
- Merging is a three-way merge of schema and data together. Two people change the same supplier's risk score, and the merge asks for a decision instead of silently keeping the last writer.
- Every change is a Revision with an author and a reason, so you can read the knowledge base as it was last month.
- Agents read through an API that never calls a language model, so answers are deterministic and take milliseconds. They write only through governed Operations.

It is pre-alpha, and I would rather say what works than oversell. The versioning engine, the REST API, a CLI, an MCP server for agents and document extraction into reviewed proposals run today. Version 0.3.0 is out, and it is about knowledge that stays correct: a sync from another system no longer overwrites a value someone corrected by hand, and values on which sources disagree wait in a queue for a person to decide. If you already run it, read the breaking changes in the release notes first. The benchmark on reference hardware is next.

If you run knowledge for agents, or you are weighing a proprietary ontology platform, I would like your view on the model. What would you version first?

First comment: https://github.com/aantenore/lorebase/releases/tag/v0.3.0. To try it, `make demo` in a clone of https://github.com/aantenore/lorebase runs a five-day scenario against a real stack.

## Shorter variant

Branches, reviews and merges for company knowledge, so that agents read something you can audit. Lorebase is an open-source versioned knowledge graph on Postgres, with a model-free read path and governed writes. Pre-alpha, current release 0.3.0 (https://github.com/aantenore/lorebase/releases/tag/v0.3.0); `make demo` runs the five-day scenario. The engine, the REST API, the CLI and the MCP server work, and corrections now survive syncs from other systems. Feedback on the merge model welcome.

## Notes

- Quote performance numbers only from docs/performance.md, with its row-level security caveat.
- Do not call it production ready.
- Reuse the same wording for any newsletter or community announcement.
