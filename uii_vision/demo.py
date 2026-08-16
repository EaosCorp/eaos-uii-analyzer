"""Live foam-coverage demo — hub + campod in sim, ~18 s, no camera.

Spins a real hub with the vision extension and an in-process campod against
a synthetic basin whose foam we ramp up, so you can watch the classical CV
track the hidden truth, watch a low-quality frame drop to permitted-use
'none', and see one grab-on-demand capture flow through the command gate
with lineage back to its frame.

    python3 -m uii_vision.demo

Needs numpy + Pillow (the vision extension's deps).
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time

from uii.hub.config import HubConfig
from uii.hub.main import Hub

from .campod import CamModule


def main():
    tmp = tempfile.mkdtemp(prefix="uii-vision-demo-")
    frames_dir = os.path.join(tmp, "frames")
    cfg = {"hub_id": "hub-vision-demo", "allowed_types": ["vision-cam"],
           "extensions": ["vision"], "data_dir": os.path.join(tmp, "data"),
           "roles": {"slot-1": {"role": "aeration-foam-cam", "cadence_s": 0.5,
                                "control_ok": False}}}
    cfg_path = os.path.join(tmp, "hub.json")
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)
    os.environ["UII_FRAMES_DIR"] = frames_dir            # hub + campod share it

    hub = Hub(HubConfig(cfg_path), sb_port=0, api_port=0, speed=1.0).start()
    print(f"hub up · api :{hub.api_port} · southbound :{hub.sb_port}")
    store = hub.store

    env = {"UII_HUB": f"127.0.0.1:{hub.sb_port}", "UII_SIM": "1", "UII_SPEED": "1",
           "UII_MODULE_ID": "campod-01", "UII_SLOT": "slot-1",
           "UII_TYPE": "vision-cam", "UII_SERIAL": "SN-CAMPOD-01",
           "UII_FRAMES_DIR": frames_dir, "UII_CADENCE_S": "0.5",
           "UII_SIM_COVERAGE": "10", "UII_SIM_FOAM": "nuisance_white"}
    mod = CamModule(env)

    def run_mod():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.create_task(mod.health_loop())
        loop.create_task(mod.cadence_loop())
        try:
            loop.run_until_complete(mod.run())
        except Exception:
            pass

    threading.Thread(target=run_mod, daemon=True).start()
    time.sleep(1.5)  # adopt

    print("\n telemetry stream (module streams frames; hub derives foam):")
    print("   t   sim truth   foam_cv   type               use")
    try:
        for i in range(14):
            truth = min(80, 10 + i * 6)
            q = 0.15 if i == 8 else 1.0        # one bad (dark) frame at t=8
            mod.source.set(coverage_pct=truth, quality=q)
            time.sleep(1.0)
            o = store.latest_observation("campod-01", "foam_coverage")
            if o:
                d, ql = o["data"], o["quality"]
                tag = "  <- dark frame" if q < 0.5 else ""
                print(f"  {i:2d}   {truth:6.0f}%   {d['value']:6.1f}%   "
                      f"{d['foam_type']:17s} {ql['permitted_use']}{tag}")

        mod.source.set(coverage_pct=40, quality=1.0)
        env2, problem = hub.gateway.submit("campod-01", "capture", {}, actor="user:local")
        time.sleep(1.0)
        if env2:
            cid = env2["data"]["command_id"]
            hits = store.query(kind="observation", module="campod-01",
                               channel="foam_coverage", command_id=cid)
            if hits:
                up = store.lineage(hits[0]["id"])["upstream"]
                print(f"\n grab-on-demand · capture {cid[:8]} -> foam "
                      f"{hits[0]['data']['value']}% · lineage: {len(up)} upstream "
                      f"envelope(s) back to the frame")
        total = len(store.query(kind="observation", module="campod-01",
                                channel="foam_coverage", limit=5000))
        print(f"\n {total} foam_coverage observations in evidence, "
              f"every one re-derivable from its stored frame.")
    finally:
        hub.stop()


if __name__ == "__main__":
    main()
