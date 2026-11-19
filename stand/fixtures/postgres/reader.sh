#!/usr/bin/env bash
# Mounted as /docker-entrypoint-initdb.d/010_reader.sh of the source PostgreSQL and
# sourced by its entrypoint once, before 020_orders.sql and 030_orders_data.sql.
# Creates the read-only role that the catalog uses for ingestion and quality checks.
# Based on docker/postgres/010_roles.sh of the order picking project (commit d21a222),
# without the BI application database. No `set -u`: it would leak into the entrypoint.

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v admin="$POSTGRES_USER" -v data_db="$POSTGRES_DB" \
  -v reader="$PICKING_PG_USER" -v reader_pw="$PICKING_PG_PASSWORD" <<'EOSQL'
CREATE ROLE :"reader" LOGIN PASSWORD :'reader_pw';
ALTER ROLE :"reader" SET default_transaction_read_only = on;
REVOKE ALL ON DATABASE :"data_db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"data_db" TO :"reader";
CREATE SCHEMA picking AUTHORIZATION :"admin";
GRANT USAGE ON SCHEMA picking TO :"reader";
ALTER DEFAULT PRIVILEGES FOR ROLE :"admin" IN SCHEMA picking GRANT SELECT ON TABLES TO :"reader";
EOSQL
