# Detections — the hub's own status engine

> How the hub watches itself and its instruments: configurable alerts and
> warnings, evaluated on the hub, emitted as evidence, rolled up to one
> glanceable status per module. Implementation:
> `extensions/detections/engine.py` (staged branch). Config: the
> `detections` list in `hub.json`.

---

## 1. What the field already knows (research grounding)

We did not invent an alerting model; we adopted the three bodies of practice
that already govern this problem and connected them to the evidence log.

**ISA-18.2 / IEC 62682 (process alarm management).** The process industry's
standard for alarms: a defined alarm lifecycle, severity assigned by
*rationalization* (consequence × time-to-respond, not vibes), and — the part
that separates good systems from pagers full of noise — explicit
**suppression states**: *shelved* (operator parks a nuisance alarm for a
bounded time), *out-of-service* (equipment deliberately down, alarms
suppressed by process, not discretion), and *suppressed-by-design* /
state-based alarming (a low-flow alarm is suppressed while the pump is
commanded off). State-based suppression is documented as the single most
effective nuisance-alarm reducer during startups and transitions. Its
sibling EEMUA 191 contributes the operational KPIs (alarms per operator per
hour; flood detection).

**NAMUR NE 107 (device self-diagnosis).** Instead of hundreds of
device-specific error codes, every field device presents one of four
standardized status signals: **Failure** (signal invalid), **Check
function** (deliberate intervention in progress — maintenance, calibration),
**Out of specification** (signal valid but uncertain), **Maintenance
required** (still valid, act soon). Operators and control systems already
speak this vocabulary; adopting it means our status column needs no
explanation in any plant on earth.

**Westgard multirules / Levey-Jennings (analytical QC).** Clinical and lab
chemistry solved analyzer quality control decades ago: run control
standards on a schedule, chart results against the expected mean and SD,
and judge runs with multirules (1₃ₛ, 2₂ₛ, R₄ₛ, 4₁ₛ, 10ₓ) that distinguish
random error from systematic drift — reagent aging, lamp degradation,
fouling. This is the established design for the drift question on any
chemical analyzer and is the centerpiece of the roadmap below.

**Modern observability (Prometheus lineage).** Two ideas carried over:
alerting rules are *declarative configuration over a stream*, not code; and
a rule needs a sustain duration (`for:`) so transients don't page anyone.
Our `debounce_s` is exactly that, and separate raise/clear thresholds
(hysteresis) come from SCADA deadband practice.

Sources: [exida on the ISA-18.2/IEC 62682 lifecycle](https://www.exida.com/webinars/Recordings/understanding-the-alarm-management-lifecycle-of-isa-18.2-iec-62682),
[ISA-18 series](https://www.isa.org/standards-and-publications/isa-standards/isa-18-series-of-standards),
[ISA-18.2 implementation guide](https://ifactoryapp.com/blog/alarm-management-scada-isa-18-2),
[Endress+Hauser on NE 107](https://www.endress.com/en/support-overview/learning-center/namur-ne-107),
[NE 107 overview](https://instrumentationtools.com/namur-ne107-standard/),
[Westgard multirules](https://westgard.com/westgard-rules.html),
[Levey-Jennings QC practice](https://www.diamonddiagnostics.com/blog/quality-control-for-small-labs-levey-jennings-charts-westgard-rules).

## 2. The model

```
hub.json "detections": [rules]          (declarative, per-site, versioned)
        |
        v
detections engine (thread) ── subscribes to the evidence fan-out
        |                      + periodic tick for time-driven rules
        v
per-(rule, module) state machines:
   inactive ──breach──> pending ──debounce_s──> ACTIVE ──clear──> inactive
                          |                      | ack (audited)
                          +──condition ends──────+
        |
        v
transitions emit EVIDENCE (`event` envelopes, schema urn:uii:schema:alert:0.1,
causation-linked to the envelope that triggered them)
        |
        v
GET /v1/alerts (active board) · POST /v1/alerts/ack · GET /v1/health
`uii alerts` · `uii ack` · `uii health`   +   NE107 rollup per module
```

Principles:

1. **Alerts are evidence.** A raised, cleared, or acknowledged alert is an
   envelope in the same hash-chained log as the data it is about, with
   `causation_id` pointing at the triggering observation. "Why did this
   alarm fire" is a lineage walk, six months later.
2. **Rules are configuration, not code.** A site's alarm philosophy lives
   in `hub.json`, is diffable, and travels through the promotion pipeline
   like any other config overlay.
3. **The class layering applies.** Rule *mechanics* (threshold, staleness,
   debounce, rollup) are universal-core; rule *content* is per class
   (cal-overdue is analyzer; scene-obstructed will be vision; vibration
   threshold will be rotating).
4. **Detections never actuate.** They raise status and evidence; commands
   still go through the command gateway with its own authority model, and
   safety stays on the module MCU. (An auto-*response* policy — e.g. alert
   triggers a diagnostic run — is a scheduler concern, on the roadmap,
   and still flows through the gateway as `actor: system:…`.)

### Severities and the NE107 rollup

Four severities: `info · warning · alert · critical`. Each maps to an NE107
signal by default (overridable per rule with `ne107:`):

| severity | default NE107 signal |
|---|---|
| critical | failure |
| alert | out_of_specification |
| warning | maintenance_required |
| info | (none) |

A module's **status** is the worst signal among its active alerts, with two
structural overrides: a DEGRADED/QUARANTINED/missing session is `failure`,
and a module mid-prime/mid-calibration shows `check_function` (that state is
deliberate, not a fault). Status appears on `/v1/modules`, `/v1/health`,
and `uii health`, and is what the future OT adapter will publish as the
per-module status word.

### Alarm-hygiene mechanics built in from day one

* **Hysteresis**: separate `raise_above`/`clear_below` (or the inverse) so
  a value hovering at the limit cannot chatter.
* **Debounce** (`debounce_s`): the condition must sustain before raising;
  transients are swallowed silently.
* **Suppressed-by-design** (`suppress_in_states`): rule evaluation holds
  while the module is in declared process states (priming, calibrating),
  the ISA state-based technique.
* **Anti-stale-alarm**: a staleness alert about a REMOVED module clears
  itself once another module holds the same role and is producing; the
  removal history lives in the identity log where it belongs. (Found by our
  own demo: module A's "no data" alert would otherwise stay active forever
  after a successful swap.)
* **Ack resets on re-raise**: acknowledging is audited evidence; a cleared
  alert that fires again is un-acked again.

## 3. Rule reference (v0)

Common fields: `id` (unique), `type`, `severity`, optional `ne107`
override, `message`, selectors (`module`, `role`, `channel`), and
`suppress_in_states` where noted. All windows/ages are divided by the
hub's `UII_SPEED` on compressed benches; in the field speed is 1.

```jsonc
{"id": "nh4-high", "type": "threshold", "channel": "nh4",
 "raise_above": 8.0, "clear_below": 7.0,      // or raise_below/clear_above
 "debounce_s": 0, "suppress_in_states": ["priming", "calibrating"],
 "severity": "alert", "message": "NH4 above 8 mg/L"}

{"id": "nh4-stale", "type": "stale_data", "channel": "nh4",
 "role": "nh4-influent", "window_s": 2700,    // no good obs for 45 min
 "severity": "warning", "message": "no fresh NH4 result"}

{"id": "cal-overdue", "type": "cal_overdue", "max_age_s": 604800,
 "severity": "warning", "ne107": "maintenance_required",
 "message": "calibration older than 7 days"}

{"id": "pump-serial-down", "type": "health_flag",
 "field": "port_b_ok", "equals": false, "debounce_s": 30,
 "severity": "critical", "message": "pump controller serial port down"}

{"id": "bad-quality-streak", "type": "quality_streak", "channel": "nh4",
 "count": 3, "severity": "alert",
 "message": "3 consecutive non-good results"}
```

## 4. Design decisions register

| # | Decision | Alternative rejected | Why |
|---|---|---|---|
| D1 | Detections run on the hub | on the module | needs cross-signal context, calibration history, and role knowledge; modules stay dumb about evidence |
| D2 | Alerts are `event` envelopes in the one log | separate alarm table/store | audit is structural; lineage from alert to cause is free; replay/export carries alarms with data |
| D3 | Four severities + NE107 rollup | free-form severity strings | rationalization discipline (ISA) and a vocabulary the OT world already speaks (NE107) |
| D4 | Rules in site config | rules in code | alarm philosophy is per-site, diffable, and rides the config overlay through the rings |
| D5 | Per-(rule, module) instances with role-supersede clearing | per-role instances | serial-keyed history stays honest, while a swap cannot strand a stale alarm |
| D6 | Detections never actuate | closed-loop responses | authority stays with the command gateway and the plant; hub is never a safety layer |

## 5. Roadmap (in rough order)

1. **Shelving and out-of-service** (ISA suppression states): `uii shelve
   <rule> --module M --for 8h` as audited evidence; OOS tied to role state.
2. **Flood controls**: alert-rate accounting per EEMUA-style KPIs; eclipse
   lower-severity alerts of the same cause; "first-out" grouping.
3. **Westgard QC engine** (the analyzer-class flagship): scheduler runs
   control standards on cadence; a QC detection applies Levey-Jennings
   multirules over the resulting evidence and distinguishes random error
   from systematic drift, raising `maintenance_required` with the failed
   rule (e.g. 4₁ₛ) in the alert body.
4. **Trend/drift detections**: calibration-slope trajectory across cal
   envelopes; detector `i0` (lamp/source) degradation trend; both are pure
   queries over existing evidence.
5. **Predictive consumables**: reagent depletion rate from run counts vs
   level telemetry; raise `maintenance_required` at projected-days-left.
6. **Response policies**: a detection may *request* a scheduler action
   (diagnostic run, extra QC standard) through the command gateway.
7. **Class detections for vision/rotating** as those profiles land
   (scene-obstructed, camera-moved, foam-event-rate; vibration bands,
   service-hours).
8. **Northbound routing**: severity-based publication (MQTT topics, digest
   vs page) once the stream publisher exists; the OT status word via the
   adapter.
