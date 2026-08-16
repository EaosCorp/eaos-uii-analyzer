"""Full field interpretation: core two-point (NH4/PO4) + NOX three-channel.

NOX fits three curves from one calibrate run (NOX, NOX at 5x dilution,
NO2) and derives NO3 = NOX - NO2 with the physical-validity rules ported
verbatim from the deployed gateway.
"""
from __future__ import annotations

from uii.hub import interpret as core


def fit_calibration_full(analyte: str, std_conc: float, captures: dict) -> dict:
    analyte = analyte.upper()
    if analyte in core.SUPPORTED:
        return core.fit_calibration(analyte, std_conc, captures)
    if analyte != "NOX":
        return {"fit": {}, "absorbance": {}, "error": f"unknown analyte {analyte}"}
    if std_conc is None or float(std_conc) <= 0:
        return {"fit": {}, "absorbance": {}, "error": "std_conc must be > 0"}
    std_conc = float(std_conc)

    r = core.safe_log10_ratio
    a_nox_diw, e1 = r(captures.get("NOX_CAL_DIW_I0"), captures.get("NOX_CAL_DIW_I1"))
    a_nox_std, e2 = r(captures.get("NOX_CAL_STD_I0"), captures.get("NOX_CAL_STD_I1"))
    a_nox_std_5x, e3 = r(captures.get("NOX_CAL_STD_I0_5X"), captures.get("NOX_CAL_STD_I1_5X"))
    a_no2_diw, e4 = r(captures.get("NO2_CAL_DIW_I0"), captures.get("NO2_CAL_DIW_I1"))
    a_no2_std, e5 = r(captures.get("NO2_CAL_STD_I0"), captures.get("NO2_CAL_STD_I1"))
    err = e1 or e2 or e3 or e4 or e5
    fit: dict = {}
    if not err:
        nox_slope, nox_intercept, e_nox = core.fit_two_point(std_conc, a_nox_std, a_nox_diw, "NOX")
        nox_slope_5x, nox_intercept_5x, e_5x = core.fit_two_point(std_conc, a_nox_std_5x, a_nox_diw, "NOX_5X")
        no2_slope, no2_intercept, e_no2 = core.fit_two_point(std_conc, a_no2_std, a_no2_diw, "NO2")
        err = e_nox or e_5x or e_no2
        fit = {"nox_slope": nox_slope, "nox_intercept": nox_intercept,
               "nox_slope_5x": nox_slope_5x, "nox_intercept_5x": nox_intercept_5x,
               "no2_slope": no2_slope, "no2_intercept": no2_intercept}
    return {"fit": fit,
            "absorbance": {"nox_diw": a_nox_diw, "nox_std": a_nox_std,
                           "nox_std_5x": a_nox_std_5x, "no2_diw": a_no2_diw,
                           "no2_std": a_no2_std, "log_base": 10},
            "error": err}


def calibration_complete_full(analyte: str, fit: dict) -> bool:
    analyte = analyte.upper()
    if analyte in core.SUPPORTED:
        return core.calibration_complete(analyte, fit)
    if analyte == "NOX":
        return fit.get("nox_slope") is not None and fit.get("no2_slope") is not None
    return False


def interpret_sample_full(analyte: str, fit: dict, captures: dict) -> dict:
    analyte = analyte.upper()
    if analyte in core.SUPPORTED:
        return core.interpret_sample(analyte, fit, captures)
    if analyte != "NOX":
        return {"channels": [], "error": f"unknown analyte {analyte}"}

    r = core.safe_log10_ratio
    a_nox, e1 = r(captures.get("NOX_SAMP_I0"), captures.get("NOX_SAMP_I1"))
    a_no2, e2 = r(captures.get("NO2_SAMP_I0"), captures.get("NO2_SAMP_I1"))
    err = e1 or e2
    nox_mgL = no2_mgL = no3_mgL = None
    if not err:
        nox_mgL = float(fit["nox_slope"]) * a_nox - float(fit["nox_intercept"])
        no2_mgL = float(fit["no2_slope"]) * a_no2 - float(fit["no2_intercept"])
        # NO3 = NOX - NO2, only when both are valid non-negative results
        if nox_mgL >= 0 and no2_mgL >= 0:
            no3_mgL = nox_mgL - no2_mgL
            if no3_mgL < 0:
                no3_mgL = None
                err = f"NO3 invalid: NOX ({nox_mgL:.4f}) < NO2 ({no2_mgL:.4f})"
        else:
            err = "Negative concentration result — check calibration"
    return {"channels": [
        {"name": "nox", "value": nox_mgL, "unit": "mg/L", "absorbance": a_nox},
        {"name": "no2", "value": no2_mgL, "unit": "mg/L", "absorbance": a_no2},
        {"name": "no3", "value": no3_mgL, "unit": "mg/L", "absorbance": None},
    ], "error": err}


def channels_for_full(analyte: str) -> list[str]:
    analyte = analyte.upper()
    if analyte in core.SUPPORTED:
        return core.channels_for(analyte)
    return ["nox", "no2", "no3"] if analyte == "NOX" else []
