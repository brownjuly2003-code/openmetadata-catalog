import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "partial-tables.json"


def report(path, *options):
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/completeness.py"), str(path), *options],
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_partial_catalog_uses_the_full_inventory():
    result = report(SAMPLE)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["expected"] == 7
    assert data["counts"] == {
        "present": 3,
        "description": 2,
        "owner": 2,
        "glossary": 1,
        "complete": 1,
    }
    assert len(data["objects"]) == 7
    assert data["objects"]["p17_picking.picking.picking.orders"] == []
    assert data["objects"]["p17_picking.picking.picking.order_facts"] == [
        "owner",
        "glossary",
    ]
    assert data["objects"]["p16_marketing.default.marketing_jan.spend"] == ["deleted"]
    assert data["objects"]["p16_marketing.default.marketing_jan.attribution"] == [
        "missing"
    ]


def test_require_present_fails_after_writing_the_report(tmp_path):
    """stand/run.sh step 8 stops here, before apply_catalog.py writes anything."""
    result = report(SAMPLE, "--columns", "--require-present")
    assert result.returncode == 4
    assert json.loads(result.stdout)["counts"]["present"] == 3
    assert "p16_marketing.default.marketing_jan.spend" in result.stderr
    assert "p16_marketing.default.marketing_jan.attribution" in result.stderr
    assert "p17_picking.picking.picking.orders" not in result.stderr

    catalog = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
    snapshot = tmp_path / "tables.json"
    snapshot.write_text(
        json.dumps([{"fullyQualifiedName": fqn} for fqn in catalog["objects"]]),
        encoding="utf-8",
    )
    result = report(snapshot, "--require-present")
    assert result.returncode == 0, result.stderr


def test_duplicate_object_is_reported(tmp_path):
    row = {"fullyQualifiedName": "p17_picking.picking.picking.orders"}
    snapshot = tmp_path / "tables.json"
    snapshot.write_text(json.dumps([row, row]), encoding="utf-8")
    result = report(snapshot)
    assert result.returncode != 0
    assert "duplicate" in result.stderr.lower()
    assert not result.stdout


def test_completed_inventory_ignores_unrelated_tables(tmp_path):
    inventory = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
    tables = [{"fullyQualifiedName": "unrelated.default.demo.orders"}]
    for name, spec in inventory["objects"].items():
        tables.append(
            {
                "fullyQualifiedName": name,
                "description": spec["description"],
                "owners": [{"type": "team", "fullyQualifiedName": spec["owner"]}],
                "tags": [
                    {"source": "Glossary", "state": "Confirmed", "tagFQN": spec["term"]}
                ],
            }
        )
    path = tmp_path / "complete.json"
    path.write_text(json.dumps(tables), encoding="utf-8")
    result = report(path)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["expected"] == 7
    assert set(data["counts"].values()) == {7}
    assert all(not gaps for gaps in data["objects"].values())


def test_incomplete_api_page_is_not_a_snapshot(tmp_path):
    snapshot = tmp_path / "page.json"
    snapshot.write_text('{"data": [], "paging": {"after": "next"}}', encoding="utf-8")
    result = report(snapshot)
    assert result.returncode != 0
    assert "array" in result.stderr.lower()


def test_column_terms_in_partial_snapshot():
    result = report(SAMPLE, "--columns")
    assert result.returncode == 0, result.stderr
    with_columns = json.loads(result.stdout)
    coverage = with_columns.pop("column_terms")
    assert with_columns == json.loads(report(SAMPLE).stdout)
    assert coverage["expected"] == 9
    assert coverage["confirmed"] == 2
    statuses = coverage["objects"]
    pg = "p17_picking.picking.picking."
    ch = "p16_marketing.default.marketing_jan."
    assert statuses == {
        pg + "orders": {"order_id": "confirmed"},
        pg + "order_facts": {"order_id": "confirmed", "pick_seconds": "unconfirmed"},
        ch + "events": {"event_id": "unconfirmed"},
        ch + "spend": {"spend_cents": "deleted_table"},
        ch + "attribution": {"weight": "missing_table"},
        ch + "channel_metrics": {
            "cpa_cents": "missing_table",
            "spend_cents": "missing_table",
        },
        ch + "user_funnel": {"step": "missing_table"},
    }


def test_column_report_checks_the_expected_field_and_term(tmp_path):
    table = "p17_picking.picking.picking.order_facts"
    picking = {
        "source": "Glossary",
        "state": "Confirmed",
        "tagFQN": "LabMetrics.PickingOrder",
    }
    duration = {**picking, "tagFQN": "LabMetrics.PickingDuration"}
    snapshot = [
        {
            "fullyQualifiedName": table,
            "tags": [duration],
            "columns": [
                {"name": "order_id", "tags": [duration]},
                {"name": "wait_seconds", "tags": [duration]},
                {"name": "pick_seconds", "tags": [picking, duration]},
            ],
        }
    ]
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    result = report(path, "--columns")
    assert result.returncode == 0, result.stderr
    coverage = json.loads(result.stdout)["column_terms"]
    assert coverage["confirmed"] == 1
    assert coverage["objects"][table] == {
        "order_id": "unconfirmed",
        "pick_seconds": "confirmed",
    }


def test_all_source_column_assignments_can_be_confirmed(tmp_path):
    pg = "p17_picking.picking.picking."
    ch = "p16_marketing.default.marketing_jan."
    assignments = {
        pg + "orders": {"order_id": "PickingOrder"},
        pg + "order_facts": {
            "pick_seconds": "PickingDuration",
            "order_id": "PickingOrder",
        },
        ch + "events": {"event_id": "MarketingEvent"},
        ch + "spend": {"spend_cents": "ChannelSpend"},
        ch + "attribution": {"weight": "AttributionCredit"},
        ch + "channel_metrics": {
            "cpa_cents": "ChannelCPA",
            "spend_cents": "ChannelSpend",
        },
        ch + "user_funnel": {"step": "FunnelStep"},
    }
    tables = []
    for table, terms in assignments.items():
        columns = []
        for name, term in terms.items():
            label = {
                "source": "Glossary",
                "state": "Confirmed",
                "tagFQN": f"LabMetrics.{term}",
            }
            columns.append({"name": name, "tags": [label]})
        tables.append({"fullyQualifiedName": table, "columns": columns})
    path = tmp_path / "all-columns.json"
    path.write_text(json.dumps(tables), encoding="utf-8")
    result = report(path, "--columns")
    assert result.returncode == 0, result.stderr
    coverage = json.loads(result.stdout)["column_terms"]
    assert coverage["confirmed"] == coverage["expected"] == 9


@pytest.mark.parametrize(
    ("fields", "status"),
    [
        ({}, "columns_unavailable"),
        ({"columns": []}, "missing_column"),
        ({"columns": [{"name": "order_id"}]}, "tags_unavailable"),
        ({"columns": [{"name": "order_id", "tags": []}]}, "unconfirmed"),
    ],
)
def test_missing_column_evidence(tmp_path, fields, status):
    table = "p17_picking.picking.picking.orders"
    path = tmp_path / "snapshot.json"
    path.write_text(
        json.dumps([{"fullyQualifiedName": table, **fields}]), encoding="utf-8"
    )
    result = report(path, "--columns")
    assert result.returncode == 0, result.stderr
    coverage = json.loads(result.stdout)["column_terms"]
    assert coverage["confirmed"] == 0
    assert coverage["objects"][table]["order_id"] == status


def test_ambiguous_column_name_fails_with_table_context(tmp_path):
    table = "p17_picking.picking.picking.orders"
    column = {"name": "order_id", "tags": []}
    path = tmp_path / "snapshot.json"
    path.write_text(
        json.dumps([{"fullyQualifiedName": table, "columns": [column, column]}])
    )
    result = report(path, "--columns")
    assert result.returncode != 0
    assert "duplicate" in result.stderr and table in result.stderr
    assert not result.stdout
