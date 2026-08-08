"""The vision profile end to end: campod (sim) -> hub -> foam_coverage.

Proves both data shapes and the class-blind reuse:
  * telemetry  — streamed frames become foam_coverage without a command
  * on demand  — a capture command returns a frame and derives foam with lineage
  * permitted-use gating — a low-quality frame reports "none"
  * adoption/swap and evidence come free from the universal core
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest

from tests.helpers import Bench, ROOT, post, wait_for

VIS_ROLES = {
    "slot-1": {"role": "aeration-foam-cam", "cadence_s": 0.05, "control_ok": False},
}


def spawn_campod(bench, module_id="campod-1", slot="slot-1", serial=None,
                 coverage=25.0, foam="nuisance_white", quality=1.0):
    frames_dir = os.path.join(bench.tmp, "data", "frames")
    env = dict(os.environ,
               UII_HUB=f"127.0.0.1:{bench.hub.sb_port}", UII_SIM="1",
               UII_SPEED=str(bench.speed),
               UII_MODULE_ID=module_id, UII_SLOT=slot, UII_TYPE="vision-cam",
               UII_SERIAL=serial or f"SN-{module_id.upper()}",
               UII_FRAMES_DIR=frames_dir, UII_CADENCE_S="0.05",
               UII_SIM_COVERAGE=str(coverage), UII_SIM_FOAM=foam,
               UII_SIM_QUALITY=str(quality), PYTHONPATH=ROOT)
    p = subprocess.Popen([sys.executable, "-m", "extensions.vision.campod"],
                         cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    bench.procs.append(p)
    return p


class VisionProfileTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("UII_FRAMES_DIR", None)   # hub uses <data_dir>/frames
        self.bench = Bench(roles=VIS_ROLES, allowed=["vision-cam"],
                           extensions=["vision"])

    def tearDown(self):
        for p in self.bench.procs:
            p.terminate()
        for p in self.bench.procs:
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        try:
            self.bench.hub.stop()
        except Exception:
            pass

    def foam_obs(self, module_id="campod-1", **kw):
        return self.bench.hub.store.query(kind="observation", module=module_id,
                                          channel="foam_coverage", **kw)

    def test_adopt_and_telemetry_stream(self):
        """Streamed frames become foam_coverage with no command in the loop."""
        spawn_campod(self.bench, coverage=25.0)
        wait_for(lambda: self.bench.state_of("campod-1") == "OPERATIONAL",
                 what="campod adopted")
        obs = wait_for(lambda: self.foam_obs() or None, what="foam_coverage stream")
        latest = self.bench.hub.store.latest_observation("campod-1", "foam_coverage")
        val = latest["data"]["value"]
        self.assertTrue(20 <= val <= 30, f"coverage {val} not ~25")
        self.assertEqual(latest["quality"]["permitted_use"], "reporting")
        # telemetry frames carry no command_id in their lineage
        self.assertIsNone((latest.get("trace") or {}).get("command_id"))
        self.assertEqual(latest["data"]["foam_type"], "nuisance_white")

    def test_on_demand_capture_has_lineage(self):
        """A capture command grabs a frame and derives foam tied to the command."""
        spawn_campod(self.bench, coverage=50.0)
        wait_for(lambda: self.bench.state_of("campod-1") == "OPERATIONAL",
                 what="campod adopted")
        status, body = post(self.bench.base, "/v1/commands",
                            {"module": "campod-1", "type": "capture"})
        self.assertEqual(status, 202, body)
        cmd_id = body["command_id"]
        cmd_obs = wait_for(lambda: self.foam_obs(command_id=cmd_id) or None,
                           what="foam from capture command")
        o = cmd_obs[0]
        self.assertEqual(o["trace"]["command_id"], cmd_id)
        self.assertTrue(45 <= o["data"]["value"] <= 55)
        # lineage: the derived foam points back at a real frame envelope
        lin = self.bench.hub.store.lineage(o["id"])
        self.assertTrue(lin["upstream"], "foam observation has no upstream frame")

    def test_low_quality_gates_to_none(self):
        """A dark/blurry frame is not fit for reporting."""
        spawn_campod(self.bench, coverage=30.0, quality=0.15)
        wait_for(lambda: self.bench.state_of("campod-1") == "OPERATIONAL",
                 what="campod adopted")
        wait_for(lambda: self.foam_obs() or None, what="foam_coverage")
        latest = self.bench.hub.store.latest_observation("campod-1", "foam_coverage")
        self.assertEqual(latest["quality"]["permitted_use"], "none")
        self.assertIn(latest["quality"]["status"], ("bad",))

    def test_brown_foam_type(self):
        """Biological (Nocardia-style) foam classifies distinctly from nuisance."""
        spawn_campod(self.bench, coverage=45.0, foam="biological_brown")
        wait_for(lambda: self.bench.state_of("campod-1") == "OPERATIONAL",
                 what="campod adopted")
        wait_for(lambda: self.foam_obs() or None, what="foam_coverage")
        latest = self.bench.hub.store.latest_observation("campod-1", "foam_coverage")
        self.assertEqual(latest["data"]["foam_type"], "biological_brown")


if __name__ == "__main__":
    unittest.main()
