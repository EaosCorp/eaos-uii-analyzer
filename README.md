# eaos-embedded

**Instrument-side and edge software — the embedded family.** One repository, three pieces that
share a contract and a hub, plus a set of hub extensions:

```
eaos-embedded/
├── uii/               the Universal Instrument Interface — the CORE: protocol · hub · cli · refmod (reference module)
├── uii_analyzer/      the chemical-analyzer piece: the NH4MOD retrofit field agent (pimod), hardware, interpretation
│   └── docs/            DEPLOY runbook
├── uii_vision/        the vision piece: camera module (campod), foam/level CV, JPEG frames, edge API, commissioning
│   └── docs/            vision profile · commissioning guide
├── extensions/        HUB extensions, enabled by config flag: authority · detections · exports · faceplate · scheduler
├── docs/              architecture · agent interface · detections · roadmap
├── tests/             core/ · test_analyzer · test_vision · test_<extension>   (python3 -m pytest tests, 71 tests)
└── demo.py · demo_full.py · BENCH.md
```

A piece is a top-level package (`uii_analyzer`, `uii_vision`) the hub loads by name from its
`extensions` config the same way it loads a hub extension; pieces are instrument classes with
their own hardware and lifecycle, extensions are hub features. Split into pieces 2026-08-16
(was `eaos-uii-analyzer` with the vision work on a branch); canon: `eaos-architecture/01-components/
registry.md` (the embedded layer — owns instrument-side interfaces and field agents; must not own
the record or a facility's truth).

---

# UII — the contract

**UII — the Universal Instrument Interface.** One contract between field
instruments and a hub, plus a working chemical-analyzer implementation of
it (the NH4MOD retrofit today; LoaD and the Jarbalyzer next).

```
a module dials in and is RECOGNIZED        (adopted into its slot's role,
                                            or quarantined if unknown)
every fact it produces becomes EVIDENCE    (one immutable, hash-chained
                                            record; nothing is a bare number)
every command passes ONE GATE              (validated against the module's
                                            own declaration; exactly one result)
```

## The contract

A module is any device that can hold one outbound TCP connection and
speak nine JSON-line messages (`uii/protocol.py`):

```
module -> hub : HELLO, ROLE_OK, TELEM, ACK, PROGRESS, RESULT, PONG, BYE
hub -> module : HELLO_OK, QUARANTINE, QUARANTINE_RELEASED, CMD, PING
```

**A module must:**

1. announce identity (type, serial, slot) and a manifest: its
   `instrument_class`, channels, and commands — each command with its
   param schema, risk class, and preconditions
2. produce raw facts only (volts, states, health), sequence-numbered —
   never derived values
3. execute commands with ack/progress/result semantics
4. buffer through hub outages and redial

**The hub guarantees:**

1. **Recognition** — a verified module gets its slot's role configuration
   pushed to it; swapping hardware is plumbing work (config follows the
   slot, history follows the serial). Unknown hardware is powered,
   logged, and mute until released — then trusted on sight.
2. **Evidence** — every fact, command, and transition is one hash-chained
   envelope with causal lineage. No side channel; the chain re-verifies
   from the API.
3. **One gate** — commands are validated against the module's own
   manifest (params, preconditions, policy) before the module sees them,
   and always terminate in exactly one result.
4. **Interpretation** — raw signals become engineering values on the hub,
   each carrying its calibration, raw references, and a machine-readable
   permitted-use designation (`control` / `reporting` / `none`).

Clients — CLIs, screens, agents, SCADA adapters — consume one HTTP + SSE
surface (`/v1`). Nothing talks to a module directly. The contract is
class-agnostic: an analyzer, a camera, and a centrifuge differ only in
their manifest and the interpreter the hub runs on their results.

## Run it (Python 3.10+, stdlib only, nothing to install)

```bash
python3 demo.py                               # the contract, live, ~20 s
python3 -m unittest discover -t . -s tests    # the core suite, ~25 s
```

Hands-on instead: **`BENCH.md`** — plug in, discover commands, try to
break the gate, calibrate and sample, swap a unit, kill the hub mid-run.

## Read it (~10 minutes, in this order)

| # | File | What it settles |
|---|---|---|
| 1 | `uii/protocol.py` (49) | the wire and the adoption handshake |
| 2 | `uii/hub/evidence.py` (~200) | the hash-chained envelope store; lineage |
| 3 | `uii/hub/southbound.py` (~350) | recognize and adopt; quarantine and release |
| 4 | `uii/hub/commands.py` (~250) | the one gate; one result per command |
| 5 | `uii/hub/interpret.py` (~200) | facts → mg/L with lineage and permitted-use |
| 6 | `uii/refmod.py` (~270) | the smallest honest module — start here to build a new instrument class |

Supporting: `config.py` (roles, trust), `api.py` (/v1 + SSE), `main.py`
(wiring + extension loader), `cli.py` (every verb has `--json`).

## Branches

`main` = the contract and nothing else. `staged` = `main` plus tested,
config-flag extensions: the real NH4MOD field agent, scheduling,
detections/NE107 alerts, authority + approvals, evidence export, and a
local faceplate — see `docs/ROADMAP.md` for what each adds and the hook
it enters through.

## More

`docs/architecture.md` (system design) · `docs/ROADMAP.md` (staged +
planned) · `AGENTS.md` (agent contract) · `tools/legacy-shim.md` (mapping
from the legacy MQTT gateway) · `DEPLOY.md` on staged (field cutover).
