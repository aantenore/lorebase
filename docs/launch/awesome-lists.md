# Awesome lists

Curated lists worth a pull request, with the entry to submit. Before each submission, read that list's contribution rules and check that the repository is still active: several lists require a tagged release, a minimum project age, a specific entry format or a specific section, and reject projects that do not meet them. Category names change, so treat the sections below as the closest fit at the time of writing. Never open a pull request to a list for a feature that does not exist yet.

Canonical description, to adapt to each list's format:

> Open-source ontology platform: a versioned knowledge graph for AI agents with git-like branching and merging for schema and data, a model-free read path and governed writes, on Postgres only.

Repository: `https://github.com/aantenore/lorebase`. License: Apache-2.0. Languages: Python (backend), TypeScript (web app).

## Submit once there is a tagged release

### awesome-selfhosted

List: https://github.com/awesome-selfhosted/awesome-selfhosted. Section: Knowledge Management Tools. The format and the requirements (release age, documentation, a working deployment path) are strict; follow its contribution file exactly and wait until the compose quickstart covers a full flow.

```markdown
- [Lorebase](https://github.com/aantenore/lorebase) - Versioned ontology and knowledge graph for AI agents with git-like branching and merging, on Postgres only. ([Source Code](https://github.com/aantenore/lorebase)) `Apache-2.0` `Python`
```

### awesome-postgres

List: https://github.com/dhamaniasad/awesome-postgres. Section: tools built on Postgres. Emphasize that Postgres is the only datastore.

```markdown
- [Lorebase](https://github.com/aantenore/lorebase) - Versioned knowledge graph with git-like branches and three-way merge, stored entirely in Postgres 16, with pgvector for optional search.
```

## Submit now if the rules allow

### awesome-knowledge-graph

List: https://github.com/totogo/awesome-knowledge-graph. Section: tools or databases. Confirm the repository is maintained and accepts tool entries before opening a pull request.

```markdown
- [Lorebase](https://github.com/aantenore/lorebase) - Open-source ontology platform with a versioned knowledge graph, three-way merge of schema and data, and a REST API, on Postgres.
```

### awesome-semantic-web

List: https://github.com/semantalytics/awesome-semantic-web. Section: ontology tools. The wording says what Lorebase is not, to avoid a mismatch with readers who expect RDF and OWL. Skip the list if it accepts RDF tooling only.

```markdown
- [Lorebase](https://github.com/aantenore/lorebase) - Operational ontology platform (typed Kinds, Relations and governed Operations) with branches and merges; not an OWL reasoner.
```

### awesome-mcp-servers

List: https://github.com/punkpeye/awesome-mcp-servers. Section: the closest to knowledge and memory. Herald exists and is documented in [docs/mcp.md](../mcp.md); submit the MCP server, not the REST API.

```markdown
- [aantenore/lorebase](https://github.com/aantenore/lorebase) - MCP server over a versioned, typed knowledge graph: context packs, schema, query, search, neighborhood, explain and diff, with governed writes through Operations.
```

## Not a fit

Lists of agents or agent frameworks (for example awesome-ai-agents): Lorebase is not an agent and does not run one. Skip them.

## Rules for every submission

- One pull request per list, with a title that follows that list's convention.
- Use the list's ordering and punctuation.
- Describe what exists. Do not write "AI-powered", "next-generation" or similar.
- State pre-alpha status in the pull request description unless the list forbids it, and do not hide it.
- If a maintainer asks for a change, make it.
