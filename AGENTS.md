# AGENTS.md — operating contract for AI agents in this repo

This repo is **eaos-uii-analyzer**: the chemical-analyzer implementation of
Eaos's Universal Instrument Interface. Hub (evidence, adoption, commands,
scheduler, detections, API/CLI) + `pimod` (the module agent that runs on
real analyzer hardware; identical code in sim). Read
`docs/architecture.md` before changing structure; `docs/detections.md`
before touching alerting; `docs/agent-interface.md` for how live systems
are exposed to you.

## Ground rules

* **Hub + CLI are stdlib-only Python 3.10+.** Do not add dependencies.
  `pyserial`/`adafruit-*` are allowed inside `uii/pimod/hw.py` only, behind
  the existing guarded imports.
* `uii/pimod/timelines.py` is ported VERBATIM from deployed field code.
  Never tune ST9 strings or timings without a wet-chemistry reason.
* The universal core (protocol, evidence, southbound, commands, scheduler,
  detections, api, cli) must stay instrument-class agnostic. Analyzer-only
  logic lives in `uii/hub/interpret.py` and behind the `instrument_class`
  guard in `southbound.py`.
* Every fact is an evidence envelope. Never add a side channel (log file,
  cache, direct socket) that carries data the log does not.
* All commands to modules go through the command gateway. No exceptions,
  including tests.

## Verify your work

```bash
python3 -m unittest discover -t . -s tests    # full suite, ~60 s, must be OK
python3 demo.py                               # end-to-end story, exits DEMO OK
```

Both must pass before any commit. The demo checks every claim against the
API, including a full hash-chain re-verification; treat a demo failure as a
real defect, not flakiness (fix the race properly if it is one).

## Operating a live hub (bench or field)

```bash
uii --json system | modules | roles | health | alerts | obs | cal
uii --json cmd <type> --module <id> [--param k=v] --watch
uii --json ack <rule> --module <id>
uii --json release <module-id>
uii --json evidence --kind identity --module <id>
uii --json lineage <evidence-id>       # answers "why is this number this way"
```

Exit codes: 0 ok · 2 command rejected · 3 failed. `--json` on any verb.
Base URL: `--hub` or `$UII_HUB_URL` (default `http://127.0.0.1:8400`).

* You are an actor: your commands carry `trace.actor` and are audited.
* Respect manifest `risk` classes; never run `disruptive` commands
  (calibrate, take_control) on field hardware without explicit human
  instruction. `take_control` latches ENDPOINT: the PLC loses authority
  until the module agent restarts.
* Diagnose from evidence (`uii evidence`, `uii lineage`), not by
  restarting services; restarts destroy the live repro.

## Map

```
uii/protocol.py        southbound framing + adoption handshake
uii/hub/               evidence.py config.py southbound.py commands.py
                       scheduler.py detections.py interpret.py api.py main.py
uii/pimod/             main.py (agent) hw.py (real+sim) timelines.py (ST9)
uii/cli.py             the `uii` CLI (agent surface)
tests/                 unittest; helpers.py spins real hubs on ephemeral ports
deploy/ + DEPLOY.md    field install (systemd, cutover, rollback)
docs/                  architecture.md detections.md agent-interface.md
```
