"""The playbook's Stage 1-2 exit demo, in software: power-cycle the hub
mid-run — the run finishes on the module, the log is intact, and the
result (with its interpretation) lands after the hub comes back. Plus the
other half of "one result per command, always": module disappearance."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from ..helpers import ROOT, get, post, wait_for


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_hub(cfg_path, sb_port, api_port):
    from uii.hub.config import HubConfig
    from uii.hub.main import Hub
    for var in ("UII_HUB_ID", "UII_ALLOWED", "UII_DATA", "UII_EXTENSIONS"):
        os.environ.pop(var, None)
    return Hub(HubConfig(cfg_path), sb_port=sb_port, api_port=api_port).start()


def spawn_refmod(sb_port, module_id, speed):
    env = dict(os.environ, UII_HUB=f"127.0.0.1:{sb_port}",
               UII_MODULE_ID=module_id, UII_SLOT="slot-1",
               UII_SPEED=str(speed), PYTHONPATH=ROOT)
    return subprocess.Popen([sys.executable, "-m", "uii.refmod"],
                            cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cmd_status(base, command_id):
    return get(base, f"/v1/commands/{command_id}")


class TestHubPowerCycleMidRun(unittest.TestCase):
    def test_run_survives_hub_restart(self):
        tmp = tempfile.mkdtemp(prefix="uii-restart-")
        cfg = os.path.join(tmp, "hub.json")
        with open(cfg, "w") as f:
            json.dump({"hub_id": "hub-restart",
                       "allowed_types": ["refmod-nh4"],
                       "data_dir": os.path.join(tmp, "data"),
                       "roles": {"slot-1": {"role": "nh4-manual",
                                            "analyte": "NH4"}}}, f)
        sb_port, api_port = free_port(), free_port()
        hub = make_hub(cfg, sb_port, api_port)
        base = f"http://127.0.0.1:{api_port}"
        proc = spawn_refmod(sb_port, "ref-r", speed=12)   # sample takes ~2 s
        try:
            wait_for(lambda: any(m["id"] == "ref-r" and m["state"] == "OPERATIONAL"
                                 for m in get(base, "/v1/modules")["items"]),
                     what="adoption")
            # calibrate first so the post-restart interpretation has a curve
            code, resp = post(base, "/v1/commands",
                              {"module": "ref-r", "type": "calibrate",
                               "params": {"std_conc": 5.0}})
            wait_for(lambda: cmd_status(base, resp["command_id"])["state"] == "done",
                     timeout=30, what="calibrate")

            # start a sample, then kill the hub while the run is in flight
            code, resp = post(base, "/v1/commands",
                              {"module": "ref-r", "type": "sample"})
            cmd_id = resp["command_id"]
            wait_for(lambda: cmd_status(base, cmd_id)["ack"] is not None,
                     timeout=15, what="ack before the outage")
            hub.stop()
            time.sleep(3)   # the module finishes its run alone, buffering

            # hub comes back on the same ports and store
            hub = make_hub(cfg, sb_port, api_port)
            st = wait_for(
                lambda: (lambda s: s if s and s["state"] == "done" else None)(
                    cmd_status(base, cmd_id)),
                timeout=45, what="result landing after reboot")
            self.assertEqual(st["result"]["data"]["status"], "succeeded")
            # interpretation ran too: the command context was recovered
            # from the store, not from lost memory
            obs = st["derived"][-1]
            self.assertEqual(obs["quality"]["status"], "good")
            self.assertEqual(obs["quality"]["permitted_use"], "control")
        finally:
            proc.kill()
            hub.stop()

    def test_module_disappearance_still_yields_one_result(self):
        tmp = tempfile.mkdtemp(prefix="uii-modloss-")
        cfg = os.path.join(tmp, "hub.json")
        with open(cfg, "w") as f:
            json.dump({"hub_id": "hub-modloss",
                       "allowed_types": ["refmod-nh4"],
                       "data_dir": os.path.join(tmp, "data"),
                       "roles": {"slot-1": {"role": "nh4-manual",
                                            "analyte": "NH4"}}}, f)
        sb_port, api_port = free_port(), free_port()
        hub = make_hub(cfg, sb_port, api_port)
        base = f"http://127.0.0.1:{api_port}"
        proc = spawn_refmod(sb_port, "ref-x", speed=4)    # sample takes ~6 s
        try:
            wait_for(lambda: any(m["id"] == "ref-x" and m["state"] == "OPERATIONAL"
                                 for m in get(base, "/v1/modules")["items"]),
                     what="adoption")
            code, resp = post(base, "/v1/commands",
                              {"module": "ref-x", "type": "sample"})
            cmd_id = resp["command_id"]
            wait_for(lambda: cmd_status(base, cmd_id)["ack"] is not None,
                     timeout=15, what="ack")
            proc.kill()   # unplug mid-command
            st = wait_for(
                lambda: (lambda s: s if s and s["state"] == "done" else None)(
                    cmd_status(base, cmd_id)),
                timeout=20, what="terminal result after module loss")
            self.assertEqual(st["result"]["data"]["status"], "failed")
            self.assertEqual(st["result"]["data"]["problem"],
                             "urn:uii:problem:module-lost")
        finally:
            try:
                proc.kill()
            except Exception:
                pass
            hub.stop()


if __name__ == "__main__":
    unittest.main()
