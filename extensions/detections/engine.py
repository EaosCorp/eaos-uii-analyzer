"""Detections — the hub's own configurable status engine.

Rules declared in hub.json are evaluated continuously over the evidence
stream; what they assert comes back OUT as evidence (`event` envelopes,
schema urn:uii:schema:alert:0.1, causation-linked to the envelope that
triggered them). The audit property stays structural: an alert that fired,
cleared, or was acknowledged is part of the same hash-chained log as the
data it was about.

Design lineage (see docs/detections.md for the full rationale):
  * ISA-18.2 / IEC 62682 — alarm lifecycle (inactive → pending → active →
    cleared), severity rationalization, and STATE-BASED SUPPRESSION
    (suppress_in_states: don't alarm "flow unstable" while priming — the
    single most effective nuisance-alarm killer).
  * Debounce (`for`-duration) + hysteresis (separate raise/clear
    thresholds) from SCADA deadband practice and Prometheus alerting.
  * NAMUR NE 107 — every module's active alerts roll up to one of four
    standardized status signals (failure / check_function /
    out_of_specification / maintenance_required) so operators and, later,
    the OT adapter get ONE glanceable state per module.

Rule types (v0):
  threshold      raise_above/clear_below (or raise_below/clear_above) on a
                 channel, debounce_s, suppress_in_states
  stale_data     no good observation on a channel for window_s — fires when
                 a module dies, is removed, or silently stops producing
  cal_overdue    newest calibration older than max_age_s (or none at all)
  health_flag    a field on health telemetry equals a value (port_b_ok
                 false, adc_ready false, ...) sustained debounce_s
  quality_streak N consecutive non-good observations on a channel

Deliberately NOT here yet (documented in docs/detections.md): shelving,
out-of-service, flood suppression/eclipsing, Westgard/Levey-Jennings QC
multirules, trend/drift detection, northbound routing.
"""
from __future__ import annotations

import calendar
import queue
import threading
import time
from typing import Optional

from uii.hub.evidence import EvidenceStore
from uii.hub.southbound import SouthboundHub

SEVERITIES = ("info", "warning", "alert", "critical")

# default severity -> NE107 signal; a rule can override with "ne107"
NE107_DEFAULT = {"critical": "failure", "alert": "out_of_specification",
                 "warning": "maintenance_required", "info": None}
NE107_RANK = {None: 0, "maintenance_required": 1, "out_of_specification": 2,
              "check_function": 2, "failure": 3}


def _parse_iso(ts: str) -> Optional[float]:
    try:
        return calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return None


class _Instance:
    """One rule evaluated against one module: the alarm state machine."""

    __slots__ = ("state", "since", "acked", "value", "streak", "last_obs_t")

    def __init__(self):
        self.state = "inactive"      # inactive | pending | active
        self.since = 0.0
        self.acked = False
        self.value = None
        self.streak = 0
        self.last_obs_t: Optional[float] = None


class Detections(threading.Thread):
    def __init__(self, store: EvidenceStore, southbound: SouthboundHub,
                 rules: list[dict], speed: float = 1.0, tick_s: float = 0.5):
        super().__init__(daemon=True)
        self.store = store
        self.southbound = southbound
        self.rules = [r for r in rules if r.get("id") and r.get("type")]
        self.speed = max(speed, 1e-6)
        self.tick_s = tick_s
        self.stop = threading.Event()
        self.t0 = time.time()
        self._sub = store.subscribe()
        # (rule_id, module) -> _Instance
        self.instances: dict[tuple[str, str], _Instance] = {}
        # caches fed from the stream
        self._latest_obs: dict[tuple[str, str], dict] = {}    # (module, channel)
        self._latest_health: dict[str, dict] = {}             # module -> data

    # -- public views ----------------------------------------------------------

    def active_alerts(self) -> list[dict]:
        out = []
        for (rule_id, module), inst in self.instances.items():
            if inst.state != "active":
                continue
            rule = self._rule(rule_id)
            out.append({"rule": rule_id, "module": module,
                        "severity": rule.get("severity", "warning"),
                        "ne107": self._ne107_of(rule),
                        "message": rule.get("message", rule_id),
                        "value": inst.value, "acked": inst.acked,
                        "since_s_ago": round(time.time() - inst.since, 1)})
        return out

    def ack(self, rule_id: str, module: str, actor: str) -> bool:
        inst = self.instances.get((rule_id, module))
        if not inst or inst.state != "active" or inst.acked:
            return False
        inst.acked = True
        self.store.append(
            "audit", {"event": "alert-acknowledged", "rule": rule_id},
            "urn:uii:schema:audit:0.1", module=module, actor=actor)
        return True

    def ne107_status(self, module_id: str) -> str:
        """One glanceable NE107-style status per module."""
        session = self.southbound.sessions.get(module_id)
        if not session or session.state in ("REMOVED", "QUARANTINED"):
            return "failure"
        if session.state == "DEGRADED":
            return "failure"
        worst = None
        for a in self.active_alerts():
            if a["module"] == module_id and NE107_RANK.get(a["ne107"], 0) > NE107_RANK.get(worst, 0):
                worst = a["ne107"]
        if worst:
            return worst
        if session.module_state in ("priming", "calibrating"):
            return "check_function"   # deliberate intervention in progress
        return "ok"

    # -- engine ------------------------------------------------------------------

    def run(self):
        while not self.stop.wait(self.tick_s):
            try:
                self._drain()
                self._evaluate()
                self._watchdog()
            except Exception as e:  # noqa: BLE001 — the engine must never die
                self.store.append("event",
                                  {"event": "detections-error", "error": str(e)},
                                  "urn:uii:schema:event:0.1",
                                  actor="system:detections")

    def _drain(self):
        while True:
            try:
                env = self._sub.get_nowait()
            except queue.Empty:
                return
            module = (env.get("source") or {}).get("module")
            channel = (env.get("source") or {}).get("channel")
            if not module:
                continue
            if env["kind"] == "observation" and channel and channel != "detector_raw":
                self._latest_obs[(module, channel)] = env
                self._on_observation(module, channel, env)
            elif env["kind"] == "health":
                self._latest_health[module] = env.get("data") or {}

    def _watchdog(self):
        """Health supervision (moved here from core): a module that goes
        quiet is DEGRADED — flagged, no commands — until traffic resumes
        (the session reader marks recovery)."""
        now = time.time()
        for s in list(self.southbound.sessions.values()):
            interval = float(s.manifest.get("health_interval_s") or 60)
            threshold = max(3 * interval, 5.0)
            if s.state == "OPERATIONAL" and now - s.last_seen > threshold:
                s.state = "DEGRADED"
                s.identity_event("degraded", {
                    "reason": f"no traffic for {int(now - s.last_seen)}s "
                              f"(health interval {int(interval)}s)"})

    def _rule(self, rule_id: str) -> dict:
        return next((r for r in self.rules if r["id"] == rule_id), {})

    def _ne107_of(self, rule: dict) -> Optional[str]:
        return rule.get("ne107", NE107_DEFAULT.get(rule.get("severity", "warning")))

    def _modules_for(self, rule: dict) -> list:
        out = []
        for s in self.southbound.sessions.values():
            if rule.get("module") and s.module_id != rule["module"]:
                continue
            if rule.get("role") and s.role != rule["role"]:
                continue
            if rule.get("channel"):
                names = [c.get("name") for c in s.manifest.get("channels", [])]
                if rule["channel"] not in names:
                    continue
            out.append(s)
        return out

    def _suppressed(self, rule: dict, session) -> bool:
        """ISA-18.2 'suppressed by design': hold evaluation during declared
        process states instead of alarming through them."""
        states = rule.get("suppress_in_states")
        return bool(states and session and session.module_state in states)

    def _role_of(self, module: str) -> Optional[str]:
        s = self.southbound.sessions.get(module)
        if s:
            return s.role
        return (getattr(self.southbound, "registry", {})
                .get(module) or {}).get("role")

    def _selected(self, rule: dict, module: str) -> bool:
        if rule.get("module") and rule["module"] != module:
            return False
        if rule.get("role") and self._role_of(module) != rule["role"]:
            return False
        return True

    # -- event-driven rules --------------------------------------------------------

    def _on_observation(self, module: str, channel: str, env: dict):
        good = (env.get("quality") or {}).get("status") == "good"
        value = (env.get("data") or {}).get("value")
        for rule in self.rules:
            if rule.get("channel") != channel:
                continue
            if not self._selected(rule, module):
                continue
            key = (rule["id"], module)
            inst = self.instances.setdefault(key, _Instance())
            session = self.southbound.sessions.get(module)

            if rule["type"] == "stale_data" and good:
                inst.last_obs_t = time.time()
                if inst.state == "active":
                    self._transition(rule, module, inst, "cleared", env, value)

            elif rule["type"] == "threshold" and good and value is not None:
                if self._suppressed(rule, session):
                    continue
                inst.value = value
                breach = ((rule.get("raise_above") is not None and value > rule["raise_above"])
                          or (rule.get("raise_below") is not None and value < rule["raise_below"]))
                clear = ((rule.get("raise_above") is not None
                          and value < rule.get("clear_below", rule["raise_above"]))
                         or (rule.get("raise_below") is not None
                             and value > rule.get("clear_above", rule["raise_below"])))
                if inst.state == "inactive" and breach:
                    inst.state, inst.since = "pending", time.time()
                    if not rule.get("debounce_s"):
                        self._transition(rule, module, inst, "raised", env, value)
                elif inst.state == "pending" and not breach:
                    inst.state = "inactive"
                elif inst.state == "active" and clear:
                    self._transition(rule, module, inst, "cleared", env, value)

            elif rule["type"] == "quality_streak":
                inst.streak = 0 if good else inst.streak + 1
                n = int(rule.get("count", 3))
                if inst.state != "active" and inst.streak >= n:
                    self._transition(rule, module, inst, "raised", env,
                                     inst.streak)
                elif inst.state == "active" and good:
                    self._transition(rule, module, inst, "cleared", env, 0)

    # -- time-driven rules -----------------------------------------------------------

    def _evaluate(self):
        now = time.time()
        for rule in self.rules:
            rtype = rule["type"]

            if rtype == "threshold":
                # promote pending -> active after debounce
                for (rid, module), inst in self.instances.items():
                    if rid != rule["id"] or inst.state != "pending":
                        continue
                    if now - inst.since >= float(rule.get("debounce_s", 0)) / self.speed:
                        self._transition(rule, module, inst, "raised", None,
                                         inst.value)

            elif rtype == "stale_data":
                window = float(rule.get("window_s", 3600)) / self.speed
                # evaluate against every module that ever produced the channel
                # (a REMOVED module is exactly when this must keep firing) and
                # against currently adopted modules that should be producing
                seen = {m for (m, ch) in self._latest_obs if ch == rule.get("channel")}
                for s in self._modules_for(rule):
                    seen.add(s.module_id)
                for module in seen:
                    if not self._selected(rule, module):
                        continue
                    key = (rule["id"], module)
                    inst = self.instances.setdefault(key, _Instance())
                    # anti-stale-alarm: a REMOVED module's staleness is moot
                    # once another module holds its role and is producing —
                    # the removal itself lives in the identity log
                    if module not in self.southbound.sessions and \
                            self._role_superseded(rule, module, window, now):
                        if inst.state == "active":
                            self._transition(rule, module, inst, "cleared",
                                             None, "role superseded")
                        continue
                    baseline = inst.last_obs_t or self.t0
                    if inst.state != "active" and now - baseline > window:
                        self._transition(rule, module, inst, "raised", None,
                                         round(now - baseline, 1))

            elif rtype == "cal_overdue":
                max_age = float(rule.get("max_age_s", 7 * 86400)) / self.speed
                for s in self._modules_for(rule):
                    key = (rule["id"], s.module_id)
                    inst = self.instances.setdefault(key, _Instance())
                    cal = self.store.latest_calibration(s.module_id)
                    ts = _parse_iso(cal["time"]) if cal else None
                    overdue = (now - ts > max_age) if ts else (now - self.t0 > max_age)
                    if inst.state != "active" and overdue:
                        self._transition(rule, s.module_id, inst, "raised", None,
                                         round(now - ts, 1) if ts else None)
                    elif inst.state == "active" and not overdue:
                        self._transition(rule, s.module_id, inst, "cleared", None, None)

            elif rtype == "health_flag":
                field, want = rule.get("field"), rule.get("equals")
                for s in self._modules_for(rule):
                    data = self._latest_health.get(s.module_id)
                    if data is None or field not in data:
                        continue
                    key = (rule["id"], s.module_id)
                    inst = self.instances.setdefault(key, _Instance())
                    hit = data.get(field) == want
                    if inst.state == "inactive" and hit:
                        inst.state, inst.since = "pending", now
                    elif inst.state == "pending":
                        if not hit:
                            inst.state = "inactive"
                        elif now - inst.since >= float(rule.get("debounce_s", 0)) / self.speed:
                            self._transition(rule, s.module_id, inst, "raised",
                                             None, data.get(field))
                    elif inst.state == "active" and not hit:
                        self._transition(rule, s.module_id, inst, "cleared",
                                         None, data.get(field))

    def _role_superseded(self, rule: dict, module: str, window: float,
                         now: float) -> bool:
        """True when another module now holds this module's role and has
        produced fresh data within the staleness window."""
        role = self._role_of(module)
        if not role:
            return False
        for s in self.southbound.sessions.values():
            if s.module_id == module or s.role != role:
                continue
            other = self.instances.get((rule["id"], s.module_id))
            if other and other.last_obs_t and now - other.last_obs_t <= window:
                return True
        return False

    # -- transitions -------------------------------------------------------------------

    def _transition(self, rule: dict, module: str, inst: _Instance,
                    to: str, trigger_env: Optional[dict], value):
        if to == "raised":
            inst.state, inst.since, inst.acked = "active", time.time(), False
        else:
            inst.state = "inactive"
            inst.last_obs_t = inst.last_obs_t  # keep stale baseline
        self.store.append(
            "event",
            {"event": "alert", "alert_state": to, "rule": rule["id"],
             "severity": rule.get("severity", "warning"),
             "ne107": self._ne107_of(rule),
             "message": rule.get("message", rule["id"]), "value": value},
            "urn:uii:schema:alert:0.1", module=module,
            actor="system:detections",
            trace={"causation_id": trigger_env["id"]} if trigger_env else None)
