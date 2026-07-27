# eaos-uii-analyzer — Architecture

> The analyzer implementation of Eaos's Universal Instrument Interface (UII).
> Covers the chemical-analyzer product line: **LoaD** (Lab-on-a-Desk) and the
> **Jarbalyzer**, and the field NH4MOD retrofit that precedes them.
> Companion docs: `detections.md` (hub self-monitoring), `agent-interface.md`
> (exposing the system to AI agents), `../DEPLOY.md` (field cutover).

---

## 1. What this is

One hub, many instruments, one contract. The hub is a headless Linux edge
device (a Raspberry Pi today, a CM5-class product board later) that owns
identity, evidence, commands, schedules, and status for every instrument
behind it. Instruments are swappable modules: an analyzer cartridge today; a
foam-detection camera, a centrifuge, or a dosing pump tomorrow. Clients —
operator UIs, SCADA adapters, cloud systems, and AI agents — never talk to
an instrument directly; they talk to the hub's API, and everything the
system asserts is an immutable, hash-chained **evidence envelope**.

The rule that organizes everything:

> **Modules produce facts about hardware. The hub produces interpretations
> and evidence. The cloud produces insight across fleets.**

## 2. The layering: universal core vs instrument-class profiles

This is the load-bearing design decision for the product line. The interface
has two layers, and the repo is named for the second:

```
+---------------------------------------------------------------+
|  INSTRUMENT-CLASS PROFILES (what kind of thing is it?)        |
|                                                               |
|  chemical-analyzer     vision              rotating           |
|  (THIS REPO)           (foam camera,       (centrifuge,       |
|                         clarity, level)     pump, blower)     |
|  methods/timelines     frame capture       run-hours          |
|  calibration fits      inference events    vibration/current  |
|  concentrations        reference images    speed/torque       |
|  reagent tracking      scene baselines     bearing trends     |
+---------------------------------------------------------------+
|  UNIVERSAL CORE (every module, identical)                     |
|                                                               |
|  identity & adoption (HELLO -> VERIFYING -> ADOPTING ->       |
|    OPERATIONAL, quarantine, trust, roles vs serials)          |
|  evidence envelopes (hash chain, lineage, sequence, replay)   |
|  command lifecycle (command -> ack -> progress -> result)     |
|  health telemetry & DEGRADED/REMOVED supervision              |
|  detections (configurable alerts, NE107 status rollup)        |
|  scheduling attached to roles                                 |
|  the /v1 API, SSE stream, CLI, agent surface                  |
+---------------------------------------------------------------+
```

A module declares its class in its HELLO manifest (`instrument_class:
"chemical-analyzer"`). The class selects which **result interpreter** runs
on the hub and which detections make sense; nothing else changes. Concretely
in this codebase, the seam is one guard in `uii/hub/southbound.py`: results
from a chemical-analyzer flow into `uii/hub/interpret.py` (captures →
calibration fit → concentration); a vision module's results would flow into
a vision interpreter (frames → classified events) registered at the same
point. Adoption, evidence, commands, health, detections, scheduling, API,
and CLI are class-blind and shared.

What each class contributes (current thinking, only the first is built):

| | chemical-analyzer (built) | vision (planned) | rotating (planned) |
|---|---|---|---|
| Facts (module) | detector volts, serial traces, states | frames/regions, exposure metadata | vibration, current, speed, temperature |
| Interpretations (hub) | absorbance → mg/L via calibration envelope | foam/no-foam events, coverage %, drift vs reference scene | run-hours, anomaly scores, trend baselines |
| Reference records | calibration envelopes | baseline/reference images as evidence | commissioning signatures |
| Class detections | cal overdue, reagent low, bad-quality streaks | scene-obstructed, camera-moved, foam-event rate | vibration threshold, runtime service due |
| Class commands | prime, calibrate, sample | capture, set-region, re-baseline | start, stop, setpoint |

The promise this layering makes: **the day we build the foam camera, we
write a module agent (its pimod) and a hub interpreter — and adoption,
swap, evidence, alerts, scheduling, and the agent surface come for free.**

## 3. The evidence model

Every fact is an envelope: `id` (UUIDv7), per-hub gap-free `sequence`,
`kind` (closed set: observation, state, event, command, ack, progress,
result, calibration, health, identity, config, audit …), `source`
(hub/module/channel), `trace` (`causation_id` links form the evidence
graph; `actor` says who caused it), `quality`, typed `data`, and an
`integrity` block chaining SHA-256 hashes. Rules:

* If it happened, it is an envelope. There is no side channel.
* Envelopes are never edited; corrections are new envelopes that reference
  what they correct.
* One store (`uii/hub/evidence.py`, SQLite WAL); every API view
  (`/v1/observations`, `/v1/commands`, `/v1/alerts` history …) is a query
  over it. `GET /v1/evidence/{id}/lineage` answers "can I trust this
  number" in one call; the whole chain re-verifies from the API (the demo
  does this every run).

## 4. Hub service decomposition

Each box is a service; the evidence core is the spine. Services communicate
through it (two exceptions: command gateway → southbound dispatch, and the
API's release call → southbound session control).

```
              WAN / operators / agents          OT (PLC/SCADA)      module LAN
                        |                            |                  |
              +---------+----------+       +---------+-----+   +--------+--------+
              |  API service       |       | OT adapter    |   | southbound      |
              |  /v1 REST + SSE    |       | (planned:     |   | gateway         |
              |  CLI, agent surface|       | tag map from  |   | sessions,       |
              +---------+----------+       | manifest)     |   | adoption FSM    |
                        |                  +-------+-------+   +--------+--------+
                        v                          |                    |
              +---------------------------------------------------------------+
              |                     EVIDENCE CORE                             |
              |   id+sequence · context enrichment · hash chain · SQLite WAL  |
              |   queries · lineage · fan-out bus                             |
              +----+-----------+------------+--------------+-----------------+
                   ^           ^            ^              ^
              +----+----+ +----+-----+ +----+------+ +-----+------+
              | command | | scheduler| | detections| | interpret  |
              | gateway | | (roles)  | | (alerts,  | | (class     |
              | validate| |          | |  NE107)   | |  profiles) |
              +---------+ +----------+ +-----------+ +------------+
```

* **Command gateway** (`commands.py`): every command from any actor is
  validated against the module's declared manifest before a module sees it;
  rejections are instant, machine-readable, and audited. One `result` per
  command, always, even on rejection or module loss.
* **Scheduler** (`scheduler.py`): cadence attaches to **roles**, not
  serials, so schedules survive module swaps. Order per role: take control
  (if the role says so) → calibration gate (never sample uncalibrated) →
  samples on interval.
* **Detections** (`detections.py`): the hub's own status engine; see
  `detections.md`.
* **Interpret** (`interpret.py`): the chemical-analyzer profile. Raw
  captures become calibration envelopes and concentrations on the hub so
  every derived value is recomputable and carries lineage.

## 5. Module lifecycle: recognize, adopt, swap

Two identities, deliberately separated:

* **Serial** (permanent, travels with hardware): history follows it — every
  identity, calibration, health, and event envelope ever notarized.
* **Role** (permanent, belongs to the installation, keyed to a slot): owns
  the analyte, schedule, cal policy, alarm limits. Restored onto whatever
  compatible module occupies the slot.

```
 module dials hub, HELLO (identity, class, slot, mini-manifest)
   -> VERIFYING   type allowlist OR serial previously released from
   |              quarantine (trust persists; recognized on sight)
   +-- fail --> QUARANTINED  powered, logged, mute; release is one
   |                         audited call, then redial -> adopted
   -> ADOPTING   role config pushed in HELLO_OK, module answers ROLE_OK
   -> OPERATIONAL  scheduler resumes the role, detections watch it
        |  missed health -> DEGRADED (flagged, no commands) -> recovers
        |  link down / BYE -> REMOVED, role-vacant event, stale-data
           detection fires until the role produces again
```

Every transition is an identity envelope; the swap history *is* the log.
The swap drill (unplug A, plug B, zero keyboard, good data in minutes) is
the exit demo for the whole design and runs in `demo.py` and `tests/`.

## 6. Compute allocation

| Function | Runs on | Why |
|---|---|---|
| Actuation, safety interlocks, method step timing | module (MCU/Pi) | must survive hub loss; hard real-time |
| Raw signal production w/ local sequence numbers | module | the module asserts facts about its own hardware; that is its whole epistemic job |
| Short telemetry buffering | module (ring buffer) | rides out hub restarts |
| Raw → engineering values (class interpreters) | hub | calibration/method are versioned evidence; values stay recomputable |
| Identity, adoption, config restore | hub | needs registry, trust, history |
| Evidence notarization, hash chain | hub | one clock, one ordering |
| Command validation, audit | hub | single chokepoint = single audit trail |
| Detections / status rollup | hub | needs cross-signal context and history; see `detections.md` |
| Fleet analytics, cross-site models, agent reasoning | cloud / Eddy | not the hub's job |

## 7. The module agent (pimod) and the simulation rule

`uii/pimod/` is the field agent for the NH4MOD retrofit: port A is the PLC,
port B the pump controller, ADS1115 the detector. **BRIDGE mode** forwards
PLC↔device serial untouched (plant authority; every line becomes evidence);
**ENDPOINT mode** (latched, exactly like the field code) executes the ST9
method timelines ported verbatim from the deployed gateway.

The simulation rule: `UII_SIM=1` swaps only the hardware layer
(`uii/pimod/hw.py`) for a simulator with hidden-truth detector physics; the
agent code is byte-identical. The hub cannot tell fake from real, which is
what makes the software bench trustworthy: anything proven against sim
modules ships to the integration bench as a git tag, never as edits-on-box.

## 8. Southbound protocol

JSON Lines over one module-initiated TCP connection (CBOR planned at the
protocol-hardening stage; message shapes will not change):

```
module -> hub : HELLO, ROLE_OK, TELEM, ACK, PROGRESS, RESULT, PONG, BYE
hub -> module : HELLO_OK (carries role config), QUARANTINE,
                QUARANTINE_RELEASED, CMD, PING
```

A module is compliant if it can: announce identity and class, describe its
channels and commands, execute commands with ack/result semantics, and emit
telemetry with local sequence numbers. That bar is deliberately reachable
for an MCU — or a camera SoC, or a VFD gateway.

## 9. Detections (summary)

Configurable rules in `hub.json` are evaluated continuously over the
evidence stream; alerts come back out as evidence, acknowledge is audited,
and each module's active alerts roll up to one NAMUR NE107-style status
(`ok / check_function / maintenance_required / out_of_specification /
failure`) shown on `/v1/modules`, `/v1/health`, and destined for the OT
status word. Full design, research grounding, config reference, and roadmap
(Westgard QC, drift, flood controls): **`detections.md`**.

## 10. Agent surface (summary)

Agents are first-class actors: same API, same command gateway, same audit
(`trace.actor: "agent:…"`). The CLI is the current agent surface (every
verb has `--json`, exit codes are meaningful, help text is the contract);
an MCP server and a /proc-style filesystem projection of the evidence log
are the designed next steps. Full rationale and roadmap:
**`agent-interface.md`**.

## 11. Authority and zones

**Enforced in the command gateway** (`uii/hub/authority.py`,
`uii/hub/commands.py`): effective authority is
**min(actor class, ingress path ceiling)**, both site-configurable in
`hub.json`, neither agent-specific — the same matrix protects against a
fat-fingered human, a confused scheduler, and an over-eager agent
identically.

| actor class | may run alone (default) | above that |
|---|---|---|
| `user:*` (human) | routine + disruptive | approval by *another* human |
| `agent:*` | routine | approval by a human |
| `system:*` (scheduler) | routine + disruptive — hub.json *is* its standing approval (an engineer wrote `auto_calibrate`) | approval |
| `plc:*` | routine | approval |
| anything | `hazardous` **always** requires a human approval envelope — structural, not config | |

Per-ingress ceilings are **absolute** (approval cannot launder them):
`local` up to hazardous · `cloud` up to disruptive · `ot` and `cellular`
routine only (a remote service path can never hold plant authority,
regardless of credential). Today every API request is stamped `local`; the
listener for each future path (OT tags, northbound, cellular) stamps its
own, and the ceilings take effect with zero gateway changes.

The approval flow is evidence end to end: `audit{approval-pending}` →
`audit{approval-granted|denied}` (approver must be human, never the
requester) → dispatch or terminal `result{rejected|expired}`. Pending
approvals live at `/v1/approvals`; `uii approve <id>` is one call.
Idempotency keys on submission make agent retries safe (a retried command
returns the original ack instead of running twice).

Zones, unchanged: the module LAN is private and unrouted; modules never
listen, never reach the WAN, and only ever see pre-validated commands. OT
integration (planned adapter) is a projection; the hub never closes plant
control loops; detections are advisory evidence, not interlocks; safety
lives on the module MCU and works with the cable unplugged.

## 12. Promotion pipeline

Three rings: **0** software bench (sim modules, every commit) → **1**
integration bench (real chemistry, tagged releases only, auto-rollback) →
**2** field (signed bundles). The only thing that differs between rings is
the site config overlay. A chemistry bug found on ring 1 exports as an
evidence bundle and replays against sim modules on ring 0; the integration
bench is the verification venue, never the debugging venue.

## 13. Roadmap and non-goals

Near-term: CBOR framing · challenge-response module identity (secure
element) · MQTT northbound publisher (store-and-forward from the log) · OT
adapter with NE107 status word · retention policy · signed updates ·
Westgard QC detections · per-user roles on top of actor classes (viewer/
operator/maintainer/engineer, spec §10) once real authentication lands.

Non-goals, permanently: per-module web servers; HTML as contract; raw
pin-level control surfaces; cloud dependency for local function; a second
telemetry path outside the evidence log; the hub acting as a safety system.
