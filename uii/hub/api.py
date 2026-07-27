"""HTTP API + SSE event stream. Stdlib only — this is the /v1 surface
from uii-spec.md, scaffold subset."""
from __future__ import annotations

import json
import queue
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ..protocol import now_iso
from .commands import CommandGateway
from .evidence import EvidenceStore
from .southbound import SouthboundHub

START = time.time()


def make_handler(store: EvidenceStore, southbound: SouthboundHub,
                 gateway: CommandGateway):

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        # -- helpers -----------------------------------------------------

        def _json(self, obj, code=200):
            body = json.dumps(obj, indent=2).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _problem(self, code, ptype, detail):
            body = json.dumps({"type": ptype, "status": code, "detail": detail}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/problem+json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # -- routes ------------------------------------------------------

        def do_GET(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            path = url.path

            if path == "/v1/system":
                return self._json({
                    "hub": store.hub_id, "uii": "0.1",
                    "time": now_iso(), "uptime_s": int(time.time() - START),
                    "modules_operational": len(southbound.sessions)})

            if path == "/v1/modules":
                return self._json({"items": [
                    {"id": s.module_id, "type": s.module_type, "serial": s.serial,
                     "fw": s.fw, "state": s.state, "role": s.role,
                     "last_seen_s_ago": round(time.time() - s.last_seen, 1)}
                    for s in southbound.sessions.values()]})

            if path == "/v1/capabilities":
                return self._json({
                    "hub": {"id": store.hub_id, "profile": "P1-scaffold"},
                    "modules": [{"id": s.module_id, "type": s.module_type,
                                 "manifest": s.manifest}
                                for s in southbound.sessions.values()]})

            if path == "/v1/observations/latest":
                out = []
                for s in southbound.sessions.values():
                    for ch in [c["name"] for c in s.manifest.get("channels", [])]:
                        env = store.latest_observation(s.module_id, ch)
                        if env:
                            out.append(env)
                return self._json({"items": out})

            if path == "/v1/evidence":
                envs = store.query(kind=q.get("kind"), module=q.get("module"),
                                   since_seq=int(q.get("since", 0)),
                                   limit=int(q.get("limit", 100)))
                return self._json({"items": envs,
                                   "next_cursor": envs[-1]["sequence"] if envs else None})

            m = re.fullmatch(r"/v1/evidence/([\w-]+)/lineage", path)
            if m:
                lin = store.lineage(m.group(1))
                return self._json(lin) if lin else self._problem(
                    404, "urn:uii:problem:not-found", "no such envelope")

            m = re.fullmatch(r"/v1/evidence/([\w-]+)", path)
            if m:
                env = store.get(m.group(1))
                return self._json(env) if env else self._problem(
                    404, "urn:uii:problem:not-found", "no such envelope")

            m = re.fullmatch(r"/v1/commands/([\w-]+)", path)
            if m:
                st = gateway.status(m.group(1))
                return self._json(st) if st else self._problem(
                    404, "urn:uii:problem:not-found", "no such command")

            if path == "/v1/events":
                return self._sse(q)

            return self._problem(404, "urn:uii:problem:not-found", path)

        def do_POST(self):
            url = urlparse(self.path)
            if url.path != "/v1/commands":
                return self._problem(404, "urn:uii:problem:not-found", url.path)
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._problem(400, "urn:uii:problem:validation", "invalid JSON")
            module = body.get("module") or (body.get("target") or {}).get("module")
            if not module or not body.get("type"):
                return self._problem(400, "urn:uii:problem:validation",
                                     "need module and type")
            env, problem = gateway.submit(module, body["type"], body.get("params"),
                                          actor=body.get("actor", "user:local"))
            if problem:
                return self._problem(422, problem["type"], problem["detail"])
            return self._json({"command_id": env["data"]["command_id"],
                               "evidence_id": env["id"]}, 202)

        # -- SSE -----------------------------------------------------------

        def _sse(self, q):
            since = int(q.get("since", self.headers.get("Last-Event-ID", 0) or 0))
            kinds = set(q["kind"].split(",")) if q.get("kind") else None
            sub = store.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                for env in store.query(since_seq=since, limit=500):
                    if not kinds or env["kind"] in kinds:
                        self._sse_write(env)
                while True:
                    try:
                        env = sub.get(timeout=15)
                        if not kinds or env["kind"] in kinds:
                            self._sse_write(env)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                store.unsubscribe(sub)

        def _sse_write(self, env):
            frame = (f"id: {env['sequence']}\nevent: {env['kind']}\n"
                     f"data: {json.dumps(env)}\n\n").encode()
            self.wfile.write(frame)
            self.wfile.flush()

    return Handler


def serve_api(store, southbound, gateway, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(store, southbound, gateway))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
