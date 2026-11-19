#!/usr/bin/env bash
# Mounted as /docker-entrypoint-initdb.d/init.sh of the source ClickHouse and sourced
# once by its entrypoint, which defines "${clickhouseclient[@]}" (clickhouse-client
# --multiquery with the stand user). The fixture directory is mounted at /fixtures.
# The order repeats scripts/load_clickhouse.py of the marketing funnel project
# (commit 3d60450): input tables, their rows, then the three derived tables.

"${clickhouseclient[@]}" --query "CREATE DATABASE IF NOT EXISTS marketing_jan"
"${clickhouseclient[@]}" --database marketing_jan < /fixtures/inputs.sql
"${clickhouseclient[@]}" --database marketing_jan \
  --query "INSERT INTO events FORMAT JSONEachRow" < /fixtures/events.jsonl
"${clickhouseclient[@]}" --database marketing_jan \
  --query "INSERT INTO spend FORMAT JSONEachRow" < /fixtures/spend.jsonl
for derived in attribution channel_metrics user_funnel; do
  "${clickhouseclient[@]}" --database marketing_jan < "/fixtures/${derived}.sql"
done
