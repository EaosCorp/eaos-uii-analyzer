# eaos-uii-analyzer

The chemical-analyzer implementation of Eaos's **Universal Instrument
Interface (UII)** — the product line covering **LoaD (Lab-on-a-Desk)**, the
**Jarbalyzer**, and the field NH4MOD retrofit that precedes them.

Two layers, deliberately separated (see `docs/architecture.md` §2):

* **Universal core** — identity/adoption, evidence envelopes, command
  lifecycle, health supervision, configurable detections with NE107 status
  rollup, role-attached scheduling, `/v1` API + SSE, CLI, agent surface.
  The same core will carry vision modules (foam-detection cameras) and
  rotating equipment (centrifuges, pumps); a module declares its
  `instrument_class` and the core doesn't care.
* **Chemical-analyzer profile** (this repo's name) — method timelines,
  calibration fits, concentrations, reagent/cal detections.

In here: the **hub** (evidence core, southbound gateway, command gateway,
scheduler, detections, HTTP+SSE API, CLI) and **pimod**, the module agent
that runs on the real NH4MOD Raspberry Pi — split-PLC serial bridge, the
deployed ST9 method timelines (NH4/NOX/PO4), ADS1115 detector capture. With
`UII_SIM=1` the *same* agent runs anywhere on synthetic detector physics,
so the software bench exercises exactly the code that ships.

Docs: **`docs/architecture.md`** (system design) ·
**`docs/detections.md`** (hub self-monitoring: alerts, NE107, roadmap) ·
**`docs/agent-interface.md`** (exposure to AI agents) ·
**`AGENTS.md`** (agent operating contract) ·
**`DEPLOY.md`** (field cutover runbook).
Internal spec lineage: `eaos-vault/06-technical/architecture/instrument-interface/`.

## The headline behavior: auto-recognize → bam, ready

Plug a module in and the hub does the rest — no keyboard:

```
HELLO -> VERIFYING (type allowlist / trusted serial)
      -> ADOPTING  (the slot's ROLE CONFIG is pushed to the module, acked)
      -> OPERATIONAL
      -> scheduler: take control -> calibrate -> sampling on cadence
```

Unknown module? **QUARANTINED** — powered, logged, mute — and releasing it
is one call (`uii release <id>`), after which its serial is trusted on
sight forever. Unplug a module and its role goes vacant; plug any
compatible unit into the slot and the role is restored onto it (config
follows the **slot**, history follows the **serial**). Every transition is
an identity envelope, so the swap history *is* the evidence log.

And the hub watches itself: **detections** configured in `hub.json`
(thresholds with hysteresis + debounce, stale data, cal overdue, health
flags, quality streaks) run continuously over the evidence stream, come
back out as evidence, are acknowledgeable (`uii ack`, audited), suppressed
by design during priming/calibration, and roll up to one NAMUR NE107-style
status per module (`uii health`). Design + roadmap: `docs/detections.md`.

Authority is **enforced at the command gateway**: effective permission =
min(actor class, ingress path ceiling). Agents run `routine` alone;
`disruptive` waits on a human's `uii approve` (audited both ways);
`hazardous` always does; capped paths (cellular, OT) cannot be laundered by
approval. Idempotency keys make retries safe. And when a call needs making,
`uii export` produces the evidence bundle: manifest + envelopes + chain
proof + a README that explains itself to whoever (or whatever) reads it.

## Run it

```bash
python3 demo.py            # the whole story at 300x, ~70 s, exits DEMO OK
                           # adoption -> hands-off to good data -> NE107 ok ->
                           # quarantine + release -> SWAP DRILL (with the
                           # stale-data detection firing and self-clearing)
                           # -> lineage + hash-chain verify

python3 -m unittest discover -t . -s tests    # 49 tests, ~75 s
```

Or by hand:

```bash
python3 -m uii.hub.main &                                # api :8400, southbound :7300
UII_SIM=1 UII_SPEED=100 python3 -m uii.pimod.main &      # dials in, gets adopted
python3 -m uii.cli modules
python3 -m uii.cli cmd take_control --module nh4mod-01 --watch
python3 -m uii.cli cmd calibrate --module nh4mod-01 --param std_conc=5.0 --watch
python3 -m uii.cli cmd sample --module nh4mod-01 --watch
python3 -m uii.cli obs
python3 -m uii.cli watch                                 # live SSE tail
```

Hub + CLI are stdlib-only — any Python 3.10+, a Pi included. pimod needs
`pyserial` + `adafruit-circuitpython-ads1x15` only on real hardware.

## Layout

```
uii/protocol.py          message framing (JSON Lines v0.2; CBOR at Stage 3)
uii/hub/evidence.py      the spine: SQLite WAL, seq, hash chain, lineage, fan-out
uii/hub/config.py        role registry (slot -> role config) + runtime trust
uii/hub/southbound.py    module sessions, full adoption FSM (VERIFYING ->
                         ADOPTING -> OPERATIONAL / QUARANTINED / DEGRADED /
                         REMOVED), role-config push, quarantine release
uii/hub/interpret.py     hub-side chemistry math, ported verbatim from the
                         field gateway: two-point DIW/STD fit, conc =
                         slope*A - intercept, NOX 3-fit + NO3 validity
uii/hub/commands.py      command gateway: validation, authority enforcement,
                         approvals, idempotency — the single chokepoint
uii/hub/authority.py     min(actor class, ingress path ceiling); approval rules
uii/hub/exports.py       evidence bundles (manifest + jsonl + chain + README)
uii/hub/scheduler.py     role-attached cadence: control -> cal gate -> samples
uii/hub/detections.py    configurable status engine: alerts as evidence,
                         hysteresis/debounce/suppression, NE107 rollup
uii/hub/api.py           /v1 REST + SSE (modules, roles, alerts, health,
                         release, evidence…)
uii/cli.py               `uii` CLI mirroring the API; --json on every verb
uii/pimod/main.py        THE MODULE AGENT for the real Pi: BRIDGE/ENDPOINT
                         split-PLC modes, timeline engine, ring buffer, redial
uii/pimod/timelines.py   ST9 tables + prime/calibrate/sample event tables,
                         ported VERBATIM from the deployed gateway
uii/pimod/hw.py          real serial/ADS1115 + simulated hardware (hidden-
                         truth detector physics)
demo.py                  end-to-end proof, exits DEMO OK
tests/                   unittest suite (interpret math, evidence chain,
                         adoption, swap drill, manual path, PLC bridge,
                         detections mechanics + live alerting)
deploy/                  install.sh, systemd units, config examples
docs/                    architecture.md · detections.md · agent-interface.md
AGENTS.md                operating contract for AI agents
tools/legacy-shim.md     mapping from the legacy NH4MOD MQTT gateway
DEPLOY.md                runbook for putting this on the real Pi
```

## Design rules carried from the spec

- Modules produce **facts** (raw volts, serial traffic, states); the hub
  produces **interpretations** (absorbance → concentration via a calibration
  *envelope*), so every derived value is recomputable and carries
  `calibration_id` + `raw_refs` lineage.
- Modules **dial the hub** and hold one TCP connection; no listening ports
  on modules. Telemetry rides a ring buffer through hub restarts.
- **Role vs serial:** config keyed to the slot's role; history keyed to the
  hardware serial. Swap = plumbing work, not IT work.
- Unknown modules are **quarantined**: powered, logged, mute, one-call release.
- Every fact is a hash-chained envelope; `uii lineage <id>` answers "can I
  trust this number" in one call, and the whole chain re-verifies from the API.
- **BRIDGE mode preserves plant authority**: the PLC drives the device
  through the agent untouched (every line logged); ENDPOINT is latched
  exactly like the field gateway's latch.

## Not yet here (tracked in docs/ and the playbook)

CBOR framing · secure-element challenge-response (v0 trusts allowlist +
released serials) · role-by-switch-port (v0: module declares its slot) ·
per-user roles atop actor classes (spec §10) · MQTT northbound publisher ·
OT adapter (PLC result tags + NE107 status word) · retention · signed
updates · time sync · alarm shelving/OOS/flood controls · Westgard QC +
drift detections · vision and rotating instrument-class profiles ·
optional MCP wrapper (see docs/agent-interface.md).
