#!/usr/bin/env bash
# The local stack of the quickstart (docs/quickstart.md), in a few commands.
#
#   quickstart.sh env     create .env from .env.example with generated database passwords and
#                         webhook key (an existing .env is left as it is; empty or `change-me`
#                         values in it are filled in)
#   quickstart.sh up      build the backend and web images from this checkout and start postgres,
#                         db-roles, migrate, keeper and web; returns when both are healthy and
#                         prints the URL of the web app. With PROFILES=assistant it also builds
#                         the backend-ml image and starts Guide and Scribe (overlay
#                         deploy/compose/assistant.yaml); with PROFILES=mcp it also starts
#                         Herald, the MCP server, over Streamable HTTP
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

# Replace (or append) NAME=VALUE in the environment file.
set_env_value() {
  local tmp
  tmp="$(mktemp "$ENV_FILE.XXXXXX")"
  if grep -q "^$1=" "$ENV_FILE"; then
    awk -v name="$1" -v value="$2" \
      'index($0, name "=") == 1 { print name "=" value; next } { print }' "$ENV_FILE" >"$tmp"
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
}

require_env() {
  if [ ! -f "$ENV_FILE" ]; then
    say "no $ENV_FILE: run \`make env\` (or scripts/quickstart.sh env) first"
    exit 1
  fi
}

up() {
  require_env
  local args=(postgres migrate keeper web)
  if has_profile assistant; then args+=(guide scribe); fi
  if has_profile mcp; then args+=(herald); fi
  say "building the images and starting project $(env_value COMPOSE_PROJECT_NAME)"
  if [ -n "$(overlays)" ]; then say "with the compose overlays: $(overlays)"; fi
  compose up -d --build --wait "${args[@]}" >&2
  say "Keeper is ready at $(keeper_url)"
  say "Web app: $(web_url)"
  if has_profile assistant; then
    say "Guide: $(web_url)/guide/ (POST /embed), Scribe is running; try \`make example-documents\`"
  fi
  if has_profile mcp; then
    say "Herald (MCP, Streamable HTTP): http://127.0.0.1:$(value_or HERALD_HOST_PORT 8091)/mcp"
  fi
}

# Value of NAME in the environment file, or DEFAULT when it is empty.
value_or() {
  local value
  value="$(env_value "$1")"
  printf '%s' "${value:-$2}"
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

load_data() {
  local url domain approvals status proposal
  url="$(keeper_url)"
  domain=supply # the id named in examples/supply/schema.yaml
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
  export LOREBASE_URL="$(keeper_url)" LOREBASE_DOMAIN=supply
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
