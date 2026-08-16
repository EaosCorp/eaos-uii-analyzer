"""analyzer extension — NOX three-channel math, and the pimod field agent
(split-PLC bridge, ST9 timelines) end to end in sim mode."""
import unittest

from extensions.analyzer.interpret_full import (calibration_complete_full,
                                                fit_calibration_full,
                                                interpret_sample_full)

from ..helpers import Bench, get, post, wait_for


def volts(i0, absorbance):
    return i0 * (10 ** -absorbance)


class TestNOXMath(unittest.TestCase):
    def _cal_captures(self, std=5.0, nox_true=20.0, no2_true=18.0):
        return {
            "NOX_CAL_DIW_I0": 2.4, "NOX_CAL_DIW_I1": 2.4,
            "NOX_CAL_STD_I0": 2.4, "NOX_CAL_STD_I1": volts(2.4, std / nox_true),
            "NOX_CAL_STD_I0_5X": 2.4,
            "NOX_CAL_STD_I1_5X": volts(2.4, (std / 5.0) / nox_true),
            "NO2_CAL_DIW_I0": 2.4, "NO2_CAL_DIW_I1": 2.4,
            "NO2_CAL_STD_I0": 2.4, "NO2_CAL_STD_I1": volts(2.4, std / no2_true),
        }

    def test_three_fits_and_no3(self):
        out = fit_calibration_full("NOX", 5.0, self._cal_captures())
        self.assertEqual(out["error"], "")
        self.assertAlmostEqual(out["fit"]["nox_slope"], 20.0, places=5)
        self.assertAlmostEqual(out["fit"]["no2_slope"], 18.0, places=5)
        self.assertAlmostEqual(out["fit"]["nox_slope_5x"], 100.0, places=4)
        self.assertTrue(calibration_complete_full("NOX", out["fit"]))

        samp = {"NOX_SAMP_I0": 2.4, "NOX_SAMP_I1": volts(2.4, 6.0 / 20.0),
                "NO2_SAMP_I0": 2.4, "NO2_SAMP_I1": volts(2.4, 1.5 / 18.0)}
        res = interpret_sample_full("NOX", out["fit"], samp)
        self.assertEqual(res["error"], "")
        by = {c["name"]: c["value"] for c in res["channels"]}
        self.assertAlmostEqual(by["nox"], 6.0, places=5)
        self.assertAlmostEqual(by["no2"], 1.5, places=5)
        self.assertAlmostEqual(by["no3"], 4.5, places=5)

    def test_no3_invalid_when_no2_exceeds_nox(self):
        out = fit_calibration_full("NOX", 5.0, self._cal_captures())
        samp = {"NOX_SAMP_I0": 2.4, "NOX_SAMP_I1": volts(2.4, 1.0 / 20.0),
                "NO2_SAMP_I0": 2.4, "NO2_SAMP_I1": volts(2.4, 3.0 / 18.0)}
        res = interpret_sample_full("NOX", out["fit"], samp)
        self.assertIn("NO3 invalid", res["error"])
        self.assertIsNone({c["name"]: c["value"] for c in res["channels"]}["no3"])

    def test_negative_concentration_flagged(self):
        out = fit_calibration_full("NOX", 5.0, self._cal_captures())
        samp = {"NOX_SAMP_I0": volts(2.4, 0.05), "NOX_SAMP_I1": 2.4,
                "NO2_SAMP_I0": 2.4, "NO2_SAMP_I1": volts(2.4, 1.0 / 18.0)}
        res = interpret_sample_full("NOX", out["fit"], samp)
        self.assertIn("Negative concentration", res["error"])

    def test_nh4_delegates_to_core(self):
        captures = {"NH4_CAL_DIW_I0": 2.4, "NH4_CAL_DIW_I1": 2.4,
                    "NH4_CAL_STD_I0": 2.4, "NH4_CAL_STD_I1": volts(2.4, 0.2)}
        out = fit_calibration_full("NH4", 5.0, captures)
        self.assertAlmostEqual(out["fit"]["slope"], 25.0, places=5)


def cmd(base, module, ctype, params=None, actor="user:test"):
    return post(base, "/v1/commands",
                {"module": module, "type": ctype, "params": params or {},
                 "actor": actor})


def cmd_done(base, command_id):
    st = get(base, f"/v1/commands/{command_id}")
    return st if st and st["state"] == "done" else None


class TestPimodFieldAgent(unittest.TestCase):
    """The real field agent in sim mode: split-PLC bridge, ENDPOINT latch,
    ST9 method timelines, multi-analyte interpretation on the hub."""

    def setUp(self):
        self.b = Bench(extensions=["analyzer"], roles={
            "slot-1": {"role": "nh4-manual", "analyte": "NH4"}})
        self.mod = self.b.spawn_inprocess("pimod-a", "slot-1")
        wait_for(lambda: self.b.state_of("pimod-a") == "OPERATIONAL",
                 what="adoption")

    def tearDown(self):
        self.b.close()

    def test_bridge_forwards_plc_traffic_and_is_evidence(self):
        wait_for(lambda: (self.b.module("pimod-a") or {}).get("mode") == "BRIDGE",
                 what="bridge mode visible")
        self.mod.port_a.inject("PLC-CMD-42")
        wait_for(lambda: "PLC-CMD-42" in self.mod.port_b.sent,
                 timeout=10, what="A->B forward")
        wait_for(lambda: any(
            e["data"].get("raw") == "PLC-CMD-42"
            for e in get(self.b.base,
                         "/v1/evidence?kind=event&module=pimod-a&limit=500")["items"]
            if e["data"].get("event") == "serial"), timeout=10,
            what="serial trace envelope")

    def test_endpoint_cycle_sends_real_st9(self):
        # sample refused while the PLC has authority — now PREDICTED at the
        # hub via the declared mode:ENDPOINT precondition (no module trip)
        wait_for(lambda: (self.b.module("pimod-a") or {}).get("mode") == "BRIDGE",
                 what="hub sees BRIDGE")
        code, resp = cmd(self.b.base, "pimod-a", "sample")
        self.assertEqual(code, 422)
        self.assertEqual(resp["type"], "urn:uii:problem:precondition-failed")
        self.assertIn("requires mode=ENDPOINT", resp["detail"])

        code, resp = cmd(self.b.base, "pimod-a", "take_control")
        wait_for(lambda: cmd_done(self.b.base, resp["command_id"]),
                 what="take_control")
        code, resp = cmd(self.b.base, "pimod-a", "calibrate", {"std_conc": 5.0})
        st = wait_for(lambda: cmd_done(self.b.base, resp["command_id"]),
                      timeout=60, what="calibrate")
        self.assertEqual(st["result"]["data"]["status"], "succeeded")
        code, resp = cmd(self.b.base, "pimod-a", "sample")
        st = wait_for(lambda: cmd_done(self.b.base, resp["command_id"]),
                      timeout=60, what="sample")
        obs = st["derived"][-1]
        self.assertEqual(obs["quality"]["permitted_use"], "control")
        self.assertGreater(obs["data"]["value"], 2.5)
        self.assertLess(obs["data"]["value"], 6.5)
        # the run drove the pump controller with real NH4 ST9 strings
        self.assertTrue([l for l in self.mod.port_b.sent if l.startswith("/2")])


class TestNOXEndToEnd(unittest.TestCase):
    def test_nox_three_channels_from_one_module(self):
        b = Bench(extensions=["analyzer"], roles={
            "slot-1": {"role": "nox-manual", "analyte": "NOX"}})
        try:
            b.spawn("nox-a", "slot-1", analyte="NOX", kind="pimod")
            wait_for(lambda: b.state_of("nox-a") == "OPERATIONAL",
                     what="adoption")
            code, resp = cmd(b.base, "nox-a", "take_control")
            wait_for(lambda: cmd_done(b.base, resp["command_id"]),
                     what="take_control")
            code, resp = cmd(b.base, "nox-a", "calibrate", {"std_conc": 5.0})
            wait_for(lambda: cmd_done(b.base, resp["command_id"]),
                     timeout=90, what="NOX calibrate")
            code, resp = cmd(b.base, "nox-a", "sample")
            st = wait_for(lambda: cmd_done(b.base, resp["command_id"]),
                          timeout=90, what="NOX sample")
            by = {o["source"]["channel"]: o for o in st["derived"]}
            self.assertEqual(set(by), {"nox", "no2", "no3"})
            self.assertEqual(by["nox"]["quality"]["permitted_use"], "control")
            self.assertAlmostEqual(
                by["no3"]["data"]["value"],
                by["nox"]["data"]["value"] - by["no2"]["data"]["value"],
                places=3)
        finally:
            b.close()


if __name__ == "__main__":
    unittest.main()
