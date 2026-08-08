# extensions/vision — the camera profile (foam first)

The `vision` instrument-class profile of UII. Two interfaces:

- **campod** — the module agent (runs on the edge box next to the camera).
- **the hub interpreter** — classical-CV foam coverage (registered by `setup(hub)`).

Everything else (adoption, evidence, the command gate, scheduling, the API,
authority) is the universal core, unchanged. Design: `docs/vision-profile.md`.

## Two data shapes

- **Telemetry** — campod streams a frame every `cadence_s` as a `frames`
  observation; `FoamService` turns the stream into `foam_coverage`. No command
  in the loop.
- **Grab on demand** — a `capture` command grabs one frame now and returns its
  hash; the interpreter derives `foam_coverage` tied to the command's lineage.

Both produce the same `foam_coverage` channel, each frame carrying a
permitted-use designation (`reporting` / `none` / `control`) from image quality.

## Run it on the bench (sim, ~15 s)

```bash
python3 -m extensions.vision.demo      # hub + campod in sim; prints live foam %
```

Or wire it by hand. Hub config (`hub.json`):

```json
{
  "hub_id": "hub-vision-001",
  "allowed_types": ["vision-cam"],
  "extensions": ["vision"],
  "data_dir": "./data",
  "roles": {
    "slot-1": {"role": "aeration-foam-cam", "cadence_s": 10, "control_ok": false,
               "roi": {"x0": 0.0, "y0": 0.15, "x1": 1.0, "y1": 1.0}}
  }
}
```

```bash
# terminal 1 — the hub
UII_CONFIG=hub.json python3 -m uii.hub.main
# terminal 2 — the camera agent, sim
UII_SIM=1 UII_SIM_COVERAGE=35 python3 -m extensions.vision.campod
# grab on demand
uii capture campod-01        # or: POST /v1/commands {"module":"campod-01","type":"capture"}
```

## Run it against a real Reolink (the Blue Plains edge boxes)

campod reads the on-device credential already at `/etc/eaos/camera-credentials.env`:

```bash
set -a; . /etc/eaos/camera-credentials.env; set +a
UII_FRAMES_DIR=/var/lib/eaos/frames python3 -m extensions.vision.campod
```

One hub per Jetson (co-located with campod); they share `UII_FRAMES_DIR`, so the
module writes a frame and the hub interpreter reads it back by hash.

## Commands (manifest)

| command | risk | note |
|---|---|---|
| `capture` | routine | grab one frame now; returns its hash |
| `set_cadence` | routine | change the telemetry interval |
| `set_exposure` | routine | imaging only |
| `re_baseline` | disruptive | new reference scene; clears re-baseline-required |
| `set_region` | disruptive | new ROI; forces re-baseline |
| `ptz` | disruptive | moves the view; forces re-baseline |

## Env

```
UII_HUB 127.0.0.1:7300   UII_MODULE_ID campod-01   UII_SLOT slot-1
UII_TYPE vision-cam      UII_SERIAL SN-...          UII_CADENCE_S 10
UII_FRAMES_DIR ./data/frames                        UII_SPEED 1
UII_SIM=1  UII_SIM_COVERAGE 25  UII_SIM_FOAM nuisance_white|biological_brown  UII_SIM_QUALITY 1.0
(real)  CAMERA_HOST / CAMERA_USER / CAMERA_PASSWORD  (from /etc/eaos/camera-credentials.env)
```

## Dependencies

The core is stdlib-only; this extension adds **numpy** (CV) and **Pillow**
(PNG encode/decode). `pip install numpy pillow`.

## Next observable: level/weir (template started)

`level_cv.py` is the **template** for the second observable — a working but
uncalibrated `detect()` (edge-based waterline; `level` and `weir` siblings)
that shows adding an observable is a known move: a `detect()` over a frame +
ROI, channels in the manifest, and a role_config flag. Adoption, evidence,
alerts, and the agent surface do not change. The three TODOs (pixel scale,
weir-crest datum, threshold tuning) plus the wiring checklist are in that file's
footer. It is **not wired into a live role** — finish and validate it first.
