"""Detections engine — rule mechanics unit-tested against a real store with
synthetic envelopes (no processes), plus one live-bench integration case."""
import os
import tempfile
import unittest

from extensions.detections.engine import Detections
from uii.hub.evidence import EvidenceStore

from ..helpers import Bench, get, post, wait_for


class FakeSession:
    def __init__(self, module_id, role="r1", channels=("nh4",),
                 module_state="idle", state="OPERATIONAL"):
        self.module_id = module_id
        self.role = role
        self.state = state
        self.module_state = module_state
        self.manifest = {"channels": [{"name": c} for c in channels]}


class FakeSouthbound:
    def __init__(self, *sessions):
        self.sessions = {s.module_id: s for s in sessions}
        self.registry = {}


def _engine(rules, sessions=None, speed=1.0):
    tmp = tempfile.mkdtemp(prefix="uii-det-")
    store = EvidenceStore(os.path.join(tmp, "e.db"), "hub-t")
    sb = FakeSouthbound(*(sessions or [FakeSession("m1")]))
    eng = Detections(store, sb, rules, speed=speed)
    return store, sb, eng


def _obs(store, module, channel, value, quality="good"):
    return store.append("observation", {"value": value, "unit": "mg/L"},
                        "urn:uii:schema:observation.concentration:0.1",
                        module=module, channel=channel,
                        quality={"status": quality})


def _alert_events(store):
    return [e["data"] for e in store.query(kind="event", limit=500)
            if e["data"].get("event") == "alert"]


class TestThreshold(unittest.TestCase):
    def test_raise_clear_with_hysteresis(self):
        store, sb, eng = _engine([{
            "id": "nh4-high", "type": "threshold", "channel": "nh4",
            "severity": "alert", "raise_above": 8.0, "clear_below": 7.0,
            "message": "NH4 high"}])
        _obs(store, "m1", "nh4", 5.0); eng._drain(); eng._evaluate()
        self.assertEqual(eng.active_alerts(), [])

        _obs(store, "m1", "nh4", 8.5); eng._drain(); eng._evaluate()
        self.assertEqual(len(eng.active_alerts()), 1)
        self.assertEqual(eng.active_alerts()[0]["ne107"], "out_of_specification")

        # hysteresis: 7.5 is below raise but above clear -> still active
        _obs(store, "m1", "nh4", 7.5); eng._drain(); eng._evaluate()
        self.assertEqual(len(eng.active_alerts()), 1)

        _obs(store, "m1", "nh4", 6.5); eng._drain(); eng._evaluate()
        self.assertEqual(eng.active_alerts(), [])
        states = [d["alert_state"] for d in _alert_events(store)]
        self.assertEqual(states, ["raised", "cleared"])

    def test_debounce_swallows_transients(self):
        store, sb, eng = _engine([{
            "id": "nh4-high", "type": "threshold", "channel": "nh4",
            "raise_above": 8.0, "debounce_s": 3600}])
        _obs(store, "m1", "nh4", 9.0); eng._drain(); eng._evaluate()
        self.assertEqual(eng.active_alerts(), [])            # pending, not active
        _obs(store, "m1", "nh4", 5.0); eng._drain(); eng._evaluate()
        self.assertEqual(eng.active_alerts(), [])            # transient swallowed
        self.assertEqual(_alert_events(store), [])

        # sustained breach: pending, then promoted once debounce elapses
        _obs(store, "m1", "nh4", 9.0); eng._drain(); eng._evaluate()
        eng.instances[("nh4-high", "m1")].since -= 3601      # fake elapsed time
        eng._evaluate()
        self.assertEqual(len(eng.active_alerts()), 1)

    def test_suppressed_by_design_during_states(self):
        sess = FakeSession("m1", module_state="calibrating")
        store, sb, eng = _engine([{
            "id": "nh4-high", "type": "threshold", "channel": "nh4",
            "raise_above": 8.0, "suppress_in_states": ["calibrating", "priming"]}],
            sessions=[sess])
        _obs(store, "m1", "nh4", 9.9); eng._drain(); eng._evaluate()
        self.assertEqual(eng.active_alerts(), [])            # held, not raised
        sess.module_state = "idle"
        _obs(store, "m1", "nh4", 9.9); eng._drain(); eng._evaluate()
        self.assertEqual(len(eng.active_alerts()), 1)

    def test_raise_below_direction(self):
        store, sb, eng = _engine([{
            "id": "do-low", "type": "threshold", "channel": "nh4",
            "raise_below": 2.0, "clear_above": 2.5}])
        _obs(store, "m1", "nh4", 1.5); eng._drain(); eng._evaluate()
        self.assertEqual(len(eng.active_alerts()), 1)
        _obs(store, "m1", "nh4", 2.2); eng._drain(); eng._evaluate()
        self.assertEqual(len(eng.active_alerts()), 1)        # hysteresis band
        _obs(store, "m1", "nh4", 2.8); eng._drain(); eng._evaluate()
        self.assertEqual(eng.active_alerts(), [])


class TestTimeDriven(unittest.TestCase):
    def test_stale_data_raises_and_clears(self):
        store, sb, eng = _engine([{
            "id": "nh4-stale", "type": "stale_data", "channel": "nh4",
            "window_s": 10, "severity": "warning"}])
        _obs(store, "m1", "nh4", 4.0); eng._drain(); eng._evaluate()
        self.assertEqual(eng.active_alerts(), [])
        eng.instances[("nh4-stale", "m1")].last_obs_t -= 11   # data goes quiet
        eng._evaluate()
        self.assertEqual(len(eng.active_alerts()), 1)
        _obs(store, "m1", "nh4", 4.1); eng._drain()           # fresh data clears
        self.assertEqual(eng.active_alerts(), [])

    def test_cal_overdue_none_and_fresh(self):
        store, sb, eng = _engine([{
            "id": "cal-overdue", "type": "cal_overdue", "max_age_s": 10,
            "severity": "warning", "ne107": "maintenance_required"}])
        eng.t0 -= 11                                          # grace expired, no cal
        eng._evaluate()
        self.assertEqual(len(eng.active_alerts()), 1)
        store.append("calibration", {"fit": {"slope": 25.0, "intercept": 0}},
                     "urn:uii:schema:calibration:0.1", module="m1")
        eng._evaluate()
        self.assertEqual(eng.active_alerts(), [])             # fresh cal clears

    def test_health_flag(self):
        store, sb, eng = _engine([{
            "id": "port-b-down", "type": "health_flag", "field": "port_b_ok",
            "equals": False, "severity": "critical"}])
        eng._latest_health["m1"] = {"port_b_ok": True}
        eng._evaluate()
        self.assertEqual(eng.active_alerts(), [])
        eng._latest_health["m1"] = {"port_b_ok": False}
        eng._evaluate(); eng._evaluate()                      # pending -> active
        self.assertEqual(len(eng.active_alerts()), 1)
        self.assertEqual(eng.ne107_status("m1"), "failure")
        eng._latest_health["m1"] = {"port_b_ok": True}
        eng._evaluate()
        self.assertEqual(eng.active_alerts(), [])

    def test_quality_streak(self):
        store, sb, eng = _engine([{
            "id": "bad-streak", "type": "quality_streak", "channel": "nh4",
            "count": 3}])
        for _ in range(2):
            _obs(store, "m1", "nh4", None, quality="bad")
        eng._drain()
        self.assertEqual(eng.active_alerts(), [])
        _obs(store, "m1", "nh4", None, quality="bad"); eng._drain()
        self.assertEqual(len(eng.active_alerts()), 1)
        _obs(store, "m1", "nh4", 4.2, quality="good"); eng._drain()
        self.assertEqual(eng.active_alerts(), [])


class TestLifecycleAndRollup(unittest.TestCase):
    def test_ack_is_audited_and_resets_on_clear(self):
        store, sb, eng = _engine([{
            "id": "nh4-high", "type": "threshold", "channel": "nh4",
            "raise_above": 8.0, "clear_below": 7.0}])
        _obs(store, "m1", "nh4", 9.0); eng._drain(); eng._evaluate()
        self.assertTrue(eng.ack("nh4-high", "m1", actor="user:test"))
        self.assertTrue(eng.active_alerts()[0]["acked"])
        self.assertFalse(eng.ack("nh4-high", "m1", actor="user:test"))  # idempotent
        audits = store.query(kind="audit", limit=10)
        self.assertEqual(audits[-1]["data"]["event"], "alert-acknowledged")
        # clear, re-raise -> ack state resets
        _obs(store, "m1", "nh4", 6.0); eng._drain()
        _obs(store, "m1", "nh4", 9.0); eng._drain(); eng._evaluate()
        self.assertFalse(eng.active_alerts()[0]["acked"])

    def test_ne107_rollup_ordering(self):
        sess = FakeSession("m1")
        store, sb, eng = _engine(
            [{"id": "w", "type": "threshold", "channel": "nh4",
              "raise_above": 5.0, "severity": "warning"},
             {"id": "c", "type": "threshold", "channel": "nh4",
              "raise_above": 8.0, "severity": "critical"}],
            sessions=[sess])
        self.assertEqual(eng.ne107_status("m1"), "ok")
        sess.module_state = "calibrating"
        self.assertEqual(eng.ne107_status("m1"), "check_function")
        sess.module_state = "idle"
        _obs(store, "m1", "nh4", 6.0); eng._drain(); eng._evaluate()
        self.assertEqual(eng.ne107_status("m1"), "maintenance_required")
        _obs(store, "m1", "nh4", 9.0); eng._drain(); eng._evaluate()
        self.assertEqual(eng.ne107_status("m1"), "failure")   # worst wins
        sess2 = FakeSession("gone", state="DEGRADED")
        sb.sessions["gone"] = sess2
        self.assertEqual(eng.ne107_status("gone"), "failure")

    def test_alert_envelopes_carry_causation(self):
        store, sb, eng = _engine([{
            "id": "nh4-high", "type": "threshold", "channel": "nh4",
            "raise_above": 8.0}])
        trigger = _obs(store, "m1", "nh4", 9.0)
        eng._drain(); eng._evaluate()
        raised = [e for e in store.query(kind="event", limit=50)
                  if e["data"].get("event") == "alert"][0]
        self.assertEqual(raised["trace"]["causation_id"], trigger["id"])
        lin = store.lineage(raised["id"])
        self.assertEqual(lin["upstream"][0]["kind"], "observation")


class TestLiveBench(unittest.TestCase):
    def test_stale_alert_fires_on_unplug_and_clears_on_swap(self):
        b = Bench(extensions=["scheduler", "detections"])
        # add detections to the hub's live config
        b.hub.detections.rules = [
            {"id": "nh4-stale", "type": "stale_data", "channel": "nh4",
             "window_s": 3000, "severity": "warning",
             "message": "no fresh NH4 data"}]
        try:
            b.spawn("unit-a", "slot-1")
            wait_for(lambda: b.good_obs("unit-a", "nh4"), timeout=90,
                     what="first good sample")
            # fresh data clears any startup staleness within one engine tick
            wait_for(lambda: not get(b.base, "/v1/alerts")["items"],
                     timeout=10, what="no active alerts while sampling")

            b.procs[0].kill()   # unplug: data goes stale, alert must fire
            wait_for(lambda: any(a["rule"] == "nh4-stale"
                                 for a in get(b.base, "/v1/alerts")["items"]),
                     timeout=60, what="stale alert")
            code, _ = post(b.base, "/v1/alerts/ack",
                           {"rule": "nh4-stale", "module": "unit-a"})
            self.assertEqual(code, 200)

            b.spawn("unit-b", "slot-1")  # swap in: fresh data clears it
            wait_for(lambda: b.good_obs("unit-b", "nh4"), timeout=90,
                     what="module B sampling")
            wait_for(lambda: not any(a["rule"] == "nh4-stale" and
                                     a["module"] == "unit-b"
                                     for a in get(b.base, "/v1/alerts")["items"]),
                     timeout=30, what="alert clear for B")
        finally:
            b.close()


if __name__ == "__main__":
    unittest.main()
