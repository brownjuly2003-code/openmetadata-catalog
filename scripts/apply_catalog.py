"""Apply catalog.json to OpenMetadata 2.0.x through REST. Idempotent.

Every write is preceded by a read, and only differences are written: a second run
against the same server sends GET requests only and reports zero changes.

Endpoints and their sources (docs v2.0.x API reference, docs.open-metadata.org):
- GET /v1/teams/name/{fqn}, PUT /v1/teams ("create or update"):
  api-reference/teams-and-users/teams/retrieve and .../teams/update.
- GET /v1/glossaries/name/{fqn}, PUT /v1/glossaries:
  api-reference/governance/glossaries/retrieve and .../glossaries/update.
- GET /v1/glossaryTerms/name/{fqn}, PUT /v1/glossaryTerms:
  api-reference/governance/glossary-terms/retrieve and .../glossary-terms/update.
- GET /v1/tables/name/{fqn}?fields=owners,tags,columns and PATCH /v1/tables/{id}
  with Content-Type application/json-patch+json:
  api-reference/data-assets/tables/retrieve and .../tables/update.
  The docs show JSON Patch on /description only; the /owners, /tags and
  /columns/{i}/tags paths follow RFC 6902 against the table document and the
  tagLabel schema (tagFQN, source, labelType, state). UNCONFIRMED by documentation.
- GET /v1/lineage/{entityType}/name/{fqn} and PUT /v1/lineage with
  {"edge": {"fromEntity": {id, type}, "toEntity": {id, type}}}:
  api-reference/lineage/get and api-reference/lineage/add.

Glossary terms are created Approved when the glossary has no reviewers
(GlossaryTermRepository.java, tag 2.0.8-release), and only Approved terms can be
applied to assets (TagLabelUtil.checkGlossaryTermsApproved). The catalog sets no
reviewers; a term found in another state is reported as an error.

    python scripts/apply_catalog.py [--expect-unchanged] [--out summary.json]

Needs OM_API_URL and OM_ADMIN_TOKEN in the environment.
"""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from om_client import ApiError, client_from_env, quote  # noqa: E402

CATALOG = Path(__file__).resolve().parents[1] / "catalog.json"


class CatalogError(Exception):
    pass


def term_label(term):
    return {
        "tagFQN": term,
        "source": "Glossary",
        "labelType": "Manual",
        "state": "Confirmed",
    }


def is_confirmed(label, term):
    return (
        label.get("tagFQN") == term
        and label.get("source") == "Glossary"
        and label.get("state") == "Confirmed"
    )


def ensure(client, kind, fqn, body, compared, report):
    """Create or update a team, glossary or term when it is missing or differs."""
    current = client.find(f"/v1/{kind}/name/{quote(fqn)}")
    if current is not None and all(current.get(k) == body.get(k) for k in compared):
        report[fqn] = "unchanged"
        return current
    report[fqn] = "created" if current is None else "updated"
    return client.put(f"/v1/{kind}", body)


def label_ops(prefix, labels, term):
    """JSON Patch operations that leave exactly one confirmed glossary label for term.

    A label is matched by source and tagFQN together: a Classification label that
    happens to carry the same FQN is someone else's label and is kept as is.
    """
    if labels is None:
        return [{"op": "add", "path": prefix, "value": [term_label(term)]}]
    for index, label in enumerate(labels):
        if is_confirmed(label, term):
            return []
        if label.get("tagFQN") == term and label.get("source") == "Glossary":
            path = f"{prefix}/{index}"
            return [{"op": "replace", "path": path, "value": term_label(term)}]
    return [{"op": "add", "path": f"{prefix}/-", "value": term_label(term)}]


def table_ops(table, spec, team_id):
    fqn = table["fullyQualifiedName"]
    ops = []
    if table.get("description") != spec["description"]:
        ops.append({"op": "add", "path": "/description", "value": spec["description"]})
    owners = table.get("owners") or []
    if [(o.get("id"), o.get("type")) for o in owners] != [(team_id, "team")]:
        owner = [{"id": team_id, "type": "team"}]
        ops.append({"op": "add", "path": "/owners", "value": owner})
    ops += label_ops("/tags", table.get("tags"), spec["term"])
    columns = table.get("columns")
    if columns is None:
        raise CatalogError(f"{fqn}: the server returned no columns")
    positions = {column.get("name"): i for i, column in enumerate(columns)}
    for name, term in spec["column_terms"].items():
        if name not in positions:
            raise CatalogError(f"{fqn}: column {name} not found")
        index = positions[name]
        tags = columns[index].get("tags")
        ops += label_ops(f"/columns/{index}/tags", tags, term)
    return ops


def edge_ids(edges):
    pairs = set()
    for edge in edges or []:
        ends = []
        for key in ("fromEntity", "toEntity"):
            value = edge.get(key)
            ends.append(value.get("id") if isinstance(value, dict) else value)
        pairs.add(tuple(ends))
    return pairs


def apply(client, catalog):
    summary = {
        "teams": {},
        "glossary": {},
        "terms": {},
        "tables": {},
        "lineage": {"added": [], "present": []},
    }
    team_ids = {}
    for team in catalog["teams"]:
        body = {k: team[k] for k in ("name", "displayName", "teamType")}
        entity = ensure(
            client,
            "teams",
            team["name"],
            body,
            ("displayName", "teamType"),
            summary["teams"],
        )
        team_ids[team["name"]] = entity["id"]

    glossary = catalog["glossary"]
    ensure(
        client,
        "glossaries",
        glossary["name"],
        dict(glossary),
        ("displayName", "description"),
        summary["glossary"],
    )
    for term in catalog["terms"]:
        fqn = f"{term['glossary']}.{term['name']}"
        entity = ensure(
            client,
            "glossaryTerms",
            fqn,
            dict(term),
            ("displayName", "description"),
            summary["terms"],
        )
        status = entity.get("entityStatus")
        if status not in (None, "Approved"):
            raise CatalogError(
                f"{fqn}: term status {status}, only Approved can be used"
            )

    tables = {}
    for fqn, spec in catalog["objects"].items():
        table = client.find(
            f"/v1/tables/name/{quote(fqn)}", {"fields": "owners,tags,columns"}
        )
        if table is None:
            raise CatalogError(f"{fqn}: table not found, run the ingestion first")
        tables[fqn] = table
        ops = table_ops(table, spec, team_ids[spec["owner"]])
        summary["tables"][fqn] = [op["path"] for op in ops]
        if ops:
            client.patch(f"/v1/tables/{quote(table['id'])}", ops)

    for fqn, spec in catalog["objects"].items():
        if not spec["upstream"]:
            continue
        lineage = client.get(
            f"/v1/lineage/table/name/{quote(fqn)}",
            {"upstreamDepth": 1, "downstreamDepth": 0},
        )
        existing = edge_ids(lineage.get("upstreamEdges"))
        target = tables[fqn]["id"]
        for upstream in spec["upstream"]:
            source = tables[upstream]["id"]
            if (source, target) in existing:
                summary["lineage"]["present"].append([upstream, fqn])
                continue
            edge = {
                "fromEntity": {"id": source, "type": "table"},
                "toEntity": {"id": target, "type": "table"},
            }
            client.put("/v1/lineage", {"edge": edge})
            summary["lineage"]["added"].append([upstream, fqn])

    summary["changes"] = (
        sum(
            status != "unchanged"
            for key in ("teams", "glossary", "terms")
            for status in summary[key].values()
        )
        + sum(bool(paths) for paths in summary["tables"].values())
        + len(summary["lineage"]["added"])
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--catalog", type=Path, default=CATALOG)
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--expect-unchanged",
        action="store_true",
        help="exit with code 3 if anything had to be written",
    )
    args = parser.parse_args()
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    try:
        summary = apply(client_from_env(), catalog)
    except (ApiError, CatalogError) as error:
        parser.exit(2, f"{parser.prog}: {error}\n")
    text = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    if args.expect_unchanged and summary["changes"]:
        parser.exit(3, f"{parser.prog}: {summary['changes']} changes on a repeat run\n")


if __name__ == "__main__":
    main()
