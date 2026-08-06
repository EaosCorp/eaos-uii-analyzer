"""Interpretation math — must match the deployed gateway's behavior exactly."""
import unittest

from uii.hub.interpret import (calibration_complete, fit_calibration,
                               interpret_sample, safe_log10_ratio)


def volts(i0, absorbance):
    return i0 * (10 ** -absorbance)


class TestAbsorbance(unittest.TestCase):
    def test_basic(self):
        a, err = safe_log10_ratio(2.4, 2.4 / 10)
        self.assertEqual(err, "")
        self.assertAlmostEqual(a, 1.0)

    def test_none_guards(self):
        self.assertIn("i0 is None", safe_log10_ratio(None, 1.0)[1])
        self.assertIn("i1 is None", safe_log10_ratio(1.0, None)[1])

    def test_domain(self):
        _, err = safe_log10_ratio(-2.0, 1.0)
        self.assertIn("ratio<=0", err)


class TestNH4Fit(unittest.TestCase):
    def test_recovers_hidden_slope(self):
        # hidden truth: slope 25, blank absorbance 0 -> A_std = 5/25 = 0.2
        captures = {
            "NH4_CAL_DIW_I0": 2.4, "NH4_CAL_DIW_I1": 2.4,
            "NH4_CAL_STD_I0": 2.4, "NH4_CAL_STD_I1": volts(2.4, 0.2),
        }
        out = fit_calibration("NH4", 5.0, captures)
        self.assertEqual(out["error"], "")
        self.assertAlmostEqual(out["fit"]["slope"], 25.0, places=6)
        self.assertAlmostEqual(out["fit"]["intercept"], 0.0, places=6)
        self.assertTrue(calibration_complete("NH4", out["fit"]))

        # then a sample at true 4.2 mg/L: A = 4.2/25
        samp = {"NH4_SAMP_I0": 2.4, "NH4_SAMP_I1": volts(2.4, 4.2 / 25.0)}
        res = interpret_sample("NH4", out["fit"], samp)
        self.assertEqual(res["error"], "")
        self.assertAlmostEqual(res["channels"][0]["value"], 4.2, places=6)

    def test_nonzero_blank_intercept(self):
        # blank absorbance 0.01 -> intercept = slope * A_diw (legacy formula),
        # and conc = slope*A - intercept
        a_diw, a_std = 0.01, 0.21
        captures = {
            "NH4_CAL_DIW_I0": 2.4, "NH4_CAL_DIW_I1": volts(2.4, a_diw),
            "NH4_CAL_STD_I0": 2.4, "NH4_CAL_STD_I1": volts(2.4, a_std),
        }
        out = fit_calibration("NH4", 5.0, captures)
        slope = 5.0 / (a_std - a_diw)
        self.assertAlmostEqual(out["fit"]["slope"], slope, places=6)
        self.assertAlmostEqual(out["fit"]["intercept"], slope * a_diw, places=6)
        # sampling the blank itself must read ~0
        samp = {"NH4_SAMP_I0": 2.4, "NH4_SAMP_I1": volts(2.4, a_diw)}
        res = interpret_sample("NH4", out["fit"], samp)
        self.assertAlmostEqual(res["channels"][0]["value"], 0.0, places=6)

    def test_degenerate_and_bad_std(self):
        captures = {"NH4_CAL_DIW_I0": 2.4, "NH4_CAL_DIW_I1": 2.0,
                    "NH4_CAL_STD_I0": 2.4, "NH4_CAL_STD_I1": 2.0}
        self.assertIn("degenerate", fit_calibration("NH4", 5.0, captures)["error"])
        self.assertIn("std_conc", fit_calibration("NH4", 0, captures)["error"])
        missing = fit_calibration("NH4", 5.0, {})
        self.assertIn("ADC read missing", missing["error"])


class TestPO4(unittest.TestCase):
    def test_round_trip(self):
        captures = {"PO4_CAL_DIW_I0": 2.4, "PO4_CAL_DIW_I1": 2.4,
                    "PO4_CAL_STD_I0": 2.4, "PO4_CAL_STD_I1": volts(2.4, 3.0 / 12.0)}
        out = fit_calibration("PO4", 3.0, captures)
        self.assertAlmostEqual(out["fit"]["slope"], 12.0, places=5)
        samp = {"PO4_SAMP_I0": 2.4, "PO4_SAMP_I1": volts(2.4, 1.8 / 12.0)}
        res = interpret_sample("PO4", out["fit"], samp)
        self.assertAlmostEqual(res["channels"][0]["value"], 1.8, places=5)


if __name__ == "__main__":
    unittest.main()
