"""Method timelines — ported VERBATIM from the deployed NH4MOD gateway.

Source: serial_mqtt_gateway_nh4_nox_po4.py (A. Williamson, 2026-03), the
gateway running on the real Pi today. Three things live here, unchanged:

  1. ST9 string tables — the PLC program's string arrays, keyed by the
     exact ST9:N indices, per analyte. Each string drives the EZ-stepper
     pump controller on port B.
  2. Timeline event tables for prime / calibrate / sample — second-offset
     "send ST9:N" and "capture ADC" events, per analyte.
  3. Capture names — the DIW/STD/SAMP I0/I1 vocabulary the interpretation
     math (uii.hub.interpret) is keyed to.

Do not tune timings here without a wet-chemistry reason; these numbers ARE
the method. The timeline engine (uii.pimod.main) scales them by UII_SPEED
for bench/sim runs; on real hardware SPEED must be 1.
"""
from __future__ import annotations

VALID_ANALYTES = ("NH4", "NOX", "PO4")

# ---------------------------------------------------------------------------
# ST9 string arrays — organised by analyte.
# Keys match the ST9:N indices from the PLC program exactly.
# ---------------------------------------------------------------------------

ST9: dict[str, dict[int, str]] = {
    "NOX": {
        0:  "/1N1Y1S12A0R",
        1:  "/1S12gO4P19200O1D9600P9600M1000D19200G3gO7P19200O1D19200G2O8P9600O1D9600R",
        2:  "/1S12gO7P19200O1D9600P9600M1000D19200O8P9600O1D9600G2R",
        3:  "/1S12O7P19200O6D19200gO8P19200O6D19200G2R",
        5:  "/1S12gO3P19200O1D19200G4gO2P19200O1D19200G4gO5P19200O1D19200G4R",
        6:  "/1S12gO4P19200O1D19200G4gO7P19200O1D19200G4R",
        7:  "/1S12O7P19200O1S16D19200S12R",
        8:  "/1S12O1P15360O3P3840gO8P4800O1D4800G2gD9600P9600M1000G3R",
        9:  "/1S12O1D5760P5760M1000O8P4800O1S16D24000A0S12R",
        10: "/1S12O5P19200O1D19200R",
        11: "/1S12O1P15360O3P3840gO8P4800O1D4800G2gD9600P9600M1000G3R",
        12: "/1S12O1D5760P5760M1000O8P4800O1S16D24000A0S12R",
        13: "/1S12O4P19200O1S16D19200S12R",
        14: "/1S12O1P15360O3P3840gO8P4800O1D4800G2gD9600P9600M1000G3R",
        15: "/1S12O1D5760P5760M1000O8P4800O1S16D24000A0S12R",
        16: "/1S12O7P19200O1S16D19200S12R",
        17: "/1P19200O8P4800O6D24000O8P5280O6D5280R",
        18: "/1P5280O1D5280O6P19200O8P4800O1S16D24000S12R",
        19: "/1S12O7P4800O5P9600O7P4800O1D9600P9600M1000S16D19200S12R",
        20: "/1P19200O8P4800O6D24000O8P5280O6D5280R",
        21: "/1P5280O1D5280O6P19200O8P4800O1S16D24000S12R",
        22: "/1S12O4P19200O1D19200R",
        23: "/1P19200O8P4800O6D24000O8P5280O6D5280R",
        24: "/1S12O6P5280O1D5280O6P19200O8P4800O1S16D24000S12R",
        25: "/1S12O7P4800O4P9600O7P4800O1gD9600P9600M1000G5S16D19200S12R",
        26: "/1S12O7P7680O4P3840O7P7680O1gD9600P9600M1000G5S16D19200S12R",
        27: "/1S12gO7P19200O1D19200O8P9600O1D9600G2R",
        28: "/1S12gO5P19200O1D4800P4800M1000D19200G3R",
        29: "/1S12gO4P19200O1D19200O8P9600O1D9600G3R",
        30: "/1S12O1P11520O2P3840O3P3840O6gD9600P9600M1000G3D19200O8P5280O6D5280R",
        31: "/1S12O1P14000M3000gD14000P14000M1000G4D14000gO4P19200O1D19200G4O7P19200O1D19200R",
        32: "/1S12O7P7680O5P3840O7P7680O1D9600P9600M1000S16D19200S12R",
    },
    "NH4": {
        35: "/2N1Y1S12A0R",
        36: "/2S12gO4P19200O1D9600P9600M1000D19200G3gO7P19200O1D19200G2O8P9600O1D9600R",
        37: "/2S12gO7P19200O1D9600P9600M1000D19200O8P9600O1D9600G2R",
        38: "/2S12O7P19200O6D19200gO8P19200O6D19200G2R",
        39: "/2S12gO4P19200D19200M1000P19200O1D19200G3O8P19200O1D19200R",
        40: "/2S12gO3P19200O1D19200G4gO2P19200O1D19200G4gO5P19200O1D19200G4R",
        41: "/2S12gO4P19200O1D19200G4gO7P19200O1D19200G4R",
        42: "/2S12O7P19200O1S16D19200S12R",
        43: "/2S12P13440O2P2880gO8P4800O1D4800G2gD9600P9600M1000G6O3P2880O1gD9600P9600M1000G6R",
        44: "/2S12O6P5280O1D5280O6P19200O1S16D19200S12R",
        45: "/2S12O5P19200O1S16D19200S12R",
        46: "/2S12P13440O2P2880gO8P4800O1D4800G2gD9600P9600M1000G6O3P2880O1gD9600P9600M1000G6R",
        47: "/2S12O6P5280O1D5280O6P19200O1S16D19200S12R",
        48: "/2S12O4P19200O1S16D19200S12R",
        49: "/2S12P13440O2P2880gO8P4800O1D4800G2gD9600P9600M1000G6O3P2880O1gD9600P9600M1000G6R",
        50: "/2S12O6P5280O1D5280O6P19200O1S16D19200S12R",
        51: "/2S12O7P4800O4P9600O7P4800O1gD9600P9600M1000G5S16D19200S12R",
        52: "/2S12O7P7680O4P3840O7P7680O1gD9600P9600M1000G5S16D19200S12R",
        53: "/2S12gO7P19200O1D19200O8P9600O1D9600G2gO7P19200O1D19200O8P9600O1D9600G2R",
        54: "/2S12gO5P19200O1D19200O8P9600O1D9600G2gO7P19200O1D19200O8P9600O1D9600G2R",
        55: "/2S12gO4P19200O1D19200O8P9600O1D9600G2gO7P19200O1D19200O8P9600O1D9600G2R",
        56: "/2O6D19200O8P5280O6D5280R",
    },
    "PO4": {
        61: "/3N1Y1S12A0R",
        62: "/3S12gO4P19200O1D9600P9600M1000D19200G3gO7P19200O1D19200G2O8P9600O1D9600R",
        63: "/3S12gO7P19200O1D9600P9600M1000D19200O8P9600O1D9600G2R",
        66: "/3S12gO2P19200O1D19200G4gO5P19200O1D19200G4R",
        67: "/3S12gO3P19200O1D19200G4gO2P19200O1D19200G4gO5P19200O1D19200G4R",
        68: "/3gO7P19200O1D19200O8P9600O1D9600G2O7P19200O1S16D19200S12R",
        69: "/3P13440O2P2880gO8P4800O1D4800G2gD9600P9600M1000G6O3P2880O1gD9600P9600M1000G6R",
        70: "/3S12D9600P9600M1000gO8P4800O1D4800G2S16D19200S12R",
        71: "/3gO5P19200O1D19200O8P9600O1D9600G2O5P19200O1S16D19200S12R",
        72: "/3P13440O2P2880gO8P4800O1D4800G2gD9600P9600M1000G6O3P2880O1gD9600P9600M1000G6R",
        73: "/3S12D9600P9600M1000gO8P4800O1D4800G2S16D19200S12R",
        74: "/3S12gO4P19200O1D19200O8P9600O1D9600G2gO7P19200O1D19200O8P9600O1D9600G2R",
        75: "/3P13440O2P2880gO8P4800O1D4800G2gD9600P9600M1000G6O3P2880O1gD9600P9600M1000G6R",
        76: "/3S12D9600P9600M1000gO8P4800O1D4800G2S16D19200S12R",
        77: "/3gO4P19200O1D19200O8P9600O1D9600G2O7P9600O4P9600O1gD9600P9600M1000G2S16D19200R",
        78: "/3gO4P19200O1D19200O8P9600O1D9600G2O7P15360O4P3840O1gD9600P9600M1000G2S16D19200R",
        79: "/3S12gO4P19200O8P4800O1D24000G2O4P19200O8P4800O1D24000R",
    },
}

# ---------------------------------------------------------------------------
# Timeline event tables. Each event: {"t": seconds, "type": "send"|"adc",
# "id": ST9 index | "name": capture name}. total = run length in seconds.
# Ported 1:1 from _run_prime / _run_calibrate_* / _run_sample_*.
# ---------------------------------------------------------------------------

PRIME: dict[str, dict] = {
    "NH4": {"total": 300, "events": [
        {"t": 6,   "type": "send", "id": 35},
        {"t": 14,  "type": "send", "id": 40},
        {"t": 197, "type": "send", "id": 41},
    ]},
    "NOX": {"total": 300, "events": [
        {"t": 2,   "type": "send", "id": 0},
        {"t": 10,  "type": "send", "id": 5},
        {"t": 120, "type": "send", "id": 6},
    ]},
    "PO4": {"total": 300, "events": [
        {"t": 10,  "type": "send", "id": 61},
        {"t": 20,  "type": "send", "id": 66},
        {"t": 203, "type": "send", "id": 67},
    ]},
}

CALIBRATE: dict[str, dict] = {
    "NH4": {"total": 1500, "events": [
        {"t": 5,    "type": "send", "id": 35},
        {"t": 34,   "type": "send", "id": 53},
        {"t": 94,   "type": "send", "id": 42},
        {"t": 123,  "type": "adc",  "name": "NH4_CAL_DIW_I0"},
        {"t": 124,  "type": "send", "id": 43},
        {"t": 199,  "type": "send", "id": 56},
        {"t": 499,  "type": "send", "id": 44},
        {"t": 528,  "type": "adc",  "name": "NH4_CAL_DIW_I1"},
        {"t": 529,  "type": "send", "id": 37},
        {"t": 599,  "type": "send", "id": 38},
        {"t": 669,  "type": "send", "id": 54},
        {"t": 729,  "type": "send", "id": 45},
        {"t": 758,  "type": "adc",  "name": "NH4_CAL_STD_I0"},
        {"t": 759,  "type": "send", "id": 46},
        {"t": 834,  "type": "send", "id": 56},
        {"t": 1134, "type": "send", "id": 47},
        {"t": 1163, "type": "adc",  "name": "NH4_CAL_STD_I1"},
        {"t": 1164, "type": "send", "id": 37},
        {"t": 1234, "type": "send", "id": 38},
    ]},
    "NOX": {"total": 1650, "events": [
        {"t": 1,    "type": "send", "id": 0},
        {"t": 91,   "type": "send", "id": 27},   # from ladder logic
        {"t": 125,  "type": "send", "id": 16},
        {"t": 155,  "type": "adc",  "name": "NOX_CAL_DIW_I0"},
        {"t": 156,  "type": "send", "id": 30},
        {"t": 191,  "type": "send", "id": 27},
        {"t": 232,  "type": "send", "id": 7},
        {"t": 250,  "type": "adc",  "name": "NO2_CAL_DIW_I0"},
        {"t": 251,  "type": "send", "id": 8},
        {"t": 391,  "type": "send", "id": 9},
        {"t": 415,  "type": "adc",  "name": "NO2_CAL_DIW_I1"},
        {"t": 416,  "type": "send", "id": 27},
        {"t": 480,  "type": "send", "id": 18},
        {"t": 510,  "type": "adc",  "name": "NOX_CAL_DIW_I1"},
        {"t": 511,  "type": "send", "id": 2},
        {"t": 577,  "type": "send", "id": 3},
        {"t": 630,  "type": "send", "id": 28},
        {"t": 670,  "type": "send", "id": 19},
        {"t": 700,  "type": "adc",  "name": "NOX_CAL_STD_I0"},
        {"t": 701,  "type": "send", "id": 30},
        {"t": 736,  "type": "send", "id": 28},
        {"t": 777,  "type": "send", "id": 10},
        {"t": 795,  "type": "adc",  "name": "NO2_CAL_STD_I0"},
        {"t": 796,  "type": "send", "id": 11},
        {"t": 936,  "type": "send", "id": 12},
        {"t": 960,  "type": "adc",  "name": "NO2_CAL_STD_I1"},
        {"t": 961,  "type": "send", "id": 27},
        {"t": 1025, "type": "send", "id": 24},
        {"t": 1055, "type": "adc",  "name": "NOX_CAL_STD_I1"},
        {"t": 1056, "type": "send", "id": 2},
        {"t": 1122, "type": "send", "id": 3},
        {"t": 1161, "type": "send", "id": 32},
        {"t": 1187, "type": "adc",  "name": "NOX_CAL_STD_I0_5X"},
        {"t": 1188, "type": "send", "id": 30},
        {"t": 1223, "type": "send", "id": 28},
        {"t": 1512, "type": "send", "id": 24},
        {"t": 1542, "type": "adc",  "name": "NOX_CAL_STD_I1_5X"},
        {"t": 1543, "type": "send", "id": 2},
        {"t": 1605, "type": "send", "id": 3},
    ]},
    "PO4": {"total": 920, "events": [
        {"t": 2,    "type": "send", "id": 61},
        {"t": 5,    "type": "send", "id": 68},
        {"t": 50,   "type": "adc",  "name": "PO4_CAL_DIW_I0"},
        {"t": 51,   "type": "send", "id": 69},
        {"t": 351,  "type": "send", "id": 70},
        {"t": 440,  "type": "adc",  "name": "PO4_CAL_DIW_I1"},
        {"t": 441,  "type": "send", "id": 63},
        {"t": 490,  "type": "send", "id": 71},
        {"t": 536,  "type": "adc",  "name": "PO4_CAL_STD_I0"},
        {"t": 537,  "type": "send", "id": 72},
        {"t": 840,  "type": "send", "id": 73},
        {"t": 880,  "type": "adc",  "name": "PO4_CAL_STD_I1"},
        {"t": 881,  "type": "send", "id": 63},
    ]},
}

SAMPLE: dict[str, dict] = {
    "NH4": {"total": 865, "events": [
        {"t": 129, "type": "send", "id": 35},
        {"t": 180, "type": "send", "id": 55},
        {"t": 240, "type": "send", "id": 48},
        {"t": 289, "type": "adc",  "name": "NH4_SAMP_I0"},
        {"t": 290, "type": "send", "id": 49},
        {"t": 365, "type": "send", "id": 56},
        {"t": 665, "type": "send", "id": 50},
        {"t": 694, "type": "adc",  "name": "NH4_SAMP_I1"},
        {"t": 695, "type": "send", "id": 36},
        {"t": 765, "type": "send", "id": 38},
    ]},
    "NOX": {"total": 865, "events": [
        {"t": 125, "type": "send", "id": 0},
        {"t": 133, "type": "send", "id": 29},
        {"t": 173, "type": "send", "id": 22},
        {"t": 223, "type": "adc",  "name": "NOX_SAMP_I0"},
        {"t": 224, "type": "send", "id": 30},
        {"t": 259, "type": "send", "id": 29},
        {"t": 300, "type": "send", "id": 13},
        {"t": 318, "type": "adc",  "name": "NO2_SAMP_I0"},
        {"t": 319, "type": "send", "id": 14},
        {"t": 459, "type": "send", "id": 15},
        {"t": 483, "type": "adc",  "name": "NO2_SAMP_I1"},
        {"t": 484, "type": "send", "id": 2},
        {"t": 548, "type": "send", "id": 24},
        {"t": 578, "type": "adc",  "name": "NOX_SAMP_I1"},
        {"t": 579, "type": "send", "id": 2},
        {"t": 645, "type": "send", "id": 3},
    ]},
    "PO4": {"total": 865, "events": [
        {"t": 132, "type": "send", "id": 61},
        {"t": 162, "type": "send", "id": 74},
        {"t": 222, "type": "send", "id": 79},
        {"t": 291, "type": "adc",  "name": "PO4_SAMP_I0"},
        {"t": 292, "type": "send", "id": 75},
        {"t": 592, "type": "send", "id": 76},
        {"t": 622, "type": "adc",  "name": "PO4_SAMP_I1"},
        {"t": 623, "type": "send", "id": 62},
    ]},
}

TIMELINES = {"prime": PRIME, "calibrate": CALIBRATE, "sample": SAMPLE}


def timeline(action: str, analyte: str) -> dict:
    """Return {"total": s, "events": [...]} for an action on an analyte."""
    return TIMELINES[action][analyte]
