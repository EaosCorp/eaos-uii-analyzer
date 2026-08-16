"""exports extension — evidence bundles (spec §8.1).

One .tgz: manifest.json + evidence.jsonl (chain intact) + chain.json + a
README written for the next reader (human or agent). Honest contiguity
flag; contiguous slices re-verify offline. `uii export -o bundle.tgz`.

Hook used: one POST route (/v1/exports, binary response).
Enable: "extensions": ["exports"] in hub.json.
"""


def setup(hub):
    from .bundles import build_bundle

    def post_export(handler, m, body):
        filters = {k: v for k, v in body.items() if not k.startswith("_")}
        blob, manifest = build_bundle(hub.store, filters)
        hub.store.append("audit",
                         {"event": "evidence-exported",
                          "filters": manifest["filters"],
                          "count": manifest["count"]},
                         "urn:uii:schema:audit:0.1",
                         actor=body.get("_actor") or body.get("actor", "user:api"))
        handler.send_response(200)
        handler.send_header("Content-Type", "application/gzip")
        handler.send_header("Content-Length", str(len(blob)))
        handler.send_header("X-UII-Bundle-Count", str(manifest["count"]))
        handler.end_headers()
        handler.wfile.write(blob)

    hub.api_post_routes.append((r"/v1/exports", post_export))
