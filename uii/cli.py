"""uii — CLI mirroring the /v1 API one-to-one (spec §9 subset). Stdlib only.

Agent contract: every verb supports --json (structured stdout, stable
field names); exit codes are meaningful (0 ok · 2 rejected · 3 failed);
errors go to stderr. Humans get tables, agents get JSON — same commands.

CORE verbs (work against any hub):

  uii system                          hub identity, auth mode, extensions
  uii modules                         every module the hub knows, all states
  uii roles                           role registry + occupancy
  uii obs [--channel nh4]             latest observations (faceplate)
  uii cal [--module m]                latest calibrations
  uii cmd TYPE --module M [--param k=v ...] [--actor a] [--watch]
  uii release MODULE                  release a quarantined module (logged)

EXTENSION verbs (need the matching extension enabled on the hub,
otherwise a clean not-found error):

  uii approvals · approve ID · deny ID          [authority] risk-gated
                                      commands await a human; approvals
                                      are audited both ways
  uii alerts · ack RULE --module M · health     [detections] active alerts
                                      + NE107 status rollup
  uii export -o bundle.tgz [--module M] [--kind k,k] [--since-seq N]
             [--since T] [--until T] [--correlation ID]
                                                [exports] evidence bundle
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


_TOKEN = None   # set by --token / $UII_TOKEN; locked hubs require it


def _headers(extra=None) -> dict:
    h = dict(extra or {})
    if _TOKEN:
        h["Authorization"] = f"Bearer {_TOKEN}"
    return h


def _get(base: str, path: str) -> dict:
    req = urllib.request.Request(base + path, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = json.loads(e.read() or b"{}").get("detail", str(e))
        print(f"error {e.code}: {detail} (extension not enabled on this hub?)"
              if e.code == 404 else f"error {e.code}: {detail}",
              file=sys.stderr)
        raise SystemExit(3)


def _post(base: str, path: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers=_headers({"Content-Type": "application/json"}), method="POST")
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
    p.add_argument("--token", default=None,
                   help="bearer token (or $UII_TOKEN) — locked hubs derive "
                        "your identity from this, not from --actor")
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
    s.add_argument("--actor", default="user:local")
    s.add_argument("--watch", action="store_true")
    sub.add_parser("approvals")
    s = sub.add_parser("approve")
    s.add_argument("id"); s.add_argument("--actor", default="user:local")
    s = sub.add_parser("deny")
    s.add_argument("id"); s.add_argument("--actor", default="user:local")
    s = sub.add_parser("export")
    s.add_argument("-o", "--out", required=True)
    s.add_argument("--module"); s.add_argument("--kind")
    s.add_argument("--since-seq", type=int)
    s.add_argument("--since"); s.add_argument("--until")
    s.add_argument("--correlation")
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
    global _JSON, _TOKEN
    _JSON = a.json
    import os
    _TOKEN = a.token or os.environ.get("UII_TOKEN")
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
                           {"module": a.module, "type": a.type,
                            "params": params, "actor": a.actor})
        if code != 202:
            print(f"REJECTED: {resp.get('detail', resp)}", file=sys.stderr)
            return 2
        cid = resp["command_id"]
        if resp.get("approval_required"):
            if _JSON:
                print(json.dumps(resp, indent=2))
            else:
                print(f"command {cid} PENDING APPROVAL "
                      f"(risk above '{a.actor}' authority)\n"
                      f"a human grants it with: uii approve {resp['approval_id']}")
            return 0
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

    elif a.verb == "approvals":
        _table(_get(base, "/v1/approvals")["items"],
               ["approval_id", "module", "type", "risk", "requested_by",
                "requested_at"])

    elif a.verb in ("approve", "deny"):
        code, resp = _post(base, f"/v1/approvals/{a.id}",
                           {"decision": a.verb, "actor": a.actor})
        print(json.dumps(resp, indent=2))
        return 0 if code == 200 else 3

    elif a.verb == "export":
        body = {"module": a.module, "kind": a.kind,
                "since_seq": a.since_seq, "since_time": a.since,
                "until_time": a.until, "correlation_id": a.correlation}
        req = urllib.request.Request(
            base + "/v1/exports",
            data=json.dumps({k: v for k, v in body.items() if v}).encode(),
            headers=_headers({"Content-Type": "application/json"}),
            method="POST")
        with urllib.request.urlopen(req, timeout=120) as r:
            blob = r.read()
            count = r.headers.get("X-UII-Bundle-Count", "?")
        with open(a.out, "wb") as f:
            f.write(blob)
        if _JSON:
            print(json.dumps({"out": a.out, "bytes": len(blob),
                              "envelopes": int(count)}))
        else:
            print(f"{a.out}: {count} envelopes, {len(blob)} bytes "
                  f"(manifest.json + evidence.jsonl + chain.json + README.md)")

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
        req = urllib.request.Request(
            base + "/v1/events" + qs,
            headers=_headers({"Accept": "text/event-stream"}))
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
