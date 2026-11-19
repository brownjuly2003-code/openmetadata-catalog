import json
import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"


@pytest.mark.parametrize(
    ("name", "schema", "included", "excluded"),
    [
        ("postgres", "picking", ["orders", "order_facts"], ["orders_archive", "users"]),
        (
            "clickhouse",
            "marketing_jan",
            ["events", "spend", "attribution", "channel_metrics", "user_funnel"],
            ["events_staging", "tmp_attribution", "query_log"],
        ),
    ],
)
def test_catalog_scope(name, schema, included, excluded):
    workflow = yaml.safe_load((WORKFLOWS / f"{name}.yaml").read_text(encoding="utf-8"))
    source = workflow["source"]
    connection = source["serviceConnection"]["config"]
    pipeline = source["sourceConfig"]["config"]
    assert source["type"] == name
    if name == "postgres":
        assert connection["database"] == "picking"
        assert connection["ingestAllDatabases"] is False
        assert pipeline["includeViews"] is True
    else:
        assert connection["databaseSchema"] == schema
    schema_patterns = pipeline["schemaFilterPattern"]["includes"]
    assert schema_patterns == [f"^{schema}$"]
    table_patterns = pipeline["tableFilterPattern"]["includes"]
    for table in included:
        assert any(
            re.match(pattern, table, re.IGNORECASE) for pattern in table_patterns
        )
    for table in excluded:
        assert not any(
            re.match(pattern, table, re.IGNORECASE) for pattern in table_patterns
        )
    assert workflow["sink"]["type"] == "metadata-rest"


FIXTURES = WORKFLOWS.parent / "stand" / "fixtures"
CREATE = re.compile(
    r"^CREATE\s+(?:OR\s+REPLACE\s+)?(TABLE|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:(\w+)\.)?(\w+)",
    re.IGNORECASE | re.MULTILINE,
)


@pytest.mark.parametrize(
    ("name", "service", "ddl", "default_schema"),
    [
        ("postgres", "p17_picking", "postgres/001_orders.sql", None),
        ("clickhouse", "p16_marketing", "clickhouse/*.sql", "marketing_jan"),
    ],
)
def test_filters_select_exactly_the_catalog_objects_of_the_ddl(
    name, service, ddl, default_schema
):
    """Both directions: every DDL object the filters keep is in catalog.json and
    every catalog.json object of the service is created by the DDL and kept."""
    workflow = yaml.safe_load((WORKFLOWS / f"{name}.yaml").read_text(encoding="utf-8"))
    pipeline = workflow["source"]["sourceConfig"]["config"]
    schemas = pipeline["schemaFilterPattern"]["includes"]
    tables = pipeline["tableFilterPattern"]["includes"]
    created = set()
    for path in sorted(FIXTURES.glob(ddl)):
        for _, schema, table in CREATE.findall(path.read_text(encoding="utf-8")):
            created.add((schema or default_schema, table))
    # OpenMetadata matches filter patterns case-insensitively; so does this test.
    kept = {
        (schema, table)
        for schema, table in created
        if any(re.match(p, schema, re.IGNORECASE) for p in schemas)
        and any(re.match(p, table, re.IGNORECASE) for p in tables)
    }
    catalog = json.loads((WORKFLOWS.parent / "catalog.json").read_text("utf-8"))
    expected = {
        (fqn.split(".")[2], fqn.split(".")[3])
        for fqn in catalog["objects"]
        if fqn.startswith(f"{service}.")
    }
    assert kept == expected
    assert len(created) == len(expected)
