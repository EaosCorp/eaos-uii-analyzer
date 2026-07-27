"""Authority: actor class × risk × ingress ceiling, and the approval flow.
Policy mechanics unit-tested; the approval lifecycle runs on a live bench."""
import unittest

from uii.hub.authority import AuthorityPolicy, actor_class

from .helpers import Bench, get, post, wait_for


class TestPolicy(unittest.TestCase):
    def setUp(self):
        self.p = AuthorityPolicy()

    def test_actor_classes(self):
        self.assertEqual(actor_class("user:ali"), "user")
        self.assertEqual(actor_class("agent:eddy-om@hrsd"), "agent")
        self.assertEqual(actor_class("system:scheduler"), "system")
        self.assertEqual(actor_class("plc:blue-north"), "plc")
        self.assertEqual(actor_class("mystery"), "agent")   # least privilege

    def test_matrix_defaults(self):
        # agents: routine alone, disruptive needs a human
        self.assertEqual(self.p.decide("agent:e", "routine"), "allow")
        self.assertEqual(self.p.decide("agent:e", "disruptive"), "approval")
        # humans and configured automation: through disruptive
        self.assertEqual(self.p.decide("user:k", "disruptive"), "allow")
        self.assertEqual(self.p.decide("system:scheduler", "disruptive"), "allow")
        # the plant: routine only
        self.assertEqual(self.p.decide("plc:x", "disruptive"), "approval")

    def test_hazardous_always_needs_approval(self):
        for actor in ("user:k", "agent:e", "system:scheduler", "plc:x"):
            self.assertEqual(self.p.decide(actor, "hazardous"), "approval")

    def test_path_ceiling_is_absolute(self):
        # cellular is capped at routine: disruptive is not even approvable
        self.assertEqual(self.p.decide("user:k", "disruptive", "cellular"),
                         "reject-path")
        self.assertEqual(self.p.decide("user:k", "routine", "cellular"), "allow")
        # OT plane likewise
        self.assertEqual(self.p.decide("plc:x", "disruptive", "ot"),
                         "reject-path")
        # local allows the full ladder (subject to actor authority)
        self.assertEqual(self.p.decide("user:k", "hazardous", "local"),
                         "approval")

    def test_site_overrides(self):
        p = AuthorityPolicy(authority={"agent": "disruptive"},
                            path_ceilings={"cloud": "routine"})
        self.assertEqual(p.decide("agent:e", "disruptive"), "allow")
        self.assertEqual(p.decide("agent:e", "disruptive", "cloud"),
                         "reject-path")

    def test_approver_rules(self):
        ok, _ = self.p.may_approve("user:ali", "agent:eddy")
        self.assertTrue(ok)
        ok, why = self.p.may_approve("agent:other", "agent:eddy")
        self.assertFalse(ok)
        ok, why = self.p.may_approve("user:k", "user:k")   # no self-approval
        self.assertFalse(ok)
        ok, _ = self.p.may_approve("user:ali", "user:k")
        self.assertTrue(ok)


class TestApprovalFlow(unittest.TestCase):
    """Agent asks for calibrate (disruptive) -> pending -> human decides."""

    def setUp(self):
        self.b = Bench(roles={
            "slot-1": {"role": "nh4-manual", "analyte": "NH4",
                       "auto_take_control": False, "auto_calibrate": False,
                       "sample_interval_s": None}})
        self.b.spawn("mod-a", "slot-1")
        wait_for(lambda: self.b.state_of("mod-a") == "OPERATIONAL",
                 what="adoption")
        # a human takes control so the module can execute
        code, resp = post(self.b.base, "/v1/commands",
                          {"module": "mod-a", "type": "take_control",
                           "actor": "user:keaton"})
        assert code == 202 and "approval_id" not in resp
        wait_for(lambda: (self.b.module("mod-a") or {}).get("mode") == "ENDPOINT",
                 what="ENDPOINT")

    def tearDown(self):
        self.b.close()

    def _agent_calibrate(self):
        code, resp = post(self.b.base, "/v1/commands",
                          {"module": "mod-a", "type": "calibrate",
                           "params": {"std_conc": 5.0},
                           "actor": "agent:eddy-om@bench"})
        self.assertEqual(code, 202)
        self.assertTrue(resp.get("approval_required"))
        return resp

    def test_agent_routine_runs_alone(self):
        code, resp = post(self.b.base, "/v1/commands",
                          {"module": "mod-a", "type": "prime",
                           "actor": "agent:eddy-om@bench"})
        self.assertEqual(code, 202)
        self.assertNotIn("approval_id", resp)
        st = wait_for(lambda: (lambda s: s if s and s["state"] == "done" else None)(
            get(self.b.base, f"/v1/commands/{resp['command_id']}")),
            timeout=60, what="prime done")
        self.assertEqual(st["result"]["data"]["status"], "succeeded")

    def test_agent_disruptive_approved_then_runs(self):
        resp = self._agent_calibrate()
        # visible as pending, command not dispatched
        pend = get(self.b.base, "/v1/approvals")["items"]
        self.assertEqual(pend[0]["approval_id"], resp["approval_id"])
        self.assertEqual(pend[0]["requested_by"], "agent:eddy-om@bench")
        st = get(self.b.base, f"/v1/commands/{resp['command_id']}")
        self.assertEqual(st["state"], "pending-approval")

        # an agent may NOT approve
        code, _ = post(self.b.base, f"/v1/approvals/{resp['approval_id']}",
                       {"decision": "approve", "actor": "agent:other"})
        self.assertEqual(code, 403)

        # a human may — and the command then actually executes
        code, out = post(self.b.base, f"/v1/approvals/{resp['approval_id']}",
                         {"decision": "approve", "actor": "user:keaton"})
        self.assertEqual(code, 200)
        st = wait_for(lambda: (lambda s: s if s and s["state"] == "done" else None)(
            get(self.b.base, f"/v1/commands/{resp['command_id']}")),
            timeout=60, what="approved calibrate done")
        self.assertEqual(st["result"]["data"]["status"], "succeeded")
        self.assertTrue(get(self.b.base,
                            "/v1/evidence?kind=calibration&module=mod-a")["items"])
        # the audit trail: pending -> granted
        audits = [e["data"]["event"] for e in
                  get(self.b.base, "/v1/evidence?kind=audit&limit=100")["items"]]
        self.assertIn("approval-pending", audits)
        self.assertIn("approval-granted", audits)

    def test_agent_disruptive_denied_is_terminal(self):
        resp = self._agent_calibrate()
        code, _ = post(self.b.base, f"/v1/approvals/{resp['approval_id']}",
                       {"decision": "deny", "actor": "user:keaton"})
        self.assertEqual(code, 200)
        st = get(self.b.base, f"/v1/commands/{resp['command_id']}")
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["result"]["data"]["status"], "rejected")
        # and no calibration happened
        self.assertFalse(get(self.b.base,
                             "/v1/evidence?kind=calibration&module=mod-a")["items"])

    def test_path_ceiling_via_gateway(self):
        # the seam: a cellular-ingress request may not even ask for disruptive
        env, problem = self.b.hub.gateway.submit(
            "mod-a", "calibrate", {"std_conc": 5.0},
            actor="user:keaton", ingress="cellular")
        self.assertIsNone(env)
        self.assertEqual(problem["type"], "urn:uii:problem:path-ceiling")

    def test_idempotency_key(self):
        import json as _json
        import urllib.request
        body = _json.dumps({"module": "mod-a", "type": "prime",
                            "actor": "user:keaton"}).encode()
        def _send():
            req = urllib.request.Request(
                self.b.base + "/v1/commands", data=body,
                headers={"Content-Type": "application/json",
                         "Idempotency-Key": "retry-abc-123"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                return _json.loads(r.read())
        first, second = _send(), _send()
        self.assertEqual(first["command_id"], second["command_id"])
        cmds = [e for e in get(self.b.base,
                               "/v1/evidence?kind=command&limit=100")["items"]
                if e["data"]["type"] == "prime"]
        self.assertEqual(len(cmds), 1)   # executed once, not twice


class TestSchedulerAuthority(unittest.TestCase):
    def test_configured_automation_still_flows(self):
        """hub.json IS the scheduler's standing approval: auto_calibrate
        (disruptive) must keep working under system: authority."""
        b = Bench()   # default role: auto_take_control + auto_calibrate
        try:
            b.spawn("mod-a", "slot-1")
            wait_for(lambda: b.good_obs("mod-a", "nh4"), timeout=90,
                     what="hands-off sample under system authority")
        finally:
            b.close()


if __name__ == "__main__":
    unittest.main()
