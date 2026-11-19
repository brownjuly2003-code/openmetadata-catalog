#!/usr/bin/env bash
# One automated run of the OpenMetadata 2.0.8 stand with two source databases.
# Needs a Linux host with Docker (Compose v2.1.1 or later), Python 3.10+ and curl.
# Everything the run produces goes to stand/out/; the stand is removed at the end
# (docker compose down -v) unless KEEP_STAND=1. Exit code 0 means every check in
# stand/out/verify.json passed. Tokens and passwords are never printed.
#
#   bash stand/run.sh
#
# Tunables: MIN_FREE_DISK_GB (15), MIN_MEMORY_GB (6), WAIT_TIMEOUT seconds (1200),
# PYTHON (python3.11, else python3), KEEP_STAND (0).
set -euo pipefail

STAND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$STAND_DIR")"
OUT="$STAND_DIR/out"
VENV="$STAND_DIR/.venv"
TOKENS="$STAND_DIR/.tokens"
ENV_FILE="$STAND_DIR/.env"
COMPOSE=(docker compose --project-directory "$STAND_DIR"
  -f "$STAND_DIR/docker-compose.yml" --env-file "$ENV_FILE")
MIN_FREE_DISK_GB="${MIN_FREE_DISK_GB:-15}"
MIN_MEMORY_GB="${MIN_MEMORY_GB:-6}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-1200}"
PYTHON="${PYTHON:-$(command -v python3.11 || command -v python3)}"
KEEP_STAND="${KEEP_STAND:-0}"
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
CURRENT_STEP="0. start"
STAND_STARTED=0

rm -rf "$OUT"
mkdir -p "$OUT"
exec > >(tee -a "$OUT/run.log") 2>&1
cd "$REPO_DIR"

step() {
  CURRENT_STEP="$1"
  printf '%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >>"$OUT/steps.tsv"
  printf '\n==== %s ====\n' "$1"
}

redact() {
  # Replace every secret value from stand/.env and stand/.tokens in the outputs.
  local files=()
  [[ -f "$ENV_FILE" ]] && files+=("$ENV_FILE")
  [[ -f "$TOKENS" ]] && files+=("$TOKENS")
  ((${#files[@]})) || return 0
  "$PYTHON" - "$OUT" "${files[@]}" <<'PY'
import pathlib, sys
out = pathlib.Path(sys.argv[1])
secrets = []
for name in sys.argv[2:]:
    for line in pathlib.Path(name).read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if value and ("PASSWORD" in key or "TOKEN" in key):
            secrets.append(value)
for path in out.rglob("*"):
    if not path.is_file():
        continue
    text = path.read_text(encoding="utf-8", errors="replace")
    clean = text
    for value in sorted(set(secrets), key=len, reverse=True):
        clean = clean.replace(value, "[REDACTED]")
    if clean != text:
        path.write_text(clean, encoding="utf-8")
PY
}

finish() {
  local rc=$?
  set +e
  local failed_step="$CURRENT_STEP"
  if ((STAND_STARTED)); then
    printf '\n==== logs and teardown ====\n'
    "${COMPOSE[@]}" ps -a >"$OUT/compose-ps.txt" 2>&1
    "${COMPOSE[@]}" logs --no-color --timestamps >"$OUT/compose-logs.txt" 2>&1
  fi
  {
    echo "stand run: OpenMetadata 2.0.8 catalog stand"
    echo "started: $STARTED_AT"
    echo "finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "exit code: $rc"
    if ((rc)); then echo "failed step: $failed_step"; else echo "failed step: none"; fi
    echo
    echo "steps (UTC start time, name):"
    cat "$OUT/steps.tsv" 2>/dev/null
    echo
    if [[ -f "$OUT/verify.json" ]]; then
      echo "checks (stand/out/verify.json):"
      "$PYTHON" -c 'import json,sys
for c in json.load(open(sys.argv[1]))["checks"]:
    print(("PASS " if c["ok"] else "FAIL ") + c["check"] + ": " + c["detail"])' \
        "$OUT/verify.json"
    else
      echo "checks: not reached"
    fi
    for name in resources.txt up-fallback.txt docker-stats.txt; do
      if [[ -f "$OUT/$name" ]]; then
        echo
        echo "--- $name"
        cat "$OUT/$name"
      fi
    done
  } >"$OUT/SUMMARY.txt"
  redact
  if ((STAND_STARTED)) && [[ "$KEEP_STAND" != "1" ]]; then
    "${COMPOSE[@]}" down -v --remove-orphans
  fi
  rm -f "$TOKENS"
  echo "exit code $rc; summary: stand/out/SUMMARY.txt"
  exit "$rc"
}
trap finish EXIT

ch() {
  "${COMPOSE[@]}" exec -T source-clickhouse clickhouse-client \
    --user "$MARKETING_CH_USER" --password "$MARKETING_CH_PASSWORD" "$@"
}

remove_negative_control() {
  # Synchronous mutation: the DELETE returns after the rows are gone.
  ch --mutations_sync=2 --query \
    "ALTER TABLE marketing_jan.attribution DELETE WHERE conversion_id = 'negative_control'"
  local left
  left="$(ch --query \
    "SELECT count() FROM marketing_jan.attribution WHERE conversion_id = 'negative_control'")"
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) negative_control rows left: $left" |
    tee -a "$OUT/negative-cleanup.txt"
  [[ "$left" == "0" ]]
}

step "1. resources"
{
  echo "cpus: $(nproc)"
  free -g
  df -h "$STAND_DIR"
  docker version --format 'docker server {{.Server.Version}}'
  docker compose version
  "$PYTHON" --version
} | tee "$OUT/resources.txt"
DOCKER_ROOT="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
FREE_GB="$(df -Pk "$DOCKER_ROOT" 2>/dev/null | awk 'NR==2 {print int($4 / 1048576)}')"
[[ -n "$FREE_GB" ]] || FREE_GB="$(df -Pk "$STAND_DIR" | awk 'NR==2 {print int($4 / 1048576)}')"
MEMORY_GB="$(awk '/MemTotal/ {print int($2 / 1048576)}' /proc/meminfo)"
echo "free disk for docker ($DOCKER_ROOT): ${FREE_GB} GB, memory: ${MEMORY_GB} GB" |
  tee -a "$OUT/resources.txt"
if ((FREE_GB < MIN_FREE_DISK_GB)); then
  echo "not enough disk: ${FREE_GB} GB free, need ${MIN_FREE_DISK_GB} GB" >&2
  exit 10
fi
if ((MEMORY_GB < MIN_MEMORY_GB)); then
  echo "not enough memory: ${MEMORY_GB} GB, need ${MIN_MEMORY_GB} GB" >&2
  exit 11
fi

step "2. environment file"
if [[ ! -f "$ENV_FILE" ]]; then
  "$PYTHON" - "$STAND_DIR/.env.example" "$ENV_FILE" <<'PY'
import pathlib, secrets, sys
text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
while "CHANGE_ME" in text:
    text = text.replace("CHANGE_ME", secrets.token_hex(16), 1)
target = pathlib.Path(sys.argv[2])
target.write_text(text, encoding="utf-8")
target.chmod(0o600)
PY
  echo "created stand/.env with random passwords"
else
  echo "using the existing stand/.env"
fi
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
export OM_API_URL="http://127.0.0.1:8585/api"
export PICKING_PG_HOST_PORT="127.0.0.1:${SOURCE_PG_PORT}"
export MARKETING_CH_HOST_PORT="127.0.0.1:${SOURCE_CH_PORT}"

step "3. compose config and image pull"
"${COMPOSE[@]}" config --quiet
STAND_STARTED=1
"${COMPOSE[@]}" pull
"${COMPOSE[@]}" config --images | sort >"$OUT/images.txt"
docker image ls --digests >"$OUT/docker-images.txt"

step "4. docker compose up --wait"
if ! "${COMPOSE[@]}" up -d --wait --wait-timeout "$WAIT_TIMEOUT"; then
  # `up --wait` may report a failure because the one-shot migration container has
  # exited, even with exit code 0. Accept the start only if the migration exited 0
  # and every long-running service is healthy.
  echo "docker compose up --wait failed; checking the services directly" |
    tee "$OUT/up-fallback.txt"
  migrate_id="$("${COMPOSE[@]}" ps -a -q execute-migrate-all)"
  migrate_rc="$(docker inspect -f '{{.State.ExitCode}} {{.State.Status}}' "$migrate_id")"
  echo "execute-migrate-all: exit code and state: $migrate_rc" | tee -a "$OUT/up-fallback.txt"
  [[ "$migrate_rc" == "0 exited" ]]
  for service in postgresql elasticsearch openmetadata-server source-postgres source-clickhouse; do
    health="$(docker inspect -f '{{.State.Health.Status}}' "$("${COMPOSE[@]}" ps -q "$service")")"
    echo "$service: $health" | tee -a "$OUT/up-fallback.txt"
    [[ "$health" == "healthy" ]]
  done
fi
"${COMPOSE[@]}" ps -a | tee "$OUT/compose-ps-up.txt"
curl -fsS http://127.0.0.1:8586/healthcheck | tee "$OUT/healthcheck.json"
echo

step "5. ingestion client"
"$PYTHON" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet \
  "openmetadata-ingestion[postgres,clickhouse]==2.0.8.0" "PyYAML==6.0.3"
"$VENV/bin/python" -m pip freeze >"$OUT/pip-freeze.txt"
"$VENV/bin/metadata" --version | tee "$OUT/metadata-version.txt"
PY_VENV="$VENV/bin/python"
METADATA="$VENV/bin/metadata"

step "6. admin login and ingestion bot token"
for attempt in 1 2 3 4 5 6; do
  if "$PY_VENV" scripts/om_client.py tokens --env-file "$TOKENS"; then
    break
  fi
  if ((attempt == 6)); then exit 12; fi
  echo "retrying in 20 s"
  sleep 20
done
set -a
# shellcheck disable=SC1090
. "$TOKENS"
set +a

step "7. ingestion of both sources"
"$METADATA" ingest -c workflows/postgres.yaml 2>&1 | tee "$OUT/ingest-postgres-1.log"
"$METADATA" ingest -c workflows/clickhouse.yaml 2>&1 | tee "$OUT/ingest-clickhouse-1.log"

step "8. export before the catalog"
"$PY_VENV" scripts/stand_export.py tables --out "$OUT/before_tables.json"
# --require-present: stop here, before the first write to the catalog, if any of
# the seven catalog.json objects is missing or deleted (exit code 4).
"$PY_VENV" scripts/completeness.py --columns --require-present \
  "$OUT/before_tables.json" | tee "$OUT/before_completeness.json"

step "9. apply catalog.json twice"
"$PY_VENV" scripts/apply_catalog.py --out "$OUT/apply_1.json" >/dev/null
"$PY_VENV" scripts/apply_catalog.py --expect-unchanged --out "$OUT/apply_2.json" >/dev/null
"$PY_VENV" -c 'import json,sys
for n in sys.argv[1:]:
    print(n.rsplit("/", 1)[-1], "changes:", json.load(open(n))["changes"])' \
  "$OUT/apply_1.json" "$OUT/apply_2.json"
"$PY_VENV" scripts/stand_export.py tables --out "$OUT/applied_tables.json"
"$PY_VENV" scripts/completeness.py --columns "$OUT/applied_tables.json" |
  tee "$OUT/applied_completeness.json"

step "10. repeated ingestion and export after"
# Source comments unique to this run: a column comment per catalog object that only
# the second ingestion can bring into the catalog, and a table comment that must
# not replace the manual description. verify compares applied_* and after_*.
"$PY_VENV" scripts/stand_export.py probe --stamp "$STARTED_AT" \
  --out "$OUT/reingest_probe.json"
probe_sql() {
  "$PY_VENV" -c 'import json,sys
sys.stdout.write(json.load(open(sys.argv[1]))["sql"][sys.argv[2]])' \
    "$OUT/reingest_probe.json" "$1"
}
probe_sql postgres | "${COMPOSE[@]}" exec -T source-postgres \
  psql -U "$SOURCE_PG_ADMIN_USER" -d picking -v ON_ERROR_STOP=1 -q
probe_sql clickhouse | ch --multiquery
"$METADATA" ingest -c workflows/postgres.yaml 2>&1 | tee "$OUT/ingest-postgres-2.log"
"$METADATA" ingest -c workflows/clickhouse.yaml 2>&1 | tee "$OUT/ingest-clickhouse-2.log"
"$PY_VENV" scripts/stand_export.py tables --out "$OUT/after_tables.json"
"$PY_VENV" scripts/completeness.py --columns "$OUT/after_tables.json" |
  tee "$OUT/after_completeness.json"
"$PY_VENV" scripts/stand_export.py lineage --out "$OUT/lineage.json"

step "11. quality checks"
# Rows of an earlier negative control (KEEP_STAND=1 and a run stopped before the
# cleanup below) are removed first, so the clean check runs on clean data.
remove_negative_control
"$METADATA" test -c workflows/quality-orders.yaml 2>&1 | tee "$OUT/test-orders.log"
"$METADATA" test -c workflows/quality-attribution.yaml 2>&1 | tee "$OUT/test-attribution.log"
"$PY_VENV" scripts/stand_export.py quality --out "$OUT/quality.json"
cat "$OUT/quality.json"
# Negative control: one attribution group whose credits sum to 0.5 must fail credit_sum.
ch --query \
  "INSERT INTO marketing_jan.attribution SELECT * REPLACE ('negative_control' AS conversion_id, 0.5 AS weight) FROM marketing_jan.attribution LIMIT 1"
negative_rc=0
"$METADATA" test -c workflows/quality-attribution.yaml 2>&1 |
  tee "$OUT/test-attribution-negative.log" || negative_rc=$?
echo "metadata test exit code on the broken data: $negative_rc" |
  tee "$OUT/test-attribution-negative-rc.txt"
"$PY_VENV" scripts/stand_export.py quality --out "$OUT/quality_negative.json"
cat "$OUT/quality_negative.json"
remove_negative_control

step "12. verify"
"$PY_VENV" scripts/stand_export.py verify --out-dir "$OUT"

step "13. docker stats"
docker stats --no-stream | tee "$OUT/docker-stats.txt"
docker system df | tee "$OUT/docker-df.txt"
df -h "$DOCKER_ROOT" "$STAND_DIR" 2>/dev/null | tee "$OUT/disk-after.txt" || true

CURRENT_STEP="done"
