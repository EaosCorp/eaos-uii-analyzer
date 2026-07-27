"""The seam, end to end, with the core alone: reference module in, evidence
out. Manual command path, one-result guarantee, quality attribution with
permitted-use, and lineage from a concentration back to its cause."""
import unittest

from ..helpers import Bench, get, post, wait_for


def cmd(base, module, ctype, params=None):
    return post(base, "/v1/commands",
                {"module": module, "type": ctype, "params": params or {}})


def cmd_done(base, command_id):
    st = get(base, f"/v1/commands/{command_id}")
    return st if st and st["state"] == "done" else None


class TestCoreSeam(unittest.TestCase):
    def setUp(self):
        self.b = Bench(roles={
            "slot-1": {"role": "nh4-manual", "analyte": "NH4"}})
        self.b.spawn("ref-a", "slot-1")
        wait_for(lambda: self.b.state_of("ref-a") == "OPERATIONAL",
                 what="adoption")

    def tearDown(self):
        self.b.close()

    def test_full_cycle_with_lineage_and_permitted_use(self):
        # sampling before any calibration -> value None, flagged, and
        # permitted for NOTHING automated
        code, resp = cmd(self.b.base, "ref-a", "sample")
        self.assertEqual(code, 202)
        st = wait_for(lambda: cmd_done(self.b.base, resp["command_id"]),
                      timeout=60, what="uncalibrated sample")
        obs = st["derived"][0]
        self.assertIsNone(obs["data"]["value"])
        self.assertIn("no_calibration", obs["quality"]["flags"])
        self.assertEqual(obs["quality"]["permitted_use"], "none")

        # calibrate: the hub recovers the module's hidden slope (25.0)
        code, resp = cmd(self.b.base, "ref-a", "calibrate", {"std_conc": 5.0})
        st = wait_for(lambda: cmd_done(self.b.base, resp["command_id"]),
                      timeout=60, what="calibrate")
        self.assertEqual(st["result"]["data"]["status"], "succeeded")
        cal = get(self.b.base,
                  "/v1/evidence?kind=calibration&module=ref-a")["items"][-1]
        self.assertAlmostEqual(cal["data"]["fit"]["slope"], 25.0, delta=1.5)

        # sample: a good mg/L, permitted for automated use
        code, resp = cmd(self.b.base, "ref-a", "sample")
        st = wait_for(lambda: cmd_done(self.b.base, resp["command_id"]),
                      timeout=60, what="sample")
        obs = st["derived"][-1]
        self.assertEqual(obs["quality"]["status"], "good")
        self.assertEqual(obs["quality"]["permitted_use"], "control")
        self.assertGreater(obs["data"]["value"], 2.5)   # sim truth 4.5 ± swing
        self.assertLess(obs["data"]["value"], 6.5)
        # command evidence records its ingress path identity
        self.assertEqual(st["command"]["data"]["ingress"], "local")

        # lineage: observation <- result <- command; calibration referenced;
        # raw detector volts reachable
        lin = get(self.b.base, f"/v1/evidence/{obs['id']}/lineage")
        self.assertEqual([u["kind"] for u in lin["upstream"]][:2],
                         ["result", "command"])
        self.assertEqual(lin["context_refs"]["calibration"]["id"], cal["id"])
        self.assertTrue(obs["data"]["raw_refs"])
        raw = get(self.b.base, f"/v1/evidence/{obs['data']['raw_refs'][0]}")
        self.assertEqual(raw["source"]["channel"], "detector_raw")

    def test_one_result_per_command_always(self):
        # module ACK-rejects a bad calibrate -> terminal result, not limbo
        code, resp = cmd(self.b.base, "ref-a", "calibrate", {"std_conc": 0})
        self.assertEqual(code, 202)
        st = wait_for(lambda: cmd_done(self.b.base, resp["command_id"]),
                      what="rejection is terminal")
        self.assertFalse(st["ack"]["data"]["accepted"])
        self.assertEqual(st["result"]["data"]["status"], "rejected")

    def test_undeclared_command_never_reaches_module(self):
        code, resp = cmd(self.b.base, "ref-a", "self_destruct")
        self.assertEqual(code, 422)
        audits = get(self.b.base, "/v1/evidence?kind=audit&limit=20")["items"]
        self.assertEqual(audits[-1]["data"]["event"], "command-rejected")


if __name__ == "__main__":
    unittest.main()
