"""The swap drill — the exit demo of playbook Stage 5–6, in software:
unplug module A, plug module B (new serial) into the same slot, and the
system does the rest: role restored, control taken, re-calibrated, sampling
resumed. History follows serials; the role's schedule survives the swap."""
import unittest

from ..helpers import Bench, get, wait_for


class TestSwapDrill(unittest.TestCase):
    def setUp(self):
        self.b = Bench(extensions=["scheduler"])

    def tearDown(self):
        self.b.close()

    def test_swap_end_to_end(self):
        # module A: plug in -> hands-off to good data
        self.b.spawn("unit-a", "slot-1", serial="SN-0142")
        wait_for(lambda: self.b.good_obs("unit-a", "nh4"),
                 timeout=90, what="module A first good sample")
        cal_a = get(self.b.base,
                    "/v1/evidence?kind=calibration&module=unit-a")["items"][-1]

        # unplug A
        self.b.procs[0].kill()
        wait_for(lambda: self.b.state_of("unit-a") == "REMOVED",
                 what="A removed")
        events = get(self.b.base, "/v1/evidence?kind=event&limit=1000")["items"]
        self.assertTrue(any(e["data"].get("event") == "role-vacant"
                            and e["data"].get("role") == "nh4-influent"
                            for e in events))

        # plug B into the same slot — new serial, factory fresh
        self.b.spawn("unit-b", "slot-1", serial="SN-0388")
        wait_for(lambda: self.b.state_of("unit-b") == "OPERATIONAL",
                 what="B adopted")
        self.assertEqual(self.b.module("unit-b")["role"], "nh4-influent")

        # B must NOT inherit A's calibration: its first good sample carries
        # a calibration envelope of its own (history follows the serial)
        obs_b = wait_for(lambda: self.b.good_obs("unit-b", "nh4"),
                         timeout=90, what="module B first good sample")
        cal_ref = obs_b["context"]["calibration_id"]
        self.assertIsNotNone(cal_ref)
        self.assertNotEqual(cal_ref, cal_a["id"])
        cal_b = get(self.b.base, f"/v1/evidence/{cal_ref}")
        self.assertEqual(cal_b["source"]["module"], "unit-b")

        # A's full history stays queryable after removal (refurb-bench read)
        hist_a = get(self.b.base,
                     "/v1/evidence?module=unit-a&kind=identity,calibration&limit=500")["items"]
        self.assertTrue(any(h["kind"] == "calibration" for h in hist_a))
        self.assertEqual(hist_a[-1]["data"]["event"], "removed")


if __name__ == "__main__":
    unittest.main()


class TestCalRequiredGate(unittest.TestCase):
    def test_cal_required_event_when_not_auto(self):
        from ..helpers import Bench as _B
        b = _B(extensions=["scheduler"], roles={
            "slot-1": {"role": "nh4-influent", "analyte": "NH4",
                       "sample_interval_s": 900,
                       "auto_take_control": True, "auto_calibrate": False}})
        try:
            b.spawn("mod-a", "slot-1")
            wait_for(lambda: any(
                e["data"].get("event") == "cal-required"
                for e in get(b.base, "/v1/evidence?kind=event&limit=500")["items"]),
                timeout=60, what="cal-required event")
            self.assertIsNone(b.good_obs("mod-a", "nh4"))
        finally:
            b.close()
