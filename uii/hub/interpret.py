"""Interpretation — raw detector captures become calibrations and
concentrations ON THE HUB (reference architecture §3: modules produce
facts, the hub produces interpretations, so every derived value carries a
calibration ID and stays recomputable from stored raws).

The math is ported verbatim from the deployed NH4MOD gateway:

  absorbance A   = log10(i0 / i1)                     (per capture pair)
  two-point fit  : slope = std_conc / (A_std - A_diw)
                   intercept = slope * A_diw
  concentration  = slope * A_sample - intercept

NH4 and PO4 are single-channel. NOX fits three curves from one run
(NOX, NOX at 5x dilution, NO2) and derives NO3 = NOX - NO2 with the
physical-validity rules from the original code.

Inputs are the RESULT's named captures {CAPTURE_NAME: vin_volts}; names
are defined by uii.pimod.timelines and never re-invented here.
"""
from __future__ import annotations

import math
from typing import Optional


def safe_log10_ratio(i0: Optional[float], i1: Optional[float],
                     eps: float = 1e-12) -> tuple[Optional[float], str]:
    """absorbance = log10(i0 / i1) with None and domain protection."""
    if i0 is None:
        return None, "i0 is None (ADC read missing)"
    if i1 is None:
        return None, "i1 is None (ADC read missing)"
    try:
        a, b = float(i0), float(i1)
        if abs(b) < eps:
            b = eps if b >= 0 else -eps
        ratio = a / b
        if ratio <= 0:
            return None, "ratio<=0 (invalid detector signal)"
        return math.log10(ratio), ""
    except Exception as e:  # noqa: BLE001 — mirror the field code's tolerance
        return None, f"log10 ratio error: {e}"


def _fit(std: float, a_std: Optional[float], a_diw: Optional[float],
         label: str) -> tuple[Optional[float], Optional[float], str]:
    denom = (a_std or 0.0) - (a_diw or 0.0)
    if a_std is None or a_diw is None:
        return None, None, f"{label}: missing absorbance"
    if abs(denom) < 1e-12:
        return None, None, f"{label}: A_std == A_diw (degenerate)"
    slope = float(std) / denom
    intercept = slope * float(a_diw)
    return slope, intercept, ""


# ---------------------------------------------------------------------------
# Calibration fits
# ---------------------------------------------------------------------------

def fit_calibration(analyte: str, std_conc: float, captures: dict) -> dict:
    """Return {"fit": {...}, "absorbance": {...}, "error": str}."""
    analyte = analyte.upper()
    if std_conc is None or float(std_conc) <= 0:
        return {"fit": {}, "absorbance": {}, "error": "std_conc must be > 0"}
    std_conc = float(std_conc)

    if analyte in ("NH4", "PO4"):
        p = f"{analyte}_CAL"
        a_diw, e1 = safe_log10_ratio(captures.get(f"{p}_DIW_I0"), captures.get(f"{p}_DIW_I1"))
        a_std, e2 = safe_log10_ratio(captures.get(f"{p}_STD_I0"), captures.get(f"{p}_STD_I1"))
        if e1:
            return {"fit": {}, "absorbance": {}, "error": f"DIW absorbance invalid: {e1}"}
        if e2:
            return {"fit": {}, "absorbance": {}, "error": f"STD absorbance invalid: {e2}"}
        slope, intercept, err = _fit(std_conc, a_std, a_diw, analyte)
        return {"fit": {"slope": slope, "intercept": intercept},
                "absorbance": {"diw": a_diw, "std": a_std, "log_base": 10},
                "error": err}

    if analyte == "NOX":
        a_nox_diw, e1 = safe_log10_ratio(captures.get("NOX_CAL_DIW_I0"), captures.get("NOX_CAL_DIW_I1"))
        a_nox_std, e2 = safe_log10_ratio(captures.get("NOX_CAL_STD_I0"), captures.get("NOX_CAL_STD_I1"))
        a_nox_std_5x, e3 = safe_log10_ratio(captures.get("NOX_CAL_STD_I0_5X"), captures.get("NOX_CAL_STD_I1_5X"))
        a_no2_diw, e4 = safe_log10_ratio(captures.get("NO2_CAL_DIW_I0"), captures.get("NO2_CAL_DIW_I1"))
        a_no2_std, e5 = safe_log10_ratio(captures.get("NO2_CAL_STD_I0"), captures.get("NO2_CAL_STD_I1"))
        err = e1 or e2 or e3 or e4 or e5
        fit: dict = {}
        if not err:
            nox_slope, nox_intercept, e_nox = _fit(std_conc, a_nox_std, a_nox_diw, "NOX")
            nox_slope_5x, nox_intercept_5x, e_5x = _fit(std_conc, a_nox_std_5x, a_nox_diw, "NOX_5X")
            no2_slope, no2_intercept, e_no2 = _fit(std_conc, a_no2_std, a_no2_diw, "NO2")
            err = e_nox or e_5x or e_no2
            fit = {"nox_slope": nox_slope, "nox_intercept": nox_intercept,
                   "nox_slope_5x": nox_slope_5x, "nox_intercept_5x": nox_intercept_5x,
                   "no2_slope": no2_slope, "no2_intercept": no2_intercept}
        return {"fit": fit,
                "absorbance": {"nox_diw": a_nox_diw, "nox_std": a_nox_std,
                               "nox_std_5x": a_nox_std_5x, "no2_diw": a_no2_diw,
                               "no2_std": a_no2_std, "log_base": 10},
                "error": err}

    return {"fit": {}, "absorbance": {}, "error": f"unknown analyte {analyte}"}


def calibration_complete(analyte: str, fit: dict) -> bool:
    analyte = analyte.upper()
    if analyte in ("NH4", "PO4"):
        return fit.get("slope") is not None and fit.get("intercept") is not None
    if analyte == "NOX":
        return fit.get("nox_slope") is not None and fit.get("no2_slope") is not None
    return False


# ---------------------------------------------------------------------------
# Sample interpretation
# ---------------------------------------------------------------------------

def interpret_sample(analyte: str, fit: dict, captures: dict) -> dict:
    """Return {"channels": [{name, value, unit, absorbance}...], "error": str}.

    NOX yields three channels: nox (total oxidised N), no2 (nitrite),
    no3 (nitrate = NOX - NO2, None when physically invalid).
    """
    analyte = analyte.upper()

    if analyte in ("NH4", "PO4"):
        p = f"{analyte}_SAMP"
        a_samp, err = safe_log10_ratio(captures.get(f"{p}_I0"), captures.get(f"{p}_I1"))
        value = None
        if not err and a_samp is not None:
            value = float(fit["slope"]) * a_samp - float(fit["intercept"])
        return {"channels": [{"name": analyte.lower(), "value": value,
                              "unit": "mg/L", "absorbance": a_samp}],
                "error": err}

    if analyte == "NOX":
        a_nox, e1 = safe_log10_ratio(captures.get("NOX_SAMP_I0"), captures.get("NOX_SAMP_I1"))
        a_no2, e2 = safe_log10_ratio(captures.get("NO2_SAMP_I0"), captures.get("NO2_SAMP_I1"))
        err = e1 or e2
        nox_mgL = no2_mgL = no3_mgL = None
        if not err:
            nox_mgL = float(fit["nox_slope"]) * a_nox - float(fit["nox_intercept"])
            no2_mgL = float(fit["no2_slope"]) * a_no2 - float(fit["no2_intercept"])
            # NO3 = NOX - NO2, only when both are valid non-negative results.
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

    return {"channels": [], "error": f"unknown analyte {analyte}"}


def channels_for(analyte: str) -> list[str]:
    analyte = analyte.upper()
    return {"NH4": ["nh4"], "PO4": ["po4"], "NOX": ["nox", "no2", "no3"]}.get(analyte, [])
