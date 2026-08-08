"""Minimal bearer-token API auth for the edge deployment.

The full authority model (locked mode, per-user roles) lives in the authority
extension. This is the small gate the vision edge needs to expose the API on
the tailnet safely: a token maps to an actor and a role.

  * operator token -> full API (GET + POST commands), actor e.g. "user:keaton"
  * viewer token   -> GET only (fetch frames/observations), actor "viewer:*"

Tokens are read by the vision extension from a JSON file (default
/etc/eaos/uii-api-tokens.json, perms 600). A request presents its token as
`Authorization: Bearer <token>` (preferred) or `?token=<token>` (browser
convenience; note tokens in URLs can land in logs). No token, or a POST with a
viewer token, is refused here before any handler runs.

Set as `hub.api_auth`; the core calls it for every request except discovery
(uii/hub/api.py do_GET/do_POST), and the auth fn writes its own 401/403.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse


def make_token_auth(tokens: dict):
    """tokens: {token_str: {"actor": str, "role": "operator"|"viewer"}}."""

    def auth(handler):
        tok = None
        header = handler.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            tok = header[7:].strip()
        if not tok:
            q = parse_qs(urlparse(handler.path).query)
            tok = (q.get("token") or [None])[0]

        info = tokens.get(tok or "")
        if not info:
            handler._problem(401, "urn:uii:problem:unauthorized",
                             "missing or invalid bearer token")
            return None, False
        if info.get("role") == "viewer" and handler.command != "GET":
            handler._problem(403, "urn:uii:problem:forbidden",
                             "read-only token: GET only")
            return None, False
        return info.get("actor", "user:api"), True

    return auth


def load_tokens(path: str):
    """Read a tokens file. Accepts {"tokens":[{token,actor,role}, ...]} or a
    flat {token: {actor, role}}. Returns {token: {actor, role}} or None."""
    import json
    import os
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return None
    out: dict = {}
    items = raw.get("tokens", raw) if isinstance(raw, dict) else raw
    if isinstance(items, list):
        for t in items:
            if t.get("token"):
                out[t["token"]] = {"actor": t.get("actor", "user:api"),
                                   "role": t.get("role", "operator")}
    elif isinstance(items, dict):
        for k, v in items.items():
            if isinstance(v, dict):
                out[k] = {"actor": v.get("actor", "user:api"),
                          "role": v.get("role", "operator")}
    return out or None
