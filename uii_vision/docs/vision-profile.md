# The vision instrument-class profile (uii-camera-sensor)

> The camera implementation of Eaos's Universal Instrument Interface (UII).
> First observable is **foam** on aeration basins and clarifiers; the profile
> is designed to carry many observables from the same hardware. Companion docs:
> `architecture.md` (the universal core and the class layering it plugs into),
> `detections.md` (the alert engine this profile configures), `agent-interface.md`.
>
> Status: **foam-coverage profile built** at `uii_vision/` (campod +
> classical-CV interpreter + telemetry service; 4 end-to-end tests green;
> `python3 -m uii_vision.demo`). The rest of the catalog (§4) is design.
> The point of this document is to show that a camera is not a new system, it
> is a new **manifest and interpreter** on the system we already have.

---

## 1. What this is

One hub, many instruments, one contract. A foam camera is just another module
behind the hub: it dials in, is recognized into a slot's role, and every fact it
produces becomes hash-chained evidence. The architecture doc already names this
as the planned second instrument class ("vision"), next to the built
chemical-analyzer class. This document fills it in.

The organizing rule, restated for vision:

> **The module produces frames (facts about what the lens saw). The hub produces
> classifications and coverage (interpretations). The cloud produces insight
> across basins and plants.**

A camera never asserts "there is foam." It asserts "here is a frame, captured at
this time, at this exposure, with this content hash." The hub's vision
interpreter turns that into "foam covers 34% of the aeration ROI, confidence
0.9, fit for reporting." That separation is the whole design, and it is what
lets a better foam model, shipped six months later, re-derive the entire history
from the frames already in evidence.

The concrete first hardware is the bench we already have: two Reolink RLC-811A
cameras on two NVIDIA Jetson edge boxes at DC Water Blue Plains, credentials
already on each box at `/etc/eaos/camera-credentials.env`, each camera pinned to
`192.168.3.160` behind its box. Section 13 treats this as the ring-1 bench.

## 2. What comes free, and what vision adds

The load-bearing claim of the UII layering (architecture §2) is that a new class
inherits the entire universal core untouched. For vision that means:

| Comes free from the universal core | Vision changes it how |
|---|---|
| Identity and adoption (HELLO → VERIFYING → ADOPTING → OPERATIONAL, quarantine, trust, role-vs-serial) | not at all |
| Evidence envelopes (hash chain, lineage, sequence, replay) | not at all; frames are large, so §11 tiers the blob, never the envelope |
| Command lifecycle (command → ack → progress → result, one result always) | not at all |
| Health telemetry and DEGRADED/REMOVED supervision | not at all |
| Detections mechanics (threshold, stale, debounce, hysteresis, NE107 rollup) | not at all; only the rule *content* is new (§8) |
| Scheduling attached to roles | not at all; capture cadence is a schedule |
| /v1 API, SSE, CLI, agent surface, authority model | not at all |

Vision contributes exactly four things, each at a hook that already exists:

1. **A manifest** declaring `instrument_class: "vision"`, its channels, and its
   commands (§9), sent in HELLO by the module agent.
2. **A hub interpreter** registered at `hub.southbound.interpreters["vision"]`
   (the same seam `main.py` uses for `"chemical-analyzer"`): frames → observables.
3. **Reference records** as evidence: baseline scenes, ROI masks, pixel scale (§6).
4. **Class detections** in `hub.json`: foam-high, scene-obstructed, camera-moved,
   and the rest (§8).

Everything else on the box is class-blind and shared. The promise the
architecture doc made ("write a module agent and a hub interpreter, and adoption,
swap, evidence, alerts, scheduling, and the agent surface come for free") is the
whole reason to build the camera *inside* UII instead of as a standalone app.

## 3. The camera as a module, and where inference runs

The physical Reolink does not speak the UII wire; it speaks RTSP, the HTTP API,
ONVIF, and Baichuan. So the UII module is a small agent process on the edge box.
By analogy to the analyzer's `pimod`, call it **campod** (the vision module
agent). campod:

- pulls frames from the camera over RTSP and still captures over the HTTP API,
  reads controls over ONVIF/HTTP, and reads its credential from
  `/etc/eaos/camera-credentials.env` (already provisioned);
- dials the hub, sends HELLO with the vision manifest, accepts a role, and then
  emits **frames as facts** on a `frames` channel: each a content hash plus
  capture metadata (time, exposure, gain, IR state, zoom, ROI), never a
  classification;
- executes camera commands (capture, zoom, exposure, re-baseline) with
  ack/progress/result;
- buffers through hub outages and redials, exactly like `refmod`.

**Where inference runs is the one genuine vision design decision.** The UII
compute rule (architecture §6) says modules produce raw facts and the hub
produces interpretations. Foam classification is heavier than absorbance math,
and the edge box is a Jetson with a GPU, so it is tempting to classify on the
module. We do not. The split stays:

- **Module (campod):** capture, timestamp, local sequence, and *cheap* triggers
  only (frame delta, brightness, a coarse "is anything happening" gate to decide
  which frames are worth promoting). Cheap triggers are facts about pixels, which
  is the module's honest job.
- **Hub (vision interpreter):** the foam model runs here and emits observables
  with lineage and a permitted-use designation. On a single Jetson the hub and
  campod co-locate and the interpreter uses the same GPU, but they stay logically
  separate so that (a) the model is versioned evidence-producing code, (b)
  results re-derive from stored frames when the model improves, and (c) the same
  interpreter serves a hub that aggregates several cameras.

**The simulation rule carries over intact.** `UII_SIM=1` swaps only the frame
source (`hw.py`) for recorded plant footage or a synthetic-foam generator; campod
and the interpreter are byte-identical. A foam model proven against recorded
Blue Plains video ships to the bench as a git tag, never as an edit on the box.

## 4. Observables: the feature catalog

The vision analog of the analyzer's **analyte** is an **observable**: a named
quantity the hub derives over a region of interest, backed by a model, a
reference record, and thresholds, and surfaced as one hub-derived channel with a
permitted-use designation. One camera can carry several observables at once, the
way a multi-analyte analyzer carries NH4 and PO4. The slot's role config lists
which observables this viewpoint watches and their policies.

This is what "a number of features" means concretely: features are observables,
and adding one is an interpreter capability plus reference records plus detection
rules, not a new product.

| Observable | What the hub derives | Model weight | Value | Status |
|---|---|---|---|---|
| **Foam coverage** | % of ROI surface covered by foam | classical CV first | primary | design |
| **Foam type** | thin-white vs thick-brown-biological (Nocardia/Microthrix signature) | learned classifier | high (drives the operational response) | design |
| Surface scum / grease | scum coverage, grease mat presence | classical/learned | high | future |
| Liquid level / weir | staff-gauge or weir submergence read visually | classical (edge/line) | high, model-light — strong second observable | future |
| Clarity / turbidity proxy | visual clarity of clarifier effluent | classical | medium | future |
| Color / tint change | hue shift vs baseline (industrial slug, dye) | classical | medium | future |
| Sheen / oil | specular-sheen coverage | learned | medium | future |
| Solids blanket | visible sludge blanket level on a clarifier | classical/learned | medium | future |
| Asset running | is the mixer/aerator/skimmer moving (motion signature) | classical (optical flow) | medium, near-free | future |
| Position read | skimmer arm / valve indicator / gate position | classical/learned | medium | future |
| Gauge / screen OCR | read an analog gauge or a SCADA screen visually | OCR | situational (bridges un-instrumented assets) | future |
| Debris / rag at screens | rag accumulation at a bar screen | learned | medium | future |
| Scene-change anomaly | "something is different here" catch-all | classical (drift) | low but cheap | future |
| Safety / intrusion | person in a restricted area, gate open | learned | **hold**: privacy and scope; recommend off by default at a plant, opt-in only | out of scope by default |

The catalog is deliberately broad to show the profile's reach, but the build
order is narrow: foam coverage, then foam type, then one model-light observable
(level/weir is the best candidate). Everything else is a later interpreter
capability on the same evidence spine.

## 5. Foam detection in depth (the flagship observable)

**Grounding.** Activated-sludge foaming is a well-characterized failure mode, not
a novelty. Nocardia foam presents as a thick, stable, brown scum inches to feet
deep on aeration-basin and final-clarifier surfaces; Microthrix parvicella is the
other common culprit; nuisance (aeration/surfactant) foam is thinner and white.
The established field metric is the **extent of tank surface covered by foam**,
alongside qualitative foam-rating and scum-index scoring. A camera measuring
coverage and classifying foam type is a direct instrument for a metric operators
already keep by eye. (Sources at the end.)

**Channels the foam interpreter emits:**

- `foam_coverage` — percent of the ROI surface classified as foam (0–100%).
- `foam_type` — categorical: `none | nuisance_white | biological_brown | uncertain`.
  Type matters because the response differs: nuisance foam is a spray/antifoam
  problem, biological foam is an SRT/wasting and filament problem.
- `foam_confidence` — model confidence, feeds permitted-use.
- optional `foam_persistence` — how long coverage has been sustained (stable
  Nocardia mats read differently from transient churn).

**Severity is a detection, not a hard-coded threshold.** Coverage thresholds live
in `hub.json` as configurable rules (§8) so a site sets its own foam philosophy,
and the same value can be `reporting`-fit but not `control`-fit (§7).

**Trend, not root cause, on the hub.** The hub raises coverage, type, and a rising
rate. Correlating foam with SRT, F/M, or a filament count is a cross-signal
question and belongs in the cloud/Eddy layer, not on the box. The hub's job is an
honest, lineage-carrying foam number; Eddy's job is why.

## 6. Reference records, ROIs, and the swap wrinkle

Reference records are the vision analog of a calibration envelope, and like
calibrations they are evidence:

- **Baseline scene** per ROI: the clean, foam-free surface, captured and notarized
  so "drift vs reference" and "camera moved" have something to compare against.
- **ROI masks**: which pixels are the aeration surface, the weir, the clarifier.
- **Pixel scale**: optional pixel→physical mapping for level/coverage-area work.

**Role vs serial, camera edition.** As everywhere in UII, the role belongs to the
installation and the history follows the hardware:

- **Role** (belongs to the slot/viewpoint, e.g. "aeration-basin-3 surface"): owns
  the ROIs, the active observables, the baselines, the thresholds, the schedule.
- **Serial** (the physical camera): carries its identity, health, and event
  history across installations.

**The vision-specific wrinkle: ROIs are pixel-bound.** The analyzer's "config
follows the slot" is clean because chemistry is position-independent. A camera's
ROIs and baselines are tied to exact pixel coordinates, so anything that moves the
view invalidates them: a hardware swap (a new camera is aimed slightly
differently), or any pan/tilt/zoom/focus command. The profile makes this a
first-class state: after a swap or a view-moving command, the hub holds the
observables in `check_function` with reason **re-baseline required** until the
scene is re-aligned or re-baselined. That is the honest camera analog of "never
sample uncalibrated," and it is why PTZ and re-baseline are disruptive commands
(§9), not routine ones.

## 7. Interpretations and permitted-use (image-quality gating)

The universal core already carries a machine-readable **permitted-use
designation** on every derived value (`control` / `reporting` / `none`), assigned
from quality attribution, and requires automated consumers to honor it. For the
analyzer that attribution is calibration presence and interpretation outcome. For
vision it is **image quality plus model confidence plus ROI validity**, which is
the exact same idea with a different quality source.

A foam coverage number is only as trustworthy as the frame it came from, so the
interpreter gates it:

| Quality signal | Effect on permitted-use |
|---|---|
| Focus / blur below threshold | drop toward `none` |
| Exposure clipped, or dynamic range too low | drop toward `none` |
| Obstruction / lens fouling over the ROI | `none` (and raises scene-obstructed) |
| Glare / specular wash-out on the surface | drop toward `reporting`/`none` |
| Night with no IR, or IR-washed | `none` unless the model is validated for it |
| Scene drift beyond tolerance (camera moved) | `none` until re-baselined |
| Good frame, valid ROI, confidence high, model in its validated envelope | `reporting`; `control` only if the site has qualified it for closed-loop use |

The result is that a foam number derived from a rain-streaked, glare-blown frame
says `none` on its face, and the scheduler or any OT adapter that might act on it
is structurally required to ignore it. A camera value never silently graduates
from "something we logged" to "something that moved a spray valve."

## 8. Class detections (concrete rules)

Detections reuse the universal engine (`extensions/detections/`) verbatim; only
the rule content is vision-specific. All of these are declarative entries in
`hub.json`, evaluated over the evidence stream, emitted back as alert evidence,
and rolled up to one NE107 status per camera.

```jsonc
{"id": "foam-high", "type": "threshold", "channel": "foam_coverage",
 "raise_above": 30, "clear_below": 20,      // percent of ROI; hysteresis
 "debounce_s": 120, "severity": "alert",
 "message": "foam covers >30% of the aeration surface"}

{"id": "foam-rising", "type": "trend", "channel": "foam_coverage",
 "rate_above_per_h": 15, "severity": "warning",
 "message": "foam coverage rising fast"}

{"id": "scene-obstructed", "type": "threshold", "channel": "image_quality",
 "raise_below": 0.4, "debounce_s": 600, "severity": "warning",
 "ne107": "maintenance_required", "message": "lens obstructed or fouled"}

{"id": "camera-moved", "type": "scene_drift", "raise_above": 0.6,
 "severity": "alert", "ne107": "check_function",
 "message": "view moved; ROIs invalid until re-baselined"}

{"id": "foam-low-confidence", "type": "quality_streak", "channel": "foam_coverage",
 "count": 5, "severity": "warning",
 "message": "5 consecutive low-confidence foam reads"}

{"id": "frames-stale", "type": "stale_data", "channel": "frames",
 "window_s": 120, "severity": "critical", "message": "no frames for 2 min"}
```

Two of these (`scene-obstructed`, `camera-moved`) are the "device knows it cannot
be trusted right now" signals the NE107 rollup was built for: they present as
`maintenance_required` and `check_function`, the vocabulary the OT world already
reads, without a foam number ever lying.

## 9. Class commands and authority

A camera is a sensor, so nothing it does threatens the plant directly. But several
commands change *what everyone downstream measures* (they move the view or
redefine the ROI), so they are disruptive under the existing authority matrix,
not routine. Nothing new is needed in the gateway; the manifest just declares the
right risk class and preconditions and the core enforces them.

| Command | Risk | Why | Precondition |
|---|---|---|---|
| `capture` | routine | one frame, changes nothing | — |
| `set_cadence` | routine | capture schedule | — |
| `set_exposure` / `day_night` | routine | imaging only; a bad setting shows up as low image_quality, not a hazard | — |
| `zoom` / `focus` / `ptz` | **disruptive** | moves the view; invalidates every ROI and baseline | forces re-baseline state |
| `set_region` (ROI) | **disruptive** | redefines what is measured | re-baseline |
| `re_baseline` | **disruptive** | overwrites the reference the whole profile compares against | image_quality ok |
| `reboot` | disruptive | drops the stream | — |
| `factory_reset` | **hazardous** | wipes identity/config; always needs a human approval envelope | — |

The ingress ceilings from architecture §11 apply unchanged: a cloud actor can
prepare a re-baseline but a human approves it; an `ot` or `cellular` path can
never move a PTZ. The camera's module LAN stays private and unrouted, and campod,
like every module, only ever sees pre-validated commands.

## 10. The module agent (campod)

campod is `refmod` with a lens under it. Transport to the Reolink:

- **RTSP** (`.../h265Preview_01_main`, `.../h264Preview_01_sub`) for the frame
  stream;
- **HTTP API** (`/cgi-bin/api.cgi`) for still capture, exposure, day/night, and
  zoom/focus, using the token flow;
- **ONVIF** (port 8000) for native motion/event subscriptions when useful;
- **Baichuan** (port 9000) for setup and as a fallback control path;
- credential and endpoints from `/etc/eaos/camera-credentials.env`.

Two operating modes, the vision analog of the analyzer's BRIDGE/ENDPOINT:

- **PASSTHROUGH**: the camera stays owned by the plant or an existing VMS; campod
  only mirrors frames and native events into evidence. Non-invasive, good for a
  first install where nobody wants Eaos touching the camera's config.
- **MANAGED**: campod owns capture cadence, ROI crops, exposure policy, and the
  re-baseline lifecycle. This is the full profile.

campod emits: `frames` observations (hash + capture metadata), cheap trigger
features, and health (stream fps, dropped-frame count, exposure, reconnects).
It never emits foam.

## 11. Evidence and retention for frames

Frames are big, and "if it happened it is an envelope" meets storage reality here
in the one place it does anywhere in UII. The resolution keeps the rule intact:

- The **envelope always exists**: every promoted frame is an `observation`
  envelope carrying a content hash, capture metadata, and a reference to the blob.
  Derived observations (foam_coverage) causation-link back to it, so "why did
  foam-high fire" is still a lineage walk to the exact frame.
- The **blob is tiered**: reference images, event frames (anything a detection
  fired on), and periodic keyframes are kept; routine between-event frames are
  downsampled or aged out. The retention policy is config and rides the promotion
  rings like any other overlay.

This is the deliberate, documented exception to "no side channel": the pixels may
be tiered, but the evidentiary record of what was captured, when, at what quality,
and what was derived from it, never is.

## 12. Compute allocation (delta from architecture §6)

| Function | Runs on | Why |
|---|---|---|
| Frame capture, timestamp, local sequence, cheap triggers | module (campod, Jetson) | the module asserts facts about pixels |
| Foam/observable inference (raw → engineering values) | hub (vision interpreter, Jetson GPU) | model is versioned evidence; results re-derive from frames |
| Reference records, ROI/baseline lifecycle, adoption | hub | needs registry, trust, history |
| Evidence notarization, retention tiering | hub | one clock, one ordering |
| Detections, NE107 rollup | hub | needs cross-frame context and history |
| Fleet foam models, cross-basin/plant correlation, retraining | cloud / Eddy | not the hub's job; local function never depends on it |

## 13. Deployment: the Blue Plains bench

The bench already exists. Two RLC-811A cameras on two Jetson edge boxes
(`dcwa-blu-ec5350-01`, `-02`), each camera DHCP-pinned to `192.168.3.160` behind
its box, credentials on-device at `/etc/eaos/camera-credentials.env`, RTSP/ONVIF/
HTTP live and verified. Each Jetson is a natural hub-plus-campod host.

First milestone (a vision analog of the analyzer's swap-drill exit demo):

1. campod dials a hub on the box and sends HELLO with `instrument_class: "vision"`.
2. Frames become evidence on the `frames` channel; the hash chain re-verifies.
3. A foam interpreter registers at `interpreters["vision"]` and raises
   `foam_coverage` with a permitted-use designation.
4. `foam-high` and `scene-obstructed` fire as evidence and roll up to NE107.
5. The swap drill: unplug camera A, plug camera B into the slot; the role's ROIs
   and observables restore, the hub holds `check_function: re-baseline required`
   until B's view is aligned, then good foam data resumes.

Everything except steps 2–5's *content* is already built in the universal core.

## 14. Decisions (confirmed 2026-08-07)

1. **Foam model: classical first.** Color/texture segmentation for
   `foam_coverage` (no training set); a learned classifier for `foam_type`
   comes later against labeled Blue Plains footage. **Built** in `foam_cv.py`.
2. **Two data shapes, both first-class.** Streamed frames as telemetry
   (`FoamService`) plus grab-on-demand via the `capture` command. **Built.**
3. **One hub per Jetson**, co-located with campod, sharing the frame blob
   directory. **Adopted.**
4. **Person/safety observables off by default**; opt-in only, per site.
5. **Second observable = level/weir** (model-light, reuses the pixel-scale
   reference record). Planned; the seam is `foam_cv.py`.
6. **Retention budget: still open** — needs a real storage number from the
   Jetsons before setting the routine-keyframe age-out. Mechanism is in
   `blobstore.sweep()`; the policy value is TBD.

---

**Sources (foam grounding):**
[Filamentous bacteria foaming in activated sludge (MECC)](https://water.mecc.edu/courses/ENV295Micro/lesson8_3b.htm) ·
[Control of Microthrix parvicella foaming (Water Research)](https://www.sciencedirect.com/science/article/abs/pii/S0043135497003801) ·
[Nocardia foaming control in activated sludge (Bioresource Technology)](https://www.sciencedirect.com/science/article/abs/pii/S0960852407006372) ·
[Foaming estimation tests in activated sludge systems (Torregrossa, 2005)](https://onlinelibrary.wiley.com/doi/10.1002/aheh.200400578) ·
[Filamentous foaming control methods (Parklink)](https://parklink.nz/news/control-methods-for-filamentous-foaming/)
