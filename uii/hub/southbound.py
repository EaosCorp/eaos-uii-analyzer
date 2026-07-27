"""Southbound gateway — module sessions on the private module LAN.

Modules dial the hub (never the reverse) and hold one TCP connection for
their whole life. Full adoption FSM (reference architecture §5.2):

    CONNECTED -> VERIFYING -> ADOPTING -> OPERATIONAL
                     |            |            |  <-> DEGRADED (missed health)
                     v            v            v
                QUARANTINED   (adopt fail)  REMOVED (link down / BYE)

* VERIFYING: protocol + trust check. Trust = type allowlist OR a serial
  previously released from quarantine (persisted — recognized on sight).
* ADOPTING: the slot's ROLE CONFIG IS PUSHED to the module in HELLO_OK and
  the module must answer ROLE_OK. This is what makes a swap "plumbing work,
  not IT work": plug in, and the role's configuration is restored onto
  whatever compatible module occupies the slot.
* QUARANTINED: powered, logged, mute. Release is one logged call
  (POST /v1/modules/{id}/release): the serial is trusted persistently,
  the module is told, reconnects, and is adopted.
* Every transition is an identity envelope — the swap history IS the log.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

import json

from ..protocol import PROTO, encode, now_iso
from .config import HubConfig
from .evidence import EvidenceStore
from .interpret import (calibration_complete, fit_calibration,
                        interpret_sample)


class ModuleSession:
    def __init__(self, reader, writer, hub: "SouthboundHub"):
        self.reader, self.writer, self.hub = reader, writer, hub
        self.module_id: Optional[str] = None
        self.module_type: Optional[str] = None
        self.serial: Optional[str] = None
        self.fw: Optional[str] = None
        self.slot: Optional[str] = None
        self.manifest: dict = {}
        self.state = "CONNECTED"
        self.role: Optional[str] = None
        self.role_config: dict = {}
        self.last_seen = time.time()
        self.mode: Optional[str] = None          # BRIDGE | ENDPOINT (from health)
        self.module_state: Optional[str] = None  # idle | priming | ...
        # module local_seq -> envelope id, for resolving raw_refs in results
        self.telem_ids: dict[int, str] = {}
        self.commands: dict[str, dict] = {}      # command_id -> command envelope

    # -- plumbing --------------------------------------------------------------

    async def send(self, msg: dict):
        self.writer.write(encode(msg))
        await self.writer.drain()

    async def _read_msg(self) -> Optional[dict]:
        try:
            line = await self.reader.readline()
        except (ConnectionResetError, asyncio.IncompleteReadError, OSError):
            return None
        if not line:
            return None
        try:
            return json.loads(line.decode())
        except json.JSONDecodeError:
            return {}

    def identity_event(self, event: str, extra: Optional[dict] = None):
        self.hub.store.append(
            "identity", {"event": event, "state": self.state,
                         "type": self.module_type, "serial": self.serial,
                         "fw": self.fw, "slot": self.slot, "role": self.role,
                         **(extra or {})},
            "urn:uii:schema:identity:0.1", module=self.module_id, actor="system:hub")
        self.hub.registry[self.module_id] = {
            "id": self.module_id, "type": self.module_type,
            "serial": self.serial, "fw": self.fw, "slot": self.slot,
            "role": self.role, "state": self.state, "last_event": event,
            "last_seen": now_iso()}

    # -- lifecycle -------------------------------------------------------------

    async def run(self):
        try:
            hello = await asyncio.wait_for(self._read_msg(), timeout=10)
            if not hello or hello.get("t") != "HELLO":
                return
            self.module_id = hello.get("module_id")
            self.module_type = hello.get("type")
            self.serial = hello.get("serial")
            self.fw = hello.get("fw")
            self.slot = hello.get("slot")
            self.manifest = hello.get("manifest") or {}
            self.state = "VERIFYING"
            self.identity_event("hello")

            # -- VERIFYING: protocol + trust (allowlist or released serial) ----
            if hello.get("proto") != PROTO:
                await self._quarantine(f"proto mismatch {hello.get('proto')} (hub {PROTO})")
                return
            if not self.hub.config.is_trusted(self.module_type, self.serial):
                await self._quarantine(f"type '{self.module_type}' not in allowlist "
                                       f"and serial '{self.serial}' not trusted")
                return

            # -- ADOPTING: restore the role config onto the module -------------
            self.state = "ADOPTING"
            self.role, self.role_config = self.hub.config.role_for(
                self.slot, self.module_type, self.manifest.get("analyte"))
            self.identity_event("adopting")
            await self.send({"t": "HELLO_OK", "hub": self.hub.store.hub_id,
                             "time": now_iso(), "role": self.role,
                             "config": self.role_config})
            try:
                role_ok = await asyncio.wait_for(self._read_msg(), timeout=10)
            except asyncio.TimeoutError:
                role_ok = None
            if not role_ok or role_ok.get("t") != "ROLE_OK":
                self.state = "REMOVED"
                self.identity_event("adoption-failed",
                                    {"reason": "no ROLE_OK from module"})
                return

            # -- OPERATIONAL ----------------------------------------------------
            self.state = "OPERATIONAL"
            self.hub.sessions[self.module_id] = self
            self.identity_event("adopted")

            while True:
                msg = await self._read_msg()
                if msg is None:
                    break
                self.last_seen = time.time()
                if self.state == "DEGRADED":
                    self.state = "OPERATIONAL"
                    self.identity_event("recovered")
                await self._handle(msg)
        finally:
            if self.state in ("OPERATIONAL", "DEGRADED"):
                self.state = "REMOVED"
                self.identity_event("removed")
                self.hub.sessions.pop(self.module_id, None)
                self.hub.store.append(
                    "event", {"event": "role-vacant", "role": self.role,
                              "slot": self.slot},
                    "urn:uii:schema:event:0.1", actor="system:hub")
            try:
                self.writer.close()
            except Exception:
                pass

    async def _quarantine(self, reason: str):
        self.state = "QUARANTINED"
        self.identity_event("quarantined", {"reason": reason})
        self.hub.quarantined[self.module_id] = self
        await self.send({"t": "QUARANTINE", "reason": reason})
        try:
            # hold the socket open, powered-but-mute; ignore everything
            while not self._released and await self._read_msg() is not None:
                pass
        finally:
            self.hub.quarantined.pop(self.module_id, None)
            if self.state == "QUARANTINED":
                self.state = "REMOVED"
                self.identity_event("removed")

    _released = False

    async def release(self, actor: str):
        """One logged call: trust the serial, tell the module, let it redial."""
        if self.serial:
            self.hub.config.trust_serial(self.serial)
        self._released = True
        self.state = "RELEASED"
        self.identity_event("released", {"by": actor})
        try:
            await self.send({"t": "QUARANTINE_RELEASED"})
        except Exception:
            pass
        try:
            self.writer.close()
        except Exception:
            pass

    # -- message handling --------------------------------------------------------

    async def _handle(self, msg: dict):
        t = msg.get("t")
        store = self.hub.store
        if t == "TELEM":
            kind = msg.get("kind", "observation")
            data = msg.get("data") or {}
            if kind == "health":
                self.mode = data.get("mode", self.mode)
                self.module_state = data.get("state", self.module_state)
            elif kind == "state":
                self.module_state = data.get("to", self.module_state)
                if data.get("mode"):
                    self.mode = data["mode"]
            cmd_id = msg.get("command_id")
            trace = {"command_id": cmd_id,
                     "causation_id": cmd_id, "correlation_id": cmd_id}
            env = store.append(
                kind if kind in ("observation", "health", "state", "event") else "event",
                data,
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
            ack = store.append("ack",
                               {"accepted": bool(msg.get("accepted")),
                                "reason": msg.get("reason")},
                               "urn:uii:schema:ack:0.1", module=self.module_id,
                               actor="system:module",
                               trace={"command_id": msg.get("command_id"),
                                      "causation_id": cmd["id"] if cmd else None,
                                      "correlation_id": msg.get("command_id")})
            if not msg.get("accepted"):
                # one result per command, always (spec §6.3): a rejection
                # is terminal
                self.commands.pop(msg.get("command_id"), None)
                store.append("result",
                             {"status": "rejected", "reason": msg.get("reason")},
                             "urn:uii:schema:result:0.1", module=self.module_id,
                             trace={"command_id": msg.get("command_id"),
                                    "causation_id": ack["id"],
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
            self._handle_result(msg)

    def _handle_result(self, msg: dict):
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

        # Interpretation happens on the hub (architecture §3), and WHICH
        # interpreter runs is selected by the module's declared
        # instrument_class — this is the seam between the universal core
        # and class profiles. Only the chemical-analyzer profile exists
        # today (raw captures -> calibration fit -> concentration); a
        # vision or rotating-equipment class would register its own
        # result interpreter here and reuse everything else unchanged.
        if self.manifest.get("instrument_class",
                             "chemical-analyzer") != "chemical-analyzer":
            return
        cmd_type = (cmd.get("data") or {}).get("type")
        outputs = msg.get("data") or {}
        captures = outputs.get("captures") or {}
        analyte = (outputs.get("analyte") or self.manifest.get("analyte") or "NH4").upper()

        if cmd_type == "calibrate" and captures:
            std = (cmd.get("data") or {}).get("params", {}).get(
                "std_conc", self.role_config.get("calibrate_std_conc", 5.0))
            fitted = fit_calibration(analyte, float(std), captures)
            if fitted["error"]:
                store.append(
                    "event", {"event": "calibration-failed",
                              "analyte": analyte, "error": fitted["error"]},
                    "urn:uii:schema:event:0.1", module=self.module_id,
                    trace={"command_id": cmd_id, "causation_id": result["id"],
                           "correlation_id": cmd_id})
                return
            store.append(
                "calibration",
                {"analyte": analyte, "std_conc_mgL": float(std),
                 "units": {"concentration": "mg/L"},
                 "fit": fitted["fit"], "absorbance": fitted["absorbance"],
                 "vin": captures, "raw_refs": raw_refs},
                "urn:uii:schema:calibration:0.1", module=self.module_id,
                trace={"command_id": cmd_id, "causation_id": result["id"],
                       "correlation_id": cmd_id})

        elif cmd_type == "sample" and captures:
            cal = store.latest_calibration(self.module_id)
            fit = (cal or {}).get("data", {}).get("fit", {})
            # quality attribution -> a machine-readable PERMITTED-USE
            # designation, so a result is never an unaudited bare number:
            # "control" (fit for automated action), "reporting" (records/
            # display only), "none" (no valid interpretation). Automated
            # consumers (scheduler, future OT adapter) must honor it.
            if cal and calibration_complete(analyte, fit):
                res = interpret_sample(analyte, fit, captures)
                quality = ({"status": "good", "flags": [],
                            "permitted_use": "control"} if not res["error"]
                           else {"status": "bad",
                                 "flags": ["interpretation_error"],
                                 "permitted_use": "reporting"})
            else:
                res = {"channels": [{"name": c, "value": None, "unit": "mg/L",
                                     "absorbance": None}
                                    for c in ([analyte.lower()] if analyte != "NOX"
                                              else ["nox", "no2", "no3"])],
                       "error": "no calibration"}
                quality = {"status": "bad", "flags": ["no_calibration"],
                           "permitted_use": "none"}
            for ch in res["channels"]:
                store.append(
                    "observation",
                    {"value": round(ch["value"], 4) if ch["value"] is not None else None,
                     "unit": ch["unit"], "analyte": analyte,
                     "absorbance": (round(ch["absorbance"], 5)
                                    if ch.get("absorbance") is not None else None),
                     "error": res["error"] or None, "raw_refs": raw_refs},
                    "urn:uii:schema:observation.concentration:0.1",
                    module=self.module_id, channel=ch["name"],
                    quality=quality,
                    context={"calibration_id": cal["id"] if cal else None,
                             "method": self.manifest.get("method"),
                             "role": self.role},
                    trace={"command_id": cmd_id, "causation_id": result["id"],
                           "correlation_id": cmd_id})


class SouthboundHub:
    def __init__(self, store: EvidenceStore, config: HubConfig):
        self.store = store
        self.config = config
        self.sessions: dict[str, ModuleSession] = {}
        self.quarantined: dict[str, ModuleSession] = {}
        self.registry: dict[str, dict] = {}   # module_id -> last-known info

    async def serve(self, host: str, port: int, ready: Optional[asyncio.Event] = None):
        server = await asyncio.start_server(self._on_connect, host, port)
        self.port = server.sockets[0].getsockname()[1]
        if ready:
            ready.set()
        async with server:
            await server.serve_forever()

    async def _on_connect(self, reader, writer):
        await ModuleSession(reader, writer, self).run()

    async def dispatch(self, module_id: str, cmd_env: dict) -> bool:
        session = self.sessions.get(module_id)
        if not session:
            return False
        session.commands[cmd_env["data"]["command_id"]] = cmd_env
        try:
            await session.send({
                "t": "CMD",
                "command_id": cmd_env["data"]["command_id"],
                "type": cmd_env["data"]["type"],
                "params": cmd_env["data"].get("params") or {},
                "expires_at": cmd_env["data"].get("expires_at"),
            })
        except (ConnectionResetError, BrokenPipeError, OSError):
            session.commands.pop(cmd_env["data"]["command_id"], None)
            return False
        return True

    # -- health supervision ------------------------------------------------------

    async def monitor(self, check_interval_s: float = 2.0):
        """Missed heartbeats degrade a module; the reader loop recovers it."""
        while True:
            now = time.time()
            for s in list(self.sessions.values()):
                interval = float(s.manifest.get("health_interval_s") or 60)
                threshold = max(3 * interval, 5.0)   # floor for compressed benches
                if s.state == "OPERATIONAL" and now - s.last_seen > threshold:
                    s.state = "DEGRADED"
                    s.identity_event("degraded", {
                        "reason": f"no traffic for {int(now - s.last_seen)}s "
                                  f"(health interval {int(interval)}s)"})
            await asyncio.sleep(check_interval_s)
