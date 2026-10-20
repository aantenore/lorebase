#!/bin/sh
# Entrypoint of the streaming replica (profile scale-out), run as the postgres user.
#
# First start (empty data directory): take a base backup from the primary through a dedicated
# replication slot, which also writes standby.signal and the connection settings (-R). Later
# starts keep the data directory and resume streaming. Then run Postgres as a hot standby.
#
# Environment: PGDATA, PRIMARY_HOST, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB,
# REPLICATION_USER, REPLICATION_PASSWORD, REPLICATION_SLOT.
set -eu
: "${REPLICATION_PASSWORD:?set REPLICATION_PASSWORD in the environment file}"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  until pg_isready -q -h "$PRIMARY_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB"; do
    sleep 1
  done
  # A slot left by an earlier copy of this replica would make -C fail; drop it when idle.
  PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$PRIMARY_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -v ON_ERROR_STOP=1 -v slot="$REPLICATION_SLOT" -qtA <<'SQL'
SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots
WHERE slot_name = :'slot' AND NOT active;
SQL
  PGPASSWORD="$REPLICATION_PASSWORD" pg_basebackup -h "$PRIMARY_HOST" -U "$REPLICATION_USER" \
    -D "$PGDATA" -X stream -R -C -S "$REPLICATION_SLOT" -c fast
  chmod 0700 "$PGDATA"
fi

exec postgres -c hot_standby=on
