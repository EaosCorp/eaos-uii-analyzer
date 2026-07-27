#!/usr/bin/env python3
"""The core seam, end to end, in ~20 seconds. No extensions, no hardware.

    python3 demo.py            (exits DEMO OK)

One sentence of architecture: a module dials in and is RECOGNIZED (adopted
into its slot's role, or quarantined if unknown); every fact it produces
becomes one hash-chained EVIDENCE record; every command passes ONE GATE
and always gets exactly one result. This demo proves each clause against
the live API — read it top to bottom next to the code:

    uii/protocol.py -> uii/hub/evidence.py -> uii/hub/southbound.py
    -> uii/hub/commands.py -> uii/refmod.py -> this file

For the full staged build (scheduler, detections/NE107, authority +
approvals, exports, the NH4MOD field agent): the `staged` branch,
python3 demo_full.py
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
SPEED = 40.0


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read())


def post(base, path, body=None):
    req = urllib.request.Request(base + path, data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def wait_for(pred, timeout=30, every=0.2, what="condition"):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(every)
    raise TimeoutError(f"timed out waiting for {what}")


def spawn(sb_port, module_id, slot, module_type=None):
    env = dict(os.environ, UII_HUB=f"127.0.0.1:{sb_port}",
               UII_MODULE_ID=module_id, UII_SLOT=slot,
               UII_SPEED=str(SPEED), PYTHONPATH=ROOT)
    if module_type:
        env["UII_TYPE"] = module_type
    return subprocess.Popen([sys.executable, "-m", "uii.refmod"],
                            cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def module_state(base, module_id):
    for m in get(base, "/v1/modules")["items"]:
        if m["id"] == module_id:
            return m.get("state")
    return None


def run_cmd(base, module, ctype, params=None):
    resp = post(base, "/v1/commands",
                {"module": module, "type": ctype, "params": params or {}})
    return wait_for(lambda: (lambda s: s if s and s["state"] == "done" else None)(
        get(base, f"/v1/commands/{resp['command_id']}")), what=ctype)


def verify_chain(base):
    envs, cursor, prev = [], 0, "genesis"
    while True:
        batch = get(base, f"/v1/evidence?since={cursor}&limit=2000")["items"]
        if not batch:
            break
        envs.extend(batch)
        cursor = batch[-1]["sequence"]
    for env in envs:
        body = {k: v for k, v in env.items() if k not in ("integrity", "sequence")}
        payload = json.dumps(body, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256((prev + payload).encode()).hexdigest()
        assert env["integrity"]["hash"] == digest, f"tamper at seq {env['sequence']}"
        prev = digest
    return len(envs)


def main():
    tmp = tempfile.mkdtemp(prefix="uii-demo-")
    cfg = os.path.join(tmp, "hub.json")
    with open(cfg, "w") as f:
        json.dump({"hub_id": "hub-demo", "allowed_types": ["refmod-nh4"],
                   "data_dir": os.path.join(tmp, "data"),
                   "roles": {"slot-1": {"role": "nh4-influent",
                                        "analyte": "NH4"}}}, f)
    for var in ("UII_HUB_ID", "UII_ALLOWED", "UII_DATA", "UII_EXTENSIONS"):
        os.environ.pop(var, None)
    from uii.hub.config import HubConfig
    from uii.hub.main import Hub
    hub = Hub(HubConfig(cfg), sb_port=0, api_port=0).start()
    base = f"http://127.0.0.1:{hub.api_port}"
    print("[1] hub up (core only) · slot-1 = role 'nh4-influent'")

    procs = []
    try:
        # -- recognized on sight ---------------------------------------------
        procs.append(spawn(hub.sb_port, "ref-a", "slot-1"))
        wait_for(lambda: module_state(base, "ref-a") == "OPERATIONAL",
                 what="adoption")
        m = [x for x in get(base, "/v1/modules")["items"] if x["id"] == "ref-a"][0]
        print(f"[2] module plugged in -> VERIFIED -> role '{m['role']}' "
              f"restored -> OPERATIONAL (zero keyboard)")

        # -- unknown module: powered, logged, mute ---------------------------
        procs.append(spawn(hub.sb_port, "vendor-x", "slot-9",
                           module_type="vendor-unknown"))
        wait_for(lambda: module_state(base, "vendor-x") == "QUARANTINED",
                 what="quarantine")
        post(base, "/v1/modules/vendor-x/release")
        wait_for(lambda: module_state(base, "vendor-x") == "OPERATIONAL",
                 what="release + re-adoption")
        print("[3] unknown module -> QUARANTINED; released with one audited "
              "call -> serial trusted on sight forever")

        # -- facts in, interpretations out ------------------------------------
        st = run_cmd(base, "ref-a", "sample")
        obs = st["derived"][0]
        assert obs["quality"]["permitted_use"] == "none"
        print(f"[4] uncalibrated sample -> value None, permitted_use "
              f"'{obs['quality']['permitted_use']}' — an unfit number says so")

        run_cmd(base, "ref-a", "calibrate", {"std_conc": 5.0})
        cal = get(base, "/v1/evidence?kind=calibration&module=ref-a")["items"][-1]
        print(f"    calibrated: hub recovered slope "
              f"{cal['data']['fit']['slope']:.3f} (module's hidden truth: 25.0)")

        st = run_cmd(base, "ref-a", "sample")
        obs = st["derived"][-1]
        print(f"    sample: nh4 = {obs['data']['value']} mg/L "
              f"[{obs['quality']['status']}, permitted_use "
              f"'{obs['quality']['permitted_use']}']")

        # -- one result per command, always ------------------------------------
        st = run_cmd(base, "ref-a", "calibrate", {"std_conc": 0})
        assert st["result"]["data"]["status"] == "rejected"
        print("[5] module rejected a bad command -> still exactly one "
              "terminal result (nothing in limbo)")

        # -- why does this number look like this? -------------------------------
        lin = get(base, f"/v1/evidence/{obs['id']}/lineage")
        chain = [obs["kind"]] + [u["kind"] for u in lin["upstream"]]
        raw = get(base, f"/v1/evidence/{obs['data']['raw_refs'][0]}")
        print(f"[6] lineage: {' <- '.join(chain)}; raw detector volts "
              f"({raw['data']['vin']}V) and the calibration are one hop away")

        n = verify_chain(base)
        print(f"[7] hash chain re-verified across all {n} envelopes — "
              f"nothing can have been edited")

        print("\nDEMO OK")
        return 0
    finally:
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass
        hub.stop()


if __name__ == "__main__":
    raise SystemExit(main())
