"""uii — CLI mirroring the /v1 API one-to-one (spec §9 subset). Stdlib only.

Agent contract: every verb supports --json (structured stdout, stable
field names); exit codes are meaningful (0 ok · 2 rejected · 3 failed);
errors go to stderr. Humans get tables, agents get JSON — same commands.

  uii system                          hub identity + counts
  uii modules                         every module the hub knows, all states
  uii roles                           role registry + occupancy
  uii obs [--channel nh4]             latest observations (faceplate)
  uii cal [--module m]                latest calibrations
  uii cmd TYPE --module M [--param k=v ...] [--watch]
  uii alerts                          active detections (severity, NE107)
  uii ack RULE --module M             acknowledge an active alert (audited)
  uii health                          per-module NE107 status rollup
  uii release MODULE                  release a quarantined module (logged)
  uii watch [--kind observation,event]  tail the SSE stream
  uii evidence [--kind k] [--module m] [--limit n]
  uii lineage EVIDENCE_ID             causal graph around one record

  --hub URL (default $UII_HUB_URL or http://127.0.0.1:8400)
  --json    machine output on any verb

Exit codes: 0 ok · 2 rejected · 3 failed.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return json.loads(r.read())


def _post(base: str, path: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


_JSON = False   # set by --json: agents get structure, humans get tables


def _table(rows: list[dict], cols: list[str]):
    if _JSON:
        print(json.dumps({"items": rows}, indent=2))
        return
    if not rows:
        print("(none)")
        return
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("  ".join(c.upper().ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))


def main(argv=None):
    p = argparse.ArgumentParser(prog="uii", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hub", default=None, help="hub API base URL")
    p.add_argument("--json", action="store_true",
                   help="machine-readable output (any verb)")
    sub = p.add_subparsers(dest="verb", required=True)

    sub.add_parser("system")
    sub.add_parser("modules")
    sub.add_parser("roles")
    s = sub.add_parser("obs"); s.add_argument("--channel")
    s = sub.add_parser("cal"); s.add_argument("--module")
    s = sub.add_parser("cmd")
    s.add_argument("type"); s.add_argument("--module", required=True)
    s.add_argument("--param", action="append", default=[])
    s.add_argument("--watch", action="store_true")
    sub.add_parser("alerts")
    sub.add_parser("health")
    s = sub.add_parser("ack")
    s.add_argument("rule"); s.add_argument("--module", required=True)
    s = sub.add_parser("release"); s.add_argument("module")
    s = sub.add_parser("watch"); s.add_argument("--kind")
    s = sub.add_parser("evidence")
    s.add_argument("--kind"); s.add_argument("--module")
    s.add_argument("--limit", type=int, default=25)
    s = sub.add_parser("lineage"); s.add_argument("id")

    a = p.parse_args(argv)
    global _JSON
    _JSON = a.json
    import os
    base = a.hub or os.environ.get("UII_HUB_URL", "http://127.0.0.1:8400")

    if a.verb == "system":
        print(json.dumps(_get(base, "/v1/system"), indent=2))

    elif a.verb == "modules":
        _table(_get(base, "/v1/modules")["items"],
               ["id", "type", "serial", "slot", "role", "state", "mode",
                "module_state", "last_seen_s_ago"])

    elif a.verb == "roles":
        _table(_get(base, "/v1/roles")["items"],
               ["slot", "role", "analyte", "sample_interval_s",
                "auto_take_control", "auto_calibrate", "occupied_by"])

    elif a.verb == "obs":
        items = _get(base, "/v1/observations/latest")["items"]
        if a.channel:
            items = [i for i in items if i["source"].get("channel") == a.channel]
        rows = [{"module": i["source"]["module"], "channel": i["source"]["channel"],
                 "value": i["data"].get("value"), "unit": i["data"].get("unit"),
                 "quality": (i.get("quality") or {}).get("status"),
                 "time": i["time"]} for i in items]
        _table(rows, ["module", "channel", "value", "unit", "quality", "time"])

    elif a.verb == "cal":
        path = "/v1/calibrations" + (f"?module={a.module}" if a.module else "")
        items = _get(base, path)["items"]
        if _JSON:
            print(json.dumps({"items": items}, indent=2))
        else:
            for c in items:
                print(f"{c['source']['module']}  {c['data']['analyte']}  "
                      f"std={c['data']['std_conc_mgL']}  fit={c['data']['fit']}  {c['time']}")

    elif a.verb == "cmd":
        params = {}
        for kv in a.param:
            k, _, v = kv.partition("=")
            try:
                params[k] = json.loads(v)
            except json.JSONDecodeError:
                params[k] = v
        code, resp = _post(base, "/v1/commands",
                           {"module": a.module, "type": a.type, "params": params})
        if code != 202:
            print(f"REJECTED: {resp.get('detail', resp)}", file=sys.stderr)
            return 2
        cid = resp["command_id"]
        print(f"command {cid} submitted (evidence {resp['evidence_id']})")
        if a.watch:
            import time as _t
            while True:
                st = _get(base, f"/v1/commands/{cid}")
                if st["progress"]:
                    print(f"  {st['progress'][-1].get('pct', '')}% "
                          f"{st['progress'][-1].get('message', '')}")
                if st["state"] == "done":
                    result = st["result"]["data"]
                    print(f"result: {result.get('status')}")
                    for d in st.get("derived", []):
                        print(f"  -> {d['source']['channel']} = {d['data'].get('value')} "
                              f"{d['data'].get('unit')} [{(d.get('quality') or {}).get('status')}]")
                    return 0 if result.get("status") == "succeeded" else 3
                _t.sleep(1)
        return 0

    elif a.verb == "alerts":
        _table(_get(base, "/v1/alerts")["items"],
               ["rule", "module", "severity", "ne107", "value", "acked",
                "since_s_ago", "message"])

    elif a.verb == "health":
        rows = [{"id": m["id"], "state": m["state"], "status": m["status"],
                 "alerts": ", ".join(al["rule"] for al in m["alerts"]) or "-"}
                for m in _get(base, "/v1/health")["modules"]]
        _table(rows, ["id", "state", "status", "alerts"])

    elif a.verb == "ack":
        code, resp = _post(base, "/v1/alerts/ack",
                           {"rule": a.rule, "module": a.module})
        print(json.dumps(resp, indent=2))
        return 0 if code == 200 else 3

    elif a.verb == "release":
        code, resp = _post(base, f"/v1/modules/{a.module}/release", {})
        print(json.dumps(resp, indent=2))
        return 0 if code == 200 else 3

    elif a.verb == "watch":
        qs = f"?kind={a.kind}" if a.kind else ""
        req = urllib.request.Request(base + "/v1/events" + qs,
                                     headers={"Accept": "text/event-stream"})
        with urllib.request.urlopen(req, timeout=3600) as r:
            for raw in r:
                line = raw.decode().rstrip()
                if line.startswith("data: "):
                    env = json.loads(line[6:])
                    src = env.get("source") or {}
                    print(f"{env['sequence']:>6} {env['kind']:<12} "
                          f"{src.get('module') or '-':<14} "
                          f"{src.get('channel') or '':<14} "
                          f"{json.dumps(env.get('data'))[:110]}")

    elif a.verb == "evidence":
        qs = [f"limit={a.limit}"]
        if a.kind:
            qs.append(f"kind={a.kind}")
        if a.module:
            qs.append(f"module={a.module}")
        items = _get(base, "/v1/evidence?" + "&".join(qs))["items"]
        if _JSON:
            print(json.dumps({"items": items}, indent=2))
        else:
            for env in items:
                src = env.get("source") or {}
                print(f"{env['sequence']:>6} {env['kind']:<12} "
                      f"{src.get('module') or '-':<14} {env['id']}  "
                      f"{json.dumps(env.get('data'))[:100]}")

    elif a.verb == "lineage":
        print(json.dumps(_get(base, f"/v1/evidence/{a.id}/lineage"), indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
