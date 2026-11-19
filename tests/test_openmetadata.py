import json
from pathlib import Path

import pytest
from metadata.config.common import load_config_file
from metadata.data_quality.api.models import TestSuiteProcessorConfig as SuiteProcessor
from metadata.generated.schema.api.data.createGlossary import CreateGlossaryRequest
from metadata.generated.schema.api.data.createGlossaryTerm import (
    CreateGlossaryTermRequest,
)
from metadata.generated.schema.api.teams.createTeam import CreateTeamRequest
from metadata.generated.schema.entity.services.connections.database.clickhouseConnection import (
    ClickhouseConnection,
)
from metadata.generated.schema.entity.services.connections.database.postgresConnection import (
    PostgresConnection,
)
from metadata.ingestion.api.parser import parse_workflow_config_gracefully
from metadata.utils.filters import filter_by_schema, filter_by_table
from metadata.utils.helpers import is_safe_sql_query


@pytest.mark.parametrize(
    ("file", "table", "check"),
    [
        ("orders", "p17_picking.picking.picking.orders", "order_quantities"),
        (
            "attribution",
            "p16_marketing.default.marketing_jan.attribution",
            "credit_sum",
        ),
    ],
)
def test_quality_workflow(monkeypatch, file, table, check):
    monkeypatch.setenv("OM_API_URL", "https://catalog.invalid/api")
    monkeypatch.setenv("OM_INGESTION_TOKEN", "quality-test-value")
    root = Path(__file__).resolve().parents[1]
    raw = load_config_file(root / "workflows" / f"quality-{file}.yaml")
    parsed = parse_workflow_config_gracefully(raw)
    pipeline = parsed.source.sourceConfig.config
    assert pipeline.entityFullyQualifiedName.root == table
    assert table in json.loads((root / "catalog.json").read_text())["objects"]
    assert parsed.source.serviceName == table.split(".")[0]
    assert [name.root for name in pipeline.testCases] == [check]
    assert raw["workflowConfig"]["openMetadataServerConfig"]["securityConfig"] == {
        "jwtToken": "quality-test-value"
    }
    assert parsed.processor.type == "orm-test-runner"
    assert parsed.sink.type == "metadata-rest"
    processor = SuiteProcessor.model_validate(raw["processor"]["config"])
    (case,) = processor.testCases
    assert case.name == check
    assert case.testDefinitionName == "tableCustomSQLQuery"
    parameters = {p.name: p.value for p in case.parameterValues}
    sql = parameters.pop("sqlExpression")
    assert is_safe_sql_query(sql)
    assert parameters == {"strategy": "COUNT", "operator": "<=", "threshold": "0"}


def test_catalog_definitions():
    path = Path(__file__).resolve().parents[1] / "catalog.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    glossary = CreateGlossaryRequest.model_validate(catalog["glossary"]).name.root
    teams = [CreateTeamRequest.model_validate(t).name.root for t in catalog["teams"]]
    terms = [CreateGlossaryTermRequest.model_validate(t) for t in catalog["terms"]]
    assert len(set(teams)) == len(teams) == 2
    assert {t.glossary.root for t in terms} == {glossary}
    term_names = {f"{glossary}.{t.name.root}" for t in terms}
    assert len(term_names) == len(terms) == 7
    objects = catalog["objects"]
    for fqn, spec in objects.items():
        assert spec["description"].strip()
        assert spec["owner"] in teams
        assert spec["term"] in term_names
        assert set(spec["column_terms"].values()) <= term_names
        assert set(spec["upstream"]) <= objects.keys() - {fqn}
    assert {t["term"] for t in objects.values()} == term_names


@pytest.mark.parametrize(
    ("name", "prefix", "port", "model", "schema", "table"),
    [
        ("postgres", "PICKING_PG", 5432, PostgresConnection, "picking", "order_facts"),
        (
            "clickhouse",
            "MARKETING_CH",
            8123,
            ClickhouseConnection,
            "marketing_jan",
            "channel_metrics",
        ),
    ],
)
def test_native_workflow(monkeypatch, name, prefix, port, model, schema, table):
    # Reserved .invalid hosts keep these tests independent of deployed services.
    host = f"db.invalid:{port}"
    password = """test: # "double" and 'single'"""
    monkeypatch.setenv(f"{prefix}_HOST_PORT", host)
    monkeypatch.setenv(f"{prefix}_USER", "catalog_reader")
    monkeypatch.setenv(f"{prefix}_PASSWORD", password)
    monkeypatch.setenv("OM_API_URL", "https://catalog.invalid/api")
    monkeypatch.setenv("OM_INGESTION_TOKEN", "unit-test-value")
    path = Path(__file__).resolve().parents[1] / "workflows" / f"{name}.yaml"
    raw = load_config_file(path)
    config = raw["source"]["serviceConnection"]["config"]
    assert config.get("authType", config)["password"] == password
    assert raw["workflowConfig"]["openMetadataServerConfig"]["securityConfig"] == {
        "jwtToken": "unit-test-value"
    }

    parsed = parse_workflow_config_gracefully(raw)
    conn = parsed.source.serviceConnection.root.config
    assert isinstance(conn, model)
    assert conn.hostPort == host
    assert conn.username == "catalog_reader"
    pipeline = parsed.source.sourceConfig.config
    assert not filter_by_schema(pipeline.schemaFilterPattern, schema)
    assert filter_by_schema(pipeline.schemaFilterPattern, "information_schema")
    assert not filter_by_table(pipeline.tableFilterPattern, table)
    assert filter_by_table(pipeline.tableFilterPattern, f"{table}_archive")
    assert pipeline.overrideMetadata is False
    assert not any(
        (
            pipeline.markDeletedDatabases,
            pipeline.markDeletedSchemas,
            pipeline.markDeletedTables,
        )
    )
