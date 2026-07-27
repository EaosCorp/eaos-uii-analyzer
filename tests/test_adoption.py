"""Adoption FSM — recognize, restore, quarantine, release, trust-on-sight."""
import unittest

from .helpers import Bench, get, post, wait_for


class TestAdoption(unittest.TestCase):
    def setUp(self):
        self.b = Bench()

    def tearDown(self):
        self.b.close()

    def test_adopt_restores_role_config(self):
        self.b.spawn("mod-a", "slot-1")
        wait_for(lambda: self.b.state_of("mod-a") == "OPERATIONAL",
                 what="adoption")
        m = self.b.module("mod-a")
        self.assertEqual(m["role"], "nh4-influent")
        self.assertEqual(m["slot"], "slot-1")
        # identity trail: hello -> adopting -> adopted
        events = [e["data"]["event"] for e in
                  get(self.b.base, "/v1/evidence?kind=identity&module=mod-a")["items"]]
        self.assertEqual(events[:3], ["hello", "adopting", "adopted"])
        # role registry shows occupancy
        roles = {r["role"]: r for r in get(self.b.base, "/v1/roles")["items"]}
        self.assertEqual(roles["nh4-influent"]["occupied_by"], "mod-a")

    def test_unknown_type_quarantined_then_released_then_trusted(self):
        self.b.spawn("intruder", "slot-7", module_type="vendor-x")
        wait_for(lambda: self.b.state_of("intruder") == "QUARANTINED",
                 what="quarantine")
        # quarantined = powered, logged, mute: no commands reach it
        code, resp = post(self.b.base, "/v1/commands",
                          {"module": "intruder", "type": "sample"})
        self.assertEqual(code, 422)

        code, resp = post(self.b.base, "/v1/modules/intruder/release", {})
        self.assertEqual(code, 200)
        self.assertEqual(resp["serial_trusted"], "SN-INTRUDER")
        wait_for(lambda: self.b.state_of("intruder") == "OPERATIONAL",
                 what="re-adoption after release")

        # trust survives unplug/replug: kill and respawn — adopted directly,
        # never quarantined again
        self.b.procs[0].kill()
        wait_for(lambda: self.b.state_of("intruder") == "REMOVED",
                 what="removal")
        self.b.spawn("intruder", "slot-7", module_type="vendor-x")
        wait_for(lambda: self.b.state_of("intruder") == "OPERATIONAL",
                 what="trusted re-adoption")
        events = [e["data"]["event"] for e in
                  get(self.b.base, "/v1/evidence?kind=identity&module=intruder&limit=500")["items"]]
        self.assertEqual(events.count("quarantined"), 1)

    def test_release_unknown_module_404(self):
        code, _ = post(self.b.base, "/v1/modules/nobody/release", {})
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
