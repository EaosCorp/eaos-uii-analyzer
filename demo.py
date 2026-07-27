#!/usr/bin/env python3
"""End-to-end demo: hub + two fake modules + calibrate + sample + lineage.

Run:  python3 demo.py
"""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

API = "http://127.0.0.1:8400"
HERE = os.path.dirname(os.path.abspath(__file__))


def api(path, payload=None):
    req = urllib.request.Request(API + path)
    if payload is not None:
        req.data = json.dumps(payload).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def wait_command(cmd_id, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = api(f"/v1/commands/{cmd_id}")
        if st and st.get("state") == "done":
            return st
        time.sleep(0.5)
    raise TimeoutError(f"command {cmd_id} did not finish")


def spawn(mod_env=None, module=False):
    env = {**os.environ, "UII_DB": "/tmp/uii-demo/evidence.db", **(mod_env or {})}
    target = "uii.fakemod.main" if module else "uii.hub.main"
    return subprocess.Popen([sys.executable, "-m", target], cwd=HERE, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def main():
    subprocess.run(["rm", "-rf", "/tmp/uii-demo"])
    os.makedirs("/tmp/uii-demo", exist_ok=True)
    procs = [spawn()]
    time.sleep(1.0)
    procs.append(spawn({"UII_MODULE_ID": "fm-0001", "UII_ANALYTE": "NH4"}, module=True))
    procs.append(spawn({"UII_MODULE_ID": "fm-0002", "UII_ANALYTE": "PO4"}, module=True))
    # an impostor the hub should quarantine
    procs.append(spawn({"UII_MODULE_ID": "fm-evil", "UII_ANALYTE": "XXX"}, module=True))

    try:
        time.sleep(2.0)
        print("== modules ==")
        for m in api("/v1/modules")["items"]:
            print(f"  {m['id']}  {m['type']}  state={m['state']}  role={m['role']}")
        quarantined = [e for e in api("/v1/evidence?kind=identity&limit=100")["items"]
                       if e["data"].get("event") == "quarantined"]
        print(f"  quarantined: {[e['source']['module'] for e in quarantined]}")

        print("\n== calibrate fm-0001 (std 5.0 mg/L) ==")
        r = api("/v1/commands", {"module": "fm-0001", "type": "calibrate",
                                 "params": {"std_conc": 5.0}, "actor": "user:keaton"})
        st = wait_command(r["command_id"])
        print(f"  result: {st['result']['data']['status']}"
              f"  outputs={st['result']['data']['outputs']}")

        print("\n== sample fm-0001 ==")
        r = api("/v1/commands", {"module": "fm-0001", "type": "sample",
                                 "actor": "plc:demo-tag"})
        st = wait_command(r["command_id"])
        derived = st["derived"][-1]
        d = derived["data"]
        print(f"  {d['analyte']} = {d['value']} {d['unit']}"
              f"  (absorbance {d['absorbance']}, quality {derived['quality']['status']})")

        print("\n== a rejected command (validation) ==")
        bad = api("/v1/commands", {"module": "fm-0001", "type": "explode"})
        print(f"  {bad.get('type')}: {bad.get('detail')}")

        print("\n== lineage of the derived observation ==")
        lin = api(f"/v1/evidence/{derived['id']}/lineage")
        print(f"  focus: {lin['focus']['kind']} {d['analyte']}={d['value']} {d['unit']}")
        for u in lin["upstream"]:
            print(f"  ^ caused by: {u['kind']:<9} actor={u['actor']}  {json.dumps(u['data'])[:90]}")
        if lin["context_refs"].get("calibration"):
            cal = lin["context_refs"]["calibration"]
            print(f"  * calibration in force: slope={cal['data']['slope']} (id {cal['id'][:13]}…)")
        print(f"  raw_refs: {len(d['raw_refs'])} detector readings stored as evidence")

        print("\n== hash chain sanity ==")
        evs = api("/v1/evidence?limit=2000")["items"]
        ok = all(evs[i + 1]["integrity"]["prev_hash"] == evs[i]["integrity"]["hash"]
                 for i in range(len(evs) - 1))
        print(f"  {len(evs)} envelopes, chain intact: {ok}")
        print("\nDEMO OK" if ok else "\nDEMO FAILED: chain broken")
    finally:
        for p in procs:
            p.send_signal(signal.SIGTERM)


if __name__ == "__main__":
    main()
