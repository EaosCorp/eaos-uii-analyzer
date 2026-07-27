# eaos-uii-analyzer

Eaos's **Universal Instrument Interface (UII)** and its chemical-analyzer
product line: **LoaD (Lab-on-a-Desk)**, the **Jarbalyzer**, and the field
NH4MOD retrofit that precedes them.

## The one idea

```
a module dials in and is RECOGNIZED        (adopted into its slot's role,
                                            or quarantined if unknown)
every fact it produces becomes EVIDENCE    (one immutable, hash-chained
                                            record; nothing is a bare number)
every command passes ONE GATE              (validated, audited, and always
                                            answered by exactly one result)
```

That contract is the **core** of this repo: ~1,200 lines, six files, one
20-second demo. It is the seam every instrument must speak — the same
contract will carry vision modules (foam-detection cameras) and rotating
equipment (centrifuges, pumps); a module declares its `instrument_class`
and the core doesn't care what it measures.

Everything else we've built — scheduling, alerting, authority, exports,
the real field agent — is **staged as extensions**: in-repo, fully tested,
one config flag away, and each one demonstrates an extension hook you
could use for something else. Nothing here is speculative scaffolding;
it's working code parked one layer up so the core stays reviewable.

## Branches

- **`main`** (you are probably here) — the core: the read order below, the
  core tests, the 20-second demo.
- **`staged`** — `main` plus the five extensions, their tests,
  `demo_full.py`, and the field deploy pack (`DEPLOY.md`, systemd units).
  Same code review rules; merges from `main` are additive-only.

## Run it (Python 3.10+, stdlib only, no install)

```bash
python3 demo.py                               # the core seam, ~20 s, DEMO OK
python3 -m unittest discover -t . -s tests    # core tests (all 56 on staged)
python3 demo_full.py                          # staged branch: everything, ~90 s
```

Or by hand:

```bash
python3 -m uii.hub.main &                     # api :8400, southbound :7300
python3 -m uii.refmod &                       # dials in, gets adopted
python3 -m uii.cli modules
python3 -m uii.cli cmd calibrate --module refmod-01 --param std_conc=5.0 --watch
python3 -m uii.cli cmd sample --module refmod-01 --watch      # prints mg/L
python3 -m uii.cli lineage <evidence-id>      # why is this number this way
```

## Read the core in this order (~10 minutes)

| # | File | What it settles |
|---|---|---|
| 1 | `uii/protocol.py` (49) | the wire: JSON-lines messages + the adoption handshake |
| 2 | `uii/hub/evidence.py` (~200) | the spine: every fact is a hash-chained envelope with causal lineage; one store, one query surface |
| 3 | `uii/hub/southbound.py` (~300) | recognize & adopt: VERIFYING → role-config push → OPERATIONAL; quarantine + one-call release with persistent trust |
| 4 | `uii/hub/commands.py` (~170) | the one gate: manifest validation, policy hook, one result per command, idempotent retries |
| 5 | `uii/hub/interpret.py` (~200) | facts → interpretations: two-point fit, mg/L with calibration lineage and a **permitted-use designation** (control / reporting / none) |
| 6 | `uii/refmod.py` (~230) | the other half of the seam: the smallest honest module. Building a camera or pump class? Start here |

Supporting cast: `uii/hub/config.py` (roles, trust, extension switchboard),
`uii/hub/api.py` (/v1 REST + SSE), `uii/hub/main.py` (wiring + the
20-line extension loader), `uii/cli.py` (every verb has `--json`).

## The extensions (staged, tested, one flag away)

Enable per site: `"extensions": ["scheduler", "detections", ...]` in
hub.json. Each package's `__init__.py` is a `setup(hub)` that wires it
through a declared hook — the extension mechanism *is* the demonstration
of how this grows. Details and sequencing: **`docs/ROADMAP.md`**.

| Extension | What it adds | Hook it demonstrates |
|---|---|---|
| `analyzer` | the full field profile: NOX 3-channel math, verbatim ST9 timelines, **pimod** (the real NH4MOD Pi agent; `UII_SIM=1` runs it anywhere) | interpreter registry per `instrument_class` |
| `scheduler` | role-attached cadence: control → cal gate → samples; survives swaps ("bam, ready") | background service + gateway client |
| `detections` | configurable alerts as evidence (hysteresis, debounce, ISA-style suppression), NAMUR NE107 status rollup, health watchdog | evidence fan-out subscription + API routes |
| `authority` | actor × risk × ingress-path enforcement, human approval flow, locked mode (identity from tokens) | gateway policy (allow / reject / **defer**) + API auth |
| `exports` | tamper-evident evidence bundles (`uii export -o bundle.tgz`) | one API route over the store |

## Docs

- **`docs/architecture.md`** — the full system design: universal core vs
  instrument-class profiles, evidence model, authority, zones
- **`docs/ROADMAP.md`** — every staged and planned piece, in order
- `docs/detections.md` · `docs/agent-interface.md` — deep design docs for
  those extensions (research grounding included)
- `AGENTS.md` — operating contract for AI agents · `DEPLOY.md` — field
  cutover runbook (real Pi, one-command rollback)
- Internal spec lineage: `eaos-vault/06-technical/architecture/instrument-interface/`

## Design rules (non-negotiable)

- Modules produce **facts**; the hub produces **interpretations**; every
  derived value is recomputable and carries its lineage.
- Modules dial the hub; no listening ports on modules; telemetry rides a
  ring buffer through hub restarts.
- Role vs serial: config follows the **slot**, history follows the
  **hardware**. Swap = plumbing work, not IT work.
- If it happened, it is an envelope. There is no side channel, and the
  chain re-verifies from the API (both demos do it every run).
- Core stays stdlib-only and instrument-class agnostic. Hardware libs
  live behind guarded imports in the analyzer extension only.
