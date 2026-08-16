"""campod — the vision module agent. refmod with a lens underneath.

The smallest honest camera module: dial the hub, announce
instrument_class "vision", accept a role, and produce FACTS — frames, as a
content hash plus capture metadata, never "there is foam." Two data shapes,
both requested by the field:

  * telemetry  — a frame every `cadence_s`, streamed as a `frames`
                 observation (command_id=None). The hub's FoamService
                 interprets the stream into foam_coverage.
  * on demand  — a `capture` command grabs a frame now and returns its hash
                 in the RESULT; the vision interpreter turns that one into
                 foam_coverage tied to the command's lineage.

Frames are written to the shared blob store (UII_FRAMES_DIR); the envelope
carries only the hash. Buffers through hub loss and redials, exactly like
refmod. UII_SIM=1 swaps the live Reolink for a synthetic basin; this file is
byte-identical against sim and real.

  UII_HUB 127.0.0.1:7300   UII_MODULE_ID campod-01   UII_SLOT slot-1
  UII_SERIAL SN-...        UII_TYPE vision-cam        UII_SPEED 1
  UII_FRAMES_DIR ./data/frames    UII_CADENCE_S 10
  (real) CAMERA_HOST/CAMERA_USER/CAMERA_PASSWORD from /etc/eaos/camera-credentials.env

Run: python3 -m uii_vision.campod
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque

from uii.protocol import PROTO, encode, now_iso

from .blobstore import FrameBlobStore
from .framesource import make_source, encode_jpeg, downscale


class CamModule:
    def __init__(self, env=None):
        e = env or os.environ
        self.module_id = e.get("UII_MODULE_ID", "campod-01")
        self.slot = e.get("UII_SLOT", "slot-1")
        self.serial = e.get("UII_SERIAL", f"SN-{self.module_id.upper()}")
        self.module_type = e.get("UII_TYPE", "vision-cam")
        host, port = e.get("UII_HUB", "127.0.0.1:7300").rsplit(":", 1)
        self.hub_addr = (host, int(port))
        self.speed = max(float(e.get("UII_SPEED", 1)), 1e-6)
        self.cadence_s = float(e.get("UII_CADENCE_S", 10))
        self.jpeg_q = int(e.get("UII_JPEG_QUALITY", 85))
        self.max_w = int(e.get("UII_FRAME_MAX_W", 1920))   # store downscaled (cellular)
        self.blobs = FrameBlobStore(e.get("UII_FRAMES_DIR", "./data/frames"))
        self.source = make_source(e)

        self.writer = None
        self.role = None
        self.state = "idle"
        self.busy = None
        self.local_seq = 0
        self.ring: deque = deque(maxlen=500)
        self.t0 = time.time()
        self.frames_captured = 0
        self.reconnects = 0
        self._last_health_frames = 0

    # -- southbound telemetry (identical contract to refmod) ------------------

    def telem(self, kind, data, channel=None, command_id=None) -> int:
        self.local_seq += 1
        msg = {"t": "TELEM", "kind": kind, "channel": channel,
               "command_id": command_id, "local_seq": self.local_seq,
               "time": now_iso(), "data": data}
        self._send_or_buffer(msg)
        return self.local_seq

    def _send_or_buffer(self, msg):
        if self.writer:
            try:
                self.writer.write(encode(msg))
                return
            except Exception:
                self.writer = None
        self.ring.append(msg)

    async def send(self, msg):
        if self.writer:
            try:
                self.writer.write(encode(msg))
                await self.writer.drain()
                return
            except (ConnectionResetError, BrokenPipeError, OSError):
                self.writer = None
        self.ring.append(msg)

    # -- the one thing this module does: turn a lens into a frame fact --------

    def _grab_frame(self, command=None) -> dict:
        """Grab one frame, downscale + JPEG it, store by hash, return the fact."""
        rgb, meta = self.source.grab(command=command)
        rgb = downscale(rgb, self.max_w)
        data = encode_jpeg(rgb, self.jpeg_q)
        digest = self.blobs.put(data, ext="jpg")
        self.frames_captured += 1
        meta = {**meta, "w": rgb.shape[1], "h": rgb.shape[0], "bytes": len(data)}
        return {"frame_hash": digest, **meta}

    def emit_frame(self, command_id=None, reference=False, on_demand=False) -> tuple[int, dict]:
        fact = self._grab_frame()
        fact["reference"] = reference
        fact["on_demand"] = on_demand
        seq = self.telem("observation", fact, channel="frames", command_id=command_id)
        return seq, fact

    # -- command execution ----------------------------------------------------

    async def execute(self, cmd):
        cmd_id, cmd_type = cmd["command_id"], cmd["type"]
        params = cmd.get("params") or {}

        if cmd_type == "abort":
            await self.send({"t": "ACK", "command_id": cmd_id, "accepted": True})
            await self.send({"t": "RESULT", "command_id": cmd_id,
                             "status": "succeeded", "data": {"note": "idle"}})
            return

        known = {"capture", "set_cadence", "re_baseline", "set_region",
                 "set_exposure", "ptz"}
        if cmd_type not in known:
            await self.send({"t": "ACK", "command_id": cmd_id, "accepted": False,
                             "reason": f"unknown command {cmd_type}"})
            return
        if self.busy:
            await self.send({"t": "ACK", "command_id": cmd_id, "accepted": False,
                             "reason": f"busy: {self.busy}"})
            return

        await self.send({"t": "ACK", "command_id": cmd_id, "accepted": True})

        try:
            if cmd_type == "capture":
                seq, fact = self.emit_frame(command_id=cmd_id, on_demand=True)
                await self.send({"t": "PROGRESS", "command_id": cmd_id, "pct": 90,
                                 "message": f"captured {fact['frame_hash'][:12]}"})
                await self.send({"t": "RESULT", "command_id": cmd_id,
                                 "status": "succeeded",
                                 "data": {"action": "capture", **fact},
                                 "raw_local_seqs": [seq]})

            elif cmd_type == "re_baseline":
                seq, fact = self.emit_frame(command_id=cmd_id, reference=True)
                await self.send({"t": "RESULT", "command_id": cmd_id,
                                 "status": "succeeded",
                                 "data": {"action": "re_baseline", **fact},
                                 "raw_local_seqs": [seq]})

            elif cmd_type == "set_cadence":
                self.cadence_s = float(params.get("cadence_s", self.cadence_s))
                await self.send({"t": "RESULT", "command_id": cmd_id,
                                 "status": "succeeded",
                                 "data": {"action": "set_cadence",
                                          "cadence_s": self.cadence_s}})

            elif cmd_type == "set_region":
                # ROI is interpreted on the hub; the module just echoes it back
                # into evidence so the reference record has provenance.
                await self.send({"t": "RESULT", "command_id": cmd_id,
                                 "status": "succeeded",
                                 "data": {"action": "set_region",
                                          "roi": params.get("roi")}})

            elif cmd_type == "set_exposure":
                if hasattr(self.source, "set"):
                    self.source.set(quality=params.get("quality"))
                self.telem("state", {"exposure": params}, command_id=cmd_id)
                await self.send({"t": "RESULT", "command_id": cmd_id,
                                 "status": "succeeded",
                                 "data": {"action": "set_exposure", **params}})

            elif cmd_type == "ptz":
                # moving the view invalidates every ROI/baseline (vision-profile §6)
                self.telem("event", {"event": "view-moved", "ptz": params},
                           command_id=cmd_id)
                await self.send({"t": "RESULT", "command_id": cmd_id,
                                 "status": "succeeded",
                                 "data": {"action": "ptz", "re_baseline_required": True,
                                          **params}})
        except Exception as e:  # a bad grab is a failed command, never a crash
            await self.send({"t": "RESULT", "command_id": cmd_id,
                             "status": "failed", "data": {"error": repr(e)[:200]}})

    # -- loops ----------------------------------------------------------------

    def manifest(self):
        return {
            "instrument_class": "vision",
            "observables": ["foam_coverage", "foam_type"],
            "model": {"id": "foam-cv", "version": "0.1.0"},
            "channels": [
                {"name": "frames", "unit": "frame"},
                {"name": "foam_coverage", "unit": "%", "derived_by_hub": True},
                {"name": "foam_type", "unit": "class", "derived_by_hub": True},
                {"name": "image_quality", "unit": "score", "derived_by_hub": True},
            ],
            "commands": [
                {"type": "capture", "risk": "routine",
                 "doc": "grab one frame now; returns its hash"},
                {"type": "set_cadence", "risk": "routine",
                 "params": {"cadence_s": {"type": "number", "min": 1, "unit": "s"}}},
                {"type": "re_baseline", "risk": "disruptive",
                 "preconditions": ["state:idle"],
                 "doc": "capture a new reference scene the profile compares against"},
                {"type": "set_region", "risk": "disruptive",
                 "params": {"roi": {"type": "object", "required": True,
                                    "doc": "fractional rect x0,y0,x1,y1"}}},
                {"type": "set_exposure", "risk": "routine",
                 "params": {"quality": {"type": "number"}}},
                {"type": "ptz", "risk": "disruptive",
                 "doc": "moves the view; forces re-baseline"},
                {"type": "abort", "risk": "routine"},
            ],
            "health_interval_s": 60 / self.speed,
            "cadence_s": self.cadence_s,
        }

    async def cadence_loop(self):
        """Stream a frame every cadence_s — the telemetry shape."""
        while True:
            await asyncio.sleep(max(self.cadence_s / self.speed, 0.02))
            if self.role and not self.busy:
                try:
                    self.emit_frame()
                except Exception:
                    pass

    async def health_loop(self):
        while True:
            fps = ((self.frames_captured - self._last_health_frames) /
                   max(60 / self.speed, 1e-6))
            self._last_health_frames = self.frames_captured
            self.telem("health", {"state": self.state,
                                   "uptime_s": int(time.time() - self.t0),
                                   "frames_captured": self.frames_captured,
                                   "fps": round(fps, 3),
                                   "reconnects": self.reconnects,
                                   "source": self.source.__class__.__name__,
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
                    while True:
                        line = await reader.readline()
                        if not line or json.loads(line.decode()).get("t") == "QUARANTINE_RELEASED":
                            break
                    writer.close(); self.writer = None
                    await asyncio.sleep(1.0 / self.speed)
                    continue

                if resp.get("t") != "HELLO_OK":
                    writer.close(); self.writer = None
                    await asyncio.sleep(backoff); backoff = min(15.0, backoff * 1.7)
                    continue

                self.role = resp.get("role")
                cfg = resp.get("config") or {}          # role config follows the slot
                self.cadence_s = float(cfg.get("cadence_s",
                                       cfg.get("capture_interval_s", self.cadence_s)))
                await self.send({"t": "ROLE_OK", "role": self.role})
                self.telem("state", {"to": "idle", "mode": "managed"})  # ready to capture
                print(f"[{self.module_id}] adopted as role={self.role} "
                      f"source={self.source.__class__.__name__} cadence={self.cadence_s}s")
                backoff = 1.0
                for msg in list(self.ring):
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
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(15.0, backoff * 1.7)


def main():
    mod = CamModule()
    print(f"[campod] {mod.module_id} · slot {mod.slot} · hub "
          f"{mod.hub_addr[0]}:{mod.hub_addr[1]} · cadence {mod.cadence_s}s")

    async def _run():
        asyncio.get_event_loop().create_task(mod.health_loop())
        asyncio.get_event_loop().create_task(mod.cadence_loop())
        await mod.run()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
