#!/usr/bin/env bash
# The local stack of the quickstart (docs/quickstart.md), in a few commands.
#
#   quickstart.sh env     create .env from .env.example with generated database passwords and
#                         webhook key (an existing .env is left as it is; empty or `change-me`
#                         values in it are filled in) and HERALD_DOMAIN set to the sample Domain
#   quickstart.sh up      build the backend and web images from this checkout and start postgres,
#                         db-roles, migrate, keeper and web; returns when both are healthy and
#                         prints the URL of the web app and the export line for the CLI. With
#                         PROFILES=assistant it also builds the backend-ml image and starts
#                         Guide and Scribe (overlay deploy/compose/assistant.yaml); with
#                         PROFILES=mcp it also starts Herald, the MCP server, over Streamable
#                         HTTP. It stops before compose runs when a host port a chosen service
#                         publishes is taken, or when a *_BIND value would publish the Keeper of
#                         this stack, which runs with authentication off, beyond this machine
#   quickstart.sh shell-env
#                         print the export line for the CLI against the running stack:
#                         eval "$(scripts/quickstart.sh shell-env)"
#   quickstart.sh data    load the supply sample through the CLI: Domain, schema and data on
#                         branch `work`, then a Proposal merged into `main`. A Domain
#                         `supply` that already holds the sample is left alone.
#   quickstart.sh documents
#                         extract the example documents with the rules tier (needs Scribe, so
#                         PROFILES=assistant): the new contract C-3099 and the four documents of
#                         examples/supply/documents. Two documents that share a field are what
#                         schema suggestions need to be listed (schema.suggestions.min_support)
#   quickstart.sh logs [SERVICE...]
#                         follow the logs of the stack (every service, or the ones named)
#   quickstart.sh down    stop the stack, whatever profiles it started, keep the data
#   quickstart.sh clean   stop the stack and delete its volumes (data, pulled models)
#
# Settings (environment):
#   PROFILES              compose profiles `up` starts besides the default services; only
#                         `assistant` (Guide and Scribe) and `mcp` (Herald), several separated
#                         by spaces or commas
#   COMPOSE_OVERLAYS      extra compose files added after the others by every command that
#                         runs compose, separated by spaces, relative to the repository root
#                         (for example `deploy/compose/hosted-model.yaml`, or a file of your
#                         own under secrets/); read from the environment, else from the
#                         environment file
#   KEEPER_PUBLISH_INSECURE
#                         true: `up` starts although a *_BIND value publishes the Keeper of this
#                         stack, which runs with authentication off, beyond 127.0.0.1; read from
#                         the environment, else from the environment file
#   LOREBASE_ENV_FILE     environment file of compose            (<repo>/.env)
#   LOREBASE_URL          Keeper URL used by `data`               (http://127.0.0.1:KEEPER_HOST_PORT)
#   QUICKSTART_APPROVALS  required_approvals of that Domain       (0: the only principal of a
#                         stack with authentication off can merge its own Proposal)
#   QUICKSTART_DOCS_TIMEOUT  seconds `documents` waits for the extraction jobs  (180)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${LOREBASE_ENV_FILE:-$ROOT/.env}"
COMPOSE_FILE="$ROOT/deploy/compose/docker-compose.yaml"
DATA="$ROOT/examples/supply/data"
OVERLAY_DIR="$ROOT/deploy/compose"
SAMPLE_DOMAIN=supply # the Domain id named in examples/supply/schema.yaml
# Extraction input of `documents`: the one contract the sample lacks, then the sample documents.
DOCUMENTS=("$ROOT/examples/five_day_demo/documents/contract-c-3099-wexrix-filtration.md"
  "$ROOT"/examples/supply/documents/*.md)
UV="${UV:-uv}"
cd "$ROOT"

say() { printf '[quickstart] %s\n' "$*" >&2; }

# Profiles `up` can start, as a list of words; anything else is refused before any command runs.
PROFILE_WORDS="$(printf '%s' "${PROFILES:-}" | tr ',' ' ')"
for word in $PROFILE_WORDS; do
  case "$word" in
    assistant | mcp) ;;
    *)
      say "PROFILES=$word is not supported here (only: assistant, mcp); start other profiles with docker compose"
      exit 2
      ;;
  esac
done

has_profile() {
  case " $PROFILE_WORDS " in
    *" $1 "*) return 0 ;;
    *) return 1 ;;
  esac
}

# Value of NAME in the environment file, empty when unset.
env_value() {
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1
}

# Value of NAME in the environment file, or DEFAULT when it is empty.
value_or() {
  local value
  value="$(env_value "$1")"
  printf '%s' "${value:-$2}"
}

# Value of NAME as compose resolves it, or DEFAULT when it is empty: a variable set in the
# environment wins over the environment file (docker compose gives the shell precedence over
# --env-file), else the environment file.
setting() {
  local value
  if ! value="$(printenv "$1")"; then value="$(env_value "$1")"; fi
  printf '%s' "${value:-$2}"
}

# Extra compose files of COMPOSE_OVERLAYS (environment first, then the environment file).
overlays() {
  local value="${COMPOSE_OVERLAYS:-}"
  if [ -z "$value" ] && [ -f "$ENV_FILE" ]; then value="$(env_value COMPOSE_OVERLAYS)"; fi
  printf '%s' "$value" | tr -d "\"'"
}

compose() {
  local files=(-f "$COMPOSE_FILE") overlay
  if has_profile assistant; then files+=(-f "$OVERLAY_DIR/assistant.yaml" --profile assistant); fi
  if has_profile mcp; then files+=(--profile mcp); fi
  for overlay in $(overlays); do
    if [ ! -f "$overlay" ]; then
      say "COMPOSE_OVERLAYS names $overlay, which does not exist (paths start from $ROOT)"
      exit 2
    fi
    files+=(-f "$overlay")
  done
  docker compose --env-file "$ENV_FILE" "${files[@]}" "$@"
}

# Set NAME=VALUE in the environment file: in place of the line that sets NAME, else in place of
# a commented-out `# NAME=...` line (the value lands where .env.example documents it), else
# appended.
set_env_value() {
  local tmp
  tmp="$(mktemp "$ENV_FILE.XXXXXX")"
  if grep -q "^$1=" "$ENV_FILE"; then
    awk -v name="$1" -v value="$2" \
      'index($0, name "=") == 1 { print name "=" value; next } { print }' "$ENV_FILE" >"$tmp"
  elif grep -q "^# $1=" "$ENV_FILE"; then
    awk -v name="$1" -v value="$2" \
      'index($0, "# " name "=") == 1 { print name "=" value; next } { print }' "$ENV_FILE" >"$tmp"
  else
    cat "$ENV_FILE" >"$tmp"
    printf '%s=%s\n' "$1" "$2" >>"$tmp"
  fi
  chmod 600 "$tmp"
  mv "$tmp" "$ENV_FILE"
}

make_env() {
  if [ ! -f "$ENV_FILE" ]; then
    (umask 077 && cp "$ROOT/.env.example" "$ENV_FILE")
    say "created $ENV_FILE from .env.example"
  fi
  local entry name value
  # NAME:random bytes (hex encoded); the webhook key needs at least 32 characters.
  for entry in POSTGRES_PASSWORD:24 LOREBASE_APP_PASSWORD:24 LOREBASE_WEBHOOK_KEY:32; do
    name="${entry%%:*}"
    value="$(env_value "$name")"
    if [ -z "$value" ] || [ "$value" = "change-me" ]; then
      set_env_value "$name" "$(openssl rand -hex "${entry#*:}")"
      say "generated $name"
    fi
  done
  # The first MCP call of `make up PROFILES=mcp` then needs no Domain of its own.
  if [ -z "$(env_value HERALD_DOMAIN)" ]; then
    set_env_value HERALD_DOMAIN "$SAMPLE_DOMAIN"
    say "set HERALD_DOMAIN=$SAMPLE_DOMAIN: MCP calls that name no Domain use the sample"
  fi
}

require_env() {
  if [ ! -f "$ENV_FILE" ]; then
    say "no $ENV_FILE: run \`make env\` (or scripts/quickstart.sh env) first"
    exit 1
  fi
}

# Whether a bind address only reaches this machine (where compose publishes by default).
is_loopback() {
  case "$1" in
    127.* | localhost | ::1 | '[::1]') return 0 ;;
    *) return 1 ;;
  esac
}

# Whether a boolean value of the environment file is true (the spellings pydantic accepts).
is_true() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
    true | 1 | yes | y | t | on) return 0 ;;
    *) return 1 ;;
  esac
}

# Keeper with the example configuration takes every request as an administrator (auth mode
# none): the stack is safe only while its ports stay on this machine. Stop when a bind address
# would publish it elsewhere, unless KEEPER_PUBLISH_INSECURE=true says so.
check_exposure() {
  local mode vars var bind exposed=""
  mode="$(setting LOREBASE__KEEPER__AUTH__MODE "")"
  if [ -n "$mode" ] && [ "$mode" != none ]; then return 0; fi
  if ! is_true "$(setting KEEPER_ALLOW_INSECURE true)"; then return 0; fi
  vars="KEEPER_BIND WEB_BIND"
  if has_profile assistant; then vars="$vars GUIDE_BIND"; fi
  if has_profile mcp; then vars="$vars HERALD_BIND"; fi
  for var in $vars; do
    bind="$(setting "$var" 127.0.0.1)"
    if ! is_loopback "$bind"; then exposed="$exposed $var=$bind"; fi
  done
  if [ -z "$exposed" ]; then return 0; fi
  if is_true "$(setting KEEPER_PUBLISH_INSECURE false)"; then
    say "warning: Keeper takes every request as an administrator (auth mode none) and$exposed publishes it beyond this machine (KEEPER_PUBLISH_INSECURE=true)"
    return 0
  fi
  say "refusing to start:$exposed publishes Keeper beyond this machine while it runs with authentication off"
  say "(auth mode none, KEEPER_ALLOW_INSECURE=true): anyone who reaches that address is an administrator of every Domain."
  say "Keep the *_BIND values on 127.0.0.1, or turn authentication on first (KEEPER_ALLOW_INSECURE=false and an auth"
  say "mode; docs/manual-setup.md). To publish it this way anyway, set KEEPER_PUBLISH_INSECURE=true."
  exit 1
}

# Host ports the chosen profiles publish: SERVICE:BIND_VARIABLE:PORT_VARIABLE:DEFAULT_PORT.
published_ports() {
  printf '%s\n' postgres:POSTGRES_BIND:POSTGRES_HOST_PORT:5432 \
    keeper:KEEPER_BIND:KEEPER_HOST_PORT:8080 web:WEB_BIND:LOREBASE_WEB_PORT:8090
  if has_profile assistant; then printf '%s\n' guide:GUIDE_BIND:GUIDE_HOST_PORT:8095; fi
  if has_profile mcp; then printf '%s\n' herald:HERALD_BIND:HERALD_HOST_PORT:8091; fi
}

# Whether something answers on BIND:PORT: a connection attempt to the bind address itself, so
# a listener on another address of this machine is no conflict.
port_in_use() {
  (exec 3<>"/dev/tcp/$1/$2") 2>/dev/null
}

# Stop before compose runs when a port a chosen service publishes is taken, naming the variable
# to change (compose would stop half way through with the raw bind error). Ports held by this
# project's own running services are fine: compose recreates those in place.
check_ports() {
  local running entry service bind_var port_var default bind port
  running="$(compose ps --services --status running 2>/dev/null || true)"
  for entry in $(published_ports); do
    IFS=: read -r service bind_var port_var default <<<"$entry"
    if printf '%s\n' "$running" | grep -qx "$service"; then continue; fi
    bind="$(setting "$bind_var" 127.0.0.1)"
    port="$(setting "$port_var" "$default")"
    if port_in_use "$bind" "$port"; then
      say "port $port on $bind ($port_var) is already in use; set $port_var in $ENV_FILE to a free port"
      exit 1
    fi
  done
}

up() {
  require_env
  local args=(postgres migrate keeper web)
  if has_profile assistant; then args+=(guide scribe); fi
  if has_profile mcp; then args+=(herald); fi
  check_exposure
  check_ports
  say "building the images and starting project $(env_value COMPOSE_PROJECT_NAME)"
  if [ -n "$(overlays)" ]; then say "with the compose overlays: $(overlays)"; fi
  compose up -d --build --wait "${args[@]}" >&2
  say "Keeper is ready at $(keeper_url)"
  say "Web app: $(web_url)"
  say "for the CLI, paste: $(shell_exports)   (or: eval \"\$(make shell-env)\")"
  say "for an MCP host over stdio: \"env\": {\"LOREBASE_URL\": \"$(keeper_url)\", \"LOREBASE_DOMAIN\": \"$SAMPLE_DOMAIN\"}"
  if has_profile assistant; then
    say "Guide: $(web_url)/guide/ (POST /embed), Scribe is running; try \`make example-documents\`"
  fi
  if has_profile mcp; then
    say "Herald (MCP, Streamable HTTP): http://127.0.0.1:$(value_or HERALD_HOST_PORT 8091)/mcp"
  fi
}

web_url() {
  local port bind
  port="$(env_value LOREBASE_WEB_PORT)"
  bind="$(env_value WEB_BIND)"
  printf 'http://%s:%s' "${bind:-127.0.0.1}" "${port:-8090}"
}

keeper_url() {
  if [ -n "${LOREBASE_URL:-}" ]; then
    printf '%s' "$LOREBASE_URL"
  else
    local port
    port="$(env_value KEEPER_HOST_PORT)"
    printf 'http://127.0.0.1:%s' "${port:-8080}"
  fi
}

# The line that points the CLI at the running stack and its sample Domain.
shell_exports() {
  printf 'export LOREBASE_URL=%s LOREBASE_DOMAIN=%s' "$(keeper_url)" "$SAMPLE_DOMAIN"
}

load_data() {
  local url domain approvals status proposal
  url="$(keeper_url)"
  domain="$SAMPLE_DOMAIN"
  approvals="${QUICKSTART_APPROVALS:-0}"
  export LOREBASE_URL="$url" LOREBASE_DOMAIN="$domain"
  status="$(curl -s -o /dev/null -w '%{http_code}' \
    ${LOREBASE_API_KEY:+-H "Authorization: Bearer $LOREBASE_API_KEY"} \
    "$url/api/v1/domains/$domain" || true)"
  case "$status" in
    200)
      if curl -sf -o /dev/null ${LOREBASE_API_KEY:+-H "Authorization: Bearer $LOREBASE_API_KEY"} \
        "$url/api/v1/domains/$domain/kinds/Supplier/records/S-1001?branch=main"; then
        say "Domain $domain at $url already holds the sample on main; nothing loaded"
        return 0
      fi
      say "Domain $domain exists at $url but main does not hold the sample (an earlier load stopped"
      say "half way?). Delete it with \`uv run lorebase domain delete $domain\` and run this again."
      exit 1
      ;;
    404) ;;
    000)
      say "no Keeper at $url: run \`make up\` first, or set LOREBASE_URL"
      exit 1
      ;;
    *)
      say "GET $url/api/v1/domains/$domain answered $status (with authentication on, set LOREBASE_API_KEY)"
      exit 1
      ;;
  esac
  say "creating Domain $domain (required_approvals $approvals)"
  curl -sf -X POST "$url/api/v1/domains" -H 'Content-Type: application/json' \
    ${LOREBASE_API_KEY:+-H "Authorization: Bearer $LOREBASE_API_KEY"} \
    -d "{\"id\": \"$domain\", \"display_name\": \"Supply chain\", \"settings\": {\"required_approvals\": $approvals}}" \
    >/dev/null
  "$UV" run lorebase branch create work >/dev/null
  say "applying the supply schema to branch work"
  "$UV" run lorebase schema apply "$ROOT/examples/supply/schema.yaml" -b work \
    -m "Apply supply schema" >/dev/null
  say "importing the sample data"
  "$UV" run lorebase import batch -b work \
    --records "Supplier=$DATA/supplier.csv" --records "Part=$DATA/part.csv" \
    --records "Plant=$DATA/plant.csv" --records "Contract=$DATA/contract.csv" \
    --records "PurchaseOrder=$DATA/purchase_order.csv" --records "RiskFlag=$DATA/risk_flag.csv" \
    --links "supplied_by=$DATA/supplied_by.csv" --links "stocked_at=$DATA/stocked_at.csv" \
    --links "with_supplier=$DATA/with_supplier.csv" --links "for_part=$DATA/for_part.csv" \
    --links "delivered_to=$DATA/delivered_to.csv" --links "flags=$DATA/flags.csv" >/dev/null
  proposal="$("$UV" run lorebase proposal create work main --title "Load the supply sample" --json |
    "$UV" run python -c 'import json, sys; print(json.load(sys.stdin)["id"])')"
  say "merging Proposal $proposal into main"
  "$UV" run lorebase proposal merge "$proposal" >/dev/null
  say "loaded the supply sample into Domain $domain (branches main and work)"
}

# Number of documents of the Domain whose extraction has no finished job yet (queued or running).
pending_jobs() {
  "$UV" run python - "$LOREBASE_URL" "$LOREBASE_DOMAIN" <<'PY'
import json
import os
import sys
import urllib.request

base = f"{sys.argv[1]}/api/v1/domains/{sys.argv[2]}/documents"
key = os.environ.get("LOREBASE_API_KEY")


def get(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"} if key else {})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


pending = 0
for document in get(f"{base}?limit=200")["items"]:
    jobs = get(f"{base}/{document['id']}")["extraction_jobs"]
    if not jobs or any(job["status"] in ("queued", "running") for job in jobs):
        pending += 1
print(pending)
PY
}

# Upload the example documents with the rules tier and wait for their jobs, then list what
# extraction suggests for the schema.
extract_documents() {
  require_env
  export LOREBASE_URL="$(keeper_url)" LOREBASE_DOMAIN="$SAMPLE_DOMAIN"
  local file waited=0 timeout="${QUICKSTART_DOCS_TIMEOUT:-180}" pending
  for file in "${DOCUMENTS[@]}"; do
    say "uploading $(basename "$file")"
    "$UV" run lorebase documents upload "$file" --tiers 0 >/dev/null
  done
  say "waiting for Scribe to extract ${#DOCUMENTS[@]} documents (needs \`make up PROFILES=assistant\`)"
  until pending="$(pending_jobs)" && [ "$pending" = 0 ]; do
    waited=$((waited + 2))
    if [ "$waited" -gt "$timeout" ]; then
      say "$pending extraction jobs are not finished after ${timeout}s: is Scribe running?"
      exit 1
    fi
    sleep 2
  done
  "$UV" run lorebase schema suggestions list
}

case "${1:-}" in
  env) make_env ;;
  up) up ;;
  shell-env)
    require_env
    printf '%s\n' "$(shell_exports)"
    ;;
  logs)
    require_env
    shift
    compose --profile "*" logs --follow --tail "${LOGS_TAIL:-200}" "$@"
    ;;
  data) load_data ;;
  documents) extract_documents ;;
  down)
    require_env
    # Every profile `up` can start: a container of one that is no longer asked for would stay.
    compose --profile "*" down --remove-orphans
    ;;
  clean)
    require_env
    compose --profile "*" down -v --remove-orphans
    ;;
  *)
    sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//' >&2
    exit 2
    ;;
esac
