# uii — Universal Instrument Interface

Working code for the AI-ready sensor platform: the **hub** (evidence core,
southbound gateway, command gateway, scheduler, HTTP+SSE API, CLI) and
**pimod**, the module agent that runs on the real NH4MOD Raspberry Pi —
split-PLC serial bridge, the deployed ST9 method timelines (NH4/NOX/PO4),
and ADS1115 detector capture, speaking the UII southbound protocol. With
`UII_SIM=1` the *same* agent runs anywhere on synthetic detector physics,
so the software bench exercises exactly the code that ships.

Spec + architecture: `eaos-vault/06-technical/architecture/instrument-interface/`
(`uii-spec.md`, `uii-reference-architecture.md`, `migration-playbook.md`).
Deploying to the real Pi: **`DEPLOY.md`**.

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

## Run it

```bash
python3 demo.py            # the whole story at 300x, ~60 s, exits DEMO OK
                           # adoption -> hands-off to good data -> quarantine
                           # + release -> SWAP DRILL -> lineage + chain verify

python3 -m unittest discover -t . -s tests    # 21 tests, ~30 s
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
uii/hub/commands.py      command gateway: validation before any module sees it
uii/hub/scheduler.py     role-attached cadence: control -> cal gate -> samples
uii/hub/api.py           /v1 REST + SSE (modules, roles, release, evidence…)
uii/cli.py               `uii` CLI mirroring the API (spec §9 subset)
uii/pimod/main.py        THE MODULE AGENT for the real Pi: BRIDGE/ENDPOINT
                         split-PLC modes, timeline engine, ring buffer, redial
uii/pimod/timelines.py   ST9 tables + prime/calibrate/sample event tables,
                         ported VERBATIM from the deployed gateway
uii/pimod/hw.py          real serial/ADS1115 + simulated hardware (hidden-
                         truth detector physics)
demo.py                  end-to-end proof, exits DEMO OK
tests/                   unittest suite (interpret math, evidence chain,
                         adoption, swap drill, manual path, PLC bridge)
deploy/                  install.sh, systemd units, config examples
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

## Not yet here (tracked in the playbook)

CBOR framing · secure-element challenge-response (v0 trusts allowlist +
released serials) · role-by-switch-port (v0: module declares its slot) ·
risk classes/RBAC · MQTT northbound publisher · OT adapter (PLC result
tags) · retention · signed updates · time sync (TIME message).
