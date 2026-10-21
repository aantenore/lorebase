#!/usr/bin/env bash
# Brings the compose stack of the five-day demo up and down.
#
#   stack.sh up        build the image, start postgres, migrate, keeper and scribe, create the
#                      admin key (idempotent: a running stack is left alone)
#   stack.sh down      stop the stack and delete its data
#   stack.sh env       print the variables run.sh needs (eval "$(stack.sh env)")
#   stack.sh demo      up, run.sh, then down; KEEP=1 leaves the stack running
#   stack.sh compose ARGS...   run `docker compose` for this stack
#
# Settings (environment):
#   DEMO_PROJECT        compose project name                (lorebase-demo)
#   DEMO_KEEPER_PORT    host port of Keeper                 (18080)
#   DEMO_POSTGRES_PORT  host port of Postgres               (15432)
#   DEMO_STATE_DIR      generated files, secrets, CSV copy  (<repo>/build/demo)
#   DEMO_UP_TIMEOUT     seconds to wait for Keeper          (240)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$ROOT/examples/five_day_demo"
PROJECT="${DEMO_PROJECT:-lorebase-demo}"
KEEPER_PORT="${DEMO_KEEPER_PORT:-18080}"
POSTGRES_PORT="${DEMO_POSTGRES_PORT:-15432}"
STATE="${DEMO_STATE_DIR:-$ROOT/build/demo}"
TIMEOUT="${DEMO_UP_TIMEOUT:-240}"

say() { printf '[stack] %s\n' "$*" >&2; }

compose() {
  docker compose -p "$PROJECT" --env-file "$STATE/stack.env" \
    -f "$ROOT/deploy/compose/docker-compose.yaml" -f "$HERE/compose.demo.yaml" "$@"
}

# A random value kept across runs, so a stack that survives (KEEP=1) keeps its credentials.
secret() {
  local file="$STATE/$1"
  if [ ! -s "$file" ]; then
    (umask 077 && openssl rand -hex 24 >"$file")
  fi
  cat "$file"
}

write_env() {
  mkdir -p "$STATE/sources"
  chmod 700 "$STATE"
  cp "$ROOT"/examples/supply/data/*.csv "$STATE/sources/"
  (
    umask 077
    cat >"$STATE/stack.env" <<EOF
LOREBASE_IMAGE=lorebase-demo
LOREBASE_TAG=local
POSTGRES_PASSWORD=$(secret postgres.password)
POSTGRES_HOST_PORT=$POSTGRES_PORT
KEEPER_HOST_PORT=$KEEPER_PORT
LOREBASE_CONFIG_FILE=$HERE/lorebase.demo.yaml
SCRIBE_SOURCES_DIR=$STATE/sources
DEMO_API_KEY_PEPPER=$(secret pepper)
EOF
  )
}

keeper_ready() {
  curl -fsS -o /dev/null "http://127.0.0.1:$KEEPER_PORT/readyz" 2>/dev/null
}

admin_key_works() {
  [ -s "$STATE/admin.key" ] &&
    curl -fsS -o /dev/null -H "Authorization: Bearer $(cat "$STATE/admin.key")" \
      "http://127.0.0.1:$KEEPER_PORT/api/v1/auth/whoami" 2>/dev/null
}

up() {
  mkdir -p "$STATE"
  write_env
  say "starting project $PROJECT (Keeper on 127.0.0.1:$KEEPER_PORT)"
  compose up -d --build postgres migrate keeper scribe >&2
  local waited=0
  until keeper_ready; do
    waited=$((waited + 2))
    if [ "$waited" -gt "$TIMEOUT" ]; then
      compose logs --tail 40 keeper >&2 || true
      say "Keeper is not ready after ${TIMEOUT}s"
      exit 1
    fi
    sleep 2
  done
  if ! admin_key_works; then
    say "creating the admin key"
    compose exec -T keeper python - <"$HERE/bootstrap_admin.py" | tail -n 1 >"$STATE/admin.key.tmp"
    (umask 077 && mv "$STATE/admin.key.tmp" "$STATE/admin.key")
  fi
  say "ready"
}

down() {
  if [ -s "$STATE/stack.env" ]; then
    say "stopping project $PROJECT and deleting its data"
    compose down -v --remove-orphans >&2
    rm -f "$STATE/admin.key" "$STATE/postgres.password" "$STATE/pepper"
  else
    say "nothing to stop (no $STATE/stack.env)"
  fi
}

print_env() {
  cat <<EOF
export LOREBASE_URL=http://127.0.0.1:$KEEPER_PORT
export LOREBASE_API_KEY=$(cat "$STATE/admin.key")
export DEMO_STORE_EXEC='docker compose -p $PROJECT --env-file $STATE/stack.env -f $ROOT/deploy/compose/docker-compose.yaml -f $HERE/compose.demo.yaml exec -T keeper'
export DEMO_POSTGRES_EXEC='docker compose -p $PROJECT --env-file $STATE/stack.env -f $ROOT/deploy/compose/docker-compose.yaml -f $HERE/compose.demo.yaml exec -T postgres'
export DEMO_SOURCES_DIR='$STATE/sources'
export DEMO_WORK_DIR='$STATE/work'
export DEMO_RECEIVER_BIND=$(receiver_bind)
EOF
}

# The receiver of day 4 listens where the Scribe container can reach it: Docker Desktop
# forwards host.docker.internal to the loopback interface, Linux needs the bridge gateway.
receiver_bind() {
  if [ "$(uname)" = Darwin ]; then echo 127.0.0.1; else echo 0.0.0.0; fi
}

demo() {
  local started=$SECONDS up_seconds status=0
  up
  up_seconds=$((SECONDS - started))
  (
    eval "$(print_env)"
    "$HERE/run.sh"
  ) || status=$?
  say "stack up in ${up_seconds}s, five days in $((SECONDS - started - up_seconds))s (exit code $status)"
  if [ -n "${KEEP:-}" ] && [ "$KEEP" != "0" ]; then
    say "KEEP is set: the stack stays up (project $PROJECT, Keeper http://127.0.0.1:$KEEPER_PORT)"
    say "run.sh again, or stop it with: make demo-down"
  else
    down
  fi
  exit "$status"
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  env) print_env ;;
  demo) demo ;;
  compose) shift; compose "$@" ;;
  *) echo "usage: $0 up|down|env|demo|compose ARGS..." >&2; exit 1 ;;
esac
