#!/usr/bin/env bash
# Prints the SQL that creates the read-only role Meetbot uses on the SABAH.OS database,
# with the user/password from .env. Run the output on the SABAH.OS Postgres as a superuser:
#
#   ./scripts/sabah-reader-sql.sh | sudo -u postgres psql -d sabah_os_ai_db
#   ./scripts/sabah-reader-sql.sh | docker exec -i <sabah-postgres-container> psql -U postgres -d sabah_os_ai_db
set -euo pipefail
cd "$(dirname "$0")/.."
get_env() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }

user="$(get_env SABAH_DB_USER)"; user="${user:-meetbot_reader}"
password="$(get_env SABAH_DB_PASSWORD)"
db="$(get_env SABAH_DB_NAME)"; db="${db:-sabah_os_ai_db}"
[ -n "$password" ] || { echo "SABAH_DB_PASSWORD is empty in .env — run ./scripts/bootstrap.sh first" >&2; exit 1; }

cat <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${user}') THEN
    CREATE ROLE ${user} LOGIN PASSWORD '${password}';
  ELSE
    ALTER ROLE ${user} WITH LOGIN PASSWORD '${password}';
  END IF;
END
\$\$;
ALTER ROLE ${user} SET default_transaction_read_only = on;
GRANT CONNECT ON DATABASE ${db} TO ${user};
GRANT USAGE ON SCHEMA public TO ${user};
GRANT SELECT ON calendars_calendarevent, calendars_eventattendee TO ${user};
GRANT SELECT (id, email, first_name, last_name) ON users TO ${user};
SQL
