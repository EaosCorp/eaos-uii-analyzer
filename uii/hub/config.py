"""Hub configuration — role registry, trust, and the extension switchboard.

Two identities, deliberately separated (the swap story):
  * MODULE identity (serial) — permanent, travels with the hardware.
    History follows the serial.
  * ROLE — permanent, belongs to the installation (a slot). Owns the
    analyte, schedule, cal policy. Restored onto whatever compatible
    module occupies the slot.

Config file (JSON, path from UII_CONFIG, default ./config/hub.json):

{
  "hub_id": "hub-bench-001",
  "allowed_types": ["nh4mod-nh4"],
  "extensions": [],                      // e.g. ["scheduler", "detections",
                                         //       "authority", "exports",
                                         //       "analyzer"]
  "roles": {
    "slot-1": {"role": "nh4-influent", "analyte": "NH4",
               "sample_interval_s": 900, "calibrate_std_conc": 5.0}
  }
}

Extension-specific config (detections rules, authority matrix,
credentials, ...) lives in the same file; extensions read it from
`config.raw` so the core stays ignorant of their shapes.

Trust earned at runtime (quarantine release) persists in
<data_dir>/trust.json so a released serial is re-adopted on sight forever.
"""
from __future__ import annotations

import json
import os
from typing import Optional

DEFAULT_ROLE_POLICY = {
    "sample_interval_s": 900,
    "cal_max_age_s": 7 * 24 * 3600,
    "calibrate_std_conc": 5.0,
    "auto_take_control": False,
    "auto_calibrate": False,
}


class HubConfig:
    def __init__(self, path: Optional[str] = None):
        self.path = path
        raw: dict = {}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                raw = json.load(f) or {}
        self.raw = raw
        self.hub_id: str = os.environ.get("UII_HUB_ID") or raw.get("hub_id", "hub-dev-001")
        env_allowed = os.environ.get("UII_ALLOWED")
        self.allowed_types: set[str] = (
            set(env_allowed.split(",")) if env_allowed
            else set(raw.get("allowed_types",
                             ["nh4mod-nh4", "nh4mod-nox", "nh4mod-po4",
                              "refmod-nh4"])))
        env_ext = os.environ.get("UII_EXTENSIONS")
        self.extensions: list[str] = (
            env_ext.split(",") if env_ext else list(raw.get("extensions") or []))

        self.roles: dict[str, dict] = {}
        for slot, role_cfg in (raw.get("roles") or {}).items():
            cfg = dict(DEFAULT_ROLE_POLICY)
            cfg.update(role_cfg or {})
            cfg.setdefault("role", f"role-{slot}")
            self.roles[slot] = cfg

        self.data_dir: str = os.environ.get(
            "UII_DATA", raw.get("data_dir", "./data"))
        self._trust_path = os.path.join(self.data_dir, "trust.json")
        self.trusted_serials: set[str] = self._load_trust()

    # -- role lookup ---------------------------------------------------------

    def role_for(self, slot: Optional[str], module_type: Optional[str],
                 analyte: Optional[str]) -> tuple[str, dict]:
        """Slot-keyed role if declared, else a default role by type."""
        if slot and slot in self.roles:
            cfg = self.roles[slot]
            return cfg["role"], cfg
        cfg = dict(DEFAULT_ROLE_POLICY)
        cfg["role"] = f"role-{module_type or 'unknown'}"
        cfg["analyte"] = (analyte or "NH4").upper()
        return cfg["role"], cfg

    # -- runtime trust ---------------------------------------------------------

    def _load_trust(self) -> set[str]:
        try:
            with open(self._trust_path, encoding="utf-8") as f:
                return set(json.load(f).get("serials", []))
        except (FileNotFoundError, json.JSONDecodeError):
            return set()

    def trust_serial(self, serial: str) -> None:
        self.trusted_serials.add(serial)
        os.makedirs(self.data_dir, exist_ok=True)
        tmp = self._trust_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"serials": sorted(self.trusted_serials)}, f, indent=2)
        os.replace(tmp, self._trust_path)

    def is_trusted(self, module_type: Optional[str], serial: Optional[str]) -> bool:
        return (module_type in self.allowed_types
                or (serial is not None and serial in self.trusted_serials))
