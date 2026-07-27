"""pimod — the module agent for the real NH4MOD Pi (and its simulator).

This is the deployed serial_mqtt_gateway re-plumbed to speak UII southbound
instead of ad-hoc MQTT. Same hardware picture, same method timelines:

  Port A /dev/ttyUSB0 = PLC (controller)      9600 8N1 line-based
  Port B /dev/ttyUSB1 = pump controller       EZ-stepper ASCII "/1...R"
  ADS1115 on I2C      = detector (the Pi reads it directly)

  BRIDGE mode  : PLC drives — the agent forwards serial A<->B (plant
                 authority). Every line both directions is evidence.
  ENDPOINT mode: hub drives — ST9 timeline tables execute the method
                 (latched, exactly like the field code; a latched module
                 returns to BRIDGE only by restart).

What changed from the legacy gateway (by design, per the UII split):
  * No MQTT, no broker, no Windows PC. One TCP connection TO the hub.
  * The agent produces FACTS ONLY: raw detector volts, serial traces,
    states, health. Calibration fits and concentrations are computed on
    the hub, so every derived value carries lineage and is recomputable.
  * calibration.json is gone — calibration is a hub evidence record.

Environment:
  UII_HUB        host:port of the hub southbound (default 127.0.0.1:7300)
  UII_MODULE_ID  module identity        (default nh4mod-01)
  UII_SLOT       physical slot claim    (default slot-1)
  UII_SERIAL     hardware serial        (default derived from module id)
  ANALYTE        NH4 | NOX | PO4        (default NH4 — same var as legacy)
  UII_SIM        1 = no hardware, synthetic detector physics
  UII_SPEED      timeline compression   (default 1 — REAL TIME in the field)
  PORT_A/PORT_B/BAUD, ADC_ENABLED/ADC_GAIN/ADC_CHANNEL/DIVIDER_RATIO/
  SHUNT_OHMS/SIGNAL_MODE — identical to the legacy gateway.

Run: python3 -m extensions.analyzer.pimod
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from typing import Optional

from uii.protocol import PROTO, encode, now_iso
from . import hw
from .timelines import ST9, VALID_ANALYTES, timeline

HEALTH_INTERVAL_S = 60


class PiModule:
    def __init__(self, env: Optional[dict] = None):
        e = env or os.environ
        self.module_id = e.get("UII_MODULE_ID", "nh4mod-01")
        self.slot = e.get("UII_SLOT", "slot-1")
        self.analyte = e.get("ANALYTE", "NH4").upper()
        if self.analyte not in VALID_ANALYTES:
            raise ValueError(f"ANALYTE must be one of {VALID_ANALYTES}")
        # UII_TYPE override lets a bench present as a foreign/vendor module
        self.module_type = e.get("UII_TYPE", f"nh4mod-{self.analyte.lower()}")
        self.serial = e.get("UII_SERIAL", f"NH4MOD-{self.module_id.upper()}")
        self.fw = e.get("UII_FW", "0.2.0")
        host, port = e.get("UII_HUB", "127.0.0.1:7300").rsplit(":", 1)
        self.hub_addr = (host, int(port))
        self.sim = str(e.get("UII_SIM", "0")).lower() in ("1", "true", "yes")
        self.speed = max(float(e.get("UII_SPEED", 1)), 1e-6)

        self._st9 = ST9[self.analyte]
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.writer = None
        self.role: Optional[str] = None
        self.role_config: dict = {}

        # -- mode: BRIDGE (plant authority) / ENDPOINT (hub authority, latched)
        self.mode = "BRIDGE"
        self.mode_reason = "boot default"
        self.endpoint_latched = False

        self.state = "idle"
        self.busy: Optional[str] = None
        self._abort = asyncio.Event()
        self.local_seq = 0
        self.ring: deque = deque(maxlen=500)   # telemetry survives hub loss
        self.t0 = time.time()

        # -- hardware
        if self.sim:
            self.port_a = hw.SimSerialPort("port_a", "sim://plc", 9600,
                                           self._on_serial_line, role="silent")
            self.port_b = hw.SimSerialPort("port_b", "sim://pump", 9600,
                                           self._on_serial_line, role="pump")
            self.adc = hw.SimADC(self.analyte)
        else:
            baud = int(e.get("BAUD", 9600))
            self.port_a = hw.RealSerialPort("port_a", e.get("PORT_A", "/dev/ttyUSB0"),
                                            baud, self._on_serial_line)
            self.port_b = hw.RealSerialPort("port_b", e.get("PORT_B", "/dev/ttyUSB1"),
                                            baud, self._on_serial_line)
            self.adc = hw.RealADC(
                enabled=str(e.get("ADC_ENABLED", "0")).lower() in ("1", "true", "yes"),
                gain=int(e.get("ADC_GAIN", 1)),
                channel=int(e.get("ADC_CHANNEL", 0)),
                divider_ratio=float(e.get("DIVIDER_RATIO", 1.0)),
                shunt_ohms=float(e.get("SHUNT_OHMS", 250.0)),
                signal_mode=e.get("SIGNAL_MODE", "voltage"))

    # ------------------------------------------------------------------
    # Serial plumbing (threads -> asyncio)
    # ------------------------------------------------------------------

    def _on_serial_line(self, port_name: str, line: str):
        """Called from serial worker threads."""
        if self.loop:
            self.loop.call_soon_threadsafe(self._handle_serial_line, port_name, line)

    def _handle_serial_line(self, port_name: str, line: str):
        self.telem("event", {"event": "serial", "port": port_name,
                             "dir": "RX", "raw": line})
        if self.mode == "BRIDGE":
            # plant authority: forward A<->B untouched
            target = self.port_b if port_name == "port_a" else self.port_a
            self._write_serial(target, line)

    def _write_serial(self, port, line: str) -> bool:
        try:
            port.write_line(line)
            self.telem("event", {"event": "serial", "port": port.name,
                                 "dir": "TX", "raw": line})
            return True
        except Exception as e:  # noqa: BLE001
            self.telem("event", {"event": "serial", "port": port.name,
                                 "dir": "TX_ERR", "raw": line, "err": str(e)})
            return False

    def send_st9(self, st9_id: int) -> bool:
        """Send a string from the active analyte's ST9 table to port B."""
        line = self._st9.get(int(st9_id))
        if not line:
            return False
        return self._write_serial(self.port_b, line)

    # ------------------------------------------------------------------
    # Southbound telemetry
    # ------------------------------------------------------------------

    def telem(self, kind: str, data: dict, channel=None, command_id=None) -> int:
        self.local_seq += 1
        msg = {"t": "TELEM", "kind": kind, "channel": channel,
               "command_id": command_id, "local_seq": self.local_seq,
               "time": now_iso(), "data": data}
        self.ring.append(msg)
        self._try_send(msg)
        return self.local_seq

    def _try_send(self, msg: dict):
        if self.writer:
            try:
                self.writer.write(encode(msg))
            except Exception:
                self.writer = None

    async def send(self, msg: dict):
        if self.writer:
            self.writer.write(encode(msg))
            await self.writer.drain()

    def set_state(self, state: str, command_id=None):
        self.state = state
        self.telem("state", {"to": state, "mode": self.mode},
                   command_id=command_id)

    def set_mode(self, new_mode: str, reason: str, latch: bool = False) -> bool:
        """Legacy latch semantics: once ENDPOINT is latched, only a restart
        returns the module to BRIDGE."""
        if self.endpoint_latched and self.mode == "ENDPOINT" and new_mode != "ENDPOINT":
            return False
        self.mode = new_mode
        self.mode_reason = reason
        if latch and new_mode == "ENDPOINT":
            self.endpoint_latched = True
        self.telem("state", {"to": self.state, "mode": self.mode,
                             "mode_reason": reason,
                             "endpoint_latched": self.endpoint_latched})
        return True

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    async def execute(self, cmd: dict):
        cmd_id, cmd_type = cmd["command_id"], cmd["type"]
        params = cmd.get("params") or {}

        if cmd_type == "take_control":
            self.set_mode("ENDPOINT", "latched by take_control", latch=True)
            await self.send({"t": "ACK", "command_id": cmd_id, "accepted": True})
            await self.send({"t": "RESULT", "command_id": cmd_id,
                             "status": "succeeded",
                             "data": {"mode": self.mode, "latched": True}})
            return

        if cmd_type == "bridge":
            ok = self.set_mode("BRIDGE", "requested bridge")
            await self.send({"t": "ACK", "command_id": cmd_id, "accepted": True})
            await self.send({"t": "RESULT", "command_id": cmd_id,
                             "status": "succeeded" if ok else "failed",
                             "data": {"mode": self.mode,
                                      "note": None if ok else
                                      "ENDPOINT latched; restart module to return to BRIDGE"}})
            return

        if cmd_type == "abort":
            await self.send({"t": "ACK", "command_id": cmd_id, "accepted": True})
            if self.busy:
                self._abort.set()
                note = f"abort requested for {self.busy}"
            else:
                note = "nothing to abort"
            await self.send({"t": "RESULT", "command_id": cmd_id,
                             "status": "succeeded", "data": {"note": note}})
            return

        if cmd_type not in ("prime", "calibrate", "sample"):
            await self.send({"t": "ACK", "command_id": cmd_id, "accepted": False,
                             "reason": f"unknown command {cmd_type}"})
            return
        if self.mode != "ENDPOINT":
            await self.send({"t": "ACK", "command_id": cmd_id, "accepted": False,
                             "reason": "Not in ENDPOINT mode"})
            return
        if self.busy:
            await self.send({"t": "ACK", "command_id": cmd_id, "accepted": False,
                             "reason": f"busy: {self.busy}"})
            return
        if cmd_type == "calibrate" and float(params.get("std_conc", 0)) <= 0:
            await self.send({"t": "ACK", "command_id": cmd_id, "accepted": False,
                             "reason": "std_conc must be > 0"})
            return

        self.busy = cmd_type
        self._abort.clear()
        await self.send({"t": "ACK", "command_id": cmd_id, "accepted": True})
        self.set_state({"prime": "priming", "calibrate": "calibrating",
                        "sample": "sampling"}[cmd_type], command_id=cmd_id)
        try:
            await self._run_timeline(cmd_id, cmd_type, params)
        finally:
            self.busy = None
            self.set_state("idle")

    async def _run_timeline(self, cmd_id: str, action: str, params: dict):
        """The method engine — executes the ported ST9 event tables."""
        spec = timeline(action, self.analyte)
        total = spec["total"]
        std_conc = params.get("std_conc")
        captures: dict[str, Optional[float]] = {}
        raw_seqs: list[int] = []
        start = time.monotonic()

        for ev in spec["events"]:
            delay = ev["t"] / self.speed - (time.monotonic() - start)
            if delay > 0:
                try:
                    await asyncio.wait_for(self._abort.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
            if self._abort.is_set():
                await self.send({"t": "RESULT", "command_id": cmd_id,
                                 "status": "aborted",
                                 "data": {"action": action, "at_s": ev["t"]}})
                return
            pct = int(min(99, (ev["t"] / total) * 100))

            if ev["type"] == "send":
                ok = self.send_st9(ev["id"])
                await self.send({"t": "PROGRESS", "command_id": cmd_id, "pct": pct,
                                 "message": f"Sent ST9:{ev['id']} ({'OK' if ok else 'FAIL'})"})
                if not ok:
                    await self.send({"t": "RESULT", "command_id": cmd_id,
                                     "status": "failed",
                                     "data": {"action": action,
                                              "error": f"Failed ST9:{ev['id']}"}})
                    return
            else:  # adc capture
                name = ev["name"]
                sig = self.adc.read({"name": name, "action": action,
                                     "std_conc": std_conc})
                captures[name] = sig.get("vin")
                seq = self.telem("observation",
                                 {"name": name, **sig, "t_method_s": ev["t"]},
                                 channel="detector_raw", command_id=cmd_id)
                raw_seqs.append(seq)
                await self.send({"t": "PROGRESS", "command_id": cmd_id, "pct": pct,
                                 "message": f"Captured {name}={sig.get('vin')}V"})

        remaining = total / self.speed - (time.monotonic() - start)
        if remaining > 0:
            await asyncio.sleep(remaining)

        await self.send({"t": "RESULT", "command_id": cmd_id, "status": "succeeded",
                         "data": {"action": action, "analyte": self.analyte,
                                  "captures": captures,
                                  "params": {k: v for k, v in params.items()}},
                         "raw_local_seqs": raw_seqs})

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health_loop(self):
        while True:
            self.telem("health", {
                "mode": self.mode, "mode_reason": self.mode_reason,
                "endpoint_latched": self.endpoint_latched,
                "state": self.state, "analyte": self.analyte,
                "port_a_ok": self.port_a.ok, "port_a_err": self.port_a.err,
                "port_b_ok": self.port_b.ok, "port_b_err": self.port_b.err,
                "adc_ready": getattr(self.adc, "ready", False),
                "sim": self.sim,
                "uptime_s": int(time.time() - self.t0),
                "buffer_depth": len(self.ring),
            })
            await asyncio.sleep(HEALTH_INTERVAL_S / self.speed)

    # ------------------------------------------------------------------
    # Identity / connection
    # ------------------------------------------------------------------

    def manifest(self) -> dict:
        conc_channels = {"NH4": ["nh4"], "PO4": ["po4"],
                         "NOX": ["nox", "no2", "no3"]}[self.analyte]
        return {
            # instrument-class selects the hub-side profile: which
            # interpreter runs on results, which detections make sense.
            # A foam camera would declare "vision", a centrifuge "rotating";
            # the UII core (adoption, evidence, health, commands) is
            # identical for all classes.
            "instrument_class": "chemical-analyzer",
            "analyte": self.analyte,
            "method": {"id": f"{self.analyte.lower()}-colorimetric-field",
                       "version": "1.0.0"},
            "channels": ([{"name": c, "unit": "mg/L", "derived_by_hub": True}
                          for c in conc_channels]
                         + [{"name": "detector_raw", "unit": "V"}]),
            "commands": [
                {"type": "take_control", "risk": "disruptive"},
                {"type": "bridge", "risk": "disruptive"},
                {"type": "prime", "risk": "routine"},
                {"type": "calibrate", "risk": "disruptive",
                 "params": {"std_conc": "mg/L of the standard"}},
                {"type": "sample", "risk": "routine"},
                {"type": "abort", "risk": "routine"},
            ],
            "requires_control": True,
            "health_interval_s": HEALTH_INTERVAL_S / self.speed,
        }

    def apply_role_config(self, role: str, config: dict):
        self.role = role
        self.role_config = config or {}
        want = (self.role_config.get("analyte") or "").upper()
        if want and want != self.analyte:
            self.telem("event", {
                "event": "config-mismatch",
                "detail": f"role '{role}' expects {want}, module is {self.analyte}"})

    async def run(self):
        self.loop = asyncio.get_event_loop()
        self.port_a.start()
        self.port_b.start()
        backoff = 1.0
        while True:
            try:
                reader, writer = await asyncio.open_connection(*self.hub_addr)
                self.writer = writer
                await self.send({"t": "HELLO", "proto": PROTO,
                                 "module_id": self.module_id,
                                 "type": self.module_type,
                                 "serial": self.serial, "fw": self.fw,
                                 "slot": self.slot,
                                 "manifest": self.manifest()})
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                resp = json.loads(line.decode())

                if resp.get("t") == "QUARANTINE":
                    print(f"[{self.module_id}] QUARANTINED: {resp.get('reason')}"
                          " — powered, mute, awaiting release")
                    while True:  # hold, mute; release closes the socket
                        line = await reader.readline()
                        if not line:
                            break
                        msg = json.loads(line.decode())
                        if msg.get("t") == "QUARANTINE_RELEASED":
                            print(f"[{self.module_id}] released — redialing")
                            break
                    writer.close()
                    self.writer = None
                    await asyncio.sleep(1.0 / self.speed)
                    backoff = 1.0
                    continue

                if resp.get("t") != "HELLO_OK":
                    writer.close()
                    self.writer = None
                    await asyncio.sleep(backoff)
                    backoff = min(15.0, backoff * 1.7)
                    continue

                # -- adopted: role config restored onto this module --------
                self.apply_role_config(resp.get("role"), resp.get("config") or {})
                await self.send({"t": "ROLE_OK", "role": self.role})
                print(f"[{self.module_id}] adopted as role={self.role} "
                      f"(slot {self.slot}, {self.analyte})")
                backoff = 1.0
                # flush telemetry that accumulated while disconnected
                for msg in list(self.ring):
                    self._try_send(msg)
                self.ring.clear()

                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    msg = json.loads(line.decode())
                    if msg.get("t") == "CMD":
                        asyncio.get_event_loop().create_task(self.execute(msg))
                    elif msg.get("t") == "PING":
                        await self.send({"t": "PONG"})
            except (ConnectionRefusedError, ConnectionResetError, OSError,
                    asyncio.TimeoutError, json.JSONDecodeError):
                pass
            self.writer = None
            await asyncio.sleep(backoff)
            backoff = min(15.0, backoff * 1.7)


def main():
    mod = PiModule()
    mode = "SIM" if mod.sim else "REAL HARDWARE"
    print(f"[pimod] {mod.module_id} · {mod.analyte} · {mode} · slot {mod.slot} "
          f"· hub {mod.hub_addr[0]}:{mod.hub_addr[1]} · speed {mod.speed}x")

    async def _run():
        asyncio.get_event_loop().create_task(mod.health_loop())
        await mod.run()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
