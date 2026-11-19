import json
from pathlib import Path
import sys

import pytest

from fake_om import ADMIN_TOKEN, FakeOpenMetadata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import apply_catalog  # noqa: E402
import completeness  # noqa: E402
import om_client  # noqa: E402
import stand_export  # noqa: E402

CATALOG = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
OBJECTS = CATALOG["objects"]
CASES = stand_export.quality_cases()
STAMP = "2026-11-20T00:00:00Z"


def result(status, count):
    return {
        "testCaseStatus": status,
        "result": f"Found {count} row(s)",
        "testResultValue": [{"name": "resultRowCount", "value": str(count)}],
    }


def add_cases(fake, statuses):
    for table, name in CASES:
        status, count = statuses[name]
        fake.test_cases[name] = {
            "name": name,
            "fullyQualifiedName": f"{table}.{name}",
            "entityLink": f"<#E::table::{table}>",
            "testCaseResult": result(status, count),
        }


def test_quality_cases_come_from_the_workflows():
    assert CASES == [
        ("p17_picking.picking.picking.orders", "order_quantities"),
        ("p16_marketing.default.marketing_jan.attribution", "credit_sum"),
    ]


def write(out, name, data):
    (out / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def successful_run(tmp_path):
    """stand/out/ as a successful run would leave it, built against the fake server."""
    with FakeOpenMetadata() as fake:
        for fqn, spec in OBJECTS.items():
            view = fqn.endswith("order_facts")
            fake.add_table(
                fqn,
                ["loaded_at", *spec["column_terms"]],
                tableType="View" if view else "Regular",
            )
        client = om_client.Client(fake.url, ADMIN_TOKEN)
        before = stand_export.export_tables(client, CATALOG)
        write(tmp_path, "before_tables.json", before)
        write(
            tmp_path,
            "before_completeness.json",
            completeness.completeness(OBJECTS, before),
        )
        write(tmp_path, "apply_1.json", apply_catalog.apply(client, CATALOG))
        write(tmp_path, "apply_2.json", apply_catalog.apply(client, CATALOG))
        applied = stand_export.export_tables(client, CATALOG)
        write(tmp_path, "applied_tables.json", applied)
        probe = stand_export.reingestion_probe(CATALOG, STAMP)
        write(tmp_path, "reingest_probe.json", probe)
        # The second ingestion brings the probe column comments into the catalog.
        for fqn, name in probe["columns"].items():
            column = next(c for c in fake.tables[fqn]["columns"] if c["name"] == name)
            column["description"] = probe["column_comment"]
        after = stand_export.export_tables(client, CATALOG)
        write(tmp_path, "after_tables.json", after)
        write(
            tmp_path,
            "after_completeness.json",
            completeness.completeness(OBJECTS, after, include_columns=True),
        )
        write(tmp_path, "lineage.json", stand_export.export_lineage(client, CATALOG))
        add_cases(
            fake, {"order_quantities": ("Success", 0), "credit_sum": ("Success", 0)}
        )
        write(tmp_path, "quality.json", stand_export.export_quality(client, CASES))
        add_cases(
            fake, {"order_quantities": ("Success", 0), "credit_sum": ("Failed", 1)}
        )
        write(
            tmp_path,
            "quality_negative.json",
            stand_export.export_quality(client, CASES),
        )
        listing = [
            r for r in fake.requests if r["path"] == "/api/v1/dataQuality/testCases"
        ]
        assert (
            listing[0]["query"]["entityLink"]
            == "<#E::table::p17_picking.picking.picking.orders>"
        )
        assert listing[0]["query"]["fields"] == "testCaseResult,testDefinition"
    return tmp_path


def test_verify_passes_on_a_complete_run(successful_run):
    quality = json.loads((successful_run / "quality.json").read_text(encoding="utf-8"))
    assert quality["credit_sum"]["values"] == {"resultRowCount": "0"}
    report = stand_export.verify(successful_run, CATALOG, CASES)
    assert report["ok"], [c for c in report["checks"] if not c["ok"]]
    assert len(report["checks"]) == 12


def test_verify_names_each_failed_criterion(successful_run):
    write(successful_run, "apply_2.json", {"changes": 3})
    (successful_run / "lineage.json").unlink()
    write(
        successful_run, "quality_negative.json", {"credit_sum": {"status": "Success"}}
    )
    report = stand_export.verify(successful_run, CATALOG, CASES)
    failed = {c["check"] for c in report["checks"] if not c["ok"]}
    assert failed == {
        "second apply changed nothing",
        "lineage edges from catalog.json present",
        "quality credit_sum failed on one broken group",
    }
    assert not report["ok"]


def test_missing_test_case_is_reported():
    with FakeOpenMetadata() as fake:
        client = om_client.Client(fake.url, ADMIN_TOKEN)
        quality = stand_export.export_quality(client, CASES)
    assert quality["credit_sum"]["status"] == "missing"


def test_empty_second_ingestion_fails_acceptance(successful_run):
    """The state right after apply, passed off as the export after re-ingestion."""
    applied = json.loads(
        (successful_run / "applied_tables.json").read_text(encoding="utf-8")
    )
    write(successful_run, "after_tables.json", applied)
    write(
        successful_run,
        "after_completeness.json",
        completeness.completeness(OBJECTS, applied, include_columns=True),
    )
    report = stand_export.verify(successful_run, CATALOG, CASES)
    failed = [c for c in report["checks"] if not c["ok"]]
    assert [c["check"] for c in failed] == [
        "second ingestion reached every catalog table"
    ]
    assert "0 of 7" in failed[0]["detail"]
    assert not report["ok"]


def test_missing_probe_file_fails_acceptance(successful_run):
    (successful_run / "reingest_probe.json").unlink()
    report = stand_export.verify(successful_run, CATALOG, CASES)
    failed = {c["check"] for c in report["checks"] if not c["ok"]}
    assert failed == {"second ingestion reached every catalog table"}


def test_second_ingestion_that_skips_a_table_is_named(successful_run):
    after = json.loads((successful_run / "after_tables.json").read_text("utf-8"))
    skipped = "p16_marketing.default.marketing_jan.spend"
    for table in after:
        if table["fullyQualifiedName"] == skipped:
            for column in table["columns"]:
                column.pop("description", None)
    write(successful_run, "after_tables.json", after)
    report = stand_export.verify(successful_run, CATALOG, CASES)
    failed = [c for c in report["checks"] if not c["ok"]]
    assert [c["check"] for c in failed] == [
        "second ingestion reached every catalog table"
    ]
    assert "6 of 7" in failed[0]["detail"] and skipped in failed[0]["detail"]


def test_reingestion_that_drops_applied_metadata_fails(successful_run):
    after = json.loads((successful_run / "after_tables.json").read_text("utf-8"))
    changed = "p17_picking.picking.picking.order_facts"
    for table in after:
        if table["fullyQualifiedName"] == changed:
            table["owners"] = []
            table["columns"][1]["tags"] = []
    write(successful_run, "after_tables.json", after)
    write(
        successful_run,
        "after_completeness.json",
        completeness.completeness(OBJECTS, after, include_columns=True),
    )
    report = stand_export.verify(successful_run, CATALOG, CASES)
    failed = {c["check"]: c["detail"] for c in report["checks"] if not c["ok"]}
    assert set(failed) == {
        "re-ingestion kept the applied metadata",
        "all objects complete after re-ingestion",
        "all column terms confirmed after re-ingestion",
    }
    assert changed in failed["re-ingestion kept the applied metadata"]


def test_probe_sql_for_both_sources():
    probe = stand_export.reingestion_probe(CATALOG, "run 'a'")
    assert probe["columns"] == {
        fqn: next(iter(spec["column_terms"])) for fqn, spec in OBJECTS.items()
    }
    postgres = probe["sql"]["postgres"].splitlines()
    assert postgres == [
        "COMMENT ON TABLE picking.orders IS "
        "'source comment run ''a'', must not replace the catalog text';",
        "COMMENT ON COLUMN picking.orders.order_id IS 're-ingestion probe run ''a''';",
        "COMMENT ON VIEW picking.order_facts IS "
        "'source comment run ''a'', must not replace the catalog text';",
        "COMMENT ON COLUMN picking.order_facts.order_id IS "
        "'re-ingestion probe run ''a''';",
    ]
    clickhouse = probe["sql"]["clickhouse"].splitlines()
    assert len(clickhouse) == 10
    assert (
        "ALTER TABLE marketing_jan.attribution COMMENT COLUMN weight "
        "'re-ingestion probe run ''a''';"
    ) in clickhouse
    assert clickhouse[0].startswith("ALTER TABLE marketing_jan.events MODIFY COMMENT ")
