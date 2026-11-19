"""In-memory stand-in for the OpenMetadata REST endpoints used by the stand scripts.

It is a real HTTP server on 127.0.0.1, so the tests exercise URL encoding, headers
and JSON bodies of scripts/om_client.py. It models only what the scripts rely on and
says nothing about how the real server behaves.
"""

import base64
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import urllib.parse
import uuid

ADMIN_TOKEN = "admin-token"
BOT_TOKEN = "bot-token"
ADMIN_PASSWORD = "admin"


def pointer_parts(path):
    return [p.replace("~1", "/").replace("~0", "~") for p in path.split("/")[1:]]


def json_patch(document, operations):
    """Apply add/replace operations (RFC 6902) as the server would."""
    document = copy.deepcopy(document)
    for op in operations:
        if op["op"] not in ("add", "replace"):
            raise ValueError(f"unsupported op {op['op']}")
        *parents, last = pointer_parts(op["path"])
        target = document
        for part in parents:
            target = target[int(part)] if isinstance(target, list) else target[part]
        value = copy.deepcopy(op["value"])
        if isinstance(target, list):
            if last == "-":
                target.append(value)
            elif op["op"] == "add":
                target.insert(int(last), value)
            else:
                target[int(last)] = value
        else:
            if op["op"] == "replace" and last not in target:
                raise ValueError(f"replace of missing member {op['path']}")
            target[last] = value
    return document


class FakeOpenMetadata:
    def __init__(self):
        self.requests = []
        self.failures = {}
        self.teams = {}
        self.glossaries = {}
        self.terms = {}
        self.tables = {}
        self.edges = []
        self.test_cases = {}
        self.bot_user_id = str(uuid.uuid4())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/api"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def add_table(self, fqn, columns, **fields):
        table = {
            "id": str(uuid.uuid4()),
            "name": fqn.rsplit(".", 1)[1],
            "fullyQualifiedName": fqn,
            "columns": [{"name": c, "dataType": "STRING"} for c in columns],
            "tableType": "Regular",
        }
        table.update(fields)
        self.tables[fqn] = table
        return table

    def writes(self):
        return [r for r in self.requests if r["method"] != "GET"]

    def handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body=None):
                data = b"" if body is None else json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def handle_any(self):
                parsed = urllib.parse.urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                record = {
                    "method": self.command,
                    "path": urllib.parse.unquote(parsed.path),
                    "raw_path": parsed.path,
                    "query": dict(urllib.parse.parse_qsl(parsed.query)),
                    "headers": dict(self.headers),
                    "body": json.loads(raw) if raw else None,
                }
                fake.requests.append(record)
                for (method, prefix), (status, body) in fake.failures.items():
                    if method == self.command and record["path"].startswith(prefix):
                        return self.reply(status, body)
                try:
                    status, body = fake.route(record)
                except KeyError:
                    status, body = 404, {"code": 404, "message": "not found"}
                self.reply(status, body)

            do_GET = do_PUT = do_PATCH = do_POST = handle_any

        return Handler

    def authorized(self, record, token):
        return record["headers"].get("Authorization") == f"Bearer {token}"

    def route(self, r):
        method, path, body = r["method"], r["path"], r["body"]
        if path == "/api/v1/users/login" and method == "POST":
            password = base64.b64decode(body["password"]).decode("utf-8")
            if body["email"] != "admin@open-metadata.org" or password != ADMIN_PASSWORD:
                return 401, {"message": "bad credentials"}
            return 200, {"accessToken": ADMIN_TOKEN, "tokenType": "Bearer"}
        if not self.authorized(r, ADMIN_TOKEN):
            return 401, {"message": "unauthorized"}
        if path == "/api/v1/bots/name/ingestion-bot":
            return 200, {"name": "ingestion-bot", "botUser": {"id": self.bot_user_id}}
        if path == f"/api/v1/users/auth-mechanism/{self.bot_user_id}":
            return 200, {"authType": "JWT", "config": {"JWTToken": BOT_TOKEN}}
        for kind, store in (
            ("teams", self.teams),
            ("glossaries", self.glossaries),
            ("glossaryTerms", self.terms),
        ):
            prefix = f"/api/v1/{kind}/name/"
            if method == "GET" and path.startswith(prefix):
                return 200, store[path[len(prefix) :]]
            if method == "PUT" and path == f"/api/v1/{kind}":
                fqn = body["name"]
                if kind == "glossaryTerms":
                    fqn = f"{body['glossary']}.{body['name']}"
                created = fqn not in store
                entity = store.get(fqn) or {"id": str(uuid.uuid4())}
                entity.update({k: v for k, v in body.items() if k != "glossary"})
                entity["fullyQualifiedName"] = fqn
                if kind == "glossaryTerms":
                    entity.setdefault("entityStatus", "Approved")
                store[fqn] = entity
                return (201 if created else 200), entity
        if method == "GET" and path.startswith("/api/v1/tables/name/"):
            table = self.tables[path[len("/api/v1/tables/name/") :]]
            return 200, self.expand(table)
        if method == "PATCH" and path.startswith("/api/v1/tables/"):
            if r["headers"].get("Content-Type") != "application/json-patch+json":
                return 415, {"message": "unsupported media type"}
            table_id = path.rsplit("/", 1)[1]
            fqn = next(f for f, t in self.tables.items() if t["id"] == table_id)
            self.tables[fqn] = json_patch(self.tables[fqn], body)
            return 200, self.expand(self.tables[fqn])
        if method == "GET" and path.startswith("/api/v1/lineage/table/name/"):
            table = self.tables[path[len("/api/v1/lineage/table/name/") :]]
            upstream = [e for e in self.edges if e[1] == table["id"]]
            ids = {e[0] for e in upstream}
            nodes = [
                {"id": t["id"], "fullyQualifiedName": f, "type": "table"}
                for f, t in self.tables.items()
                if t["id"] in ids
            ]
            entity = {
                "id": table["id"],
                "fullyQualifiedName": table["fullyQualifiedName"],
            }
            edges = [{"fromEntity": a, "toEntity": b} for a, b in upstream]
            return 200, {
                "entity": entity,
                "nodes": nodes,
                "upstreamEdges": edges,
                "downstreamEdges": [],
            }
        if method == "PUT" and path == "/api/v1/lineage":
            edge = (body["edge"]["fromEntity"]["id"], body["edge"]["toEntity"]["id"])
            if edge not in self.edges:
                self.edges.append(edge)
            return 200, None
        if method == "GET" and path == "/api/v1/dataQuality/testCases":
            link = r["query"]["entityLink"]
            data = [c for c in self.test_cases.values() if c["entityLink"] == link]
            return 200, {"data": data, "paging": {"total": len(data)}}
        raise KeyError(path)

    def expand(self, table):
        """Owners come back with the team name filled in, as entity references do."""
        result = copy.deepcopy(table)
        by_id = {team["id"]: team for team in self.teams.values()}
        for owner in result.get("owners") or []:
            team = by_id.get(owner["id"])
            if team:
                owner["name"] = owner["fullyQualifiedName"] = team["fullyQualifiedName"]
        return result
