#!/usr/bin/env bash
# The local stack of the quickstart (docs/quickstart.md), in a few commands.
#
#   quickstart.sh env     create .env from .env.example with generated database passwords
#                         (an existing .env is left as it is; empty or `change-me` passwords in
#                         it are filled in)
#   quickstart.sh up      build the backend and web images from this checkout and start postgres,
#                         db-roles, migrate, keeper and web; returns when both are healthy and
#                         prints the URL of the web app
#   quickstart.sh data    load the supply sample through the CLI: Domain, schema and data on
#                         branch `work`, then a Proposal merged into `main`. A Domain
#                         `supply` that already holds the sample is left alone.
#   quickstart.sh down    stop the stack, keep the data
#   quickstart.sh clean   stop the stack and delete its volumes (data, pulled models)
#
# Settings (environment):
#   LOREBASE_ENV_FILE     environment file of compose            (<repo>/.env)
#   LOREBASE_URL          Keeper URL used by `data`               (http://127.0.0.1:KEEPER_HOST_PORT)
#   QUICKSTART_APPROVALS  required_approvals of that Domain       (0: the only principal of a
#                         stack with authentication off can merge its own Proposal)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${LOREBASE_ENV_FILE:-$ROOT/.env}"
COMPOSE_FILE="$ROOT/deploy/compose/docker-compose.yaml"
DATA="$ROOT/examples/supply/data"
UV="${UV:-uv}"
cd "$ROOT"

say() { printf '[quickstart] %s\n' "$*" >&2; }

compose() {
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

# Value of NAME in the environment file, empty when unset.
env_value() {
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1
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
  local name value
  for name in POSTGRES_PASSWORD LOREBASE_APP_PASSWORD; do
    value="$(env_value "$name")"
    if [ -z "$value" ] || [ "$value" = "change-me" ]; then
      set_env_value "$name" "$(openssl rand -hex 24)"
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
  say "building the images and starting project $(env_value COMPOSE_PROJECT_NAME)"
  compose up -d --build --wait postgres migrate keeper web >&2
  say "Keeper is ready at $(keeper_url)"
  say "Web app: $(web_url)"
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

case "${1:-}" in
  env) make_env ;;
  up) up ;;
  data) load_data ;;
  down)
    require_env
    compose down
    ;;
  clean)
    require_env
    compose --profile "*" down -v --remove-orphans
    ;;
  *)
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
    ;;
esac
