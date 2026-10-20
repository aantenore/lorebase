#!/bin/sh
# One-shot, profile scale-out: lets a standby stream from the primary.
#
# The official image accepts replication connections from nowhere but localhost, and creates no
# replication role. This script appends one pg_hba.conf rule to the data directory shared with
# the primary, creates (or updates) a role that can replicate and nothing else, and reloads the
# configuration. It is idempotent, so it also works on a volume initialised long ago.
#
# Environment: PGDATA, PRIMARY_HOST, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB,
# REPLICATION_USER, REPLICATION_PASSWORD.
set -eu
: "${REPLICATION_PASSWORD:?set REPLICATION_PASSWORD in the environment file}"

until pg_isready -q -h "$PRIMARY_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB"; do
  sleep 1
done

rule="host replication $REPLICATION_USER all scram-sha-256"
grep -qxF "$rule" "$PGDATA/pg_hba.conf" || printf '%s\n' "$rule" >> "$PGDATA/pg_hba.conf"

PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$PRIMARY_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v ON_ERROR_STOP=1 -v role="$REPLICATION_USER" -v secret="$REPLICATION_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN REPLICATION PASSWORD %L', :'role', :'secret')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role')
\gexec
SELECT format('ALTER ROLE %I PASSWORD %L', :'role', :'secret') \gexec
SELECT pg_reload_conf();
SQL
