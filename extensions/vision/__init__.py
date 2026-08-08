"""The vision instrument-class profile (uii-camera-sensor).

Foam detection first, classical CV, multi-observable by design. Loaded like
any UII extension: add "vision" to `extensions` in hub.json and this
`setup(hub)` wires it in through the declared hooks before the API serves.

What it registers:
  * interpreters["vision"]      — grab-on-demand + the re_baseline/set_region/
                                  ptz lifecycle (fires on a capture RESULT)
  * FoamService (add_service)   — the telemetry stream: streamed frames ->
                                  foam_coverage
  * module_row_enrichers        — current foam % per camera on /v1/modules

The module agent is `extensions.vision.campod` (run on the edge box). Design
rationale and the full profile: docs/vision-profile.md.
"""
from __future__ import annotations

import os

from .blobstore import FrameBlobStore
from .interpreter import VisionState, make_vision_interpreter
from .service import FoamService


def _foam_enricher(store):
    def enrich(session):
        obs = store.latest_observation(session.module_id, "foam_coverage")
        iq = store.latest_observation(session.module_id, "image_quality")
        row = {}
        if obs:
            d = obs.get("data") or {}
            row["foam_coverage_pct"] = d.get("value")
            row["foam_type"] = d.get("foam_type")
            row["foam_permitted_use"] = (obs.get("quality") or {}).get("permitted_use")
        if iq:
            row["image_quality"] = (iq.get("data") or {}).get("value")
        return row
    return enrich


def setup(hub):
    frames_dir = os.environ.get("UII_FRAMES_DIR") or os.path.join(
        hub.config.data_dir, "frames")
    blobs = FrameBlobStore(frames_dir)
    state = VisionState()

    hub.southbound.interpreters["vision"] = make_vision_interpreter(blobs, state)
    hub.add_service(FoamService(hub.store, hub.southbound, blobs, state))
    hub.module_row_enrichers.append(_foam_enricher(hub.store))

    # expose the shared handles for tests / other extensions
    hub.vision = {"blobs": blobs, "state": state, "frames_dir": frames_dir}
