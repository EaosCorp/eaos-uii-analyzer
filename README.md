# uii — Universal Instrument Interface (scaffold)

Working code for the AI-ready sensor platform: the **hub** (evidence core,
southbound gateway, command gateway, HTTP+SSE API) and the **fake module**
(indistinguishable from a real analyzer to the hub). This is ring 0 of the
promotion pipeline — the software bench that needs no chemistry.

Spec + architecture: `eaos-vault/06-technical/architecture/instrument-interface/`
(`uii-spec.md`, `uii-reference-architecture.md`, `migration-playbook.md`).

## Run it

```bash
python3 demo.py          # hub + 2 fake modules + impostor + calibrate + sample + lineage
```

Or by hand:

```bash
python3 -m uii.hub.main &                                  # api :8400, southbound :7300
UII_MODULE_ID=fm-0001 python3 -m uii.fakemod.main &        # dials the hub, gets adopted
curl -s localhost:8400/v1/modules | python3 -m json.tool
curl -s -X POST localhost:8400/v1/commands \
  -d '{"module":"fm-0001","type":"calibrate","params":{"std_conc":5.0}}'
curl -s -X POST localhost:8400/v1/commands -d '{"module":"fm-0001","type":"sample"}'
curl -s localhost:8400/v1/observations/latest | python3 -m json.tool
curl -N 'localhost:8400/v1/events?kind=observation,result'   # live SSE stream
```

Stdlib only — no dependencies, runs on any Python 3.10+ (a Pi included).

## Layout

```
uii/protocol.py        message framing (JSON Lines v0; CBOR replaces at Stage 3)
uii/hub/evidence.py    the spine: SQLite WAL, seq, hash chain, queries, lineage, fan-out
uii/hub/southbound.py  module sessions, adoption FSM (VERIFYING→OPERATIONAL/QUARANTINED),
                       hub-side interpretation (absorbance, calibration fit, derived values)
uii/hub/commands.py    command gateway: validation before any module sees a command
uii/hub/api.py         /v1 REST + SSE (spec subset)
uii/fakemod/main.py    fake analyzer: HELLO/manifest, timelines modeled on the real
                       NH4MOD ST9 tables, synthetic detector physics, ring buffer + redial
demo.py                end-to-end proof, exits DEMO OK
tools/legacy-shim.md   mapping from the current NH4MOD serial/MQTT gateway to this protocol
```

## Design rules carried from the spec

- Modules produce **facts** (raw volts, states); the hub produces **interpretations**
  (absorbance → concentration via a calibration *envelope*), so every derived value
  is recomputable and carries `calibration_id` + `raw_refs` lineage.
- Modules **dial the hub** and hold one TCP connection; no listening ports on modules.
- Unknown module types are **quarantined**: powered, logged, mute.
- Every fact is a hash-chained envelope; `GET /v1/evidence/{id}/lineage` answers
  "can I trust this number" in one call.

## Not yet here (tracked in the playbook)

CBOR framing · secure-element challenge-response (v0 trusts the type allowlist) ·
role-by-switch-port (v0 assigns role-by-type) · risk classes/RBAC · MQTT northbound ·
OT adapter · retention · signed updates.
