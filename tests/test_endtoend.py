"""Manual command path (operator/agent via API), the split-PLC bridge, and
raw-to-derived lineage — against the same agent code that ships to the Pi."""
import time
import unittest

from .helpers import Bench, get, post, wait_for


def cmd(base, module, ctype, params=None):
    code, resp = post(base, "/v1/commands",
                      {"module": module, "type": ctype, "params": params or {}})
    return code, resp


def cmd_done(base, command_id):
    st = get(base, f"/v1/commands/{command_id}")
    return st if st and st["state"] == "done" else None


class TestManualPath(unittest.TestCase):
    """No scheduler involvement: role has no auto policies."""

    def setUp(self):
        self.b = Bench(roles={
            "slot-1": {"role": "nh4-manual", "analyte": "NH4",
                       "auto_take_control": False, "auto_calibrate": False,
                       "sample_interval_s": None}})
        self.mod = self.b.spawn_inprocess("manual-a", "slot-1")
        wait_for(lambda: self.b.state_of("manual-a") == "OPERATIONAL",
                 what="adoption")

    def tearDown(self):
        self.b.close()

    def test_bridge_forwards_plc_traffic_and_is_evidence(self):
        # BRIDGE is the boot mode: PLC (port A) drives the device (port B)
        wait_for(lambda: (self.b.module("manual-a") or {}).get("mode") == "BRIDGE",
                 what="bridge mode visible")
        self.mod.port_a.inject("PLC-CMD-42")
        wait_for(lambda: "PLC-CMD-42" in self.mod.port_b.sent,
                 timeout=10, what="A->B forward")
        # and the traffic is evidence, both directions
        wait_for(lambda: any(
            e["data"].get("raw") == "PLC-CMD-42"
            for e in get(self.b.base,
                         "/v1/evidence?kind=event&module=manual-a&limit=500")["items"]
            if e["data"].get("event") == "serial"), timeout=10,
            what="serial trace envelope")

    def test_command_rejections(self):
        # sample refused while the PLC has authority
        code, resp = cmd(self.b.base, "manual-a", "sample")
        self.assertEqual(code, 202)  # gateway accepts; module ACK-rejects
        st = wait_for(lambda: cmd_done(self.b.base, resp["command_id"]),
                      what="rejection ack")
        self.assertFalse(st["ack"]["data"]["accepted"])
        self.assertIn("Not in ENDPOINT", st["ack"]["data"]["reason"])
        # commands not in the manifest never reach the module
        code, resp = cmd(self.b.base, "manual-a", "self_destruct")
        self.assertEqual(code, 422)

    def test_full_cycle_with_lineage(self):
        # take control (latches ENDPOINT, exactly like the field latch)
        code, resp = cmd(self.b.base, "manual-a", "take_control")
        wait_for(lambda: cmd_done(self.b.base, resp["command_id"]),
                 what="take_control")
        wait_for(lambda: (self.b.module("manual-a") or {}).get("mode") == "ENDPOINT",
                 what="mode ENDPOINT")

        # sampling before any calibration -> value None, flagged no_calibration
        code, resp = cmd(self.b.base, "manual-a", "sample")
        st = wait_for(lambda: cmd_done(self.b.base, resp["command_id"]),
                      timeout=60, what="uncalibrated sample")
        derived = st["derived"]
        self.assertTrue(derived)
        self.assertIsNone(derived[0]["data"]["value"])
        self.assertIn("no_calibration", derived[0]["quality"]["flags"])

        # calibrate, then sample -> a good number near the sim's hidden truth
        code, resp = cmd(self.b.base, "manual-a", "calibrate", {"std_conc": 5.0})
        st = wait_for(lambda: cmd_done(self.b.base, resp["command_id"]),
                      timeout=60, what="calibrate")
        self.assertEqual(st["result"]["data"]["status"], "succeeded")
        cal = get(self.b.base,
                  "/v1/evidence?kind=calibration&module=manual-a")["items"][-1]
        self.assertAlmostEqual(cal["data"]["fit"]["slope"], 25.0, delta=1.5)

        code, resp = cmd(self.b.base, "manual-a", "sample")
        st = wait_for(lambda: cmd_done(self.b.base, resp["command_id"]),
                      timeout=60, what="sample")
        obs = st["derived"][-1]
        self.assertEqual(obs["quality"]["status"], "good")
        # sim truth: 4.5 +/- 25% swing
        self.assertGreater(obs["data"]["value"], 2.5)
        self.assertLess(obs["data"]["value"], 6.5)

        # lineage: observation -> result -> command; context has the cal;
        # raw detector captures are referenced
        lin = get(self.b.base, f"/v1/evidence/{obs['id']}/lineage")
        self.assertEqual([u["kind"] for u in lin["upstream"]][:2],
                         ["result", "command"])
        self.assertEqual(lin["context_refs"]["calibration"]["id"], cal["id"])
        self.assertTrue(obs["data"]["raw_refs"])
        raw = get(self.b.base, f"/v1/evidence/{obs['data']['raw_refs'][0]}")
        self.assertEqual(raw["source"]["channel"], "detector_raw")

        # the run sent real ST9 strings to the pump controller
        st9_lines = [l for l in self.mod.port_b.sent if l.startswith("/2")]
        self.assertTrue(st9_lines, "NH4 ST9 strings must reach port B")


class TestScheduledPath(unittest.TestCase):
    def test_cal_required_event_when_not_auto(self):
        b = Bench(roles={
            "slot-1": {"role": "nh4-influent", "analyte": "NH4",
                       "sample_interval_s": 900,
                       "auto_take_control": True, "auto_calibrate": False}})
        try:
            b.spawn("mod-a", "slot-1")
            wait_for(lambda: any(
                e["data"].get("event") == "cal-required"
                for e in get(b.base, "/v1/evidence?kind=event&limit=500")["items"]),
                timeout=60, what="cal-required event")
            # and no samples were scheduled without a calibration
            self.assertIsNone(b.good_obs("mod-a", "nh4"))
        finally:
            b.close()


if __name__ == "__main__":
    unittest.main()
