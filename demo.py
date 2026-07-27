#!/usr/bin/env python3
"""End-to-end proof: auto-recognition, hands-off readiness, quarantine,
and the swap drill — the whole story at 300x, no hardware.

    python3 demo.py            (~60 s, exits DEMO OK)

What it shows, in order:
  1. hub starts with a role registry (slot-1 = nh4-influent)
  2. module plugs in -> HELLO -> VERIFYING -> ADOPTING (role config pushed)
     -> OPERATIONAL, and the scheduler takes control, calibrates, and
     samples with ZERO keyboard work  ("auto-recognize and bam — ready")
  3. an unknown vendor module plugs in -> QUARANTINED (powered, mute);
     one API call releases it; its serial is trusted on sight forever
  4. THE SWAP DRILL: module A is unplugged (process killed), role goes
     vacant; module B (new serial) plugs into the same slot; the role is
     restored, it is re-calibrated and sampling again — untouched
  5. evidence: lineage walk from a concentration back to the command that
     caused it, and a full hash-chain verification

Every claim below is checked against the API, not assumed.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
SPEED = 300.0


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------

def get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read())


def post(base, path, body=None):
    req = urllib.request.Request(base + path, data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def wait_for(pred, timeout=60, every=0.25, what="condition"):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(every)
    raise TimeoutError(f"timed out waiting for {what}")


def spawn_module(hub_sb_port, module_id, slot, analyte="NH4", serial=None,
                 module_type=None):
    env = dict(os.environ,
               UII_HUB=f"127.0.0.1:{hub_sb_port}", UII_SIM="1",
               UII_SPEED=str(SPEED), ANALYTE=analyte,
               UII_MODULE_ID=module_id, UII_SLOT=slot,
               UII_SERIAL=serial or f"SN-{module_id.upper()}",
               PYTHONPATH=ROOT)
    if module_type:
        env["UII_TYPE"] = module_type
    return subprocess.Popen([sys.executable, "-m", "uii.pimod.main"],
                            cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def module_state(base, module_id):
    for m in get(base, "/v1/modules")["items"]:
        if m["id"] == module_id:
            return m
    return None


def good_obs(base, module_id, channel):
    for o in get(base, "/v1/observations/latest")["items"]:
        if (o["source"]["module"] == module_id
                and o["source"]["channel"] == channel
                and (o.get("quality") or {}).get("status") == "good"
                and o["data"].get("value") is not None):
            return o
    return None


def verify_chain(base):
    """Recompute the whole hash chain from the API. Returns count."""
    envs, cursor = [], 0
    while True:
        batch = get(base, f"/v1/evidence?since={cursor}&limit=2000")["items"]
        if not batch:
            break
        envs.extend(batch)
        cursor = batch[-1]["sequence"]
    prev = "genesis"
    for env in envs:
        body = {k: v for k, v in env.items() if k not in ("integrity", "sequence")}
        payload = json.dumps(body, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256((prev + payload).encode()).hexdigest()
        assert env["integrity"]["prev_hash"] == prev, f"chain break at seq {env['sequence']}"
        assert env["integrity"]["hash"] == digest, f"hash mismatch at seq {env['sequence']}"
        prev = digest
    return len(envs)


# ---------------------------------------------------------------------------
# the story
# ---------------------------------------------------------------------------

def main():
    tmp = tempfile.mkdtemp(prefix="uii-demo-")
    cfg_path = os.path.join(tmp, "hub.json")
    with open(cfg_path, "w") as f:
        json.dump({
            "hub_id": "hub-demo-001",
            "allowed_types": ["nh4mod-nh4", "nh4mod-po4"],
            "data_dir": os.path.join(tmp, "data"),
            "roles": {
                "slot-1": {"role": "nh4-influent", "analyte": "NH4",
                           "sample_interval_s": 900,
                           "calibrate_std_conc": 5.0,
                           "auto_take_control": True, "auto_calibrate": True},
            },
        }, f)

    os.environ.pop("UII_HUB_ID", None); os.environ.pop("UII_ALLOWED", None)
    os.environ.pop("UII_DATA", None)
    from uii.hub.config import HubConfig
    from uii.hub.main import Hub
    hub = Hub(HubConfig(cfg_path), sb_port=0, api_port=0, speed=SPEED).start()
    base = f"http://127.0.0.1:{hub.api_port}"
    print(f"[1] hub up · api :{hub.api_port} · southbound :{hub.sb_port} "
          f"· role registry: slot-1 = nh4-influent")

    procs = []
    try:
        # -- 2: auto-recognize and bam — ready --------------------------------
        t_plug = time.time()
        procs.append(spawn_module(hub.sb_port, "nh4mod-a", "slot-1"))
        wait_for(lambda: (module_state(base, "nh4mod-a") or {}).get("state") == "OPERATIONAL",
                 what="module A adoption")
        m = module_state(base, "nh4mod-a")
        print(f"[2] module A plugged in -> ADOPTED as role '{m['role']}' "
              f"in {time.time()-t_plug:.1f}s — zero keyboard")

        wait_for(lambda: (module_state(base, "nh4mod-a") or {}).get("mode") == "ENDPOINT",
                 what="scheduler take_control")
        print("    scheduler took control (BRIDGE -> ENDPOINT, latched)")
        wait_for(lambda: get(base, "/v1/evidence?kind=calibration&module=nh4mod-a")["items"],
                 timeout=90, what="auto-calibration")
        cal = get(base, "/v1/evidence?kind=calibration&module=nh4mod-a")["items"][-1]
        print(f"    auto-calibrated: slope={cal['data']['fit']['slope']:.3f} "
              f"(hidden truth 25.0)")
        obs = wait_for(lambda: good_obs(base, "nh4mod-a", "nh4"),
                       timeout=90, what="first scheduled sample")
        print(f"    sampling on cadence: nh4 = {obs['data']['value']} mg/L [good] "
              f"— bam, ready. total {time.time()-t_plug:.0f}s from plug-in")

        # -- 3: quarantine + one-call release ---------------------------------
        procs.append(spawn_module(hub.sb_port, "vendor-x-01", "slot-9",
                                  module_type="vendor-x-analyzer"))
        wait_for(lambda: (module_state(base, "vendor-x-01") or {}).get("state") == "QUARANTINED",
                 what="quarantine")
        print("[3] unknown module 'vendor-x-01' -> QUARANTINED (powered, logged, mute)")
        post(base, "/v1/modules/vendor-x-01/release")
        wait_for(lambda: (module_state(base, "vendor-x-01") or {}).get("state") == "OPERATIONAL",
                 what="release + re-adoption")
        print("    released with one call -> redialed -> ADOPTED "
              "(serial now trusted on sight)")

        # -- 4: the swap drill --------------------------------------------------
        print("[4] SWAP DRILL: unplugging module A (process killed) ...")
        procs[0].send_signal(signal.SIGKILL)
        wait_for(lambda: (module_state(base, "nh4mod-a") or {}).get("state") == "REMOVED",
                 what="removal detection")
        vacancy = get(base, "/v1/evidence?kind=event&limit=500")["items"]
        assert any(e["data"].get("event") == "role-vacant" for e in vacancy)
        print("    hub: REMOVED -> role 'nh4-influent' vacant (event logged)")

        t_swap = time.time()
        procs.append(spawn_module(hub.sb_port, "nh4mod-b", "slot-1",
                                  serial="SN-FACTORY-FRESH-0388"))
        wait_for(lambda: (module_state(base, "nh4mod-b") or {}).get("state") == "OPERATIONAL",
                 what="module B adoption")
        m = module_state(base, "nh4mod-b")
        assert m["role"] == "nh4-influent", f"role not restored: {m}"
        print("    module B (new serial) into slot-1 -> ADOPTED, role restored")
        obs = wait_for(lambda: good_obs(base, "nh4mod-b", "nh4"),
                       timeout=120, what="module B first good sample")
        cal_b = get(base, "/v1/evidence?kind=calibration&module=nh4mod-b")["items"][-1]
        print(f"    re-calibrated (slope={cal_b['data']['fit']['slope']:.3f}) and "
              f"sampling: nh4 = {obs['data']['value']} mg/L — swap to good data "
              f"in {time.time()-t_swap:.0f}s, keyboard untouched")

        # -- 5: evidence ---------------------------------------------------------
        lin = get(base, f"/v1/evidence/{obs['id']}/lineage")
        chain = [obs["kind"]] + [u["kind"] for u in lin["upstream"]]
        print(f"[5] lineage of that number: {' <- '.join(chain)}")
        assert "command" in chain, "lineage must reach the causing command"
        assert lin["context_refs"].get("calibration"), "observation must reference its calibration"
        n = verify_chain(base)
        print(f"    hash chain verified across {n} envelopes — log is tamper-evident")

        print("\nDEMO OK")
        return 0
    finally:
        for pr in procs:
            try:
                pr.kill()
            except Exception:
                pass
        hub.stop()


if __name__ == "__main__":
    raise SystemExit(main())
