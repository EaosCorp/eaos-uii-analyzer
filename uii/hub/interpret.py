"""Core interpreter — the single two-point colorimetric profile (NH4/PO4).

THE SEAM, half two: modules produce FACTS (raw detector volts, named
captures); the hub produces INTERPRETATIONS. The math is ported verbatim
from the deployed field gateway:

  absorbance A   = log10(i0 / i1)                      (per capture pair)
  two-point fit  : slope = std_conc / (A_std - A_diw)
                   intercept = slope * A_diw
  concentration  = slope * A_sample - intercept        (mg/L)

Every derived value carries `calibration_id` + `raw_refs` lineage and a
machine-readable PERMITTED-USE designation from its quality attribution
("control" fit for automated action / "reporting" / "none") — a result is
never an unaudited bare number.

This file also defines `analyzer_interpreter`, the callable the core
registers under instrument_class "chemical-analyzer" (the extension hook
in southbound.py). The analyzer extension replaces it with the full
multi-analyte field version (NOX three-channel etc.); vision or rotating
classes register their own interpreters at the same hook.
"""
from __future__ import annotations

import math
from typing import Optional

SUPPORTED = ("NH4", "PO4")   # the analyzer extension adds NOX


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


def fit_two_point(std: float, a_std: Optional[float], a_diw: Optional[float],
                  label: str = "") -> tuple[Optional[float], Optional[float], str]:
    if a_std is None or a_diw is None:
        return None, None, f"{label}: missing absorbance"
    denom = a_std - a_diw
    if abs(denom) < 1e-12:
        return None, None, f"{label}: A_std == A_diw (degenerate)"
    slope = float(std) / denom
    intercept = slope * float(a_diw)
    return slope, intercept, ""


def fit_calibration(analyte: str, std_conc: float, captures: dict) -> dict:
    """Two-point DIW/STD fit. Returns {"fit", "absorbance", "error"}."""
    analyte = analyte.upper()
    if analyte not in SUPPORTED:
        return {"fit": {}, "absorbance": {}, "error": f"unsupported analyte {analyte}"}
    if std_conc is None or float(std_conc) <= 0:
        return {"fit": {}, "absorbance": {}, "error": "std_conc must be > 0"}
    p = f"{analyte}_CAL"
    a_diw, e1 = safe_log10_ratio(captures.get(f"{p}_DIW_I0"), captures.get(f"{p}_DIW_I1"))
    a_std, e2 = safe_log10_ratio(captures.get(f"{p}_STD_I0"), captures.get(f"{p}_STD_I1"))
    if e1:
        return {"fit": {}, "absorbance": {}, "error": f"DIW absorbance invalid: {e1}"}
    if e2:
        return {"fit": {}, "absorbance": {}, "error": f"STD absorbance invalid: {e2}"}
    slope, intercept, err = fit_two_point(float(std_conc), a_std, a_diw, analyte)
    return {"fit": {"slope": slope, "intercept": intercept},
            "absorbance": {"diw": a_diw, "std": a_std, "log_base": 10},
            "error": err}


def calibration_complete(analyte: str, fit: dict) -> bool:
    if analyte.upper() not in SUPPORTED:
        return False
    return fit.get("slope") is not None and fit.get("intercept") is not None


def interpret_sample(analyte: str, fit: dict, captures: dict) -> dict:
    """Returns {"channels": [{name, value, unit, absorbance}], "error"}."""
    analyte = analyte.upper()
    if analyte not in SUPPORTED:
        return {"channels": [], "error": f"unsupported analyte {analyte}"}
    p = f"{analyte}_SAMP"
    a_samp, err = safe_log10_ratio(captures.get(f"{p}_I0"), captures.get(f"{p}_I1"))
    value = None
    if not err and a_samp is not None:
        value = float(fit["slope"]) * a_samp - float(fit["intercept"])
    return {"channels": [{"name": analyte.lower(), "value": value,
                          "unit": "mg/L", "absorbance": a_samp}],
            "error": err}


def channels_for(analyte: str) -> list[str]:
    return [analyte.lower()] if analyte.upper() in SUPPORTED else []


# ---------------------------------------------------------------------------
# The interpreter callable registered at the southbound hook.
# ---------------------------------------------------------------------------

def make_analyzer_interpreter(fit_calibration=fit_calibration,
                              calibration_complete=calibration_complete,
                              interpret_sample=interpret_sample,
                              channels_for=channels_for):
    """Build the chemical-analyzer interpreter. The analyzer extension
    calls this with its full multi-analyte math; the core calls it with
    the NH4/PO4 functions above. Same evidence shape either way."""

    def interpreter(session, cmd, result, outputs, raw_refs):
        store = session.hub.store
        cmd_id = (cmd.get("data") or {}).get("command_id")
        cmd_type = (cmd.get("data") or {}).get("type")
        captures = outputs.get("captures") or {}
        if not captures:
            return
        analyte = (outputs.get("analyte")
                   or session.manifest.get("analyte") or "NH4").upper()

        if cmd_type == "calibrate":
            std = (cmd.get("data") or {}).get("params", {}).get(
                "std_conc", session.role_config.get("calibrate_std_conc", 5.0))
            fitted = fit_calibration(analyte, float(std), captures)
            if fitted["error"]:
                store.append(
                    "event", {"event": "calibration-failed",
                              "analyte": analyte, "error": fitted["error"]},
                    "urn:uii:schema:event:0.1", module=session.module_id,
                    trace={"command_id": cmd_id, "causation_id": result["id"],
                           "correlation_id": cmd_id})
                return
            store.append(
                "calibration",
                {"analyte": analyte, "std_conc_mgL": float(std),
                 "units": {"concentration": "mg/L"},
                 "fit": fitted["fit"], "absorbance": fitted["absorbance"],
                 "vin": captures, "raw_refs": raw_refs},
                "urn:uii:schema:calibration:0.1", module=session.module_id,
                trace={"command_id": cmd_id, "causation_id": result["id"],
                       "correlation_id": cmd_id})

        elif cmd_type == "sample":
            cal = store.latest_calibration(session.module_id)
            fit = (cal or {}).get("data", {}).get("fit", {})
            # quality attribution -> machine-readable permitted-use
            # designation: "control" (fit for automated action),
            # "reporting" (records/display only), "none"
            if cal and calibration_complete(analyte, fit):
                res = interpret_sample(analyte, fit, captures)
                quality = ({"status": "good", "flags": [],
                            "permitted_use": "control"} if not res["error"]
                           else {"status": "bad",
                                 "flags": ["interpretation_error"],
                                 "permitted_use": "reporting"})
            else:
                res = {"channels": [{"name": c, "value": None, "unit": "mg/L",
                                     "absorbance": None}
                                    for c in (channels_for(analyte)
                                              or [analyte.lower()])],
                       "error": "no calibration"}
                quality = {"status": "bad", "flags": ["no_calibration"],
                           "permitted_use": "none"}
            for ch in res["channels"]:
                store.append(
                    "observation",
                    {"value": round(ch["value"], 4) if ch["value"] is not None else None,
                     "unit": ch["unit"], "analyte": analyte,
                     "absorbance": (round(ch["absorbance"], 5)
                                    if ch.get("absorbance") is not None else None),
                     "error": res["error"] or None, "raw_refs": raw_refs},
                    "urn:uii:schema:observation.concentration:0.1",
                    module=session.module_id, channel=ch["name"],
                    quality=quality,
                    context={"calibration_id": cal["id"] if cal else None,
                             "method": session.manifest.get("method"),
                             "role": session.role},
                    trace={"command_id": cmd_id, "causation_id": result["id"],
                           "correlation_id": cmd_id})

    return interpreter
