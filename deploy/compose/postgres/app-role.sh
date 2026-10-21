#!/bin/sh
# One-shot, every start: the login role Keeper and Scribe connect as.
#
# POSTGRES_USER, created by the image, is a superuser: it owns the schema, runs the migrations
# and the index maintenance, and skips row-level security. The services connect as APP_USER
# instead, a role without SUPERUSER, BYPASSRLS, CREATEDB, CREATEROLE or REPLICATION, so the
# `domain_isolation` policies apply to every statement they run. The role may read and write
# the rows of the tables in the public schema (present and future ones, through default
# privileges of the owner) and nothing else; revision 0005 grants it EXECUTE on the read
# functions when the migration runs with database.application_role set to it.
#
# The script is idempotent and runs before the migrations on every `up`, so it also works on a
# volume created before this role existed and picks up a changed APP_PASSWORD.
#
# Environment: PGHOST, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB, APP_USER, APP_PASSWORD.
set -eu
: "${APP_USER:?set LOREBASE_APP_USER in the environment file}"
: "${APP_PASSWORD:?set LOREBASE_APP_PASSWORD in the environment file}"
if [ "$APP_USER" = "$POSTGRES_USER" ]; then
  echo "LOREBASE_APP_USER must differ from POSTGRES_USER (the owner role)" >&2
  exit 1
fi

until pg_isready -q -h "$PGHOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB"; do
  sleep 1
done

PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$PGHOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v ON_ERROR_STOP=1 -v role="$APP_USER" -v secret="$APP_PASSWORD" -qtA <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'role', :'secret')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role')
\gexec
SELECT format('ALTER ROLE %I LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE '
              'NOREPLICATION PASSWORD %L', :'role', :'secret') \gexec
-- Only the owner creates objects; the read functions rely on it (store schema notes).
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SELECT format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO %I', current_database(), :'role')
\gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'role') \gexec
SELECT format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO %I',
              :'role') \gexec
SELECT format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO %I', :'role') \gexec
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
              'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I', current_user, :'role')
\gexec
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
              'GRANT USAGE, SELECT ON SEQUENCES TO %I', current_user, :'role') \gexec
-- A volume migrated before the role existed: grant the read functions of revision 0005
-- (lorebase_store.definer.READ_FUNCTIONS), as the migration does for a new database.
SELECT format('GRANT EXECUTE ON FUNCTION %s TO %I', CAST(p.oid AS regprocedure), :'role')
FROM pg_proc p
WHERE p.proname IN ('lb_search', 'lb_links_in', 'lb_neighborhood', 'lb_query_by_attribute')
  AND p.pronamespace = CAST('public' AS regnamespace)
\gexec
SQL
