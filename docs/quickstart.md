# Quickstart

Lorebase is pre-alpha. This page takes you from a fresh clone to a running Lorebase with the
sample `supply` Domain loaded, the web app open, a first query answered, an agent connected over
MCP and a model answering, then shows how to stop and clean up. Every command runs from the
repository root unless the step says otherwise.

| Step | What you get | Time measured on a fresh clone |
|------|--------------|--------------------------------|
| [2](#2-five-minute-path) | Postgres, Keeper, the web app, the sample Domain, a first query | 1.5 minutes measured; a few minutes more on a machine that has never pulled the base images |
| [3](#3-connect-an-agent-over-mcp) | An agent reading the Domain through the MCP server (Herald) | under a minute |
| [2.7](#27-guide-scribe-and-the-example-documents) | Guide and Scribe with `make up PROFILES=assistant`: query embeddings, semantic search, schema suggestions from the example documents | a few minutes for the second image |
| [4](#4-use-a-real-model) | A chat model for document extraction: Ollama, or a hosted endpoint | seconds with Ollama already on the machine or a hosted endpoint; about 11 minutes the first time for Ollama in Docker |
| [5](#5-stop-and-clean-up) | Stop the stack, keep or delete its data | seconds |
| [6](#6-troubleshooting) | What to do when a step fails | |
| [7 to 12](#7-run-the-core-demo) | The same path step by step: the core engine without services, tests, compose by hand, the REST API with curl and the CLI, the web app in mock mode | |

The measured times of each command are in [Timings](#timings) and in the
[fresh-clone check](validation.md#fresh-clone-quickstart-check).

## 1. Prerequisites

| Tool | Version | Check | Needed for |
|------|---------|-------|------------|
| git | any recent | `git --version` | the clone |
| [uv](https://docs.astral.sh/uv/) | a recent release; it installs Python 3.12 by itself | `uv --version` | the CLI, Herald, the tests |
| Docker with the compose plugin | compose v2 with `up --wait` and `--profile "*"` (checked with Docker 29 and compose 5.1) | `docker compose version` | Postgres, Keeper, Scribe, Ollama |
| Node.js and pnpm | Node 24 or later; pnpm 11 (`corepack enable` installs the version of `apps/web/package.json`) | `node --version`, `pnpm --version` | only the web app dev server (`pnpm dev`, contributors); `make up` serves the web app from a container |
| curl, openssl, make | any | | the helper commands |

Resources: give Docker at least 4 GB of memory (8 GB with the Ollama container), and keep
about 3 GB of disk free for the images (10 GB more for the Ollama image and a model). The
stack publishes these ports on `127.0.0.1` only:

| Port | Service | Variable in `.env` |
|------|---------|--------------------|
| 8080 | Keeper (REST API) | `KEEPER_HOST_PORT` |
| 5432 | Postgres | `POSTGRES_HOST_PORT` |
| 11434 | Ollama (profile `local-models`) | `OLLAMA_HOST_PORT` |
| 8090 | web app (container started by `make up`) | `LOREBASE_WEB_PORT` |
| 5173 | web app dev server (`pnpm dev`, contributors) | picks the next free port by itself |

`lsof -nP -iTCP:8080 -sTCP:LISTEN` (and the same for 8090 and 5432) shows whether a port is taken; if it
is, change the variable in `.env` after step 2.2. The compose project name comes from
`COMPOSE_PROJECT_NAME` in `.env` (default `lorebase`): give a second checkout on the same
machine another name, so that the two stacks never share containers or volumes.

## 2. Five-minute path

### 2.1 Clone and install

```bash
git clone https://github.com/aantenore/lorebase.git
cd lorebase
uv sync --all-packages
```

`uv sync` installs Python 3.12 if needed and every workspace package, among them the `lorebase`
command line and Herald.

### 2.2 Create the environment file

```bash
make env
```

It copies `.env.example` to `.env` and fills `POSTGRES_PASSWORD` and `LOREBASE_APP_PASSWORD`
with random values (`openssl rand -hex 24`), and `LOREBASE_WEBHOOK_KEY`, the key that seals
webhook signing keys, with `openssl rand -hex 32`; an existing `.env` keeps its values. Without
make: `cp .env.example .env`, then set the three yourself (URL-safe characters only for the
passwords: they are embedded in connection strings). The other values have working defaults; this is where you
change ports and `COMPOSE_PROJECT_NAME`.

### 2.3 Start Postgres, Keeper and the web app

```bash
make up
```

This runs `docker compose --env-file .env -f deploy/compose/docker-compose.yaml up -d --build
--wait postgres migrate keeper web`. The first run builds the backend and web images (a few
minutes; later runs reuse the layers). Compose starts Postgres 16, creates the application role Keeper connects
as (`db-roles`), runs the migrations once (`migrate` exits when done), starts the web app and returns when Keeper
and the web app are healthy. It ends by printing the web app URL (`http://127.0.0.1:8090`). Check
Keeper:

```bash
curl http://localhost:8080/readyz
```

```json
{"status":"ready","checks":{"database":{"status":"ok","detail":null}}}
```

`http://localhost:8080/api/v1/docs` serves the interactive OpenAPI docs. The web app answers on
`http://127.0.0.1:8090`, and `http://127.0.0.1:8090/api/v1/auth/whoami` reaches Keeper through it.

**Credentials.** There are none to type. The example configuration
(`config/lorebase.example.yaml`) sets `keeper.auth.mode: none`: Keeper answers every request as
an anonymous installation administrator, and the web app needs no sign-in. That is for a laptop
only: compose publishes the port on `127.0.0.1` and Keeper refuses that mode on any other
address. The database passwords are the ones in `.env`; you never need them for the steps on
this page. Turning authentication on is in
[The first administrator key](#the-first-administrator-key).

### 2.4 Load the example data

```bash
make example-data
```

```text
[quickstart] creating Domain supply (required_approvals 0)
[quickstart] applying the supply schema to branch work
[quickstart] importing the sample data
batch 1/1: rows 1-426 committed, 426 edits
[quickstart] merging Proposal prp_... into main
[quickstart] loaded the supply sample into Domain supply (branches main and work)
```

It uses the `lorebase` CLI against the Keeper of `.env`: creates the Domain `supply`, applies
[`examples/supply/schema.yaml`](https://github.com/aantenore/lorebase/blob/main/examples/supply/schema.yaml)
to a branch `work`, imports the CSV files of `examples/supply/data` (185 Records with their
Links), opens a Proposal from `work` to `main` and merges it. `main` is protected, so changes
reach it only through a Proposal; the quickstart Domain is created with `required_approvals: 0`
so that you, the only principal of a stack without authentication, can merge your own
Proposal. Real Domains keep the default of one approval from someone else. Running it again
does nothing once the sample is on `main`.

### 2.5 First query

```bash
export LOREBASE_URL=http://localhost:8080 LOREBASE_DOMAIN=supply
uv run lorebase record list Part -n 5 \
  --where '{"attr": "category", "op": "eq", "value": "bearing"}' --select category --select unit_cost
uv run lorebase search bearing --kind Part
```

```text
key      title                        category  unit_cost
P-10001  Tapered roller bearing 6305  bearing   10.79
P-10010  Needle roller bearing 6305   bearing   40.48
P-10013  Needle roller bearing NK25   bearing   25.62
P-10023  Needle roller bearing NK25   bearing   37.59
P-10029  Needle roller bearing 6001   bearing   45.94
```

The same over REST, with no CLI:

```bash
curl -s -X POST http://localhost:8080/api/v1/domains/supply/query \
  -H 'Content-Type: application/json' \
  -d '{"kind": "Part", "aggregate": {"group_by": "category"}}'
```

The answer lists the Parts and ends with `"aggregate":{"counts":[{"kind":"Part","value":"bearing","count":6}, ...]}`.

Reads never call a model; every answer names the Revision it was served from in the
`Lorebase-Revision` header. Sections [10](#10-create-a-domain-and-apply-the-supply-schema) and
[11](#11-import-the-sample-data-and-read-it-back) go through the API one call at a time.

### 2.6 Open the web app

`make up` already started it. Open `http://127.0.0.1:8090` (`LOREBASE_WEB_PORT` in `.env`). The
app opens on the `supply` Domain and its `main` branch: Explorer lists the six Kinds, Canvas
draws the graph around a Record, Review lists Proposals (the one that loaded the sample is under
the Merged filter). There is no sign-in with authentication off; with an API key, paste it in
Settings.

The page and the API share one origin: the `web` service of the compose file
(`deploy/docker/web.Dockerfile`, nginx) serves the built app and forwards `/api/` to Keeper on
the compose network, event streams included, so Keeper needs no CORS setting. The Content
Security Policy allows connections to that origin only. With `make up PROFILES=assistant`
([2.7](#27-guide-scribe-and-the-example-documents)) `/guide/` is forwarded to Guide the same way.
The web
settings are in [`.env.example`](https://github.com/aantenore/lorebase/blob/main/.env.example);
[apps/web/README.md](https://github.com/aantenore/lorebase/blob/main/apps/web/README.md#live-mode)
lists the variables of the app itself, and a mock mode with built-in sample data and no Keeper is
in [section 12](#12-web-app).

**Browser sign-in** (`WEB_AUTH_MODE=session`) can be tried on this stack with a mock OpenID
Connect provider, the overlay `deploy/compose/oidc-mock.yaml` (it signs in anyone: local use
only). Add a session key to `.env`, then start Keeper, the web app and the provider with it:

```bash
echo "LOREBASE_SESSION_KEY=$(openssl rand -base64 48)" >> .env
docker compose --env-file .env -f deploy/compose/docker-compose.yaml \
  -f deploy/compose/oidc-mock.yaml up -d --wait keeper web oidc-mock
```

Keeper switches to `auth.mode: oidc` with `auth.sessions` (client `lorebase-web`, PKCE, callback
`http://127.0.0.1:8090/api/v1/auth/callback`) and the app to session mode. Open
`http://127.0.0.1:8090` and sign in: the provider's page asks for any user name (and optional
claims such as `{"name": "Alice"}`), then returns to the app with a session cookie. The provider
runs in Keeper's network namespace and is published on `127.0.0.1:8096` (`OIDC_MOCK_PORT`), so
the browser and Keeper use the same issuer URL, `http://localhost:8096/lorebase`. The user is
created at its first sign-in (`keeper.auth.oidc.auto_provision`) and, like any principal, reads
what its grants allow. To go back to authentication off, remove the provider (the same command
with `rm -sf oidc-mock` instead of `up ...`) and run `make up`. Settings and cookies:
[deployment](deployment.md#web-sign-in).

**Contributors** who change the app run the dev server instead (Node.js 24 and pnpm), with hot
reload, on the Keeper of the stack:

```bash
cd apps/web
pnpm install
printf 'VITE_API_MODE=live\nVITE_KEEPER_BASE_URL=/\n' > .env.local
LOREBASE_KEEPER_PROXY=http://localhost:8080 pnpm dev
```

Open the URL it prints (`http://localhost:5173`, or the next free port). The dev server forwards
`/api` to Keeper, so no CORS setting is needed here either. Use `KEEPER_HOST_PORT` in the proxy
URL if you changed it.

### 2.7 Guide, Scribe and the example documents

Guide (the exploration assistant, `/embed` for query vectors) and Scribe (the worker: extraction,
embeddings) are not part of the plain `make up`. Start them with the `assistant` profile:

```bash
make up PROFILES=assistant     # or: make up-assistant
```

It builds a second image, `lorebase-ml` (target `backend-ml` of the backend Dockerfile: the
backend image plus fastembed and the embedding model `BAAI/bge-small-en-v1.5`, downloaded at
build time so nothing is fetched at run time; 844 MB against 453 MB for the backend image, which
is why Keeper, the migrations and Herald stay on the small one), starts Guide and Scribe next to
the default services, and adds the overlay `deploy/compose/assistant.yaml`: the web app learns
its Guide at `/guide` and Guide accepts calls without credentials, which is only right while
Keeper runs with authentication off as here. `make down` stops every profile `make up` can
start. `EMBEDDING_MODEL` in `.env` names another model to bake in (empty: none, and the model is
downloaded on first use into a writable `inference.embeddings.cache_dir`).

**Semantic search.** The `supply` schema gives `Part` and `Supplier` an embedding template, so
Scribe embeds them on its next pass (every `scribe.scheduler.embeddings_interval_s`, 60 s).
Guide embeds the query, and the vector goes to the search:

```bash
curl -s http://127.0.0.1:8090/guide/embed -H 'Content-Type: application/json' \
  -d '{"text": "protects a rotating shaft from oil leaks", "mode": "query"}' \
  | python3 -c 'import json, sys; print(json.dumps(json.load(sys.stdin)["vector"]))' > query.json
uv run lorebase search "protects a rotating shaft from oil leaks" --kind Part \
  --mode semantic --vector-file query.json -n 3
```

```text
kind  key      title                       score  snippet
Part  P-10028  Radial shaft seal 25x42x7   0.732  Radial shaft seal 25x42x7 Stock item for machine tool spind...
```

The first hits are radial shaft seals although the query shares no word with the name. `/embed`
needs no chat model. Guide's questions (`/explore`, the Explore panel) do: set `LLM_*` as in
[section 4](#4-use-a-real-model).

**Schema suggestions.** Extraction reports what the documents hold that the schema has no place
for, and lists a suggestion once `schema.suggestions.min_support` observations (2) support it. A
single document supports each of its suggestions once, so uploading only the contract C-3099
lists nothing (`lorebase schema suggestions list --weak` shows the one it made, an Attribute
`buyer` of Contract). The step below uploads that contract and the four documents of
`examples/supply/documents`, rules tier only, so it needs no model, and prints the suggestions:

```bash
make example-documents
```

```text
id                              type       name               of        support  docs  status   samples
ssg_...                         attribute  ends               Supplier  3        1     pending  2026-02-07, 2026-02-09, 2026-04-12
ssg_...                         relation   sole_source_parts  Supplier  3        1     pending
ssg_...                         attribute  buyer              Contract  2        2     pending  Corvane Industries, purchasing entity of the Distribution d...
```

`buyer` is supported by two documents (C-3099 and C-3025), `ends` and `sole_source_parts` by the
three rows of the supplier review. The contract C-3099 also opens a Proposal with its new
Contract Record; the other documents describe Records that `make example-data` loaded. Review
the suggestions in the web app (Review, Schema suggestions) or with
`uv run lorebase schema suggestions accept ID`; see [extraction](extraction.md#schema-suggestions).

## 3. Connect an agent over MCP

Herald, the MCP server, turns the Domain into tools an MCP host can call (`lorebase_search`,
`lorebase_get`, `lorebase_query`, `lorebase_context` and more, listed in [mcp.md](mcp.md#tools)).
It runs from the checkout; the host starts it over stdio. Add this entry to the MCP
configuration of your host (most hosts read an `mcpServers` map; [mcp.md](mcp.md#mcp-host-configuration)
has the file for each host), with the absolute path of your clone:

```json
{
  "mcpServers": {
    "lorebase": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/lorebase", "run", "lorebase-herald"],
      "env": {
        "LOREBASE_URL": "http://127.0.0.1:8080",
        "LOREBASE_DOMAIN": "supply"
      }
    }
  }
}
```

Restart the host and ask it, for example, "which parts depend on a single supplier?". With
authentication off no key is needed. Once authentication is on, give the agent its own key with
only the grants it needs, and keep it out of the file with
`"LOREBASE_API_KEY": "secret://file//absolute/path/to/agent.key"`
([mcp.md](mcp.md#run-it)).

To check Herald without a host, list its tools and run one search from Python (the `mcp`
package is installed by `uv sync`):

```bash
LOREBASE_URL=http://127.0.0.1:8080 LOREBASE_DOMAIN=supply uv run python - <<'EOF'
import asyncio, os
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    server = StdioServerParameters(command="uv", args=["run", "lorebase-herald"], env=dict(os.environ))
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        print([tool.name for tool in (await session.list_tools()).tools])
        result = await session.call_tool("lorebase_search", {"query": "bearing", "limit": 2})
        print(result.content[0].text)

asyncio.run(main())
EOF
```

Herald logs its requests to Keeper on standard error; standard output shows:

```text
['lorebase_schema', 'lorebase_get', 'lorebase_query', 'lorebase_search', ...]
rev bb30d34bebf7... (lexical): 2 hits
1. Part/P-10001 "Tapered roller bearing 6305" (2.4) - Tapered roller <b>bearing</b> 6305 ...
```

For agent frameworks that speak Streamable HTTP, run one shared Herald with
`LOREBASE__HERALD__TRANSPORT=streamable_http` ([mcp.md](mcp.md#run-it)).

## 4. Use a real model

Reads never call a model. Models are used by Scribe, the worker, to extract documents into
Proposals ([extraction.md](extraction.md)). Lorebase talks to any OpenAI-compatible endpoint,
selected by three variables of `.env`: `LLM_BASE_URL`, `LLM_MODEL` (default: `OLLAMA_MODEL`) and
`LLM_API_KEY`. `inference-check` sends one short prompt and prints the model, the latency and
the token counts. Pick one of the three options.

**Ollama in Docker (the default).** The `local-models` profile starts Ollama and pulls
`OLLAMA_MODEL`. The image is about 7 GB and the default model `qwen2.5:7b-instruct` 4.7 GB; it
runs on the CPU inside Docker. A small model is enough to try it:

```bash
# in .env: OLLAMA_MODEL=qwen2.5:1.5b-instruct  (and OLLAMA_HOST_PORT=11435 if Ollama already runs on the host)
docker compose --env-file .env -f deploy/compose/docker-compose.yaml \
  --profile local-models run --rm inference-check
```

```text
chat       ok     provider=openai_compatible model=qwen2.5:1.5b-instruct latency_ms=36015 tokens=30/9 structured_output=json_schema
```

The model download prints nothing while it runs; follow it with
`docker compose --env-file .env -f deploy/compose/docker-compose.yaml logs -f ollama-pull`.

**Ollama already on your machine** (faster on a Mac, where Docker has no GPU). Point the stack at
it and skip the Ollama container with `--no-deps`:

```bash
ollama pull qwen2.5:7b-instruct
# in .env: LLM_BASE_URL=http://host.docker.internal:11434/v1 and LLM_MODEL=qwen2.5:7b-instruct
docker compose --env-file .env -f deploy/compose/docker-compose.yaml run --rm --no-deps inference-check
```

Any instruct model works. Prefer one that answers without a long reasoning phase: a reasoning
model such as `qwen3:4b` passes the check but can run past the 120-second request timeout on a
whole document.

On Linux, Ollama must listen beyond loopback for containers to reach it (`OLLAMA_HOST=0.0.0.0`).

**A hosted provider.** Any OpenAI-compatible endpoint works. Keep the key in a file, never in
`.env` or on the command line: `secrets/` is ignored by git, and the overlay
`deploy/compose/hosted-model.yaml` mounts the file as a compose secret that the services read
through `secret://file//run/secrets/llm_api_key`.

```bash
mkdir -p secrets && (umask 077 && cat > secrets/llm_api_key)   # paste the key, then Ctrl+D
# in .env: LLM_BASE_URL=https://api.example-provider.com/v1, LLM_MODEL=<model name>, LLM_API_KEY empty
docker compose --env-file .env -f deploy/compose/docker-compose.yaml \
  -f deploy/compose/hosted-model.yaml run --rm --no-deps inference-check
```

Providers that do not speak the OpenAI protocol go through the LiteLLM proxy (profile `litellm`);
per-task routing, prices and embeddings are in [inference-providers.md](inference-providers.md).

**Extract a document.** Start Scribe with the same files (`make up PROFILES=assistant` starts it;
or, by hand, add `-f deploy/compose/hosted-model.yaml` for a hosted key) and upload a contract that the sample does not hold yet; tier 0 uses rules
only, tier 2 adds the chat model:

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.yaml up -d --no-deps scribe
uv run lorebase documents upload examples/five_day_demo/documents/contract-c-3099-wexrix-filtration.md --tiers 0,2
uv run lorebase documents list                 # then: uv run lorebase documents get <id>
uv run lorebase proposal list --status open
```

When the job has succeeded, the extraction is an open Proposal from branch `extract/<job>` to
`main`, with one item per Record, value and Link and the evidence of each (here the Contract
C-3099 and its `with_supplier` Link). Review it under Review in the web app, or with
`uv run lorebase proposal items <id>` and `uv run lorebase proposal decide`. The documents of
`examples/supply/documents` describe Records that `make example-data` already loaded, so their
jobs succeed with no items and open no Proposal. With `lfm2.5:8b` on the host the job took 91
seconds; a small model on the CPU inside Docker takes several minutes.

## 5. Stop and clean up

```bash
make down          # stop the containers of every profile `make up` can start, keep the data
make clean-stack   # stop them and delete the volumes: database, pulled models
```

`make clean-stack` runs `docker compose ... --profile "*" down -v --remove-orphans`, so the
Ollama and observability containers go too. (If you started the contributor dev server, stop it with Ctrl+C.) To remove the built images as
well: `docker image rm lorebase:dev lorebase-ml:dev lorebase-web:dev` (the `LOREBASE_IMAGE`,
`LOREBASE_ML_IMAGE`, `LOREBASE_WEB_IMAGE` and `LOREBASE_TAG` of `.env`). Delete the clone and nothing else is left on the machine.

## 6. Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `Bind for 127.0.0.1:5432 failed: port is already allocated` (or 8080, 8090, 11434) | Another Postgres, service or Ollama holds the port. Set `POSTGRES_HOST_PORT`, `KEEPER_HOST_PORT`, `LOREBASE_WEB_PORT` or `OLLAMA_HOST_PORT` in `.env`, run `make up` again, and use the new Keeper port in `LOREBASE_URL` (and `LOREBASE_KEEPER_PROXY` of the dev server). |
| `required variable POSTGRES_PASSWORD is missing a value`, `set LOREBASE_APP_PASSWORD in the environment file` or `set LOREBASE_WEBHOOK_KEY in the environment file` | `.env` is missing or a password or the webhook key is empty: `make env` fills them. |
| Keeper unhealthy, `make up` fails with `dependency failed to start` | `docker compose --env-file .env -f deploy/compose/docker-compose.yaml logs migrate keeper` names the error. A password changed after the first start does not change the database: run `make clean-stack` (deletes the data) and `make up`. |
| `curl: (52) Empty reply from server` right after `up -d` without `--wait` | Keeper is still starting; `make up` waits for its health check. |
| The stack runs code older than the checkout | The image tag `lorebase:dev` is shared by every checkout on the machine. `make up` always passes `--build`; with plain compose add `--build`, or set `LOREBASE_TAG` per checkout. |
| Containers or volumes of another Lorebase stack are reused or replaced | Both use the project name `lorebase`. Set `COMPOSE_PROJECT_NAME` in `.env`. |
| `docker compose` hangs on pull or build with a credential helper error | The Docker credential helper is stuck. Run the command with an empty Docker config: `DOCKER_CONFIG=$(mktemp -d) make up` (public images need no login). |
| `no configuration file provided: not found` | The command left out `-f deploy/compose/docker-compose.yaml`, or did not run from the repository root. |
| `make example-data`: `Domain supply exists ... but main does not hold the sample` | An earlier load stopped half way, or the Domain was created by hand (sections 10 and 11). `uv run lorebase domain delete supply -y`, then `make example-data`. |
| `make example-data`: `no Keeper at http://127.0.0.1:8080` | The stack is not running, or Keeper runs on another port: `make up`, or `LOREBASE_URL=http://127.0.0.1:PORT make example-data`. |
| The web app shows "This Branch has no Kinds yet" | It is on a branch without the sample: pick `main` or `work` in the top bar, or run `make example-data`. |
| The web app at `http://127.0.0.1:8090` shows a network error or `502 Bad Gateway` on `/api/` | Keeper is not up (or restarting): `docker compose --env-file .env -f deploy/compose/docker-compose.yaml ps keeper web` and `logs keeper web`. The web service reaches Keeper at `keeper:KEEPER_PORT`, so `KEEPER_PORT` must be the port Keeper listens on. |
| The web app does not load, or the browser blocks requests (CSP) | Open it at the address `make up` printed (`LOREBASE_WEB_PORT`), not through another host name or a tunnel: the policy allows connections to the origin that served the page. Add other origins with `LOREBASE_WEB_CSP_CONNECT_SRC` on the `web` service. |
| The assistant (Guide) does not answer from the web app | Start it with `make up PROFILES=assistant`. A `502` on `/guide/` means Guide is not running. With plain compose, add the profile and `-f deploy/compose/assistant.yaml`, or set `WEB_GUIDE_URL=/guide` and `GUIDE_ALLOW_ANONYMOUS=true` in `.env`. |
| `/guide/embed` answers 502 | The service runs on the plain backend image: build the `lorebase-ml` image (`make up PROFILES=assistant` does) or point `inference.embeddings` at an OpenAI-compatible endpoint. |
| `make example-documents` waits and gives up | Scribe is not running: `make up PROFILES=assistant`. |
| Schema suggestions are empty after one upload | A pending suggestion is listed from `schema.suggestions.min_support` (2) observations: run `make example-documents`, or `lorebase schema suggestions list --weak`. |
| The contributor dev server (`pnpm dev`) cannot reach Keeper | `LOREBASE_KEEPER_PROXY` must be set when `pnpm dev` starts and must name Keeper's port; `.env.local` must hold `VITE_API_MODE=live` and `VITE_KEEPER_BASE_URL=/`. Restart `pnpm dev` after a change. |
| The MCP host shows no Lorebase tools | The host could not start `uv`: give its absolute path (`which uv`) in `command`, and the absolute path of the clone after `--directory`. Check Herald with the script of [section 3](#3-connect-an-agent-over-mcp). |
| Herald answers `[not-found]` | `LOREBASE_DOMAIN` names a Domain that does not exist, or `LOREBASE_BRANCH` a branch without the Record. |
| `inference-check`: connection refused or timeout | Ollama is not reachable from the containers: in Docker the address is `http://ollama:11434/v1` (profile `local-models`), on the host `http://host.docker.internal:11434/v1` with `--no-deps`. |
| An extraction job fails with `EndpointError: /chat/completions: request failed (ReadTimeout)` | The model needs more than `inference.llm.timeout_s` (120 s) per call: use a smaller or non-reasoning instruct model, or raise the timeout in a copy of `config/lorebase.example.yaml` named by `LOREBASE_CONFIG_FILE` in `.env`. |
| `inference-check`: `model ... not found` | The model is not pulled: `ollama pull NAME` on the host, or wait for `ollama-pull` (`docker compose ... logs -f ollama-pull`). |
| `inference-check` with the hosted overlay: `bind source path does not exist: .../secrets/llm_api_key` | Create `secrets/llm_api_key`, or point `LLM_API_KEY_FILE` at the key file (relative paths start from `deploy/compose`). |
| A step is slow the first time | The backend image build, the Ollama image (about 7 GB) and model downloads happen once; [Timings](#timings) shows what to expect. |

## Timings

Measured on 2026-10-22 on an Apple silicon laptop (Docker Desktop with Docker 29.5 and compose
5.1, 15 CPUs and 7.7 GB of memory for Docker), from a fresh copy of the repository, following
this page. The Docker build cache and the pgvector and Python base images were already on the
machine; a machine that has never pulled them spends a few more minutes in `make up`.

| Step | Command | Time |
|------|---------|------|
| 2.1 | `git clone` | 6 s |
| 2.1 | `uv sync --all-packages`, empty uv cache | 29 s (1 s with a warm cache) |
| 2.2 | `make env` | under 1 s |
| 2.3 | `make up` (backend and web images, Postgres, migrations, Keeper and web healthy) | 38 s (the web image adds a `pnpm install` and a build) |
| 2.4 | `make example-data` | 5 s |
| 2.5 | first query, CLI and curl | 1 s |
| 2.6 | open the web app (already running); contributor path: `pnpm install`, empty store; `pnpm dev` until ready | 0 s; 11 s; 2 s |
| 3 | Herald check script (start, list tools, one search) | 3 s |
| 4 | `inference-check`, Ollama in Docker, first time (7 GB image, `qwen2.5:1.5b-instruct`, CPU) | 645 s; 209 s with the image already pulled |
| 4 | `inference-check`, Ollama on the host (`lfm2.5:8b`) | 9 s |
| 4 | `inference-check` with the hosted overlay and a key file | 5 s |
| 4 | extraction of contract C-3099, tiers 0 and 2, `lfm2.5:8b` on the host | 91 s; tier 0 alone under 1 s |
| 5 | `make down`; `make clean-stack` | 1 s; under 1 s |

`make up PROFILES=assistant` adds the `lorebase-ml` image: 844 MB against 453 MB for the backend
image (fastembed with ONNX Runtime, and a 65 MB model).

The five-minute path (2.1 to 2.6) took about 80 seconds end to end.

## 7. Run the core demo

This needs only step 2.1.

```bash
uv run python examples/merge_demo.py
```

No Postgres and no network. The script loads the supply schema and its CSV data into an
in-memory tree on `main`, forks `model/risk` and `source/erp`, makes conflicting and
non-conflicting changes on both, merges, lists the Conflicts by kind, resolves them, merges
again and diffs two Revisions. The output is the same on every run. Excerpt:

```text
== 5. merge source/erp into main
merge base 7ad4a553: 5 Conflicts

DV1 ATTRIBUTE_VALUE (1)
  where            attribute   base  ours  theirs  choices
  -----            ---------   ----  ----  ------  -------
  Supplier S-1001  risk_score  32    80    45      ours/theirs/base/custom
...
XS4 MISSING_VALUE (1)
  where                        attribute  base  ours  theirs  choices
  -----                        ---------  ----  ----  ------  -------
  Supplier x2: S-1032, S-1033  risk_band                      custom/relax
```

More examples are indexed in [examples/README.md](../examples/README.md); editing a schema as
code is in [examples/schema_as_code.md](../examples/schema_as_code.md).

## 8. Run the tests

```bash
make test-unit
```

This runs the unit tests of every package and of the repository tooling, including the demo
above; none of them needs Docker or the network. To run only the demo test:

```bash
uv run pytest tests/tooling/test_merge_demo.py
```

`make lint type` runs the linters and the type checker. Integration tests (`make test-int`)
need Docker.

## 9. Start Postgres and Keeper by hand

What `make env` and `make up` do, with compose directly:

```bash
cp .env.example .env
```

Edit `.env` and set `POSTGRES_PASSWORD` and `LOREBASE_APP_PASSWORD` (URL-safe characters
only, for example `openssl rand -hex 24`; they are embedded in the connection strings) and
`LOREBASE_WEBHOOK_KEY` (`openssl rand -hex 32`). The other values have working defaults. Then:

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.yaml up -d --build --wait postgres migrate keeper
```

The first run builds the backend image, which takes a few minutes; `--build` rebuilds it when the
checkout changed (the tag `lorebase:dev` is shared by every checkout on the machine) and `--wait`
returns once Keeper is healthy. Compose starts Postgres 16,
creates the application role Keeper connects as (`db-roles`), runs the Alembic migrations once
(`migrate` exits when done) and starts Keeper on
`127.0.0.1:8080`. Check it:

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.yaml ps
curl http://localhost:8080/readyz
```

```json
{"status":"ready","checks":{"database":{"status":"ok","detail":null}}}
```

`/healthz` is the liveness probe and `http://localhost:8080/api/v1/docs` serves the OpenAPI
docs.

> **Development only.** The shipped `config/lorebase.example.yaml` sets `keeper.auth.mode:
> none`: Keeper accepts every request as an administrator
> (`curl http://localhost:8080/api/v1/auth/whoami` answers `"anonymous"` with
> `"installation_admin":true`). Compose publishes the port on `127.0.0.1` only and sets
> `KEEPER_ALLOW_INSECURE=true`, without which Keeper refuses that mode off loopback. Do not expose it,
> and use `api_key` or `oidc` modes (they need the `database` section and further settings,
> see the comments in the file) for anything shared.

### The first administrator key

In `api_key` mode Keeper accepts no request without a key, and only an administrator can issue
one. The first key comes from the database side, with Keeper's own settings: list the
administrator in `keeper.auth.installation_admins` (for example `[svc_admin]`), then run

```bash
docker compose -f deploy/compose/docker-compose.yaml exec -T keeper \
  python -m lorebase_keeper bootstrap > admin.key      # or `uv run python -m lorebase_keeper bootstrap`
```

It creates the service principal `svc_admin` when it does not exist and a key with the default
lifetime (`keeper.auth.api_keys.default_ttl_days`), writes an `apikey.bootstrap` entry to the audit
log, and prints the key once on standard output (messages go to standard error). It refuses when
an installation administrator already holds a usable key: issue further keys with that one, or
pass `--force` to add another (existing keys stay valid; revoke them over the API).
It connects as the owner role of `database.maintenance_dsn` when that is set (compose sets it), else
with `database.dsn`.
`--principal` picks another entry of `installation_admins`. The administrator creates a Domain and
holds `admin` in it; `python -m lorebase_keeper principal create ID` adds a service principal with a
chosen id, for a schema that names it in Operation `permissions`.

## 10. Create a Domain and apply the supply schema

This section and the next build by hand what `make example-data` loads. On a stack where it
already ran, delete the Domain first (`uv run lorebase domain delete supply -y`), or start from
a clean stack (`make clean-stack && make up`).

Each step is shown twice: with curl against the REST API, and with the `lorebase` command line
(installed by `uv sync --all-packages`, run as `uv run lorebase`). Both call the same
endpoints. Tell the CLI where Keeper is:

```bash
export LOREBASE_URL=http://localhost:8080
export LOREBASE_DOMAIN=supply        # the default for --domain
uv run lorebase whoami
```

With `keeper.auth.mode: api_key` or `oidc` also set `LOREBASE_API_KEY` (or `LOREBASE_TOKEN`, an
OIDC access token). Instead of environment variables you can keep a profile with the URL,
credentials, `domain` and `branch` in `~/.config/lorebase/config.toml` (see
[the settings module](../packages/cli/src/lorebase_cli/settings.py)). Every command takes `--json`
for machine-readable output; exit codes are 0 success, 1 usage or not found, 2 validation or
conflict, 3 authentication or permission, 4 server error. `lorebase --install-completion` sets
up shell completion. The same API is available from Python (`lorebase_client`, sync and async).

Create the Domain `supply`. It starts with one Revision and a protected `main` branch.

```bash
# curl
curl -s -X POST http://localhost:8080/api/v1/domains \
  -H 'Content-Type: application/json' \
  -d '{"id": "supply", "display_name": "Supply chain"}'

# CLI
uv run lorebase domain create supply --name "Supply chain"
```

`main` is protected by default (`keeper.domains.protect_default_branch`): changes to it go
through a Proposal (`POST /api/v1/domains/supply/proposals` or `lorebase proposal create`, then a
review and a merge; approvals are set by `keeper.proposals` and the Domain's `required_approvals`
setting), so a direct change answers `409 protected-branch` (exit code 2 in the CLI). Work on
another branch:

```bash
# curl
curl -s -X POST http://localhost:8080/api/v1/domains/supply/branches \
  -H 'Content-Type: application/json' \
  -d '{"name": "work"}'

# CLI
uv run lorebase branch create work
```

`--mode copy|overlay` chooses how the branch stores its rows (own copy, or only its changes); without it `store.materialize.branch_mode` applies. A copy of a large branch is usable at once and finishes copying in the background; `--wait` waits for it ([operations](operations.md#large-branches)).

The schema of the sample Domain has two Operations (`flag_supplier_risk`, `approve_contract`)
and two Derivations (`Part.supplier_count`, `Part.single_sourced`); the Operations and
`single_sourced` are written in CEL. Expressions are type checked against the schema when it is
applied (`schema.expressions_enabled`, on by default). Check it first with the validate
endpoint, which writes nothing:

```bash
# curl
curl -s -X POST 'http://localhost:8080/api/v1/domains/supply/schema:validate?branch=work' \
  -H 'Content-Type: text/yaml' --data-binary @examples/supply/schema.yaml

# CLI (exit code 2 when the document is not valid)
uv run lorebase schema validate examples/supply/schema.yaml -b work
```

The answer has `"valid":true`. Apply it to `work` as one schema change set (a YAML body is the
full schema document; options go in the query string). `schema diff` shows what would change
first:

```bash
# curl
curl -s -X POST 'http://localhost:8080/api/v1/domains/supply/branches/work/schema/changes?message=Apply%20supply%20schema' \
  -H 'Content-Type: text/yaml' --data-binary @examples/supply/schema.yaml

# CLI
uv run lorebase schema diff examples/supply/schema.yaml -b work
uv run lorebase schema apply examples/supply/schema.yaml -b work -m "Apply supply schema"
```

The answer is `"status":"committed"` with the new Revision and 21 changes (the Domain, six
Kinds, seven Relations, a Trait, two Derivations, two Operations and two Sources). Read the schema back:

```bash
# curl
curl -s 'http://localhost:8080/api/v1/domains/supply/schema?branch=work&format=yaml'

# CLI (YAML by default, JSON with --json)
uv run lorebase schema get -b work
```

## 11. Import the sample data and read it back

Every write goes through an Operation. The built-in `import_records`, `import_links` and
`import_batch` Operations take rows keyed by Attribute api name; booleans must be JSON booleans
and Link rows use `from` and `to`. A Record row may carry `links`, a map from a Relation name to
the key (or keys) at the other end: `Contract`, `PurchaseOrder` and `RiskFlag` need their
`with_supplier`, `for_part` and `flags` Link to exist, so each one is committed in the same
Revision as its Links. Imports upsert, so running one twice is safe.

The CLI does the conversions for you: it reads the schema of the branch, turns `true` and
`false` into booleans, keeps integers and decimals exact, leaves empty cells unset, maps
`from_key` and `to_key` to `from` and `to`, and sends the file in batches (one Revision each,
`--batch-size`) with progress on standard error. `import records` and `import links` load the
Kinds that need no Link on creation and the Link files:

```bash
# CLI
uv run lorebase import records Supplier examples/supply/data/supplier.csv -b work
uv run lorebase import records Part examples/supply/data/part.csv -b work
uv run lorebase import records Plant examples/supply/data/plant.csv -b work
uv run lorebase import links supplied_by examples/supply/data/supplied_by.csv -b work
uv run lorebase import links stocked_at examples/supply/data/stocked_at.csv -b work
```

```text
batch 1/1: rows 1-30 committed, 30 edits
field      value
record     Supplier
branch     work
mode       committed
rows       30
edits      30
batches    1
revisions  3403d8d356d9
```

`Contract`, `PurchaseOrder` and `RiskFlag` need a Link when they are created, so their Link
files travel with them. `import batch` takes a Kind file per `--records` and a Relation file per
`--links` (same formats and conversions) and cuts them into requests that never separate a
Record from its Links: a Link goes with the Record at its `from` end, or at its `to` end when
that Record is not in the import. Records without Links go first, so name the Kinds that others
point to before the ones that point to them. `--batch-size` counts Records and Links together:

```bash
# CLI
uv run lorebase import batch -b work \
  --records Contract=examples/supply/data/contract.csv \
  --records PurchaseOrder=examples/supply/data/purchase_order.csv \
  --records RiskFlag=examples/supply/data/risk_flag.csv \
  --links with_supplier=examples/supply/data/with_supplier.csv \
  --links for_part=examples/supply/data/for_part.csv \
  --links delivered_to=examples/supply/data/delivered_to.csv \
  --links flags=examples/supply/data/flags.csv
```

```text
batch 1/1: rows 1-240 committed, 240 edits
field      value
batch      Contract, PurchaseOrder, RiskFlag, with_supplier, for_part, delivered_to, flags
branch     work
mode       committed
rows       240
edits      240
batches    1
revisions  2d84947d8d75
```

The same call exists as the `import_batch` Operation, which takes `records` (rows with a `kind`)
and `links` (rows with a `relation`, `from` and `to`) and commits a Record with its Links in one
Revision. This one only computes the edits (`dry_run`):

```bash
# curl
curl -s -X POST http://localhost:8080/api/v1/domains/supply/operations/import_batch/execute \
  -H 'Content-Type: application/json' \
  -d '{"branch": "work", "dry_run": true, "parameters": {
        "records": [{"kind": "Contract", "contract_number": "C-3999", "title": "Spot buy",
                     "start_date": "2026-02-01"}],
        "links": [{"relation": "with_supplier", "from": "C-3999", "to": "S-1001"}]}}'
```

When a file already has a column with the key at the other end, `import records` sends it as the
Link of each row: `import records Contract contracts.csv --link with_supplier=supplier_code`
(repeatable; JSON Lines and Parquet cells may hold a list of keys). Files can be CSV, TSV, JSON
Lines, a JSON array or Parquet (`pip install 'lorebase-cli[parquet]'`); `--map
file_column=attribute` renames columns, `--dry-run` computes without committing. A rejected
request names the file and row of each issue and how many rows are already committed.

Add `"dry_run": true` next to `"branch"` (CLI: `--dry-run`) to compute the edits and
violations without committing. Rows that break the schema are rejected as a whole with `422` and
`issues` that point at the row and Attribute; the CLI prints them with the row numbers of the
file and says which earlier rows are already committed.

Each import is a Revision; the branch moved ahead of `main`:

```bash
# curl
curl -s http://localhost:8080/api/v1/domains/supply/branches/work

# CLI
uv run lorebase branch list
uv run lorebase log work -n 3
```

### Read it back

Reads go through the REST API and need no model. Fetch one Record with its Lineage, list and
query with the JSON DSL (the query of the [design](design.md#82-query-model); `branch` picks where
to read), search and diff, on `work`:

```bash
# curl
curl -s 'http://localhost:8080/api/v1/domains/supply/kinds/Supplier/records/S-1001?branch=work&lineage=true'
curl -s -G 'http://localhost:8080/api/v1/domains/supply/kinds/Part/records' \
  --data-urlencode 'branch=work' --data-urlencode 'limit=5' \
  --data-urlencode 'where={"attr":"category","op":"eq","value":"bearing"}'
curl -s -X POST http://localhost:8080/api/v1/domains/supply/query \
  -H 'Content-Type: application/json' \
  -d '{"kind": "Part", "branch": "work", "aggregate": {"group_by": "category"}}'
curl -s -X POST http://localhost:8080/api/v1/domains/supply/search \
  -H 'Content-Type: application/json' -d '{"branch": "work", "q": "bearing", "kinds": ["Part"]}'
curl -s 'http://localhost:8080/api/v1/domains/supply/diff?from=main&to=work&kinds=Supplier&limit=3'

# CLI
uv run lorebase record get Supplier S-1001 -b work
uv run lorebase record list Part -b work -n 5 \
  --where '{"attr": "category", "op": "eq", "value": "bearing"}' --select category --select unit_cost
uv run lorebase query '{"kind": "Part", "aggregate": {"group_by": "category"}}' -b work
uv run lorebase search bearing --kind Part -b work
uv run lorebase diff main work --kind Supplier -n 3
```

```text
key      title                        category  unit_cost
P-10001  Tapered roller bearing 6305  bearing   10.79
P-10010  Needle roller bearing 6305   bearing   40.48
P-10013  Needle roller bearing NK25   bearing   25.62
P-10023  Needle roller bearing NK25   bearing   37.59
P-10029  Needle roller bearing 6001   bearing   45.94
```

The same API serves `traverse`, `neighborhood`, the branch `log`, `revisions/{id}`, context
packs and Record explanations. Every response carries the Revision it was served from in the
`Lorebase-Revision` header and an `ETag`. The full list is in the OpenAPI docs at
`http://localhost:8080/api/v1/docs` and in [openapi.json](openapi.json). If you want to look at
the materialized rows directly, query Postgres (the tables are under row-level security, so the
session names the Domain first):

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.yaml exec -T postgres \
  psql -U lorebase -d lorebase \
  -c "set lorebase.domain_ids = 'supply'" \
  -c "select branch, kind_id, count(*) from mat_records group by 1, 2 order by 2" \
  -c "select key, title from mat_records where key = 'S-1001'"
```

You should see 30 Suppliers, 40 Parts, 20 Plants, 25 Contracts, 50 PurchaseOrders and 20
RiskFlags on `work`, and `S-1001` titled `Kavsen Alloys Sp. z o.o.`.

### Propose the changes to `main`

`main` takes changes through a Proposal. The CLI covers the flow (`proposal create`, `list`,
`get`, `review`, `merge`, `close`, `conflicts` and `resolve`; each takes `--json`):

```bash
uv run lorebase proposal create work main --title "Load the supply sample"
uv run lorebase proposal list --status open
uv run lorebase proposal review <id> approve
uv run lorebase proposal merge <id>
```

The development config answers every request as the same anonymous administrator, and
`keeper.proposals.allow_self_approval` is `false`, so the approval has to come from another
principal; for a local trial set `allow_self_approval: true`, or create the Domain with
`"settings": {"required_approvals": 0}` as `make example-data` does. Open Conflicts or missing approvals
refuse the merge with exit code 2. `uv run lorebase context --ref Supplier/S-1001 -b work` and
`uv run lorebase explain Supplier S-1001 -b work` print what an agent gets from the context pack
and Record explanation endpoints ([agents.md](agents.md)).

### Derived values and custom Operations

`Part.supplier_count` and `Part.single_sourced` are computed in the materialized head on every
commit (never stored in the Revision tree) and read like any other value:

```bash
curl -s 'http://localhost:8080/api/v1/domains/supply/kinds/Part/records/P-10001?branch=work'
curl -s -X POST http://localhost:8080/api/v1/domains/supply/query \
  -H 'Content-Type: application/json' \
  -d '{"branch": "work", "kind": "Part",
       "where": {"attr": "single_sourced", "op": "eq", "value": true}, "limit": 5}'
```

The custom Operations of the schema run with their CEL preconditions, steps and review policy
(`keeper.operations.expressions_enabled`, on by default). Flag a supplier, try a terminated
one (`422 precondition-failed`, with the expression and its position) and approve a small draft
contract. A `critical` flag or a contract above 500000 (such as `C-3003`) answers
`409 review-required`: its review policy sends the edits to a Proposal.

```bash
curl -s -X POST http://localhost:8080/api/v1/domains/supply/operations/flag_supplier_risk/execute \
  -H 'Content-Type: application/json' \
  -d '{"branch": "work", "parameters": {"supplier": "S-1001", "severity": "high",
       "reason": "Two late deliveries in March"}}'
curl -s -X POST http://localhost:8080/api/v1/domains/supply/operations/flag_supplier_risk/execute \
  -H 'Content-Type: application/json' \
  -d '{"branch": "work", "parameters": {"supplier": "S-1019", "severity": "low", "reason": "x"}}'
curl -s -X POST http://localhost:8080/api/v1/domains/supply/operations/approve_contract/execute \
  -H 'Content-Type: application/json' \
  -d '{"branch": "work", "parameters": {"contract": "C-3015"}}'
```

## 12. Web app

The web app talks to a `KeeperApi` interface with two adapters. In `mock` mode it serves
its own sample Domains from memory and needs no backend; the `supply` Domain you created above
is not shown there. In `live` mode it talks to Keeper, and the flows of the app (Explorer, the
Canvas, the Modeler through a Proposal, Review with Conflicts, Settings) are tested against a
real Keeper on every change.

```bash
cd apps/web
pnpm install
cp .env.example .env     # VITE_API_MODE=mock
pnpm dev                 # http://localhost:5173, sample data
```

To see the Domain of the steps above instead, point the dev server at Keeper. The proxy makes
the browser talk to the app's own origin, so Keeper needs no CORS setting:

```bash
printf 'VITE_API_MODE=live\nVITE_KEEPER_BASE_URL=/\n' > .env.local
LOREBASE_KEEPER_PROXY=http://localhost:8080 pnpm dev
```

Pick the `supply` Domain and the `work` or `main` Branch in the top bar. `main` is protected, so
a schema change there goes through a Branch and a Proposal, and merging it needs an approval
(`keeper.proposals.allow_self_approval: true` for a trial with authentication off). See
[apps/web/README.md](../apps/web/README.md#live-mode) for the variables, the API token and the
limits against Keeper, and for the end-to-end suite (`pnpm e2e`).

## Next steps

- Plug in an agent: [examples/agent](../examples/agent/README.md) reads the `supply` Domain through Herald and proposes changes as Proposals, with Ollama or a hosted model.

## Coming next

The [roadmap](roadmap.md) tracks what is planned, milestone by milestone.
