"""Minimal OpenMetadata 2.0.x REST client for the stand (standard library only).

Endpoint sources (tag 2.0.8-release of open-metadata/OpenMetadata, docs v2.0.x):
- POST /v1/users/login: code only, UserResource.java (operationId loginUserWithPwd),
  body LoginRequest {email, password as base64}, response JwtResponse.accessToken.
  NOT FOUND in the v2.0.x API reference: unconfirmed by documentation.
- GET /v1/bots/name/{name}: code only, BotResource.java (getBotByFQN); the bot holds
  botUser. NOT FOUND in the API reference: unconfirmed by documentation.
- GET /v1/users/auth-mechanism/{id}: code only, UserResource.java; returns
  {authType, config: {JWTToken, ...}} for bot users. NOT FOUND in the API reference
  (the docs take the bot token from Settings > Bots): unconfirmed by documentation.

Command line (run.sh uses it; tokens never go to stdout):

    python scripts/om_client.py tokens --env-file stand/.tokens
"""

import argparse
import base64
import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_ADMIN_EMAIL = "admin@open-metadata.org"
DEFAULT_BOT = "ingestion-bot"


class ApiError(Exception):
    def __init__(self, method, path, status, body):
        super().__init__(f"{method} {path}: HTTP {status}: {body[:500]}")
        self.method = method
        self.path = path
        self.status = status
        self.body = body


def quote(value):
    return urllib.parse.quote(value, safe="")


class Client:
    """JSON over HTTP against base_url, for example http://localhost:8585/api."""

    def __init__(self, base_url, token=None, timeout=60):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, method, path, *, params=None, body=None, content_type=None):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = content_type or "application/json"
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
                text = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            text = error.read().decode("utf-8", errors="replace")
            raise ApiError(method, path, error.code, text) from None
        return status, (json.loads(text) if text.strip() else None)

    def get(self, path, params=None):
        return self.request("GET", path, params=params)[1]

    def find(self, path, params=None):
        """GET that returns None on 404 instead of raising."""
        try:
            return self.get(path, params)
        except ApiError as error:
            if error.status == 404:
                return None
            raise

    def put(self, path, body):
        return self.request("PUT", path, body=body)[1]

    def patch(self, path, operations):
        return self.request(
            "PATCH", path, body=operations, content_type="application/json-patch+json"
        )[1]


def login(client, email, password):
    encoded = base64.b64encode(password.encode("utf-8")).decode("ascii")
    _, response = client.request(
        "POST", "/v1/users/login", body={"email": email, "password": encoded}
    )
    token = (response or {}).get("accessToken")
    if not token:
        raise ApiError("POST", "/v1/users/login", 200, "response without accessToken")
    return token


def bot_token(client, bot_name=DEFAULT_BOT):
    bot = client.get(f"/v1/bots/name/{quote(bot_name)}")
    user_id = (bot.get("botUser") or {}).get("id")
    if not user_id:
        raise ApiError("GET", f"/v1/bots/name/{bot_name}", 200, "bot without botUser")
    mechanism = client.get(f"/v1/users/auth-mechanism/{quote(user_id)}")
    token = ((mechanism or {}).get("config") or {}).get("JWTToken")
    if not token:
        raise ApiError("GET", "/v1/users/auth-mechanism/{id}", 200, "no JWTToken")
    return token


def client_from_env(token_variable="OM_ADMIN_TOKEN"):
    return Client(os.environ["OM_API_URL"], os.environ.get(token_variable))


def write_tokens(env_file, admin, ingestion):
    path = Path(env_file)
    path.write_text(
        f"OM_ADMIN_TOKEN={admin}\nOM_INGESTION_TOKEN={ingestion}\n", encoding="utf-8"
    )
    path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description="OpenMetadata stand helper")
    sub = parser.add_subparsers(dest="command", required=True)
    tokens = sub.add_parser("tokens", help="log in as admin, read the bot token")
    tokens.add_argument("--env-file", type=Path, required=True)
    tokens.add_argument("--bot", default=DEFAULT_BOT)
    args = parser.parse_args()
    client = Client(os.environ["OM_API_URL"])
    email = os.environ.get("OM_ADMIN_EMAIL", DEFAULT_ADMIN_EMAIL)
    client.token = login(client, email, os.environ["OM_ADMIN_PASSWORD"])
    ingestion = bot_token(client, args.bot)
    write_tokens(args.env_file, client.token, ingestion)
    print(f"tokens written for admin and {args.bot} (values not shown)")


if __name__ == "__main__":
    main()
