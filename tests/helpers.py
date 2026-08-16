"""Shared bench harness for the test suite. Stdlib only.

A "bench" is a real hub on ephemeral ports with a tmp evidence store.
Extensions are enabled per-bench exactly the way a site would
("extensions": [...] in the config). Modules are spawned either as
subprocesses (kill = unplug) — refmod (the core reference module) or the
analyzer extension's pimod in sim mode — or in-process when a test needs
to reach the sim hardware.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEED = 400.0


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read())


def post(base, path, body=None):
    req = urllib.request.Request(base + path, data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def wait_for(pred, timeout=60, every=0.2, what="condition"):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(every)
    raise TimeoutError(f"timed out waiting for {what}")


DEFAULT_ROLES = {
    "slot-1": {"role": "nh4-influent", "analyte": "NH4",
               "sample_interval_s": 900, "calibrate_std_conc": 5.0,
               "auto_take_control": True, "auto_calibrate": True},
}


class Bench:
    def __init__(self, roles=None, allowed=None, extensions=None,
                 extra_config=None, speed=SPEED):
        self.tmp = tempfile.mkdtemp(prefix="uii-test-")
        cfg_path = os.path.join(self.tmp, "hub.json")
        cfg = {"hub_id": "hub-test",
               "allowed_types": allowed or ["refmod-nh4", "nh4mod-nh4",
                                            "nh4mod-nox", "nh4mod-po4"],
               "extensions": extensions or [],
               "data_dir": os.path.join(self.tmp, "data"),
               "roles": roles if roles is not None else DEFAULT_ROLES}
        cfg.update(extra_config or {})
        with open(cfg_path, "w") as f:
            json.dump(cfg, f)
        for var in ("UII_HUB_ID", "UII_ALLOWED", "UII_DATA", "UII_EXTENSIONS"):
            os.environ.pop(var, None)
        from uii.hub.config import HubConfig
        from uii.hub.main import Hub
        self.speed = speed
        self.cfg_path = cfg_path
        self.hub = Hub(HubConfig(cfg_path), sb_port=0, api_port=0,
                       speed=speed).start()
        self.base = f"http://127.0.0.1:{self.hub.api_port}"
        self.procs: list[subprocess.Popen] = []

    def spawn(self, module_id, slot, analyte="NH4", serial=None,
              module_type=None, kind="refmod", speed=None) -> subprocess.Popen:
        """kind='refmod' (core reference module) or 'pimod' (the analyzer
        extension's field agent, sim mode)."""
        env = dict(os.environ,
                   UII_HUB=f"127.0.0.1:{self.hub.sb_port}", UII_SIM="1",
                   UII_SPEED=str(speed or self.speed), ANALYTE=analyte,
                   UII_MODULE_ID=module_id, UII_SLOT=slot,
                   UII_SERIAL=serial or f"SN-{module_id.upper()}",
                   PYTHONPATH=ROOT)
        if module_type:
            env["UII_TYPE"] = module_type
        target = ("uii.refmod" if kind == "refmod"
                  else "uii_analyzer.pimod")
        p = subprocess.Popen([sys.executable, "-m", target],
                             cwd=ROOT, env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        return p

    def spawn_inprocess(self, module_id, slot, analyte="NH4", serial=None):
        """In-process analyzer pimod — tests can reach the sim hardware
        (inject PLC lines, read .sent)."""
        from uii_analyzer.pimod import PiModule
        env = {"UII_HUB": f"127.0.0.1:{self.hub.sb_port}", "UII_SIM": "1",
               "UII_SPEED": str(self.speed), "ANALYTE": analyte,
               "UII_MODULE_ID": module_id, "UII_SLOT": slot,
               "UII_SERIAL": serial or f"SN-{module_id.upper()}"}
        mod = PiModule(env)

        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.create_task(mod.health_loop())
            try:
                loop.run_until_complete(mod.run())
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()
        return mod

    # -- api conveniences ---------------------------------------------------

    def module(self, module_id):
        for m in get(self.base, "/v1/modules")["items"]:
            if m["id"] == module_id:
                return m
        return None

    def state_of(self, module_id):
        return (self.module(module_id) or {}).get("state")

    def good_obs(self, module_id, channel):
        for o in get(self.base, "/v1/observations/latest")["items"]:
            if (o["source"]["module"] == module_id
                    and o["source"]["channel"] == channel
                    and (o.get("quality") or {}).get("status") == "good"
                    and o["data"].get("value") is not None):
                return o
        return None

    def close(self):
        for p in self.procs:
            try:
                p.kill()
                p.wait(timeout=3)
            except Exception:
                pass
        self.hub.stop()
