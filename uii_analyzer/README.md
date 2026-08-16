# uii_analyzer — the chemical-analyzer piece

The NH4MOD retrofit as a UII module: `pimod.py` (the field agent that runs on the instrument's Pi and
dials in to the hub), `hw.py` (hardware), `interpret_full.py` (full result interpretation, calibration
completeness), `timelines.py`. Enable on the hub with `"extensions": ["analyzer"]`; run the agent
with `python3 -m uii_analyzer.pimod`. Deploy runbook: `docs/DEPLOY.md`. Tests: `tests/test_analyzer.py`.
