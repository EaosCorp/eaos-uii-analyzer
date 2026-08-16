"""faceplate extension — the local screen, read-only v0.

One static HTML page at GET /faceplate (kiosk browser on the unit's panel
or any tablet on the LAN): live module cards, latest values with quality +
permitted-use, active alerts and NE107 status when the detections
extension is on, and a live evidence ticker over SSE.

Design rules it lives under (docs/ROADMAP.md 3b):
  * HTML is a client, never the contract — the page consumes /v1 + SSE
    exactly like every other client and renders nothing the API doesn't say.
  * Read-only in v0. The curated command-button row (panel actor identity +
    `panel` ingress ceiling through the one gate) is the planned next step;
    the panel is deliberately never the main interface.

Locked-mode caveat: the page fetches without a bearer token, so on a
locked hub it needs either open mode (bench) or a kiosk-side token
injection — resolved properly when the panel actor/ceiling lands.

Enable: "extensions": ["faceplate"] in hub.json -> http://<hub>:8400/faceplate
"""
import pathlib

_PAGE = (pathlib.Path(__file__).parent / "page.html").read_text(
    encoding="utf-8").encode()


def setup(hub):
    def get_faceplate(handler, m, q):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(_PAGE)))
        handler.end_headers()
        handler.wfile.write(_PAGE)

    hub.api_get_routes.append((r"/faceplate", get_faceplate))
