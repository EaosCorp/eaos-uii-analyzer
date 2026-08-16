"""analyzer extension — the full field chemical-analyzer profile.

What the core doesn't need but the field does:
  * NOX three-channel interpretation (NOX / NO2 / NO3 = NOX - NO2, with
    the physical-validity rules from the deployed gateway) on top of the
    core's NH4/PO4 two-point math
  * the real method timelines (ST9 tables, all analytes) — timelines.py
  * real hardware: split-PLC serial bridge + ADS1115 — hw.py
  * pimod, the module agent for the actual NH4MOD Raspberry Pi
    (python3 -m uii_analyzer.pimod; UII_SIM=1 runs it anywhere)

Hook used: hub.southbound.interpreters["chemical-analyzer"] — replaces the
core's NH4/PO4-only interpreter with the multi-analyte version. Everything
else (adoption, evidence, commands, API) is reused unchanged.

Enable: "extensions": ["analyzer"] in hub.json.
"""
from uii.hub.interpret import make_analyzer_interpreter

from .interpret_full import (calibration_complete_full, channels_for_full,
                             fit_calibration_full, interpret_sample_full)


def setup(hub):
    hub.southbound.interpreters["chemical-analyzer"] = make_analyzer_interpreter(
        fit_calibration=fit_calibration_full,
        calibration_complete=calibration_complete_full,
        interpret_sample=interpret_sample_full,
        channels_for=channels_for_full)
