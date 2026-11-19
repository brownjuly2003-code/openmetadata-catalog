"""Run the SQL from the two catalog quality checks on isolated source engines."""

from pathlib import Path
import subprocess

import yaml


WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"
CLICKHOUSE = (
    "clickhouse/clickhouse-server:24.8.14.39@"
    "sha256:1ffa82edee000a42c09313bd9f1293d94c570aee74babc1b3ca9983a35fa597b"
)


def check_sql(file):
    workflow = yaml.safe_load((WORKFLOWS / file).read_text(encoding="utf-8"))
    (case,) = workflow["processor"]["config"]["testCases"]
    return next(
        p["value"] for p in case["parameterValues"] if p["name"] == "sqlExpression"
    )


def run_sql(args, sql):
    result = subprocess.run(
        ["docker", "run", "--rm", "-i", "--network", "none", *args],
        input=sql,
        text=True,
        capture_output=True,
        timeout=150,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def test_postgres_order_quantities():
    query = check_sql("quality-orders.yaml")
    # Omit the production CHECK constraints to exercise each failure condition.
    sql = f"""
        CREATE SCHEMA picking;
        CREATE TABLE picking.orders (requested integer, picked integer, errors integer);
        INSERT INTO picking.orders VALUES (3, 0, 0), (3, 2, 1), (3, 3, 3);
        {query};
        INSERT INTO picking.orders VALUES
            (0, 0, 0), (3, -1, 0), (3, 4, 0), (3, 2, -1), (3, 2, 3),
            (NULL, 0, 0), (3, NULL, 0), (3, 2, NULL);
        {query};
    """
    start = """
        pgdir=$(mktemp -d)
        initdb -D "$pgdir" --no-sync --auth-local=trust >/dev/null
        pg_ctl -D "$pgdir" -o "-k /tmp -c listen_addresses=''" -l /tmp/pg.log -w start >/dev/null
        exec psql -h /tmp -d postgres -XqAt -v ON_ERROR_STOP=1
    """
    rows = run_sql(
        [
            "--user",
            "postgres",
            "--entrypoint",
            "sh",
            "postgres:16.10-alpine",
            "-ec",
            start,
        ],
        sql,
    )
    assert rows == ["0", "8"]


def test_clickhouse_attribution_weights():
    query = check_sql("quality-attribution.yaml")
    sql = f"""
        CREATE DATABASE marketing_jan;
        CREATE TABLE marketing_jan.attribution
            (conversion_id String, model String, weight Float64) ENGINE = Memory;
        INSERT INTO marketing_jan.attribution VALUES
            ('A','linear',0.4), ('A','linear',0.6), ('A','last_click',1),
            ('B','linear',0.5), ('B','linear',0.5000000002);
        {query};
        INSERT INTO marketing_jan.attribution VALUES
            ('C','linear',0.8), ('C','time_decay',0.6), ('C','time_decay',0.6),
            ('D','linear',-0.2), ('D','linear',0.6), ('D','linear',0.6),
            ('E','linear',0), ('E','linear',1), ('F','linear',nan);
        {query};
    """
    rows = run_sql(
        [
            "--entrypoint",
            "clickhouse-local",
            CLICKHOUSE,
            "--path",
            "/tmp/p28",
            "--multiquery",
        ],
        sql,
    )
    assert rows == ["0", "5"]


def test_clickhouse_attribution_tolerance_boundary():
    """Pairs whose decimal weights sum to exactly 1 +/- 1e-9 pass; 1.002e-9 fails.

    In Float64 the sum 0.5 + 0.500000001 deviates from 1 by about 1.00000008e-9,
    so the boundary needs an explicit rounding margin.
    """
    query = check_sql("quality-attribution.yaml")
    sql = f"""
        CREATE DATABASE marketing_jan;
        CREATE TABLE marketing_jan.attribution
            (conversion_id String, model String, weight Float64) ENGINE = Memory;
        INSERT INTO marketing_jan.attribution VALUES
            ('G','linear',0.5), ('G','linear',0.500000001),
            ('H','linear',0.5), ('H','linear',0.499999999),
            ('K','linear',0.4), ('K','linear',0.600000001),
            ('M','linear',0.25), ('M','linear',0.749999999);
        {query};
        INSERT INTO marketing_jan.attribution VALUES
            ('I','linear',0.5), ('I','linear',0.5000000011),
            ('J','linear',0.5), ('J','linear',0.4999999989),
            ('N','linear',0.5), ('N','linear',0.500000001002),
            ('O','linear',0.5), ('O','linear',0.499999998998);
        {query};
        SELECT conversion_id, sum(weight), abs(sum(weight) - 1)
        FROM marketing_jan.attribution GROUP BY conversion_id ORDER BY conversion_id
        FORMAT TSV;
    """
    rows = run_sql(
        [
            "--entrypoint",
            "clickhouse-local",
            CLICKHOUSE,
            "--path",
            "/tmp/p28",
            "--multiquery",
        ],
        sql,
    )
    # Shown by pytest on failure: each pair's Float64 sum and deviation as computed.
    print("\n".join(rows[2:]))
    assert rows[:2] == ["0", "4"], rows
