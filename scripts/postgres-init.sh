#!/bin/sh
# Runs once, on the first start of an empty Postgres volume.
# The `meetbot` database is created by POSTGRES_DB; Attendee gets its own database next to it.
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE DATABASE attendee;
SQL
