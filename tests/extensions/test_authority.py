"""authority extension: actor class × risk × ingress ceiling, the approval
flow, and idempotent retries. Policy mechanics unit-tested; the approval
lifecycle runs on a live bench with the extension enabled."""
import unittest

from extensions.authority.policy import AuthorityPolicy, actor_class

from ..helpers import Bench, get, post, wait_for


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
        self.assertEqual(self.p.decide("agent:e", "routine"), "allow")
        self.assertEqual(self.p.decide("agent:e", "disruptive"), "approval")
        self.assertEqual(self.p.decide("user:k", "disruptive"), "allow")
        self.assertEqual(self.p.decide("system:scheduler", "disruptive"), "allow")
        self.assertEqual(self.p.decide("plc:x", "disruptive"), "approval")

    def test_hazardous_always_needs_approval(self):
        for actor in ("user:k", "agent:e", "system:scheduler", "plc:x"):
            self.assertEqual(self.p.decide(actor, "hazardous"), "approval")

    def test_path_ceiling_is_absolute(self):
        self.assertEqual(self.p.decide("user:k", "disruptive", "cellular"),
                         "reject-path")
        self.assertEqual(self.p.decide("user:k", "routine", "cellular"), "allow")
        self.assertEqual(self.p.decide("plc:x", "disruptive", "ot"),
                         "reject-path")
        self.assertEqual(self.p.decide("user:k", "hazardous", "local"),
                         "approval")

    def test_site_overrides(self):
        p = AuthorityPolicy(authority={"agent": "disruptive"},
                            path_ceilings={"cloud": "routine"})
        self.assertEqual(p.decide("agent:e", "disruptive"), "allow")
        self.assertEqual(p.decide("agent:e", "disruptive", "cloud"),
                         "reject-path")

    def test_approver_rules(self):
        self.assertTrue(self.p.may_approve("user:ali", "agent:eddy")[0])
        self.assertFalse(self.p.may_approve("agent:other", "agent:eddy")[0])
        self.assertFalse(self.p.may_approve("user:k", "user:k")[0])
        self.assertTrue(self.p.may_approve("user:ali", "user:k")[0])


class TestApprovalFlow(unittest.TestCase):
    """Agent asks for calibrate (disruptive) -> deferred -> human decides."""

    def setUp(self):
        self.b = Bench(extensions=["authority"], roles={
            "slot-1": {"role": "nh4-manual", "analyte": "NH4"}})
        self.b.spawn("ref-a", "slot-1")
        wait_for(lambda: self.b.state_of("ref-a") == "OPERATIONAL",
                 what="adoption")

    def tearDown(self):
        self.b.close()

    def _agent_calibrate(self):
        code, resp = post(self.b.base, "/v1/commands",
                          {"module": "ref-a", "type": "calibrate",
                           "params": {"std_conc": 5.0},
                           "actor": "agent:eddy-om@bench"})
        self.assertEqual(code, 202)
        self.assertTrue(resp.get("approval_required"))
        return resp

    def test_agent_routine_runs_alone(self):
        code, resp = post(self.b.base, "/v1/commands",
                          {"module": "ref-a", "type": "sample",
                           "actor": "agent:eddy-om@bench"})
        self.assertEqual(code, 202)
        self.assertNotIn("approval_id", resp)
        st = wait_for(lambda: (lambda s: s if s and s["state"] == "done" else None)(
            get(self.b.base, f"/v1/commands/{resp['command_id']}")),
            timeout=60, what="sample done")
        self.assertEqual(st["result"]["data"]["status"], "succeeded")

    def test_agent_disruptive_approved_then_runs(self):
        resp = self._agent_calibrate()
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
                            "/v1/evidence?kind=calibration&module=ref-a")["items"])
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
        self.assertFalse(get(self.b.base,
                             "/v1/evidence?kind=calibration&module=ref-a")["items"])

    def test_path_ceiling_via_gateway(self):
        env, problem = self.b.hub.gateway.submit(
            "ref-a", "calibrate", {"std_conc": 5.0},
            actor="user:keaton", ingress="cellular")
        self.assertIsNone(env)
        self.assertEqual(problem["type"], "urn:uii:problem:path-ceiling")

    def test_idempotency_key(self):
        import json as _json
        import urllib.request
        body = _json.dumps({"module": "ref-a", "type": "sample",
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
                if e["data"]["type"] == "sample"]
        self.assertEqual(len(cmds), 1)   # executed once, not twice


class TestSchedulerAuthority(unittest.TestCase):
    def test_configured_automation_still_flows(self):
        """hub.json IS the scheduler's standing approval: auto_calibrate
        (disruptive) must keep working under system: authority."""
        b = Bench(extensions=["authority", "scheduler"])
        try:
            b.spawn("ref-a", "slot-1")
            wait_for(lambda: b.good_obs("ref-a", "nh4"), timeout=90,
                     what="hands-off sample under system authority")
        finally:
            b.close()


if __name__ == "__main__":
    unittest.main()
