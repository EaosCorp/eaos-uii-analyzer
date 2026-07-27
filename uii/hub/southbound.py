"""Southbound gateway — module sessions on the private module LAN.

Modules dial the hub (never the reverse) and hold one TCP connection for
their whole life. Adoption FSM: CONNECTED -> VERIFYING -> OPERATIONAL,
unknown types land in QUARANTINED. Every transition is an identity envelope.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from typing import Optional

from ..protocol import PROTO, encode, now_iso
from .evidence import EvidenceStore


class ModuleSession:
    def __init__(self, reader, writer, hub: "SouthboundHub"):
        self.reader, self.writer, self.hub = reader, writer, hub
        self.module_id: Optional[str] = None
        self.module_type: Optional[str] = None
        self.serial: Optional[str] = None
        self.fw: Optional[str] = None
        self.manifest: dict = {}
        self.state = "CONNECTED"
        self.role: Optional[str] = None
        self.last_seen = time.time()
        # module local_seq -> envelope id, for resolving raw_refs in results
        self.telem_ids: dict[int, str] = {}
        self.commands: dict[str, dict] = {}  # command_id -> command envelope

    async def send(self, msg: dict):
        self.writer.write(encode(msg))
        await self.writer.drain()

    def identity_event(self, event: str, extra: Optional[dict] = None):
        self.hub.store.append(
            "identity", {"event": event, "state": self.state,
                         "type": self.module_type, "serial": self.serial,
                         "fw": self.fw, "role": self.role, **(extra or {})},
            "urn:uii:schema:identity:0.1", module=self.module_id, actor="system:hub")

    async def run(self):
        try:
            hello = await asyncio.wait_for(self._read_msg(), timeout=10)
            if not hello or hello.get("t") != "HELLO":
                return
            self.module_id = hello.get("module_id")
            self.module_type = hello.get("type")
            self.serial = hello.get("serial")
            self.fw = hello.get("fw")
            self.manifest = hello.get("manifest") or {}
            self.state = "VERIFYING"
            self.identity_event("hello")

            if hello.get("proto") != PROTO or self.module_type not in self.hub.allowed_types:
                self.state = "QUARANTINED"
                self.identity_event("quarantined", {
                    "reason": f"type '{self.module_type}' not in allowlist"
                    if hello.get("proto") == PROTO else f"proto mismatch {hello.get('proto')}"})
                await self.send({"t": "QUARANTINE", "reason": "not adopted"})
                # hold the socket open, powered-but-mute; ignore everything
                while await self._read_msg() is not None:
                    pass
                return

            self.state = "OPERATIONAL"
            self.role = self.hub.roles.get(self.module_id, f"role-{self.module_type}")
            self.hub.sessions[self.module_id] = self
            self.identity_event("adopted")
            await self.send({"t": "HELLO_OK", "hub": self.hub.store.hub_id,
                             "time": now_iso(), "role": self.role})

            while True:
                msg = await self._read_msg()
                if msg is None:
                    break
                self.last_seen = time.time()
                await self._handle(msg)
        finally:
            if self.state == "OPERATIONAL":
                self.state = "REMOVED"
                self.identity_event("removed")
                self.hub.sessions.pop(self.module_id, None)
            try:
                self.writer.close()
            except Exception:
                pass

    async def _read_msg(self) -> Optional[dict]:
        try:
            line = await self.reader.readline()
        except (ConnectionResetError, asyncio.IncompleteReadError):
            return None
        if not line:
            return None
        try:
            return json.loads(line.decode())
        except json.JSONDecodeError:
            return {}

    async def _handle(self, msg: dict):
        t = msg.get("t")
        store = self.hub.store
        if t == "TELEM":
            kind = msg.get("kind", "observation")
            cmd_id = msg.get("command_id")
            trace = {"command_id": cmd_id,
                     "causation_id": cmd_id, "correlation_id": cmd_id}
            env = store.append(
                kind if kind in ("observation", "health", "state", "event") else "event",
                msg.get("data") or {},
                f"urn:uii:schema:{kind}:0.1",
                module=self.module_id, channel=msg.get("channel"),
                trace=trace if cmd_id else None,
                quality=msg.get("quality") or ({"status": "good"} if kind == "observation" else None),
                time_=msg.get("time"))
            if msg.get("local_seq") is not None:
                self.telem_ids[int(msg["local_seq"])] = env["id"]
                if len(self.telem_ids) > 500:
                    for k in sorted(self.telem_ids)[:250]:
                        self.telem_ids.pop(k, None)
        elif t == "ACK":
            cmd = self.commands.get(msg.get("command_id"))
            store.append("ack",
                         {"accepted": bool(msg.get("accepted")),
                          "reason": msg.get("reason")},
                         "urn:uii:schema:ack:0.1", module=self.module_id,
                         actor="system:module",
                         trace={"command_id": msg.get("command_id"),
                                "causation_id": cmd["id"] if cmd else None,
                                "correlation_id": msg.get("command_id")})
        elif t == "PROGRESS":
            cmd = self.commands.get(msg.get("command_id"))
            store.append("progress",
                         {"pct": msg.get("pct"), "message": msg.get("message")},
                         "urn:uii:schema:progress:0.1", module=self.module_id,
                         trace={"command_id": msg.get("command_id"),
                                "causation_id": cmd["id"] if cmd else None,
                                "correlation_id": msg.get("command_id")})
        elif t == "RESULT":
            await self._handle_result(msg)

    async def _handle_result(self, msg: dict):
        store = self.hub.store
        cmd_id = msg.get("command_id")
        cmd = self.commands.pop(cmd_id, None)
        raw_refs = [self.telem_ids[s] for s in (msg.get("raw_local_seqs") or [])
                    if s in self.telem_ids]
        result = store.append(
            "result",
            {"status": msg.get("status"), "outputs": msg.get("data") or {},
             "raw_refs": raw_refs},
            "urn:uii:schema:result:0.1", module=self.module_id,
            trace={"command_id": cmd_id,
                   "causation_id": cmd["id"] if cmd else None,
                   "correlation_id": cmd_id})
        if msg.get("status") != "succeeded" or not cmd:
            return
        cmd_type = (cmd.get("data") or {}).get("type")
        outputs = msg.get("data") or {}
        i0, i1 = outputs.get("i0"), outputs.get("i1")
        absorbance = _absorbance(i0, i1)

        # Interpretation happens on the hub: calibration records and method
        # versions are evidence, so derived values stay recomputable.
        if cmd_type == "calibrate" and absorbance:
            std = float((cmd.get("data") or {}).get("params", {}).get("std_conc", 5.0))
            slope = std / absorbance
            store.append(
                "calibration",
                {"slope": round(slope, 4), "intercept": 0.0,
                 "std_conc": std, "absorbance": round(absorbance, 5),
                 "points": 1},
                "urn:uii:schema:calibration:0.1", module=self.module_id,
                trace={"command_id": cmd_id, "causation_id": result["id"],
                       "correlation_id": cmd_id})
        elif cmd_type == "sample" and absorbance:
            cal = store.latest_calibration(self.module_id)
            analyte = (self.manifest.get("analyte") or "NH4").upper()
            if cal:
                value = cal["data"]["slope"] * absorbance + cal["data"]["intercept"]
                quality = {"status": "good", "flags": []}
            else:
                value, quality = None, {"status": "bad", "flags": ["no_calibration"]}
            store.append(
                "observation",
                {"value": round(value, 3) if value is not None else None,
                 "unit": "mg/L", "analyte": analyte,
                 "absorbance": round(absorbance, 5), "raw_refs": raw_refs},
                "urn:uii:schema:observation.concentration:0.1",
                module=self.module_id, channel=(self.manifest.get("analyte") or "nh4").lower(),
                quality=quality,
                context={"calibration_id": cal["id"] if cal else None,
                         "method": self.manifest.get("method")},
                trace={"command_id": cmd_id, "causation_id": result["id"],
                       "correlation_id": cmd_id})


def _absorbance(i0, i1) -> Optional[float]:
    try:
        ratio = float(i0) / max(float(i1), 1e-12)
        return math.log10(ratio) if ratio > 0 else None
    except (TypeError, ValueError):
        return None


class SouthboundHub:
    def __init__(self, store: EvidenceStore, allowed_types: set[str],
                 roles: Optional[dict] = None):
        self.store = store
        self.allowed_types = allowed_types
        self.roles = roles or {}
        self.sessions: dict[str, ModuleSession] = {}

    async def serve(self, host: str, port: int):
        server = await asyncio.start_server(self._on_connect, host, port)
        async with server:
            await server.serve_forever()

    async def _on_connect(self, reader, writer):
        await ModuleSession(reader, writer, self).run()

    async def dispatch(self, module_id: str, cmd_env: dict) -> bool:
        session = self.sessions.get(module_id)
        if not session:
            return False
        session.commands[cmd_env["data"]["command_id"]] = cmd_env
        await session.send({
            "t": "CMD",
            "command_id": cmd_env["data"]["command_id"],
            "type": cmd_env["data"]["type"],
            "params": cmd_env["data"].get("params") or {},
            "expires_at": cmd_env["data"].get("expires_at"),
        })
        return True
