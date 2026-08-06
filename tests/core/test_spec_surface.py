"""The spec's remaining core surfaces: discovery, the module digital
record, the observations query, and cooperative cancel."""
import unittest

from ..helpers import Bench, get, post, wait_for


def run_cmd(base, module, ctype, params=None):
    code, resp = post(base, "/v1/commands",
                      {"module": module, "type": ctype, "params": params or {}})
    wait_for(lambda: get(base, f"/v1/commands/{resp['command_id']}")["state"] == "done",
             timeout=60, what=ctype)
    return resp["command_id"]


class TestSpecSurfaces(unittest.TestCase):
    def setUp(self):
        self.b = Bench(roles={
            "slot-1": {"role": "nh4-manual", "analyte": "NH4"}})
        self.b.spawn("ref-a", "slot-1")
        wait_for(lambda: self.b.state_of("ref-a") == "OPERATIONAL",
                 what="adoption")

    def tearDown(self):
        self.b.close()

    def test_wellknown_discovery(self):
        d = get(self.b.base, "/.well-known/uii")
        self.assertEqual(d["hub"], "hub-test")
        self.assertEqual(d["api"], "/v1")
        self.assertEqual(d["auth"]["modes"], ["open"])

    def test_module_history_is_the_digital_record(self):
        run_cmd(self.b.base, "ref-a", "calibrate", {"std_conc": 5.0})
        hist = get(self.b.base, "/v1/modules/ref-a/history")["items"]
        kinds = [(e["kind"], (e.get("data") or {}).get("event")) for e in hist]
        self.assertIn(("identity", "hello"), kinds)
        self.assertIn(("identity", "adopted"), kinds)
        self.assertIn("calibration", [k for k, _ in kinds])
        # and it is filterable
        cals = get(self.b.base,
                   "/v1/modules/ref-a/history?kind=calibration")["items"]
        self.assertTrue(all(e["kind"] == "calibration" for e in cals))

    def test_observations_query(self):
        run_cmd(self.b.base, "ref-a", "calibrate", {"std_conc": 5.0})
        run_cmd(self.b.base, "ref-a", "sample")
        run_cmd(self.b.base, "ref-a", "sample")
        obs = get(self.b.base, "/v1/observations?channel=nh4")["items"]
        self.assertGreaterEqual(len(obs), 2)
        self.assertTrue(all(o["source"]["channel"] == "nh4" for o in obs))
        raw = get(self.b.base,
                  "/v1/observations?channel=detector_raw&module=ref-a")["items"]
        self.assertTrue(raw)

    def test_cancel_is_cooperative_and_audited(self):
        # cancel an in-flight command: accepted, audited, module asked to abort
        code, resp = post(self.b.base, "/v1/commands",
                          {"module": "ref-a", "type": "sample"})
        code, out = post(self.b.base,
                         f"/v1/commands/{resp['command_id']}/cancel", {})
        self.assertEqual(code, 202)
        self.assertEqual(out["cancel_of"], resp["command_id"])
        self.assertIn("abort_command_id", out)
        audits = [e["data"].get("event") for e in
                  get(self.b.base, "/v1/evidence?kind=audit&limit=50")["items"]]
        self.assertIn("cancel-requested", audits)

        # a finished command cannot be cancelled
        wait_for(lambda: get(self.b.base,
                             f"/v1/commands/{resp['command_id']}")["state"] == "done",
                 timeout=30, what="original command terminal")
        code, out = post(self.b.base,
                         f"/v1/commands/{resp['command_id']}/cancel", {})
        self.assertEqual(code, 422)




class TestExposedCommands(unittest.TestCase):
    """The exposed-command surface: discover what a module can do, get
    hub-side validation against the declared param spec, and predictable
    precondition rejections instead of module round-trips."""

    def setUp(self):
        self.b = Bench(roles={
            "slot-1": {"role": "nh4-manual", "analyte": "NH4"}})

    def tearDown(self):
        self.b.close()

    def _adopt(self, speed=None):
        self.b.spawn("ref-c", "slot-1", speed=speed)
        wait_for(lambda: self.b.state_of("ref-c") == "OPERATIONAL",
                 what="adoption")

    def test_command_discovery(self):
        self._adopt()
        out = get(self.b.base, "/v1/modules/ref-c/commands")
        cal = {c["type"]: c for c in out["commands"]}["calibrate"]
        self.assertEqual(cal["risk"], "disruptive")
        self.assertTrue(cal["params"]["std_conc"]["required"])
        self.assertEqual(cal["params"]["std_conc"]["type"], "number")
        self.assertIn("state:idle", cal["preconditions"])
        self.assertIn("typical_duration_s", cal)

    def test_param_schema_enforced_at_the_gate(self):
        self._adopt()
        cases = [
            ({}, "missing required param 'std_conc'"),
            ({"std_conc": "five"}, "must be a number"),
            ({"std_conc": 5.0, "stdconc_typo": 1}, "unknown param"),
            ({"std_conc": 99999}, "above maximum"),
        ]
        for params, expect in cases:
            code, resp = post(self.b.base, "/v1/commands",
                              {"module": "ref-c", "type": "calibrate",
                               "params": params})
            self.assertEqual(code, 422, params)
            self.assertIn(expect, resp["detail"])
        # nothing reached the module: no command envelopes, only audits
        cmds = get(self.b.base, "/v1/evidence?kind=command&limit=50")["items"]
        self.assertEqual(cmds, [])

    def test_precondition_predicts_the_rejection(self):
        self._adopt(speed=2)   # sample takes ~12 s: plenty of busy time
        code, resp = post(self.b.base, "/v1/commands",
                          {"module": "ref-c", "type": "sample"})
        self.assertEqual(code, 202)
        wait_for(lambda: (self.b.module("ref-c") or {}).get("module_state")
                 == "sampling", timeout=15, what="hub sees busy state")
        code, resp2 = post(self.b.base, "/v1/commands",
                           {"module": "ref-c", "type": "sample"})
        self.assertEqual(code, 422)
        self.assertEqual(resp2["type"], "urn:uii:problem:precondition-failed")
        self.assertIn("requires state=idle", resp2["detail"])


class TestObservationsLastWindow(unittest.TestCase):
    def test_last_returns_newest_ascending(self):
        b = Bench(roles={"slot-1": {"role": "nh4-manual", "analyte": "NH4"}})
        try:
            b.spawn("ref-t", "slot-1")
            wait_for(lambda: b.state_of("ref-t") == "OPERATIONAL", what="adoption")
            run_cmd(b.base, "ref-t", "calibrate", {"std_conc": 5.0})
            for _ in range(3):
                run_cmd(b.base, "ref-t", "sample")
            out = get(b.base, "/v1/observations?channel=nh4&last=2")["items"]
            self.assertEqual(len(out), 2)
            self.assertLess(out[0]["sequence"], out[1]["sequence"])   # ascending
            allobs = get(b.base, "/v1/observations?channel=nh4&limit=50")["items"]
            self.assertEqual(out[-1]["sequence"], allobs[-1]["sequence"])  # newest
        finally:
            b.close()


if __name__ == "__main__":
    unittest.main()
