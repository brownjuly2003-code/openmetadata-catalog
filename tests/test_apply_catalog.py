import base64
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from fake_om import ADMIN_TOKEN, BOT_TOKEN, FakeOpenMetadata, json_patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import apply_catalog  # noqa: E402
import completeness  # noqa: E402
import om_client  # noqa: E402
import stand_export  # noqa: E402

CATALOG = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
OBJECTS = CATALOG["objects"]


def ingested(fake):
    """The seven tables as the ingestion would leave them: no catalog metadata."""
    for fqn, spec in OBJECTS.items():
        fake.add_table(fqn, ["loaded_at", *spec["column_terms"], "note"])


@pytest.fixture
def fake():
    with FakeOpenMetadata() as server:
        ingested(server)
        yield server


def client(fake, token=ADMIN_TOKEN):
    return om_client.Client(fake.url, token)


def test_first_run_writes_the_whole_catalog(fake):
    summary = apply_catalog.apply(client(fake), CATALOG)

    writes = [(r["method"], r["path"]) for r in fake.writes()]
    assert writes.count(("PUT", "/api/v1/teams")) == 2
    assert writes.count(("PUT", "/api/v1/glossaries")) == 1
    assert writes.count(("PUT", "/api/v1/glossaryTerms")) == 7
    assert sum(m == "PATCH" for m, _ in writes) == 7
    assert writes.count(("PUT", "/api/v1/lineage")) == 6
    assert summary["changes"] == 2 + 1 + 7 + 7 + 6
    assert all(
        r["headers"]["Authorization"] == f"Bearer {ADMIN_TOKEN}" for r in fake.requests
    )

    team = next(r for r in fake.writes() if r["path"] == "/api/v1/teams")
    assert team["body"] == CATALOG["teams"][0]
    term = next(r for r in fake.writes() if r["path"] == "/api/v1/glossaryTerms")
    assert term["body"] == CATALOG["terms"][0]

    orders = fake.tables["p17_picking.picking.picking.orders"]
    patch = next(
        r for r in fake.writes() if r["path"] == f"/api/v1/tables/{orders['id']}"
    )
    assert patch["headers"]["Content-Type"] == "application/json-patch+json"
    team_id = fake.teams["p28_fulfillment"]["id"]
    label = {
        "tagFQN": "LabMetrics.PickingOrder",
        "source": "Glossary",
        "labelType": "Manual",
        "state": "Confirmed",
    }
    assert patch["body"] == [
        {
            "op": "add",
            "path": "/description",
            "value": OBJECTS["p17_picking.picking.picking.orders"]["description"],
        },
        {"op": "add", "path": "/owners", "value": [{"id": team_id, "type": "team"}]},
        {"op": "add", "path": "/tags", "value": [label]},
        {"op": "add", "path": "/columns/1/tags", "value": [label]},
    ]

    edge = next(r for r in fake.writes() if r["path"] == "/api/v1/lineage")
    assert set(edge["body"]["edge"]) == {"fromEntity", "toEntity"}
    assert edge["body"]["edge"]["fromEntity"]["type"] == "table"


def test_second_run_only_reads_and_the_catalog_is_complete(fake):
    apply_catalog.apply(client(fake), CATALOG)
    before = len(fake.requests)

    summary = apply_catalog.apply(client(fake), CATALOG)

    assert summary["changes"] == 0
    assert all(r["method"] == "GET" for r in fake.requests[before:])
    assert len(summary["lineage"]["present"]) == 6
    assert len(fake.edges) == 6

    tables = stand_export.export_tables(client(fake), CATALOG)
    result = completeness.completeness(OBJECTS, tables, include_columns=True)
    assert result["counts"]["complete"] == 7
    assert (
        result["column_terms"]["confirmed"] == result["column_terms"]["expected"] == 9
    )
    lineage = stand_export.export_lineage(client(fake), CATALOG)
    wanted = sorted(
        [up, fqn] for fqn, spec in OBJECTS.items() for up in spec["upstream"]
    )
    assert lineage["edges"] == wanted


def test_only_differences_are_written(fake):
    view = "p17_picking.picking.picking.order_facts"
    orders = fake.tables["p17_picking.picking.picking.orders"]
    table = fake.tables[view]
    fake.edges.append((orders["id"], table["id"]))
    table["description"] = OBJECTS[view]["description"]
    suggested = {
        "tagFQN": "LabMetrics.PickingDuration",
        "source": "Glossary",
        "labelType": "Automated",
        "state": "Suggested",
    }
    other = {"tagFQN": "PII.None", "source": "Classification"}
    table["tags"] = [other]
    table["columns"][2]["tags"] = [suggested]

    summary = apply_catalog.apply(client(fake), CATALOG)

    assert summary["tables"][view] == [
        "/owners",
        "/tags/-",
        "/columns/1/tags",
        "/columns/2/tags/0",
    ]
    assert len(summary["lineage"]["added"]) == 5
    assert ["p17_picking.picking.picking.orders", view] in summary["lineage"]["present"]
    assert fake.tables[view]["tags"][0] == other
    assert fake.tables[view]["columns"][2]["tags"][0]["state"] == "Confirmed"
    assert len(fake.tables[view]["columns"][2]["tags"]) == 1


def test_classification_label_with_the_term_name_is_kept():
    """The saved events card holds a Classification label named like the term."""
    snapshot = json.loads(
        (ROOT / "examples" / "partial-tables.json").read_text(encoding="utf-8")
    )
    fqn = "p16_marketing.default.marketing_jan.events"
    card = next(t for t in snapshot if t["fullyQualifiedName"] == fqn)
    spec = OBJECTS[fqn]
    classification = dict(card["tags"][0])
    assert classification["source"] == "Classification"
    assert classification["tagFQN"] == spec["term"]

    ops = apply_catalog.table_ops(card, spec, "team-id")

    assert all(op["op"] == "add" for op in ops), ops
    assert {"/tags/-", "/columns/0/tags/-"} <= {op["path"] for op in ops}
    patched = json_patch(card, ops)
    for labels in (patched["tags"], patched["columns"][0]["tags"]):
        assert labels[0] == classification
        assert labels[1] == apply_catalog.term_label(spec["term"])
    assert apply_catalog.table_ops(patched, spec, "team-id") == []


def test_missing_table_and_column_stop_the_run(fake):
    del fake.tables["p16_marketing.default.marketing_jan.spend"]
    with pytest.raises(apply_catalog.CatalogError, match="table not found"):
        apply_catalog.apply(client(fake), CATALOG)

    ingested(fake)
    fake.tables["p16_marketing.default.marketing_jan.spend"]["columns"] = []
    with pytest.raises(apply_catalog.CatalogError, match="column spend_cents"):
        apply_catalog.apply(client(fake), CATALOG)


def test_term_that_is_not_approved_stops_the_run(fake):
    fake.terms["LabMetrics.PickingOrder"] = {
        "id": "t1",
        "fullyQualifiedName": "LabMetrics.PickingOrder",
        "displayName": CATALOG["terms"][0]["displayName"],
        "description": CATALOG["terms"][0]["description"],
        "entityStatus": "Draft",
    }
    with pytest.raises(apply_catalog.CatalogError, match="Draft"):
        apply_catalog.apply(client(fake), CATALOG)


def test_api_error_is_raised_with_status(fake):
    fake.failures[("PATCH", "/api/v1/tables/")] = (500, {"message": "boom"})
    with pytest.raises(om_client.ApiError) as error:
        apply_catalog.apply(client(fake), CATALOG)
    assert error.value.status == 500
    assert error.value.method == "PATCH"


def test_command_line_reports_api_error(fake):
    fake.failures[("PATCH", "/api/v1/tables/")] = (500, {"message": "boom"})
    env = dict(os.environ, OM_API_URL=fake.url, OM_ADMIN_TOKEN=ADMIN_TOKEN)
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/apply_catalog.py")],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 2
    assert "HTTP 500" in result.stderr


def test_command_line_expect_unchanged(fake, tmp_path):
    env = dict(os.environ, OM_API_URL=fake.url, OM_ADMIN_TOKEN=ADMIN_TOKEN)
    command = [
        sys.executable,
        str(ROOT / "scripts/apply_catalog.py"),
        "--expect-unchanged",
    ]
    first = subprocess.run(command, capture_output=True, text=True, env=env, timeout=30)
    assert first.returncode == 3
    out = tmp_path / "apply_2.json"
    second = subprocess.run(
        [*command, "--out", str(out)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert second.returncode == 0, second.stderr
    assert json.loads(out.read_text(encoding="utf-8"))["changes"] == 0


def test_login_sends_base64_password_and_reads_the_bot_token(fake, tmp_path):
    anonymous = client(fake, token=None)
    token = om_client.login(anonymous, "admin@open-metadata.org", "admin")
    assert token == ADMIN_TOKEN
    login = fake.requests[-1]
    assert login["method"] == "POST"
    assert login["body"]["password"] == base64.b64encode(b"admin").decode("ascii")

    assert om_client.bot_token(client(fake)) == BOT_TOKEN
    assert fake.requests[-2]["path"] == "/api/v1/bots/name/ingestion-bot"
    assert (
        fake.requests[-1]["path"] == f"/api/v1/users/auth-mechanism/{fake.bot_user_id}"
    )

    with pytest.raises(om_client.ApiError) as error:
        om_client.login(anonymous, "admin@open-metadata.org", "wrong")
    assert error.value.status == 401


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_token_file_is_private(tmp_path):
    target = tmp_path / ".tokens"
    om_client.write_tokens(target, "a", "b")
    assert (
        target.read_text(encoding="utf-8") == "OM_ADMIN_TOKEN=a\nOM_INGESTION_TOKEN=b\n"
    )
    assert target.stat().st_mode & 0o077 == 0
