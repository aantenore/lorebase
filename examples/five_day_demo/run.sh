#!/usr/bin/env bash
# The Lorebase 5-day proof (docs/design.md 3.2) as a script: five days, the `lorebase` command
# line and a few helpers (demo.py), against a running stack. Idempotent: it deletes the demo
# Domain left by an earlier run (only a Domain this script created) and starts over.
#
#   examples/five_day_demo/run.sh            # against the stack of `make demo`, or any Keeper
#
# Every day ends with assertions (demo.py assert-*): the run exits with a non-zero code at the
# first one that fails, and ends with a table of the seconds each day took.
#
# Environment (all optional):
#   LOREBASE_URL         Keeper URL                              (http://127.0.0.1:8080)
#   LOREBASE_API_KEY     administrator key; none: Keeper must run with keeper.auth.mode none
#   LOREBASE_DOMAIN      Domain to create                        (supply)
#   DEMO_RESET           1: delete the Domain of an earlier run  (1)
#   DEMO_WORK_DIR        scratch files and logs                  (<repo>/build/demo/work)
#   DEMO_SOURCES_DIR     directory Scribe reads CSV Sources from (unset: the Source step is skipped)
#   DEMO_STORE_EXEC      command prefix that runs a command in the backend container, such as
#                        `docker compose ... exec -T keeper` (unset: bundle and GC are skipped)
#   DEMO_POSTGRES_EXEC   the same for the postgres container (creates the restore database)
#   DEMO_RECEIVER_PORT   port of the webhook receiver            (19090)
#   DEMO_RECEIVER_BIND   address it binds                        (127.0.0.1; 0.0.0.0 on Linux)
#   DEMO_WEBHOOK_HOST    how Scribe reaches the receiver         (host.docker.internal)
#   DEMO_WIDTH           longest output line shown               (130)
#   LOREBASE_CLI         the command line                        (uv run --quiet lorebase)
#   DEMO_PYTHON          Python of demo.py                       (uv run --quiet python)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
cd "$ROOT" # paths in the transcript are relative to the repository root

export LOREBASE_URL="${LOREBASE_URL:-http://127.0.0.1:8080}"
export LOREBASE_DOMAIN="${LOREBASE_DOMAIN:-supply}"
DOMAIN="$LOREBASE_DOMAIN"
if [ -z "${LOREBASE_API_KEY:-}" ]; then unset LOREBASE_API_KEY; fi
DEMO_RESET="${DEMO_RESET:-1}"
WORK="${DEMO_WORK_DIR:-$ROOT/build/demo/work}"
SOURCES_DIR="${DEMO_SOURCES_DIR:-}"
STORE_EXEC="${DEMO_STORE_EXEC:-}"
POSTGRES_EXEC="${DEMO_POSTGRES_EXEC:-}"
RECEIVER_PORT="${DEMO_RECEIVER_PORT:-19090}"
RECEIVER_BIND="${DEMO_RECEIVER_BIND:-127.0.0.1}"
WEBHOOK_HOST="${DEMO_WEBHOOK_HOST:-host.docker.internal}"
WIDTH="${DEMO_WIDTH:-130}"
DEMO_NAME="Lorebase five-day demo" # marks the Domains this script may delete
DATA="examples/supply/data"
RESTORE_DB="lorebase_restore"

read -r -a LB <<<"${LOREBASE_CLI:-uv run --quiet lorebase}"
read -r -a PY <<<"${DEMO_PYTHON:-uv run --quiet python}"
PY+=("$HERE/demo.py")

mkdir -p "$WORK"
TIMINGS="$WORK/timings.tsv" # one line per finished day: label, seconds
: >"$TIMINGS"
: >"$WORK/cli.toml"
export LOREBASE_CONFIG="$WORK/cli.toml" # an empty config file: no profile of the user applies
unset LOREBASE_PROFILE LOREBASE_BRANCH LOREBASE_TOKEN

exec 3>&1 # commands are echoed here, so that $(...) captures stay clean
if [ -t 1 ]; then
  BOLD=$'\033[1m' DIM=$'\033[2m' RESET=$'\033[0m'
else
  BOLD='' DIM='' RESET=''
fi
RECEIVER_PID=""
STARTED=$SECONDS
DAY_STARTED=$SECONDS
CURRENT="preparation"

cleanup() {
  local status=$?
  if [ -n "$RECEIVER_PID" ]; then kill "$RECEIVER_PID" 2>/dev/null || true; fi
  "${PY[@]}" timings "$TIMINGS" --status "$status" --during "$CURRENT" || true
}
trap cleanup EXIT

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

shell_quote() {
  local word out=""
  for word in "$@"; do
    if [[ "$word" =~ ^[A-Za-z0-9_./:=@,+%-]+$ ]]; then out+="$word "; else out+="'${word}' "; fi
  done
  printf '%s' "${out% }"
}

show() {
  local text
  text="$(shell_quote "$@")"
  printf '%s$ %s%s\n' "$DIM" "${text//"$ROOT"\//}" "$RESET" >&3
}
note() { printf '\n%s# %s%s\n' "$DIM" "$*" "$RESET"; }
banner() {
  printf '\n%s== %s ==%s\n' "$BOLD" "$*" "$RESET"
  CURRENT="$*"
  DAY_STARTED=$SECONDS
}
day_done() {
  printf '\n%s(%s took %ss)%s\n' "$DIM" "$1" "$((SECONDS - DAY_STARTED))" "$RESET"
  printf '%s\t%s\n' "$1" "$((SECONDS - DAY_STARTED))" >>"$TIMINGS"
}

lb() {
  show lorebase "$@"
  "${LB[@]}" "$@"
}
# Like lb, with long lines cut.
lbw() {
  show lorebase "$@"
  "${LB[@]}" "$@" | cut -c1-"$WIDTH"
}
# Like lbw, and only the first N lines.
lbn() {
  local lines="$1"
  shift
  show lorebase "$@"
  "${LB[@]}" "$@" | awk -v n="$lines" -v w="$WIDTH" \
    'NR <= n { print substr($0, 1, w) } END { if (NR > n) printf "... %d more lines\n", NR - n }'
}
# Like lb, for captures: the output is returned, the command is still shown.
lbc() {
  show lorebase "$@"
  "${LB[@]}" "$@"
}
py() {
  show demo.py "$@"
  "${PY[@]}" "$@"
}
# Run a command that is expected to fail with exit code 2 and say so.
lb_refused() {
  show lorebase "$@"
  local status=0
  "${LB[@]}" "$@" || status=$?
  if [ "$status" -eq 0 ]; then die "this command should have been refused"; fi
  printf '%s(refused, exit code %s)%s\n' "$DIM" "$status" "$RESET"
}

store() { # run a command in the backend container
  show docker compose exec keeper "$@" # the compose project and files are noise in a transcript
  # shellcheck disable=SC2086 # the prefix is a command line
  $STORE_EXEC "$@"
}

# The restore database lives in the same Postgres server as the live one. The container sees the
# owner role's connection string in LOREBASE_DB_MAINTENANCE_DSN (the services connect as the
# application role, which may not create tables); the restore commands swap the database name in
# it.
create_empty_database() {
  show psql -c "CREATE DATABASE $RESTORE_DB" "(in the postgres container)"
  # shellcheck disable=SC2016 # expanded inside the container
  $POSTGRES_EXEC sh -c 'psql -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "DROP DATABASE IF EXISTS '"$RESTORE_DB"'" -c "CREATE DATABASE '"$RESTORE_DB"'"'
  show alembic upgrade head "(on $RESTORE_DB)"
  # shellcheck disable=SC2016
  $STORE_EXEC sh -c 'cd /opt/lorebase/store && LOREBASE_DB_DSN="${LOREBASE_DB_MAINTENANCE_DSN%/*}/'"$RESTORE_DB"'" alembic upgrade head' 2>&1 | tail -n 1
}

restore_to_second_database() {
  show docker compose exec keeper python -m lorebase_store.bundle import --in /tmp/demo-bundle.tar "(on $RESTORE_DB)"
  # shellcheck disable=SC2016
  $STORE_EXEC sh -c 'LOREBASE_DB_DSN="${LOREBASE_DB_MAINTENANCE_DSN%/*}/'"$RESTORE_DB"'" python -m lorebase_store.bundle import --in /tmp/demo-bundle.tar'
}

db_summary() { # DATABASE ('$POSTGRES_DB' for the live one): head of main, Revisions, Records
  local sql="set lorebase.domain_ids = '$DOMAIN'; select encode(head, 'hex'), (select count(*) from revisions where domain_id = '$DOMAIN'), (select count(*) from mat_records where branch = 'main') from refs where domain_id = '$DOMAIN' and name = 'main'"
  $POSTGRES_EXEC sh -c "psql -q -t -A -F ' ' -U \"\$POSTGRES_USER\" -d $1 -c \"$sql\"" | grep -v '^$'
}

compare_heads() {
  note "Same head of main and same Records. The live database counts one Revision more: the one of the deleted scratch branch, which no branch reaches and the bundle leaves out (a restore compacts)."
  show psql -c "select head of main, Revisions, Records of main" "(on the live database and on $RESTORE_DB)"
  # shellcheck disable=SC2016 # the container's shell expands $POSTGRES_DB
  printf 'live      %s\n' "$(db_summary '$POSTGRES_DB')"
  printf 'restored  %s\n' "$(db_summary "$RESTORE_DB")"
  py assert-restore "$(db_summary '$POSTGRES_DB')" "$(db_summary "$RESTORE_DB")"
}

head_of() { # the head Revision of a branch
  "${LB[@]}" --json branch get "$1" | "${PY[@]}" jget head
}

new_proposal() { # SOURCE TARGET TITLE -> id
  local id
  id="$(lbc --json proposal create "$1" "$2" --title "$3" | "${PY[@]}" jget id)"
  printf '%s' "$id"
}

# The Proposal Scribe opens for an uploaded document.
extraction_proposal() { # FILE NAME -> id
  show demo.py extraction-proposal "$1"
  "${PY[@]}" extraction-proposal "$1"
}

# The one open Proposal the agent's key raised.
agent_proposal() { # -> id
  show demo.py agent-proposal
  "${PY[@]}" agent-proposal
}

review_and_merge() { # PROPOSAL MESSAGE
  lb proposal review "$1" approve --comment "Reviewed"
  lb proposal merge "$1" -m "$2"
}

ship() { # SOURCE TARGET TITLE: open a Proposal, approve it, merge it
  local id
  id="$(new_proposal "$1" "$2" "$3")"
  review_and_merge "$id" "$3"
}

# --- Before day 1 ----------------------------------------------------------------------------

banner "Preparation"
note "Keeper at $LOREBASE_URL, Domain '$DOMAIN', scratch files in $WORK"
rm -f "$WORK"/*.csv "$WORK"/*.yaml "$WORK"/*.json "$WORK"/*.log "$WORK"/*.secret "$WORK"/*.key
lb whoami
if "${LB[@]}" domain get "$DOMAIN" >/dev/null 2>&1; then
  existing="$("${LB[@]}" --json domain get "$DOMAIN" | "${PY[@]}" jget display_name)"
  if [ "$DEMO_RESET" = "1" ] && [ "$existing" = "$DEMO_NAME" ]; then
    note "Domain '$DOMAIN' is left over from an earlier run: deleting it to start clean"
    lb domain delete "$DOMAIN" --yes
  else
    die "Domain '$DOMAIN' exists and is not a demo Domain (or DEMO_RESET is 0): set LOREBASE_DOMAIN to another id"
  fi
fi
if [ -n "$SOURCES_DIR" ]; then cp "$DATA"/*.csv "$SOURCES_DIR"/; fi

# --- Day 1 -----------------------------------------------------------------------------------

banner "Day 1: install and model from zero"
note "Priya has the stack up (docker compose) and creates the Domain; main is protected from the start."
lb domain create "$DOMAIN" --name "$DEMO_NAME" --description "Suppliers, parts, plants, orders and contracts of Corvane Industries"
lb branch list

note "Marco starts from the client's CSV files: schema:infer proposes the ontology and writes nothing."
py infer "$WORK/inferred.yaml"

note "He applies the proposal on a branch, then curates it with what only a modeler knows: a Trait, two Derivations, two Operations, two Sources."
lb branch create model/initial
lbn 8 schema apply "$WORK/inferred.yaml" -b model/initial -m "Apply the inferred schema"
py curate "$WORK/curated.yaml" --branch model/initial
lbn 12 schema diff "$WORK/curated.yaml" -b model/initial
lbn 8 schema apply "$WORK/curated.yaml" -b model/initial -m "Curate the schema"

note "Sofia reviews the schema change in a Proposal and merges it into main."
PROPOSAL="$(new_proposal model/initial main "Model the supply Domain")"
lb proposal get "$PROPOSAL"
review_and_merge "$PROPOSAL" "Model the supply Domain"
lb log main -n 3
note "Checks: main holds the curated schema, through a Proposal, and is protected."
py assert-day1
day_done "Day 1"

# --- Day 2 -----------------------------------------------------------------------------------

banner "Day 2: structured ingestion, exploration and a first document"
note "Dana loads the CSV files with one batch import: Records and their Links travel together, one Revision."
lb branch create data/initial
lb import batch -b data/initial \
  --records Supplier="$DATA/supplier.csv" --records Part="$DATA/part.csv" \
  --records Plant="$DATA/plant.csv" --records Contract="$DATA/contract.csv" \
  --records PurchaseOrder="$DATA/purchase_order.csv" --records RiskFlag="$DATA/risk_flag.csv" \
  --links supplied_by="$DATA/supplied_by.csv" --links stocked_at="$DATA/stocked_at.csv" \
  --links with_supplier="$DATA/with_supplier.csv" --links for_part="$DATA/for_part.csv" \
  --links delivered_to="$DATA/delivered_to.csv" --links flags="$DATA/flags.csv"
ship data/initial main "Load the supply data"
DAY2_HEAD="$(head_of main)"

note "Luca explores. Records with their derived values (computed by the materializer, never stored in the tree):"
lbw record list Part -n 4 --select category --select unit_cost --select supplier_count --select single_sourced
note "A query: single-sourced parts per category."
lbw query '{"kind": "Part", "where": {"attr": "single_sourced", "op": "eq", "value": true}, "aggregate": {"group_by": "category"}}'
note "Lexical search."
lbw search bearing --kind Part -n 3
note "A context pack: what an agent gets for one Record, within a token budget."
lbw context --ref Supplier/S-1001 --budget 1500
note "Where did a Record come from? Each value has its origin, actor and Revision."
lbw explain Supplier S-1001
note "Is the data sound?"
lbw health
note "Checks: every CSV row is a Record or a Link on main; the derived value, query and search answer."
py assert-day2

note "Procurement also sends a framework agreement as a document. Scribe reads it with the deterministic tiers (rules, no model) and opens a Proposal of items, each with its evidence."
lb documents upload examples/five_day_demo/documents/contract-c-3099-wexrix-filtration.md --tiers 0,1
EXTRACTION="$(extraction_proposal contract-c-3099-wexrix-filtration.md)"
lbw proposal items "$EXTRACTION"
py assert-items "$EXTRACTION"
note "Dana reviews the items and accepts both (a new Contract needs its Link to pass validation), then the Proposal merges like any other."
lb proposal decide "$EXTRACTION" --where-status pending --decision accepted
review_and_merge "$EXTRACTION" "Add contract C-3099 from the document"
lbw explain Contract C-3099
py assert-extraction "$EXTRACTION"
day_done "Day 2"

# --- Day 3 -----------------------------------------------------------------------------------

banner "Day 3: versioning and conflicts"
note "Two teams, two branches, both from main."
lb branch create model/risk
lb branch create cleanup/tiers

note "Team risk (Marco) changes the schema and the data: a required Attribute with a default, new scores, a new supplier."
lb schema get -b main --output "$WORK/main.yaml"
py risk-model "$WORK/main.yaml" "$WORK/risk.yaml"
lb schema diff "$WORK/risk.yaml" -b model/risk
lbn 8 schema apply "$WORK/risk.yaml" -b model/risk -m "Add risk_band"
py team-csv risk "$WORK/team_risk.csv"
lb import records Supplier "$WORK/team_risk.csv" -b model/risk

note "Team procurement (Sofia) works on the same suppliers, unaware of that."
py team-csv tiers "$WORK/team_tiers.csv"
lb import records Supplier "$WORK/team_tiers.csv" -b cleanup/tiers

note "What each branch changed against main:"
lbw diff main model/risk --kind Supplier -n 4
lbw diff main cleanup/tiers --kind Supplier -n 4

note "Team risk goes first: schema and data merge clean."
ship model/risk main "Add risk_band and rescore suppliers"

note "Team procurement is merged against the new main: the same Attribute changed on both sides."
TIERS_PROPOSAL="$(new_proposal cleanup/tiers main "Reclassify supplier tiers")"
lbn 14 proposal get "$TIERS_PROPOSAL"
lb proposal review "$TIERS_PROPOSAL" approve --comment "Reviewed"
lb_refused proposal merge "$TIERS_PROPOSAL"
py conflicts "$TIERS_PROPOSAL"

note "One Conflict is decided by hand, the rest in bulk from a file (the policy: keep the Record team risk added)."
CONFLICT="$("${PY[@]}" conflict-id "$TIERS_PROPOSAL")"
lb proposal resolve "$TIERS_PROPOSAL" "$CONFLICT" --choice custom --value '"75"'
py resolutions "$TIERS_PROPOSAL" "$WORK/resolutions.json"
lb proposal resolve "$TIERS_PROPOSAL" --file "$WORK/resolutions.json"
lb proposal conflicts "$TIERS_PROPOSAL" --status open
lb proposal merge "$TIERS_PROPOSAL" -m "Reclassify supplier tiers"

note "Both teams' work is in main; every resolution is recorded. Time travel: S-1001 now, and as loaded on day 2."
lb record get Supplier S-1001
lb record get Supplier S-1001 --at "$DAY2_HEAD"
lbw diff "$DAY2_HEAD" main --kind Supplier -n 5
note "Checks: both teams' work is in main, no Conflict is open, time travel still shows day 2."
py assert-day3 "$TIERS_PROPOSAL" "$DAY2_HEAD"
day_done "Day 3"

# --- Day 4 -----------------------------------------------------------------------------------

banner "Day 4: a Source that keeps syncing, and a change feed"
if [ -z "$SOURCES_DIR" ]; then
  note "DEMO_SOURCES_DIR is not set: Scribe has no directory to read the CSV Source from. Skipped."
else
  note "Priya registers a webhook for main; a tiny receiver on this machine verifies the signature of each delivery."
  "${PY[@]}" receiver --port "$RECEIVER_PORT" --bind "$RECEIVER_BIND" --secret-file "$WORK/webhook.secret" >"$WORK/receiver.log" 2>&1 &
  RECEIVER_PID=$!
  until grep -q listening "$WORK/receiver.log" 2>/dev/null; do sleep 0.2; done
  py webhook-register "http://$WEBHOOK_HOST:$RECEIVER_PORT/hook" "$WORK/webhook.secret" --branch main
  BEFORE="$(head_of main)"

  note "Dana's Source approved.suppliers (a CSV, declared in the schema) maps the file to Suppliers. Procurement drops its list; Scribe syncs it."
  py export-suppliers "$SOURCES_DIR/supplier.csv"
  PREVIOUS="$("${PY[@]}" last-run approved.suppliers)"
  lb sources sync approved.suppliers
  py wait-run approved.suppliers --after "$PREVIOUS"

  note "Procurement changes two cells and the next run finds them."
  py csv-set "$SOURCES_DIR/supplier.csv" S-1005 city=Madrid
  py csv-set "$SOURCES_DIR/supplier.csv" S-1008 tier=1
  PREVIOUS="$("${PY[@]}" last-run approved.suppliers)"
  lb sources sync approved.suppliers
  py wait-run approved.suppliers --after "$PREVIOUS"
  lbw sources runs approved.suppliers -n 2
  note "The run committed on branch source/approved.suppliers and opened a Proposal; the merge policy (auto_when_clean) merged it."
  lbw proposal list --status merged -n 2
  lbw explain Supplier S-1005

  note "The change feed since the start of the day (polling form), and what the webhook delivered meanwhile."
  py changes main --since "$BEFORE"
  py wait-lines "$WORK/receiver.log" "signature=ok" --count 1 --timeout 30
  note "Checks: two runs, one Proposal merged by policy, the two cells changed on main, the feed lists them."
  py assert-day4 --since "$BEFORE"
fi
day_done "Day 4"

# --- Day 5 -----------------------------------------------------------------------------------

banner "Day 5: agents"
note "Alex gets a key for his agent: role agent-reader on the Domain, plus a grant on one Operation."
lb branch create agent/flags
py agent-setup agent-alex "$WORK/agent.key"
AGENT_KEY="$(cat "$WORK/agent.key")"
note "The key authenticates as the agent, with its own quota:"
printf '%s$ LOREBASE_API_KEY=<the agent key> lorebase whoami%s\n' "$DIM" "$RESET"
LOREBASE_API_KEY="$AGENT_KEY" "${LB[@]}" whoami

note "The agent: a scripted MCP client (the mcp SDK) starts Herald over stdio and calls its tools."
py agent "$WORK/agent.key"

note "Rate limits are per principal and class; every response says where the bucket stands."
py limits "$WORK/agent.key"

note "Sofia takes the agent's flag through a Proposal like any other change. The critical flag is already a Proposal, opened by the agent's own task branch; she reviews it too. Priya shows the audit log."
ship agent/flags main "Risk flags raised by the agent"
AGENT_PROPOSAL="$(agent_proposal)"
lb proposal get "$AGENT_PROPOSAL"
review_and_merge "$AGENT_PROPOSAL" "Critical flag raised by the agent"
py audit --limit 8
note "Checks: both flags are on main only through merged Proposals, the audit log has them, a direct write to main is refused."
py assert-day5 "$WORK/agent.key"
if [ -n "$RECEIVER_PID" ]; then
  note "Deliveries the receiver got so far:"
  py wait-lines "$WORK/receiver.log" "signature=ok" --count 1 --timeout 30
fi

if [ -z "$STORE_EXEC" ] || [ -z "$POSTGRES_EXEC" ]; then
  note "DEMO_STORE_EXEC and DEMO_POSTGRES_EXEC are not set: bundle export and garbage collection need the backend container. Skipped."
else
  note "Priya checks what a collection would reclaim: a branch that was thrown away leaves unreachable Revisions."
  lb branch create scratch/experiment
  lb import records Supplier "$WORK/team_risk.csv" -b scratch/experiment
  lb branch delete scratch/experiment
  store python -m lorebase_store.gc --domain "$DOMAIN" --dry-run --grace-seconds 0

  note "A backup: export the Domain as a bundle and restore it into a fresh, empty database (a Revision id includes the Domain id, so a bundle keeps its Domain id; it moves between installations)."
  store python -m lorebase_store.bundle export --domain "$DOMAIN" --out /tmp/demo-bundle.tar
  create_empty_database
  restore_to_second_database
  compare_heads
fi
day_done "Day 5"

printf '\n%sThe five days took %ss, every check passed.%s\n' "$BOLD" "$((SECONDS - STARTED))" "$RESET"
