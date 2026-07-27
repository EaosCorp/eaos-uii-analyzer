"""refmod — the reference module. The other half of the seam, minimal.

This is the smallest honest implementation of the module contract: dial
the hub, announce identity, accept your role, produce FACTS (raw detector
volts, never concentrations), execute commands with ack/progress/result
semantics, buffer through hub loss. ~180 lines. If you are building a new
instrument class (a camera, a centrifuge), start by reading this file —
your module agent is this file with different hardware underneath.

Simulated chemistry: a hidden true concentration drives absorbance; the
module only ever reports volts. The hub must recover the hidden slope
(25.0) through a calibrate run — the same epistemics as the field.

  UII_HUB        host:port (default 127.0.0.1:7300)
  UII_MODULE_ID  (default refmod-01)     UII_SLOT   (default slot-1)
  UII_SERIAL     (default SN-<id>)       UII_SPEED  compression (default 30)

Run: python3 -m uii.refmod
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import time
from collections import deque

from .protocol import PROTO, encode, now_iso

TRUE_SLOPE = 25.0     # hidden truth; the hub must earn it via calibration
I0_VOLTS = 2.400

# a compressed two-point method: capture names match the hub's
# chemical-analyzer interpreter (DIW blank, STD standard, SAMP stream)
TIMELINES = {
    "calibrate": {"total": 40, "captures": [
        (6,  "NH4_CAL_DIW_I0"), (12, "NH4_CAL_DIW_I1"),
        (22, "NH4_CAL_STD_I0"), (34, "NH4_CAL_STD_I1")]},
    "sample":    {"total": 24, "captures": [
        (6,  "NH4_SAMP_I0"), (18, "NH4_SAMP_I1")]},
}


class RefModule:
    def __init__(self, env=None):
        e = env or os.environ
        self.module_id = e.get("UII_MODULE_ID", "refmod-01")
        self.slot = e.get("UII_SLOT", "slot-1")
        self.serial = e.get("UII_SERIAL", f"SN-{self.module_id.upper()}")
        # UII_TYPE lets a bench present as a foreign/vendor module
        self.module_type = e.get("UII_TYPE", "refmod-nh4")
        host, port = e.get("UII_HUB", "127.0.0.1:7300").rsplit(":", 1)
        self.hub_addr = (host, int(port))
        self.speed = max(float(e.get("UII_SPEED", 30)), 1e-6)
        self.writer = None
        self.role = None
        self.state = "idle"
        self.busy = None
        self.local_seq = 0
        self.ring: deque = deque(maxlen=500)   # telemetry survives hub loss
        self.t0 = time.time()
        self._pending_i0: dict[str, tuple[float, float]] = {}

    # -- the hidden physics ---------------------------------------------------

    def true_conc(self) -> float:
        t = time.time() - self.t0
        return max(0.05, 4.5 + 1.1 * math.sin(t / 90.0) + random.gauss(0, 0.05))

    def detector_volts(self, name: str, std_conc) -> float:
        conc = (0.0 if "_DIW_" in name
                else float(std_conc or 5.0) if "_STD_" in name
                else self.true_conc())
        pair = name.replace("_I0", "").replace("_I1", "")
        if "_I0" in name:
            i0 = I0_VOLTS * (1 + random.gauss(0, 0.002))
            self._pending_i0[pair] = (i0, conc / TRUE_SLOPE)
            return round(i0, 5)
        i0, a = self._pending_i0.pop(pair, (I0_VOLTS, conc / TRUE_SLOPE))
        return round(i0 * (10 ** -a) * (1 + random.gauss(0, 0.002)), 5)

    # -- southbound telemetry ---------------------------------------------------

    def telem(self, kind, data, channel=None, command_id=None) -> int:
        self.local_seq += 1
        msg = {"t": "TELEM", "kind": kind, "channel": channel,
               "command_id": command_id, "local_seq": self.local_seq,
               "time": now_iso(), "data": data}
        self.ring.append(msg)
        if self.writer:
            try:
                self.writer.write(encode(msg))
            except Exception:
                self.writer = None
        return self.local_seq

    async def send(self, msg):
        if self.writer:
            self.writer.write(encode(msg))
            await self.writer.drain()

    # -- command execution --------------------------------------------------------

    async def execute(self, cmd):
        cmd_id, cmd_type = cmd["command_id"], cmd["type"]
        params = cmd.get("params") or {}
        if cmd_type == "abort":
            await self.send({"t": "ACK", "command_id": cmd_id, "accepted": True})
            await self.send({"t": "RESULT", "command_id": cmd_id,
                             "status": "succeeded", "data": {"note": "idle"}})
            return
        spec = TIMELINES.get(cmd_type)
        if not spec:
            await self.send({"t": "ACK", "command_id": cmd_id, "accepted": False,
                             "reason": f"unknown command {cmd_type}"})
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
        await self.send({"t": "ACK", "command_id": cmd_id, "accepted": True})
        self.state = "calibrating" if cmd_type == "calibrate" else "sampling"
        self.telem("state", {"to": self.state}, command_id=cmd_id)
        try:
            captures, raw_seqs = {}, []
            start = time.monotonic()
            for t_s, name in spec["captures"]:
                delay = t_s / self.speed - (time.monotonic() - start)
                if delay > 0:
                    await asyncio.sleep(delay)
                volts = self.detector_volts(name, params.get("std_conc"))
                captures[name] = volts
                raw_seqs.append(self.telem(
                    "observation", {"name": name, "vin": volts},
                    channel="detector_raw", command_id=cmd_id))
                await self.send({"t": "PROGRESS", "command_id": cmd_id,
                                 "pct": int(95 * t_s / spec["total"]),
                                 "message": f"captured {name}={volts}V"})
            await self.send({"t": "RESULT", "command_id": cmd_id,
                             "status": "succeeded",
                             "data": {"action": cmd_type, "analyte": "NH4",
                                      "captures": captures, "params": params},
                             "raw_local_seqs": raw_seqs})
        finally:
            self.busy = None
            self.state = "idle"
            self.telem("state", {"to": "idle"})

    # -- connection loop ------------------------------------------------------------

    def manifest(self):
        return {
            "instrument_class": "chemical-analyzer",
            "analyte": "NH4",
            "method": {"id": "nh4-two-point-ref", "version": "0.1.0"},
            "channels": [{"name": "nh4", "unit": "mg/L", "derived_by_hub": True},
                         {"name": "detector_raw", "unit": "V"}],
            "commands": [
                {"type": "calibrate", "risk": "disruptive",
                 "params": {"std_conc": "mg/L of the standard"}},
                {"type": "sample", "risk": "routine"},
                {"type": "abort", "risk": "routine"},
            ],
            "health_interval_s": 60 / self.speed,
        }

    async def health_loop(self):
        while True:
            self.telem("health", {"state": self.state,
                                  "uptime_s": int(time.time() - self.t0),
                                  "buffer_depth": len(self.ring)})
            await asyncio.sleep(60 / self.speed)

    async def run(self):
        backoff = 1.0
        while True:
            try:
                reader, writer = await asyncio.open_connection(*self.hub_addr)
                self.writer = writer
                await self.send({"t": "HELLO", "proto": PROTO,
                                 "module_id": self.module_id,
                                 "type": self.module_type, "serial": self.serial,
                                 "fw": "0.1.0", "slot": self.slot,
                                 "manifest": self.manifest()})
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                resp = json.loads(line.decode())

                if resp.get("t") == "QUARANTINE":
                    print(f"[{self.module_id}] QUARANTINED: {resp.get('reason')}")
                    while True:   # powered, mute; release closes the socket
                        line = await reader.readline()
                        if not line or json.loads(line.decode()).get("t") == "QUARANTINE_RELEASED":
                            break
                    writer.close()
                    self.writer = None
                    await asyncio.sleep(1.0 / self.speed)
                    continue

                if resp.get("t") != "HELLO_OK":
                    writer.close()
                    self.writer = None
                    await asyncio.sleep(backoff)
                    backoff = min(15.0, backoff * 1.7)
                    continue

                self.role = resp.get("role")
                await self.send({"t": "ROLE_OK", "role": self.role})
                print(f"[{self.module_id}] adopted as role={self.role}")
                backoff = 1.0
                for msg in list(self.ring):   # flush buffered telemetry
                    self.writer.write(encode(msg))
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
    mod = RefModule()
    print(f"[refmod] {mod.module_id} · slot {mod.slot} · hub "
          f"{mod.hub_addr[0]}:{mod.hub_addr[1]} · speed {mod.speed}x")

    async def _run():
        asyncio.get_event_loop().create_task(mod.health_loop())
        await mod.run()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
