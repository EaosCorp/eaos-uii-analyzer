# BENCH.md — a hands-on session (~15 minutes, no hardware)

`demo.py` proves the system to itself. This is the version where **you**
drive: three terminals, plain Python 3.10+, nothing to install. Every
behavior below is also covered by the test suite
(`python3 -m unittest discover -t . -s tests`) — this walkthrough is for
getting a feel, and for trying to break it.

Throughout, `uii` means `python3 -m uii.cli` (alias it if you like).

## 0. Setup — a hub with one named slot

```bash
mkdir -p config && cat > config/hub.json <<'EOF'
{
  "hub_id": "hub-bench",
  "allowed_types": ["refmod-nh4"],
  "roles": {"slot-1": {"role": "nh4-influent", "analyte": "NH4"}}
}
EOF
python3 -m uii.hub.main                    # terminal A — leave running
```

## 1. Plug in an instrument

```bash
python3 -m uii.refmod                      # terminal B — leave running
```

In terminal C:

```bash
uii modules
```

The module was verified against the allowlist, handed the slot-1 role, and
is OPERATIONAL — nothing was configured on the module itself. Check what
just happened, as evidence:

```bash
uii evidence --kind identity               # hello -> adopting -> adopted
```

## 2. Discover the exposed command surface

```bash
uii commands refmod-01
```

Every command with its params, risk class, preconditions, and typical
duration. Clients (and agents) build behavior from this, not from
documentation.

## 3. Try to break the gate

```bash
uii cmd calibrate --module refmod-01                          # missing required param
uii cmd calibrate --module refmod-01 --param std_conc=five    # wrong type
uii cmd calibrate --module refmod-01 --param stdconc=5        # typo'd param name
uii cmd sample --module refmod-01 & uii cmd sample --module refmod-01
                                            # second one: precondition-failed
```

Each rejection is instant, machine-readable, and audited
(`uii evidence --kind audit`) — nothing invalid ever reached the module.

## 4. Run the chemistry

```bash
uii cmd calibrate --module refmod-01 --param std_conc=5.0 --watch
uii cal                                    # the fit — hub recovered ~25.0,
                                           # the module's hidden true slope
uii cmd sample --module refmod-01 --watch  # mg/L, quality, permitted_use
uii obs
```

Note `permitted_use` on the result: before you calibrated, a sample would
have carried `"none"` — a value unfit for automated action says so on its
face.

## 5. Ask "why does this number look like this"

Take the observation's evidence id from the `--watch` output (or
`uii --json obs`):

```bash
uii lineage <evidence-id>
```

Observation ← result ← command, with the calibration and the raw detector
voltages one hop away. Six months from now this call answers the audit
question.

## 6. Unplug it / swap it

Ctrl-C terminal B, then:

```bash
uii modules                                # REMOVED; role-vacant in evidence
UII_MODULE_ID=refmod-02 UII_SERIAL=SN-SPARE python3 -m uii.refmod   # the spare
uii modules                                # adopted into nh4-influent
uii cmd sample --module refmod-02 --watch  # permitted_use "none" — the spare
                                           # has no calibration of its own
uii history refmod-01                      # the old unit's full record,
                                           # still queryable (refurb story)
```

Config followed the slot; history followed the serial.

## 7. An impostor

```bash
UII_TYPE=vendor-x UII_MODULE_ID=intruder python3 -m uii.refmod
uii modules                                # QUARANTINED: powered, logged, mute
uii release intruder                       # one audited call
uii modules                                # adopted — and its serial is now
                                           # trusted on sight (data/trust.json)
```

## 8. Kill the hub mid-run

Start a slow sample, then Ctrl-C the hub (terminal A) while it runs:

```bash
# terminal B (replace the module): UII_SPEED=4 python3 -m uii.refmod
uii cmd sample --module refmod-01          # ~6 s run; now kill the hub
python3 -m uii.hub.main                    # bring it back
uii cmd  # no-op; then check the command id from before:
uii --json evidence --kind result --limit 3
```

The run finished on the module while the hub was dead; the buffered
messages landed after restart, the interpretation still happened, and the
outage never got misrecorded as a module failure.

## 9. Prove nothing was edited

```bash
python3 demo.py          # ends by re-verifying the entire hash chain
```

or hit the API directly: `curl -s localhost:8400/.well-known/uii`.

---

That's the whole core surface. When you want more — hands-off scheduling,
alerting with NE107 status, agent approval controls, evidence export
bundles, and the real NH4MOD field agent — it's all on the `staged`
branch, each piece one config flag away (`docs/ROADMAP.md`).
