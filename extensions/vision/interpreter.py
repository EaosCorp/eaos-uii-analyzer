"""The vision interpreter — half two of the seam, for cameras.

The module produces frames (facts); this produces foam_coverage
(interpretations), each with a permitted-use designation from image quality
and lineage back to the exact frame. It is registered two ways, one per data
shape:

  * at hub.southbound.interpreters["vision"] — fires on a `capture` command
    RESULT (the grab-on-demand path), and also drives the re_baseline /
    set_region / ptz lifecycle.
  * inside FoamService (service.py) — fires on every streamed `frames`
    observation (the telemetry path).

Both call one function, `interpret_frame`, so a frame is measured the same
way however it arrived. Per-module ROI and baseline live in VisionState,
shared between the two, so a frame is never measured against a stale region;
a view move (ptz) or a new ROI holds the module in re-baseline-required
until re_baseline clears it (vision-profile §6).
"""
from __future__ import annotations

from .foam_cv import detect, rect_roi, permitted_use
from .framesource import decode

FOAM_SCHEMA = "urn:uii:schema:observation.foam:0.1"
REF_SCHEMA = "urn:uii:schema:vision.reference:0.1"
ROI_SCHEMA = "urn:uii:schema:vision.roi:0.1"
EVENT_SCHEMA = "urn:uii:schema:event:0.1"


class ModuleVision:
    """Live per-module vision config; seeded from role_config, mutated by
    set_region / re_baseline / ptz."""
    def __init__(self, roi=None, params=None, control_ok=False):
        self.roi = roi              # (x0,y0,x1,y1) fractions, or None = whole frame
        self.params = params        # foam-cv threshold overrides, or None
        self.control_ok = control_ok
        self.baseline_hash = None
        self.re_baseline_required = False


class VisionState:
    def __init__(self):
        self.mods: dict[str, ModuleVision] = {}

    def get(self, module_id, role_config=None) -> ModuleVision:
        mv = self.mods.get(module_id)
        if mv is None:
            rc = role_config or {}
            roi = rc.get("roi")
            roi_t = (roi["x0"], roi["y0"], roi["x1"], roi["y1"]) if roi else None
            mv = ModuleVision(roi=roi_t, params=rc.get("foam_params"),
                              control_ok=bool(rc.get("control_ok", False)))
            self.mods[module_id] = mv
        return mv


def interpret_frame(store, blobs, module_id, role, frame_hash, mv,
                    causation_id, cmd_id=None):
    """Load the frame by hash, run foam CV over the ROI, append the derived
    observations with permitted-use and lineage. Returns the CV dict (or None)."""
    data = blobs.get(frame_hash)
    if not data:
        store.append("event", {"event": "frame-missing", "frame_hash": frame_hash},
                     EVENT_SCHEMA, module=module_id,
                     trace={"causation_id": causation_id, "command_id": cmd_id})
        return None
    rgb = decode(data)
    roi = rect_roi(rgb.shape, *mv.roi) if mv.roi else None
    cv = detect(rgb, roi=roi, params=mv.params)

    use, flags = permitted_use(cv, control_ok=mv.control_ok)
    if mv.re_baseline_required:
        use, flags = "none", flags + ["re_baseline_required"]
    q_cov = {"status": "good" if use != "none" else "bad",
             "flags": flags, "permitted_use": use}
    ctx = {"model_id": cv["model_id"], "foam_type": cv["foam_type"],
           "role": role, "roi": mv.roi, "frame_hash": frame_hash,
           "baseline_hash": mv.baseline_hash}
    trace = {"causation_id": causation_id, "correlation_id": cmd_id,
             "command_id": cmd_id}

    store.append("observation",
                 {"value": cv["coverage_pct"], "unit": "%",
                  "foam_type": cv["foam_type"], "confidence": cv["confidence"],
                  "image_quality": cv["image_quality"], "frame_hash": frame_hash},
                 FOAM_SCHEMA, module=module_id, channel="foam_coverage",
                 quality=q_cov, context=ctx, trace=trace)
    store.append("observation",
                 {"value": cv["image_quality"], "unit": "score",
                  "components": cv["quality_components"]},
                 FOAM_SCHEMA, module=module_id, channel="image_quality",
                 quality={"status": "good", "flags": [], "permitted_use": "reporting"},
                 context=ctx, trace=trace)
    return cv


def interpret_frame_env(store, blobs, state, env, session_lookup=None):
    """FoamService entry: interpret one streamed `frames` observation envelope.
    Skips command-tied frames (the command interpreter handles those) so a
    frame is never measured twice."""
    src = env.get("source") or {}
    if (src.get("channel") or env.get("channel")) != "frames":
        return None
    if env.get("kind") not in (None, "observation"):
        return None
    data = env.get("data") or {}
    if data.get("frame_hash") is None:
        return None
    trace = env.get("trace") or {}
    if trace.get("command_id"):        # on-demand frame -> command interpreter owns it
        return None
    module_id = src.get("module") or data.get("module")
    if not module_id:
        return None
    role = None
    role_config = None
    if session_lookup:
        sess = session_lookup(module_id)
        role = getattr(sess, "role", None) if sess else None
        role_config = getattr(sess, "role_config", None) if sess else None
    mv = state.get(module_id, role_config)
    return interpret_frame(store, blobs, module_id, role,
                           data["frame_hash"], mv, causation_id=env.get("id"))


def make_vision_interpreter(blobs, state: VisionState):
    """The callable registered at hub.southbound.interpreters['vision'].
    Fires on a command RESULT (grab-on-demand + lifecycle commands)."""

    def interpreter(session, cmd, result, outputs, raw_refs):
        store = session.hub.store
        module_id = session.module_id
        cmd_id = (cmd.get("data") or {}).get("command_id")
        mv = state.get(module_id, getattr(session, "role_config", None))
        action = (outputs or {}).get("action")

        if action == "capture":
            fh = outputs.get("frame_hash")
            if fh:
                interpret_frame(store, blobs, module_id, session.role, fh, mv,
                                causation_id=result["id"], cmd_id=cmd_id)

        elif action == "re_baseline":
            mv.baseline_hash = outputs.get("frame_hash")
            mv.re_baseline_required = False
            store.append("config",
                         {"reference_hash": mv.baseline_hash, "roi": mv.roi},
                         REF_SCHEMA, module=module_id,
                         trace={"causation_id": result["id"], "command_id": cmd_id})

        elif action == "set_region":
            roi = outputs.get("roi") or {}
            mv.roi = ((roi["x0"], roi["y0"], roi["x1"], roi["y1"])
                      if {"x0", "y0", "x1", "y1"} <= set(roi) else None)
            mv.re_baseline_required = True      # new ROI => re-baseline first
            store.append("config", {"roi": roi}, ROI_SCHEMA, module=module_id,
                         trace={"causation_id": result["id"], "command_id": cmd_id})

        elif action == "ptz":
            mv.re_baseline_required = True       # view moved; ROIs invalid

    return interpreter
