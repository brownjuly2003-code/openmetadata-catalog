"""Static checks of the stand: nothing here starts a container."""

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
STAND = ROOT / "stand"
COMPOSE = yaml.safe_load((STAND / "docker-compose.yml").read_text(encoding="utf-8"))
UPSTREAM_FILE = STAND / "upstream" / "docker-compose-postgres.yml"
UPSTREAM = yaml.safe_load(UPSTREAM_FILE.read_text(encoding="utf-8"))
UPSTREAM_SHA256 = "b0013093c8a78f7e877fb258fd7ba688adac3effdc65958569a5f9bd310d29ee"
OM_SERVICES = (
    "postgresql",
    "elasticsearch",
    "execute-migrate-all",
    "openmetadata-server",
)
SOURCES = ("source-postgres", "source-clickhouse")
# Deviations from the release asset, each explained in the header of the compose file.
REGISTRY_MOVE = ("docker.getcollate.io/openmetadata/", "openmetadata/")
ENV_OVERRIDES = {"PIPELINE_SERVICE_CLIENT_ENABLED": "false"}
SERVER_HEALTH_ADDED = {"interval", "timeout", "retries", "start_period"}
DIGEST = re.compile(r"^[a-z0-9./_-]+:[\w.-]+@sha256:[0-9a-f]{64}$")


def test_upstream_copy_is_the_release_asset():
    data = UPSTREAM_FILE.read_bytes()
    assert hashlib.sha256(data).hexdigest() == UPSTREAM_SHA256


def test_stand_name_and_services():
    assert COMPOSE["name"] == "openmetadata-catalog-stand"
    assert set(COMPOSE["services"]) == set(OM_SERVICES) | set(SOURCES)
    assert "ingestion" not in COMPOSE["services"]


@pytest.mark.parametrize("service", sorted(OM_SERVICES + SOURCES))
def test_every_image_is_pinned_by_tag_and_digest(service):
    image = COMPOSE["services"][service]["image"]
    assert DIGEST.match(image), image
    assert ":latest" not in image


@pytest.mark.parametrize("service", OM_SERVICES)
def test_openmetadata_services_follow_the_release(service):
    ours = COMPOSE["services"][service]
    theirs = UPSTREAM["services"][service]
    expected_image = theirs["image"].replace(*REGISTRY_MOVE)
    assert ours["image"].split("@", 1)[0] == expected_image
    assert ours.get("command") == theirs.get("command")
    environment = theirs["environment"]
    if isinstance(environment, dict):
        overrides = {k: v for k, v in ENV_OVERRIDES.items() if k in environment}
        environment = {**environment, **overrides}
    assert ours["environment"] == environment
    if "healthcheck" in theirs:
        health = ours["healthcheck"]
        for key, value in theirs["healthcheck"].items():
            assert health[key] == value
        added = set(health) - set(theirs["healthcheck"])
        assert added <= (
            SERVER_HEALTH_ADDED if service == "openmetadata-server" else set()
        )
    assert ours.get("depends_on") == theirs.get("depends_on")


def test_external_ingestion_only():
    for service in ("execute-migrate-all", "openmetadata-server"):
        env = COMPOSE["services"][service]["environment"]
        assert env["PIPELINE_SERVICE_CLIENT_ENABLED"] == "false"


def test_published_ports_are_local():
    for name, service in COMPOSE["services"].items():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), (name, port)
    assert "ports" not in COMPOSE["services"]["postgresql"]
    assert "ports" not in COMPOSE["services"]["elasticsearch"]


def test_source_images_match_the_versions_already_pinned():
    cases = (ROOT / "tests" / "quality_sql_cases.py").read_text(encoding="utf-8")
    clickhouse = COMPOSE["services"]["source-clickhouse"]["image"]
    # The literal in quality_sql_cases.py is split after "@" across two lines.
    name, digest = clickhouse.split("@")
    assert f'"{name}@"' in cases and f'"{digest}"' in cases
    assert COMPOSE["services"]["source-postgres"]["image"].startswith(
        "postgres:16.10-alpine@"
    )


def test_volume_sources_exist():
    for service in COMPOSE["services"].values():
        for volume in service.get("volumes", []):
            source = volume.split(":", 1)[0]
            if source.startswith("./"):
                assert (STAND / source).exists(), source
            else:
                assert source in COMPOSE["volumes"], source


def test_env_example_covers_required_variables():
    text = (STAND / "docker-compose.yml").read_text(encoding="utf-8")
    required = set(re.findall(r"\$\{(\w+):\?", text))
    example = (STAND / ".env.example").read_text(encoding="utf-8")
    defined = set(re.findall(r"^(\w+)=", example, flags=re.MULTILINE))
    assert required <= defined
    assert "CHANGE_ME" in example
    om_variables = set(re.findall(r"\$\{(\w+):-", text))
    assert not (defined & om_variables) - {"SOURCE_PG_PORT", "SOURCE_CH_PORT"}


def test_fixture_sources_and_checksums():
    sources = json.loads((STAND / "fixtures" / "SOURCES.json").read_text("utf-8"))
    files = sources["copies"] + sources["derived"]
    for item in files:
        data = (ROOT / item["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == item["sha256"], item["file"]
        assert re.fullmatch(r"[0-9a-f]{40}", item["commit"])
    for item in sources["adapted"]:
        assert (ROOT / item["file"]).exists()
    orders = (STAND / "fixtures" / "postgres" / "orders_data.sql").read_text("utf-8")
    assert orders.count("\n    (") == 200
    events = (STAND / "fixtures" / "clickhouse" / "events.jsonl").read_text("utf-8")
    assert len(events.splitlines()) == 488


def run_sh_paths():
    text = (STAND / "run.sh").read_text(encoding="utf-8")
    return sorted(set(re.findall(r"\b(?:scripts|workflows|stand)/[\w./-]+\w", text)))


def test_paths_used_by_run_sh_exist():
    generated = {"stand/.env", "stand/.tokens"}
    paths = [
        p
        for p in run_sh_paths()
        if p not in generated and not p.startswith("stand/out")
    ]
    assert "scripts/apply_catalog.py" in paths
    assert "workflows/quality-attribution.yaml" in paths
    for path in paths:
        assert (ROOT / path).exists(), path
    for name in ("docker-compose.yml", ".env.example"):
        assert (STAND / name).exists()


def test_run_sh_has_strict_mode_and_teardown():
    text = (STAND / "run.sh").read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    assert "trap finish EXIT" in text
    assert "down -v" in text
    assert "SUMMARY.txt" in text


def test_run_sh_order_of_checks_and_cleanup():
    """Absent tables stop step 8; the negative control row is removed both ways."""
    text = (STAND / "run.sh").read_text(encoding="utf-8")
    steps = {m.group(1): m.start() for m in re.finditer(r'^step "(\d+)\.', text, re.M)}
    require = text.index("completeness.py --columns --require-present")
    assert steps["8"] < require < steps["9"]
    probe = text.index("stand_export.py probe")
    second_ingest = text.index("ingest-postgres-2.log")
    assert steps["10"] < probe < second_ingest
    calls = [m.start() for m in re.finditer(r"^remove_negative_control$", text, re.M)]
    insert = text.index("INSERT INTO marketing_jan.attribution")
    negative = text.index('--out "$OUT/quality_negative.json"')
    assert len(calls) == 2
    assert steps["11"] < calls[0] < insert < negative < calls[1] < steps["12"]
    assert "--mutations_sync=2" in text


def test_outputs_and_secrets_are_ignored():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for entry in ("stand/out/", "stand/.env", "stand/.tokens", "stand/.venv/"):
        assert entry in ignored


@pytest.mark.skipif(sys.platform == "win32", reason="bash syntax check runs on Linux")
@pytest.mark.parametrize(
    "script", ["run.sh", "fixtures/postgres/reader.sh", "fixtures/clickhouse/init.sh"]
)
def test_shell_scripts_parse(script):
    bash = shutil.which("bash")
    assert bash, "bash is required for this test"
    result = subprocess.run(
        [bash, "-n", str(STAND / script)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


def test_devcontainer_is_pinned():
    config = json.loads(
        (ROOT / ".devcontainer" / "devcontainer.json").read_text("utf-8")
    )
    assert DIGEST.match(config["image"])
    for feature in config["features"]:
        assert re.search(r":\d+\.\d+\.\d+$", feature), feature
    assert any("docker-in-docker" in f for f in config["features"])
    assert any("sshd" in f for f in config["features"])
    python = next(v for f, v in config["features"].items() if "/python:" in f)
    assert python["version"] == "3.11"
    assert config["hostRequirements"] == {
        "cpus": 4,
        "memory": "16gb",
        "storage": "32gb",
    }


def test_fixture_builder_converts_time_zones_and_quotes(tmp_path):
    sys.path.insert(0, str(STAND / "fixtures"))
    import build_data

    orders = tmp_path / "orders.json"
    order = dict.fromkeys(build_data.ORDER_COLUMNS, 1)
    order.update(
        order_id="o'1", started_at=None, created_at="2026-01-01T00:00:00+00:00"
    )
    orders.write_text(json.dumps({"orders": [order]}), encoding="utf-8")
    marketing = tmp_path / "marketing.json"
    events = [
        {
            "event_id": "e2",
            "user_id": "u",
            "timestamp": "2026-01-01T03:00:00+03:00",
            "name": "click",
            "channel": "search",
            "campaign": None,
        },
        {
            "event_id": "e1",
            "user_id": "u",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "name": "subscription",
            "channel": None,
            "revenue_cents": 500,
        },
    ]
    spend = [{"channel": "search", "spend_cents": 7, "extra": 1}]
    marketing.write_text(
        json.dumps({"events": events, "spend": spend}), encoding="utf-8"
    )

    counts = build_data.build(orders, marketing, out=tmp_path)

    assert counts == {"orders": 1, "events": 2, "spend": 1}
    sql = (tmp_path / "postgres" / "orders_data.sql").read_text(encoding="utf-8")
    assert "('o''1', 1, 1, '2026-01-01T00:00:00+00:00', NULL, 1, 1, 1, 1, 1);" in sql
    rows = [
        json.loads(line)
        for line in (tmp_path / "clickhouse" / "events.jsonl")
        .read_text("utf-8")
        .splitlines()
    ]
    assert [r["event_id"] for r in rows] == ["e1", "e2"]
    assert rows[0]["at"] == rows[1]["at"] == "2026-01-01 00:00:00.000000"
    assert rows[0]["channel"] == "" and rows[0]["revenue_cents"] == 500
    assert rows[1]["campaign"] == "" and rows[1]["cost_cents"] == 0
    spend_rows = (tmp_path / "clickhouse" / "spend.jsonl").read_text("utf-8")
    assert json.loads(spend_rows) == {"channel": "search", "spend_cents": 7}
