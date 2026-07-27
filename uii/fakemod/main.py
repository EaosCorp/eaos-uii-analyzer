"""Fake module — indistinguishable from a real analyzer to the hub.

Dials the hub, speaks HELLO/TELEM/CMD/ACK/PROGRESS/RESULT, executes
prime/calibrate/sample as compressed timelines modeled on the real
NH4MOD gateway (ST9 step sends, i0/i1 detector captures), and produces
synthetic detector physics: a hidden true concentration drives absorbance,
the module only ever reports raw volts. The hub does the interpreting.

  UII_MODULE_ID   (default fm-0001)     UII_HUB   host:port (default 127.0.0.1:7300)
  UII_ANALYTE     NH4|PO4 (default NH4) UII_SPEED timeline compression (default 30x)
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import time
from collections import deque

from ..protocol import PROTO, encode, now_iso

# Hidden physics: conc_true = TRUE_SLOPE * absorbance. The hub never sees
# this constant — it must recover it through a calibrate run.
TRUE_SLOPE = 25.0
I0_VOLTS = 2.400

# Timelines mirror the real gateway's ST9 event tables, compressed by SPEED.
TIMELINES = {
    "prime":     {"total": 300, "steps": [(6, "ST9:35"), (14, "ST9:40"), (197, "ST9:41")]},
    "calibrate": {"total": 420, "steps": [(10, "ST9:42"), (60, "ST9:43"), (240, "ST9:44")],
                  "captures": [(250, "i0"), (400, "i1")]},
    "sample":    {"total": 420, "steps": [(10, "ST9:45"), (60, "ST9:46"), (240, "ST9:47")],
                  "captures": [(250, "i0"), (400, "i1")]},
}


class FakeModule:
    def __init__(self):
        self.module_id = os.environ.get("UII_MODULE_ID", "fm-0001")
        self.analyte = os.environ.get("UII_ANALYTE", "NH4").upper()
        host, port = os.environ.get("UII_HUB", "127.0.0.1:7300").split(":")
        self.hub_addr = (host, int(port))
        self.speed = float(os.environ.get("UII_SPEED", 30))
        self.serial = f"FAKE-2026-{self.module_id[-4:]}"
        self.fw = "0.1.0"
        self.local_seq = 0
        self.ring: deque = deque(maxlen=500)   # telemetry survives hub loss
        self.writer = None
        self.state = "idle"
        self.reagent_pct = 90.0
        self.busy: str | None = None
        self.t0 = time.time()

    # -- hidden truth ------------------------------------------------------

    def true_conc(self) -> float:
        t = time.time() - self.t0
        return 4.5 + 1.5 * math.sin(t / 90.0) + random.gauss(0, 0.05)

    def detector_pair(self, forced_conc: float | None = None) -> tuple[float, float]:
        conc = forced_conc if forced_conc is not None else self.true_conc()
        absorbance = conc / TRUE_SLOPE
        i0 = I0_VOLTS * (1 + random.gauss(0, 0.002))
        i1 = i0 * (10 ** -absorbance) * (1 + random.gauss(0, 0.002))
        return round(i0, 5), round(i1, 5)

    # -- telemetry ----------------------------------------------------------

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
        self.telem("state", {"from": None, "to": state}, command_id=command_id)

    # -- command execution ----------------------------------------------------

    async def execute(self, cmd: dict):
        cmd_id, cmd_type = cmd["command_id"], cmd["type"]
        params = cmd.get("params") or {}
        if self.busy:
            await self.send({"t": "ACK", "command_id": cmd_id, "accepted": False,
                             "reason": f"busy: {self.busy}"})
            return
        if cmd_type == "abort":
            await self.send({"t": "ACK", "command_id": cmd_id, "accepted": True})
            await self.send({"t": "RESULT", "command_id": cmd_id, "status": "succeeded",
                             "data": {"note": "nothing to abort"}})
            return
        spec = TIMELINES.get(cmd_type)
        if not spec:
            await self.send({"t": "ACK", "command_id": cmd_id, "accepted": False,
                             "reason": f"unknown command {cmd_type}"})
            return

        self.busy = cmd_type
        await self.send({"t": "ACK", "command_id": cmd_id, "accepted": True})
        self.set_state(cmd_type.replace("calibrate", "calibrating")
                       .replace("sample", "sampling").replace("prime", "priming"),
                       command_id=cmd_id)
        try:
            captures, raw_seqs = {}, []
            events = ([(t, "send", name) for t, name in spec["steps"]] +
                      [(t, "adc", name) for t, name in spec.get("captures", [])])
            events.sort()
            total = spec["total"] / self.speed
            start = time.monotonic()
            # calibrate runs against the standard, not the stream
            forced = float(params.get("std_conc", 5.0)) if cmd_type == "calibrate" else None

            for t_real, etype, name in events:
                delay = t_real / self.speed - (time.monotonic() - start)
                if delay > 0:
                    await asyncio.sleep(delay)
                pct = int(min(99, (t_real / spec["total"]) * 100))
                if etype == "send":
                    await self.send({"t": "PROGRESS", "command_id": cmd_id,
                                     "pct": pct, "message": f"sent {name} (sim)"})
                else:
                    i0, i1 = self.detector_pair(forced)
                    volts = i0 if name == "i0" else i1
                    seq = self.telem("observation", {"name": name, "volts": volts},
                                     channel="detector_raw", command_id=cmd_id)
                    captures[name] = volts
                    raw_seqs.append(seq)
                    await self.send({"t": "PROGRESS", "command_id": cmd_id,
                                     "pct": pct, "message": f"captured {name}={volts}V"})
            remaining = total - (time.monotonic() - start)
            if remaining > 0:
                await asyncio.sleep(remaining)

            self.reagent_pct = max(0.0, self.reagent_pct - 0.4)
            data = {"i0": captures.get("i0"), "i1": captures.get("i1")} \
                if "i0" in captures else {"note": f"{cmd_type} complete"}
            await self.send({"t": "RESULT", "command_id": cmd_id,
                             "status": "succeeded", "data": data,
                             "raw_local_seqs": raw_seqs})
        finally:
            self.busy = None
            self.set_state("idle")

    # -- health loop ------------------------------------------------------------

    async def health_loop(self):
        while True:
            self.telem("health", {
                "uptime_s": int(time.time() - self.t0),
                "state": self.state, "reagent_a_pct": round(self.reagent_pct, 1),
                "buffer_depth": len(self.ring), "temp_c": round(23 + random.gauss(0, .3), 1),
            })
            await asyncio.sleep(120 / self.speed)

    # -- connection loop ----------------------------------------------------------

    def manifest(self) -> dict:
        return {
            "analyte": self.analyte,
            "method": {"id": f"{self.analyte.lower()}-colorimetric-sim", "version": "0.1.0"},
            "channels": [
                {"name": self.analyte.lower(), "unit": "mg/L", "derived_by_hub": True},
                {"name": "detector_raw", "unit": "V"},
            ],
            "commands": [
                {"type": "prime", "risk": "routine"},
                {"type": "calibrate", "risk": "disruptive",
                 "params": {"std_conc": "mg/L of the standard"}},
                {"type": "sample", "risk": "routine"},
                {"type": "abort", "risk": "routine"},
            ],
        }

    async def run(self):
        backoff = 1.0
        while True:
            try:
                reader, writer = await asyncio.open_connection(*self.hub_addr)
                self.writer = writer
                await self.send({"t": "HELLO", "proto": PROTO,
                                 "module_id": self.module_id, "type": f"fake-{self.analyte.lower()}",
                                 "serial": self.serial, "fw": self.fw,
                                 "manifest": self.manifest()})
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                resp = json.loads(line.decode())
                if resp.get("t") != "HELLO_OK":
                    print(f"[{self.module_id}] {resp.get('t')}: {resp.get('reason')} — retrying in 30s")
                    writer.close()
                    self.writer = None
                    await asyncio.sleep(30 / self.speed * 10)
                    continue
                print(f"[{self.module_id}] adopted as role={resp.get('role')}")
                backoff = 1.0
                # flush the ring (telemetry that accumulated while disconnected)
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
            except (ConnectionRefusedError, OSError, asyncio.TimeoutError, json.JSONDecodeError):
                pass
            self.writer = None
            await asyncio.sleep(backoff)
            backoff = min(15.0, backoff * 1.7)


def main():
    mod = FakeModule()

    async def _run():
        asyncio.get_event_loop().create_task(mod.health_loop())
        await mod.run()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
