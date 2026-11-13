# Lorebase

**A governed, versioned knowledge graph that AI agents read over MCP: schema and data branch, merge and get reviewed like code, on the Postgres you already run, with no model in the read path.**

[Documentation](https://aantenore.github.io/lorebase/) | [Quickstart](docs/quickstart.md) | [Why Lorebase](docs/why.md) | [Comparison](docs/comparison.md) | [Validation](docs/validation.md) | [Roadmap](docs/roadmap.md)

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
![Status: pre-alpha](https://img.shields.io/badge/status-pre--alpha-orange)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776ab)
![Node 24+ (web app dev)](https://img.shields.io/badge/node-24%2B%20(web%20app%20dev)-5fa04e)
![Postgres 16](https://img.shields.io/badge/Postgres-16-336791)

- **For data and platform teams** who own company knowledge (suppliers, parts, contracts) and want it typed, reviewed and versioned, with Sources that disagree sent to a reconciliation queue instead of the newest write winning.
- **For agent builders** who want typed Records with provenance, answers stamped with the Revision they came from, and writes only through governed Operations and Proposals.
- **Instead of** chunk retrieval over stale documents, agent memory engines that a model writes unreviewed, or a proprietary ontology platform: see the [comparison](docs/comparison.md).

## An agent call

Start with the [quickstart](#quickstart) to get a Keeper running; this is what an agent then does. Herald, the MCP server, is on PyPI. Against a running Keeper with the sample Domain ([quickstart](#quickstart)):

```bash
export LOREBASE_URL=http://127.0.0.1:8080   # your Keeper; `make up` prints the local one
claude mcp add lorebase -e LOREBASE_URL="$LOREBASE_URL" -e LOREBASE_DOMAIN=supply -- uvx lorebase-herald
```

The agent then calls `lorebase_search` with `{"query": "bearing", "kinds": ["Part"], "limit": 2, "format": "json"}` and gets typed hits stamped with the Revision they were read from (64 hex digits, cut short here; snippets shortened):

```json
{"revision": "10a13aad99df...", "mode": "lexical", "items": [
  {"ref": "Part/P-10001", "title": "Tapered roller bearing 6305", "snippet": "Tapered roller <b>bearing</b> 6305 ...", "score": 2.4},
  {"ref": "Part/P-10010", "title": "Needle roller bearing 6305", "snippet": "Needle roller <b>bearing</b> 6305 ...", "score": 2.4}]}
```

No model ran to answer it. Cursor, VS Code and other hosts take the same `uvx lorebase-herald` command ([MCP host configuration](docs/mcp.md#mcp-host-configuration)); a Keeper with authentication on also needs `LOREBASE_API_KEY`.

> **Pre-alpha, v0.3.1.** The versioning and merge engine, the Keeper REST API, the MCP server (Herald), the Python client, the `lorebase` CLI, Scribe (Sources, document extraction, embeddings) and the web app run today, and interfaces may change before 1.0. [Status and roadmap](#status-and-roadmap) lists what is open. The [quickstart](docs/quickstart.md) is the place to start and the [validation record](docs/validation.md) the place to check the claims.

## 30-second demo

The core engine runs in memory, with no services. This script loads the sample `supply` Domain into `main`, branches `model/risk` (analysts change the schema and data) and `source/erp` (a nightly feed changes data), merges both, lists the Conflicts, resolves them and diffs.

```bash
git clone https://github.com/aantenore/lorebase.git && cd lorebase
uv sync --all-packages
uv run python examples/merge_demo.py
```

```text
== 5. merge source/erp into main
merge base 44de5fcc: 5 Conflicts

DV1 ATTRIBUTE_VALUE (1)
  where            attribute   base  ours  theirs  choices
  -----            ---------   ----  ----  ------  -------
  Supplier S-1001  risk_score  32    80    45      ours/theirs/base/custom

...
== 7. merge again
merged: Revision f6e285ab, parents bd2792a4, 95cc2625
validation: 72 Records against the merged schema, 0 issues
```

The analysts say 80, the ERP says 45, and someone has to decide. That decision is a recorded resolution, not a silent overwrite. The walkthrough of the whole output is in [manual setup](docs/manual-setup.md#1-run-the-core-demo).

## Quickstart

Requirements: [uv](https://docs.astral.sh/uv/) (it installs Python 3.12), Docker with compose (the Docker daemon must be running), `make`, `curl` and `openssl`; no Node.js needed (the web app runs in a container). Keeper uses port 8080, the web app port 8090 and Postgres port 5432 on `127.0.0.1`; change `KEEPER_HOST_PORT`, `LOREBASE_WEB_PORT` and `POSTGRES_HOST_PORT` in `.env` if they are taken.

```bash
git clone https://github.com/aantenore/lorebase.git && cd lorebase
uv sync --all-packages

make env            # .env from .env.example, with generated database passwords
make up             # build the images, start Postgres, migrations, Keeper and the web app; prints the web URL
make example-data   # Domain `supply`: schema, 185 Records with their Links, merged into main

eval "$(make shell-env)"   # exports LOREBASE_URL (the Keeper URL `make up` printed) and LOREBASE_DOMAIN=supply
uv run lorebase search bearing --kind Part     # first query
```

Then open the web app at `http://127.0.0.1:8090` (the URL `make up` printed). It is served by the `web` container, which forwards `/api/` to Keeper, so nothing else is installed or configured. There is nothing to sign in to: the example configuration runs with authentication off, on loopback only. `make down` stops the stack and `make clean-stack` deletes its data. The first `make up` builds the backend and web images (a few minutes; `make up IMAGE_SOURCE=release` pulls the published ones instead); the rest takes about a minute. The [quickstart](docs/quickstart.md) explains each step and continues with [an agent over MCP](docs/quickstart.md#3-connect-an-agent-over-mcp), [a real model](docs/quickstart.md#4-use-a-real-model) and [troubleshooting](docs/quickstart.md#6-troubleshooting). The CLI, the Python client and the MCP server also install from PyPI without a clone (`uv tool install lorebase-cli`, `pip install lorebase-client`, `uvx lorebase-herald`; [host configuration](docs/mcp.md#from-pypi-without-a-clone)).

## Five-day demo

`make demo` runs the [proof scenario](docs/design.md#32-the-5-day-proof-scenario) of the design against a real stack under its own compose project: schema inferred from CSV files, two teams editing the same suppliers on two branches, Conflicts resolved from the command line, a Source sync, an agent reading and writing over MCP, and a backup restored into a second database. It needs Docker and uv; [docs/demo.md](docs/demo.md) tells the story day by day with the transcript.

## Core and experimental

- **Core**: the versioning and merge engine (`lorebase-core`), the Postgres store (`lorebase-store`), the Keeper REST API (`lorebase-keeper`), the Herald MCP server (`lorebase-herald`), the Python client (`lorebase-client`) and the CLI (`lorebase-cli`). This is what the claims above rest on and where stability comes first on the way to 1.0.
- **Experimental**: the [Guide assistant](docs/guide.md), [agent trace](docs/agent-trace.md), the [retrieval lab](docs/retrieval-lab.md), [LinkML](docs/linkml.md) import and export, the MySQL/MariaDB and SQL Server [Sources](docs/scribe.md#sources), the [Helm chart](docs/kubernetes.md), [read replicas](docs/deployment.md) and [storage profiles](docs/performance.md#working-set). They run on `main` and may change or be dropped in any release.

Scribe (the other Sources, document extraction, embeddings) and the web app run today and are neither: tested, but not where interface stability comes first. The [roadmap](docs/roadmap.md#core-and-experimental) keeps the same split.

## How it compares

Lorebase sits between proprietary ontology platforms, versioned databases, graph databases and agent memory engines. The full comparison, including where each of them is the better choice, is in [docs/comparison.md](docs/comparison.md).

| Product | What it is | Branch and merge of knowledge | Choose it instead when |
|---------|-----------|-------------------------------|------------------------|
| **Lorebase** | Typed ontology as a shared system of record, Postgres only | Yes, schema and data together, three-way | You want agents to read a reviewed, versioned knowledge base and change it only through governed Operations |
| Palantir Foundry Ontology | Proprietary operational ontology inside a full data platform | Within the platform | You need the whole platform and a vendor to run it |
| TerminusDB | Versioned document and graph database | Yes, on its own storage engine | You want a general versioned graph database to build on |
| Dolt | SQL database with git semantics | Yes, on tables | Your data is tabular and you want versioned SQL |
| Omnigraph | Typed graph database on Lance, with branches and review | Yes, according to its README, on its own storage | You want a graph database with its own storage engine |

## Status and roadmap

Pre-alpha. [v0.3.1](https://github.com/aantenore/lorebase/releases/tag/v0.3.1) is the current release (the first was [v0.1.0](https://github.com/aantenore/lorebase/releases/tag/v0.1.0)); see the [CHANGELOG](CHANGELOG.md) and the [release process](docs/release.md). Interfaces may change, and before 1.0 any minor version may change them. Open: the performance targets of design 6.14 at 10M Records on the 32 GB reference hardware, and the API stability guarantees for `/api/v1`, which bind from 1.0 ([API stability](docs/api-stability.md)). The [roadmap](docs/roadmap.md) tracks every item and the [validation record](docs/validation.md) lists the runs behind what is described here.

## Documentation

The documentation is published at [aantenore.github.io/lorebase](https://aantenore.github.io/lorebase/); the same pages are in [docs/](docs/). [Issues](https://github.com/aantenore/lorebase/issues) and [discussions](https://github.com/aantenore/lorebase/discussions) are open; see [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md) and the [Code of Conduct](CODE_OF_CONDUCT.md).

- [Features](docs/index.md#features) by area, the [architecture](docs/index.md#architecture) and the [design](docs/design.md)
- [Read performance](docs/index.md#read-performance): p95 of every agent read at 100k Records, with the 1M and 10M runs in [performance](docs/performance.md)
- [Visual tour](docs/tour.md) of the web app, [why Lorebase](docs/why.md), the [FAQ](docs/faq.md) and the [glossary](docs/glossary.md)
- [Agents](docs/agents.md), the [MCP server](docs/mcp.md), [modeling](docs/modeling.md), [Scribe](docs/scribe.md), [operations](docs/operations.md) and [deployment](docs/deployment.md)
- [Security review](docs/security-review.md), [examples](examples/README.md) and the [projects Lorebase works with](docs/index.md#ecosystem)

## License

Apache License 2.0. See [LICENSE](LICENSE). Copyright 2026 Antonio Antenore.
