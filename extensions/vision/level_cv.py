"""TEMPLATE — the level / weir observable (scaffold, not yet calibrated).

The planned second vision observable (vision-profile.md §4, §14). This is the
*pattern*, not a finished detector: a first-cut classical waterline finder you
fill in with a pixel-scale reference record and site tuning before any reading
is trusted. It mirrors foam_cv.py so adding an observable is a known move —
a `detect()` that turns a frame + ROI into a value, plus channels in the
manifest and a role_config flag to turn it on. Adoption, evidence, alerts, and
the agent surface do not change.

Two siblings share the same edge primitive:
  * level — the free water surface in a channel/basin, as a fraction of the ROI
            height, and (once calibrated) a physical stage via pixel scale.
  * weir  — the water line relative to a surveyed weir-crest datum (submergence).

Status: **TEMPLATE**. `detect()` runs and is deterministic, but the thresholds
are placeholders and there is no calibration. Do NOT wire this into a live role
until a crest datum + pixel scale are recorded (as a reference record, the way
re_baseline records the foam baseline) and the reading is validated against a
staff gauge. The three TODOs below are the whole job.
"""
from __future__ import annotations

import numpy as np

from .foam_cv import _rgb_to_vsh, rect_roi  # noqa: F401  (rect_roi re-exported)

MODEL_ID = "level-cv/0.1.0-template"

DEFAULTS = {
    "smooth_rows": 5,            # vertical smoothing before the gradient
    "min_edge_strength": 0.02,   # TODO(site): tune to the basin lighting
    # TODO(calibrate): physical stage across the full ROI height, e.g. metres.
    # None => report the normalized fraction only.
    "units_per_frac": None,
    "unit": "frac",
    # TODO(survey): ROI-fractional row of the weir crest for the weir variant.
    # None => plain level (no submergence).
    "weir_crest_frac": None,
}


def _roi_bounds(roi):
    ys, xs = np.where(roi)
    return int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max())


def detect(rgb, roi=None, params=None):
    """First-cut waterline: the ROI row with the strongest air<->water
    brightness step. Returns a normalized level, plus a physical value only
    once a scale is calibrated. TEMPLATE — see the module docstring."""
    p = {**DEFAULTS, **(params or {})}
    rgb = np.asarray(rgb)
    if rgb.ndim == 2:
        rgb = np.stack([rgb] * 3, -1)
    elif rgb.ndim == 3 and rgb.shape[2] == 1:
        rgb = np.repeat(rgb, 3, axis=2)
    h, w = rgb.shape[0], rgb.shape[1]
    if roi is None:
        roi = np.ones((h, w), bool)
    if not roi.any():
        return _empty(p, "empty_roi")
    y0, y1, x0, x1 = _roi_bounds(roi)
    v, _, _ = _rgb_to_vsh(rgb)
    band = v[y0:y1 + 1, x0:x1 + 1]
    if band.shape[0] < 3:
        return _empty(p, "degenerate")

    row_mean = band.mean(axis=1)
    k = max(1, int(p["smooth_rows"]))
    if k > 1:                                    # edge-pad so smoothing adds no false edge
        row_p = np.pad(row_mean, k // 2, mode="edge")
        row_s = np.convolve(row_p, np.ones(k) / k, mode="valid")[:len(row_mean)]
    else:
        row_s = row_mean
    grad = np.abs(np.diff(row_s))
    m = min(k, grad.size // 4)                    # ignore a small margin at band edges
    if grad.size - 2 * m >= 1:
        edge_i = int(np.argmax(grad[m:grad.size - m])) + m
    else:
        edge_i = int(np.argmax(grad))
    strength = float(grad[edge_i])
    surface_frac = edge_i / max(band.shape[0] - 1, 1)    # 0 top .. 1 bottom
    level_frac = 1.0 - surface_frac                      # 0 empty .. 1 full
    confidence = (min(1.0, strength / 0.15)
                  if strength >= p["min_edge_strength"] else 0.0)

    out = {"model_id": MODEL_ID, "unit": p["unit"],
           "level_frac": round(level_frac, 4),
           "surface_row_frac": round(surface_frac, 4),
           "edge_strength": round(strength, 4),
           "confidence": round(confidence, 4),
           "calibrated": False, "value": None}
    # physical stage — only if the site recorded a scale (TODO calibrate)
    if p["units_per_frac"] is not None:
        out["value"] = round(level_frac * float(p["units_per_frac"]), 4)
        out["unit"] = p.get("unit", "m")
        out["calibrated"] = True
    # weir submergence — only with a surveyed crest (TODO survey)
    if p["weir_crest_frac"] is not None:
        out["weir_submergence_frac"] = round(
            level_frac - (1.0 - float(p["weir_crest_frac"])), 4)
    return out


def _empty(p, reason=""):
    return {"model_id": MODEL_ID, "unit": p["unit"], "level_frac": 0.0,
            "surface_row_frac": 0.0, "edge_strength": 0.0, "confidence": 0.0,
            "calibrated": False, "value": None, "reason": reason}


# ---------------------------------------------------------------------------
# Wiring a second observable (the pattern, for whoever finishes this):
#
# 1. Manifest (campod.py): add channels {"name":"level","derived_by_hub":True}
#    (and "weir_submergence" if used). No new command is required — level rides
#    the same frames.
# 2. Interpreter (interpreter.py): in interpret_frame, when the role's
#    observables include "level", call level_cv.detect(rgb, roi=mv.roi,
#    params=mv.level_params) and append a "level" observation with a
#    permitted-use gate (uncalibrated => "none"; calibrated + confident =>
#    "reporting"). Reuse the foam frame; do not grab a second one.
# 3. Reference record: add a "level datum" alongside the foam baseline
#    (units_per_frac + weir_crest_frac), recorded like re_baseline so it is
#    evidence and travels with the role.
# 4. Detections (hub.json): level-high / level-low thresholds, weir-overtopping.
#
# Turn it on per role via role_config, e.g.
#   "roles": {"slot-1": {"observables": ["foam_coverage","level"],
#                        "level": {"units_per_frac": 1.8, "unit": "m"}}}
# ---------------------------------------------------------------------------
