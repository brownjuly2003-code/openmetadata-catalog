"""Measure table metadata coverage against the seven expected catalog entries."""

import argparse
import json
from pathlib import Path


CATALOG = Path(__file__).resolve().parents[1] / "catalog.json"


def references(entity, field):
    values = entity.get(field)
    if values is None:
        return []
    if not isinstance(values, list) or not all(isinstance(v, dict) for v in values):
        name = entity.get("fullyQualifiedName") or entity.get("name")
        raise ValueError(f"{name}: {field} must be an array of objects")
    return values


def has_confirmed_term(entity, term):
    return any(
        label.get("source") == "Glossary"
        and label.get("state") == "Confirmed"
        and label.get("tagFQN") == term
        for label in references(entity, "tags")
    )


def column_term_statuses(targets, table):
    if table is None:
        return dict.fromkeys(targets, "missing_table")
    if table.get("deleted") is True:
        return dict.fromkeys(targets, "deleted_table")
    if table.get("columns") is None:
        return dict.fromkeys(targets, "columns_unavailable")

    statuses = dict.fromkeys(targets, "missing_column")
    for column in references(table, "columns"):
        name = column.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"{table['fullyQualifiedName']}: column needs a name")
        if name not in targets:
            continue
        if statuses[name] != "missing_column":
            raise ValueError(f"{table['fullyQualifiedName']}: duplicate column {name}")
        if column.get("tags") is None:
            statuses[name] = "tags_unavailable"
        elif has_confirmed_term(column, targets[name]):
            statuses[name] = "confirmed"
        else:
            statuses[name] = "unconfirmed"
    return statuses


def completeness(expected, snapshot, *, include_columns=False):
    if not isinstance(snapshot, list):
        raise ValueError(
            "expected a JSON array of table objects from the complete export"
        )
    tables = {}
    for table in snapshot:
        if not isinstance(table, dict) or not isinstance(
            table.get("fullyQualifiedName"), str
        ):
            raise ValueError("each table needs a fullyQualifiedName")
        name = table["fullyQualifiedName"]
        if name in expected:
            if name in tables:
                raise ValueError(f"duplicate table: {name}")
            tables[name] = table

    counts = dict.fromkeys(
        ("present", "description", "owner", "glossary", "complete"), 0
    )
    objects = {}
    for name, spec in expected.items():
        table = tables.get(name)
        if table is None or table.get("deleted") is True:
            objects[name] = ["missing" if table is None else "deleted"]
            continue
        counts["present"] += 1
        description = table.get("description")
        criteria = {
            "description": isinstance(description, str) and bool(description.strip()),
            "owner": any(
                ref.get("type") == "team"
                and ref.get("fullyQualifiedName") == spec["owner"]
                and not ref.get("deleted")
                for ref in references(table, "owners")
            ),
            "glossary": has_confirmed_term(table, spec["term"]),
        }
        objects[name] = [key for key, ok in criteria.items() if not ok]
        for key, ok in criteria.items():
            counts[key] += int(ok)
        counts["complete"] += int(not objects[name])
    result = {"expected": len(expected), "counts": counts, "objects": objects}
    if include_columns:
        columns = {
            name: column_term_statuses(spec["column_terms"], tables.get(name))
            for name, spec in expected.items()
        }
        result["column_terms"] = {
            "expected": sum(len(fields) for fields in columns.values()),
            "confirmed": sum(
                status == "confirmed"
                for fields in columns.values()
                for status in fields.values()
            ),
            "objects": columns,
        }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument(
        "--columns", action="store_true", help="include column glossary coverage"
    )
    parser.add_argument(
        "--require-present",
        action="store_true",
        help="exit with code 4 after the report if an expected table is missing",
    )
    args = parser.parse_args()
    try:
        expected = json.loads(CATALOG.read_text(encoding="utf-8"))["objects"]
        tables = json.loads(args.snapshot.read_text(encoding="utf-8"))
        result = completeness(expected, tables, include_columns=args.columns)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    absent = [
        name
        for name, gaps in result["objects"].items()
        if gaps in (["missing"], ["deleted"])
    ]
    if args.require_present and absent:
        parser.exit(4, f"{parser.prog}: not in the catalog: {', '.join(absent)}\n")


if __name__ == "__main__":
    main()
