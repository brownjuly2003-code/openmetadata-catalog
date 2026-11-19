"""Export what the stand holds and check the run against the success criteria.

    python scripts/stand_export.py tables --out out/before_tables.json
    python scripts/stand_export.py lineage --out out/lineage.json
    python scripts/stand_export.py quality --out out/quality.json
    python scripts/stand_export.py probe --stamp <run start> --out out/reingest_probe.json
    python scripts/stand_export.py verify --out-dir out

Endpoints and their sources (docs v2.0.x API reference, docs.open-metadata.org):
- GET /v1/tables/name/{fqn}?fields=owners,tags,columns&include=all:
  api-reference/data-assets/tables/retrieve.
- GET /v1/lineage/{entityType}/name/{fqn}?upstreamDepth=1&downstreamDepth=0:
  api-reference/lineage/get (response: entity, nodes, upstreamEdges with entity ids).
- GET /v1/dataQuality/testCases?entityLink=<#E::table::FQN>&fields=testCaseResult:
  api-reference/data-quality/test-cases/list; result fields testCaseStatus and
  testResultValue: api-reference/data-quality/test-cases/results.
The result value name resultRowCount comes from the 2.0.8 ingestion code
(data_quality/validations/table/base/tableCustomSQLQuery.py), not from the docs.

Needs OM_API_URL and OM_ADMIN_TOKEN for the export commands; probe and verify
work with files only. That a source column comment reaches an empty column
description on re-ingestion with overrideMetadata: false is an ASSUMPTION about
OpenMetadata 2.0.8 to be confirmed by the first stand run.
"""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from om_client import ApiError, client_from_env, quote  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "catalog.json"
QUALITY_WORKFLOWS = ("quality-orders.yaml", "quality-attribution.yaml")
NEGATIVE_CASE = "credit_sum"
VIEWS = {"p17_picking.picking.picking.order_facts"}


def load_catalog():
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def quality_cases():
    """(table FQN, test case name) pairs from the committed quality workflows."""
    import yaml

    cases = []
    for name in QUALITY_WORKFLOWS:
        config = yaml.safe_load((ROOT / "workflows" / name).read_text("utf-8"))
        table = config["source"]["sourceConfig"]["config"]["entityFullyQualifiedName"]
        for case in config["processor"]["config"]["testCases"]:
            cases.append((table, case["name"]))
    return cases


def export_tables(client, catalog):
    tables = []
    for fqn in catalog["objects"]:
        table = client.find(
            f"/v1/tables/name/{quote(fqn)}",
            {"fields": "owners,tags,columns", "include": "all"},
        )
        if table is not None:
            tables.append(table)
    return tables


def export_lineage(client, catalog):
    edges = set()
    for fqn in catalog["objects"]:
        lineage = client.find(
            f"/v1/lineage/table/name/{quote(fqn)}",
            {"upstreamDepth": 1, "downstreamDepth": 0},
        )
        if lineage is None:
            continue
        names = {}
        for node in [lineage.get("entity") or {}] + (lineage.get("nodes") or []):
            if node.get("id"):
                names[node["id"]] = node.get("fullyQualifiedName")
        for edge in lineage.get("upstreamEdges") or []:
            ends = []
            for key in ("fromEntity", "toEntity"):
                value = edge.get(key)
                ends.append(value.get("id") if isinstance(value, dict) else value)
            edges.add((names.get(ends[0], ends[0]), names.get(ends[1], ends[1])))
    return {"edges": sorted(list(edge) for edge in edges)}


def export_quality(client, cases):
    result = {}
    for table, name in cases:
        listing = client.get(
            "/v1/dataQuality/testCases",
            {
                "entityLink": f"<#E::table::{table}>",
                "fields": "testCaseResult,testDefinition",
                "limit": 50,
            },
        )
        found = [case for case in listing.get("data") or [] if case.get("name") == name]
        if not found:
            result[name] = {"table": table, "status": "missing"}
            continue
        outcome = found[0].get("testCaseResult") or {}
        result[name] = {
            "table": table,
            "fullyQualifiedName": found[0].get("fullyQualifiedName"),
            "status": outcome.get("testCaseStatus", "no result"),
            "values": {
                value.get("name"): value.get("value")
                for value in outcome.get("testResultValue") or []
            },
            "result": outcome.get("result"),
        }
    return result


def sql_text(value):
    """A string literal that PostgreSQL and ClickHouse read the same way."""
    if "\\" in value:
        raise ValueError("backslashes are not supported in probe comments")
    return "'" + value.replace("'", "''") + "'"


def reingestion_probe(catalog, stamp):
    """Source comments set between the two ingestions, unique to one run.

    Each catalog object gets a comment on one column, which only the second
    ingestion can carry into the catalog, and a table comment that must not
    replace the manual description (overrideMetadata: false).
    """
    column_comment = f"re-ingestion probe {stamp}"
    table_comment = f"source comment {stamp}, must not replace the catalog text"
    columns, sql = {}, {"postgres": [], "clickhouse": []}
    for fqn, spec in catalog["objects"].items():
        service, _, schema, table = fqn.split(".")
        column = next(iter(spec["column_terms"]))
        columns[fqn] = column
        if service == "p17_picking":
            kind = "VIEW" if fqn in VIEWS else "TABLE"
            sql["postgres"] += [
                f"COMMENT ON {kind} {schema}.{table} IS {sql_text(table_comment)};",
                f"COMMENT ON COLUMN {schema}.{table}.{column}"
                f" IS {sql_text(column_comment)};",
            ]
        else:
            sql["clickhouse"] += [
                f"ALTER TABLE {schema}.{table} MODIFY COMMENT {sql_text(table_comment)};",
                f"ALTER TABLE {schema}.{table} COMMENT COLUMN {column}"
                f" {sql_text(column_comment)};",
            ]
    return {
        "stamp": stamp,
        "column_comment": column_comment,
        "table_comment": table_comment,
        "columns": columns,
        "sql": {engine: "\n".join(lines) + "\n" for engine, lines in sql.items()},
    }


def column_description(table, name):
    for column in (table or {}).get("columns") or []:
        if column.get("name") == name:
            return column.get("description")
    return None


def manual_metadata(table, spec):
    """What apply_catalog.py writes: description, owners, glossary labels."""
    if table is None:
        return None

    def glossary(labels):
        return sorted(
            (label.get("tagFQN"), label.get("state"))
            for label in labels or []
            if label.get("source") == "Glossary"
        )

    columns = {c.get("name"): c for c in table.get("columns") or []}
    return {
        "description": table.get("description"),
        "owners": sorted(
            (o.get("id"), o.get("type")) for o in table.get("owners") or []
        ),
        "tags": glossary(table.get("tags")),
        "columns": {
            name: glossary((columns.get(name) or {}).get("tags"))
            for name in spec["column_terms"]
        },
    }


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def verify(out_dir, catalog, cases):
    """Success criteria of one run; each check names the file it reads."""
    out = Path(out_dir)
    objects = catalog["objects"]
    checks = []

    def check(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    before = read_json(out / "before_completeness.json") or {}
    present = (before.get("counts") or {}).get("present")
    check(
        "ingestion found all catalog tables",
        present == len(objects),
        f"before_completeness.json present={present} of {len(objects)}",
    )

    tables = {
        t.get("fullyQualifiedName"): t
        for t in read_json(out / "before_tables.json") or []
    }
    (view,) = VIEWS
    table_type = (tables.get(view) or {}).get("tableType")
    check(
        "order_facts ingested as a view",
        table_type == "View",
        f"tableType={table_type}",
    )

    second = read_json(out / "apply_2.json") or {}
    check(
        "second apply changed nothing",
        second.get("changes") == 0,
        f"apply_2.json changes={second.get('changes')}",
    )

    applied_tables = {
        t.get("fullyQualifiedName"): t
        for t in read_json(out / "applied_tables.json") or []
    }
    after_tables = {
        t.get("fullyQualifiedName"): t
        for t in read_json(out / "after_tables.json") or []
    }
    probe = read_json(out / "reingest_probe.json") or {}
    probe_columns = probe.get("columns") or {}
    comment = probe.get("column_comment")
    unreached = [
        fqn
        for fqn in objects
        if not comment
        or fqn not in probe_columns
        or column_description(applied_tables.get(fqn), probe_columns[fqn]) == comment
        or column_description(after_tables.get(fqn), probe_columns[fqn]) != comment
    ]
    check(
        "second ingestion reached every catalog table",
        not unreached,
        f"probe column comment in after_tables.json, not in applied_tables.json: "
        f"{len(objects) - len(unreached)} of {len(objects)}; missing={unreached}",
    )
    changed = [
        fqn
        for fqn, spec in objects.items()
        if manual_metadata(applied_tables.get(fqn), spec) is None
        or manual_metadata(applied_tables.get(fqn), spec)
        != manual_metadata(after_tables.get(fqn), spec)
    ]
    check(
        "re-ingestion kept the applied metadata",
        not changed,
        f"applied_tables.json vs after_tables.json: description, owners, glossary "
        f"labels; differing={changed}",
    )

    after = read_json(out / "after_completeness.json") or {}
    counts = after.get("counts") or {}
    columns = after.get("column_terms") or {}
    check(
        "all objects complete after re-ingestion",
        counts.get("complete") == len(objects),
        f"after_completeness.json complete={counts.get('complete')} of {len(objects)}",
    )
    expected_columns = sum(len(spec["column_terms"]) for spec in objects.values())
    check(
        "all column terms confirmed after re-ingestion",
        columns.get("confirmed") == expected_columns,
        f"confirmed={columns.get('confirmed')} of {expected_columns}",
    )

    differing = [
        fqn
        for fqn, spec in objects.items()
        if (after_tables.get(fqn) or {}).get("description") != spec["description"]
    ]
    check("descriptions equal catalog.json", not differing, f"differing={differing}")

    lineage = read_json(out / "lineage.json") or {}
    edges = {tuple(edge) for edge in lineage.get("edges") or []}
    wanted = {(up, fqn) for fqn, spec in objects.items() for up in spec["upstream"]}
    missing = sorted(list(edge) for edge in wanted - edges)
    check(
        "lineage edges from catalog.json present",
        not missing,
        f"{len(wanted) - len(missing)} of {len(wanted)}; missing={missing}",
    )

    quality = read_json(out / "quality.json") or {}
    for _, name in cases:
        entry = quality.get(name) or {}
        values = entry.get("values") or {}
        check(
            f"quality {name} passed on clean data",
            entry.get("status") == "Success" and values.get("resultRowCount") == "0",
            f"status={entry.get('status')} values={values}",
        )

    negative = (read_json(out / "quality_negative.json") or {}).get(NEGATIVE_CASE) or {}
    values = negative.get("values") or {}
    check(
        f"quality {NEGATIVE_CASE} failed on one broken group",
        negative.get("status") == "Failed" and values.get("resultRowCount") == "1",
        f"status={negative.get('status')} values={values}",
    )
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("tables", "lineage", "quality"):
        sub.add_parser(name).add_argument("--out", type=Path, required=True)
    sub.add_parser("verify").add_argument("--out-dir", type=Path, required=True)
    probe = sub.add_parser("probe")
    probe.add_argument("--stamp", required=True)
    probe.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    catalog = load_catalog()
    try:
        if args.command == "probe":
            result = reingestion_probe(catalog, args.stamp)
            target = args.out
        elif args.command == "verify":
            result = verify(args.out_dir, catalog, quality_cases())
            target = args.out_dir / "verify.json"
        else:
            client = client_from_env()
            if args.command == "tables":
                result = export_tables(client, catalog)
            elif args.command == "lineage":
                result = export_lineage(client, catalog)
            else:
                result = export_quality(client, quality_cases())
            target = args.out
    except ApiError as error:
        parser.exit(2, f"{parser.prog}: {error}\n")
    target.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"{args.command}: written {target}")
    if args.command == "verify":
        for item in result["checks"]:
            print(
                ("PASS " if item["ok"] else "FAIL ")
                + item["check"]
                + ": "
                + item["detail"]
            )
        if not result["ok"]:
            parser.exit(1, f"{parser.prog}: some checks failed\n")


if __name__ == "__main__":
    main()
