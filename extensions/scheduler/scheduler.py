"""Scheduler — schedules attach to ROLES, not serials, so they survive
module swaps (architecture §3). This service is the "bam, ready" half of
auto-recognition: the moment a module is adopted into a role, the role's
cadence resumes against it with zero keyboard work.

Per adopted role, in order:
  1. auto_take_control  — if the module sits in BRIDGE (plant authority)
     and the role says so, request ENDPOINT once.
  2. calibration gate   — a sample is never scheduled without a complete,
     fresh calibration. Missing/stale cal raises a `cal-required` event
     (once) and, if auto_calibrate, submits one calibrate.
  3. cadence            — submit `sample` every sample_interval_s while
     the module is idle.

All submissions go through the command gateway like any other actor, as
`actor: system:scheduler` — the audit trail is unified.

UII_SPEED (shared with pimod sim) divides all intervals for bench runs.
"""
from __future__ import annotations

import calendar
import threading
import time
from typing import Optional

from uii.hub.commands import CommandGateway
from uii.hub.evidence import EvidenceStore
from uii.hub.interpret import calibration_complete
from uii.hub.southbound import SouthboundHub


def _parse_iso(ts: str) -> Optional[float]:
    try:
        return calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return None


class Scheduler(threading.Thread):
    def __init__(self, store: EvidenceStore, southbound: SouthboundHub,
                 gateway: CommandGateway, speed: float = 1.0,
                 tick_s: float = 1.0):
        super().__init__(daemon=True)
        self.store = store
        self.southbound = southbound
        self.gateway = gateway
        self.speed = max(speed, 1e-6)
        self.tick_s = tick_s
        self.stop = threading.Event()
        self._last_sample: dict[str, float] = {}     # role -> monotonic
        self._pending: dict[str, str] = {}           # module_id -> command_id in flight
        self._control_requested: set[str] = set()
        self._cal_flagged: set[str] = set()          # role -> cal-required raised

    def run(self):
        while not self.stop.wait(self.tick_s):
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001 — scheduler must never die
                self.store.append("event",
                                  {"event": "scheduler-error", "error": str(e)},
                                  "urn:uii:schema:event:0.1", actor="system:scheduler")

    # -- one pass over adopted roles -------------------------------------------

    def _tick(self):
        # forget modules that left (a re-adopted module starts fresh in BRIDGE)
        present = set(self.southbound.sessions.keys())
        self._control_requested &= present
        for gone in set(self._pending) - present:
            self._pending.pop(gone, None)

        for session in list(self.southbound.sessions.values()):
            if session.state != "OPERATIONAL":
                continue
            cfg = session.role_config or {}
            interval = cfg.get("sample_interval_s")
            if not interval:
                continue

            # anything already in flight for this module?
            pending = self._pending.get(session.module_id)
            if pending:
                st = self.gateway.status(pending)
                if st and st["state"] != "done":
                    continue
                self._pending.pop(session.module_id, None)

            # 1. control: role wants ENDPOINT, module sits in BRIDGE
            if session.mode == "BRIDGE":
                if cfg.get("auto_take_control") and session.module_id not in self._control_requested:
                    self._control_requested.add(session.module_id)
                    self._submit(session, "take_control", {})
                continue
            elif session.mode not in (None, "ENDPOINT"):
                continue  # unknown mode
            if session.module_state not in (None, "idle"):
                continue

            # 2. calibration gate
            if not self._cal_valid(session, cfg):
                if session.role not in self._cal_flagged:
                    self._cal_flagged.add(session.role)
                    self.store.append(
                        "event", {"event": "cal-required", "role": session.role,
                                  "module": session.module_id},
                        "urn:uii:schema:event:0.1", module=session.module_id,
                        actor="system:scheduler")
                if cfg.get("auto_calibrate"):
                    self._submit(session, "calibrate",
                                 {"std_conc": cfg.get("calibrate_std_conc", 5.0)})
                continue
            self._cal_flagged.discard(session.role)

            # 3. cadence
            now = time.monotonic()
            due = self._last_sample.get(session.role, 0) + float(interval) / self.speed
            if now >= due:
                self._last_sample[session.role] = now
                self._submit(session, "sample", {})

    def _cal_valid(self, session, cfg) -> bool:
        cal = self.store.latest_calibration(session.module_id)
        if not cal:
            return False
        analyte = (cal["data"].get("analyte")
                   or session.manifest.get("analyte") or "NH4")
        fit = cal["data"].get("fit", {})
        if not (calibration_complete(analyte, fit)
                or any("slope" in k and v is not None for k, v in fit.items())):
            return False
        max_age = cfg.get("cal_max_age_s")
        if max_age:
            ts = _parse_iso(cal["time"])
            if ts is not None and (time.time() - ts) > float(max_age) / self.speed:
                return False
        return True

    def _submit(self, session, cmd_type: str, params: dict):
        env, problem = self.gateway.submit(
            session.module_id, cmd_type, params, actor="system:scheduler")
        if env:
            self._pending[session.module_id] = env["data"]["command_id"]
