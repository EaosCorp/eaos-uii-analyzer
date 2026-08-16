"""Locked mode (authority extension) — identity comes from the credential,
not the claim. With credentials configured, every API request needs a
valid bearer token, the actor IS the token's mapping (a body-supplied
actor is ignored), and "human" means "holds a user:* credential"."""
import json
import unittest
import urllib.error
import urllib.request

from ..helpers import Bench, wait_for

AGENT_TOKEN = "tok-agent-000000000000000000000001"
USER_TOKEN = "tok-user-0000000000000000000000002"


def call(base, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class TestLockedMode(unittest.TestCase):
    def setUp(self):
        self.b = Bench(
            extensions=["authority"],
            roles={"slot-1": {"role": "nh4-manual", "analyte": "NH4"}},
            extra_config={"credentials": {
                AGENT_TOKEN: "agent:eddy-om@bench",
                USER_TOKEN: "user:keaton"}})
        self.b.spawn("ref-a", "slot-1")
        wait_for(lambda: self._state("ref-a") == "OPERATIONAL",
                 what="adoption")

    def _state(self, module_id):
        code, out = call(self.b.base, "/v1/modules", token=USER_TOKEN)
        for m in out.get("items", []):
            if m["id"] == module_id:
                return m["state"]
        return None

    def tearDown(self):
        self.b.close()

    def test_no_token_no_api(self):
        code, _ = call(self.b.base, "/v1/modules")
        self.assertEqual(code, 401)
        code, _ = call(self.b.base, "/v1/commands",
                       {"module": "ref-a", "type": "sample"})
        self.assertEqual(code, 401)
        code, _ = call(self.b.base, "/v1/modules", token="wrong-token")
        self.assertEqual(code, 401)
        code, out = call(self.b.base, "/v1/system", token=USER_TOKEN)
        self.assertEqual(code, 200)
        self.assertEqual(out["auth"], "token")

    def test_actor_spoof_is_ignored(self):
        """An agent token claiming to be a user in the body is still an
        agent: disruptive command defers to approval, and the command
        envelope records the TOKEN's identity."""
        code, resp = call(self.b.base, "/v1/commands",
                          {"module": "ref-a", "type": "calibrate",
                           "params": {"std_conc": 5.0},
                           "actor": "user:fake-human"},   # the lie
                          token=AGENT_TOKEN)
        self.assertEqual(code, 202)
        self.assertTrue(resp.get("approval_required"))    # lie didn't work
        code, ev = call(self.b.base, f"/v1/evidence/{resp['evidence_id']}",
                        token=USER_TOKEN)
        self.assertEqual(ev["trace"]["actor"], "agent:eddy-om@bench")

    def test_agent_cannot_fake_the_approving_human(self):
        code, resp = call(self.b.base, "/v1/commands",
                          {"module": "ref-a", "type": "calibrate",
                           "params": {"std_conc": 5.0}},
                          token=AGENT_TOKEN)
        approval_id = resp["approval_id"]
        # agent token + claimed human actor in the body -> still an agent
        code, out = call(self.b.base, f"/v1/approvals/{approval_id}",
                         {"decision": "approve", "actor": "user:keaton"},
                         token=AGENT_TOKEN)
        self.assertEqual(code, 403)
        # the real human's credential works
        code, out = call(self.b.base, f"/v1/approvals/{approval_id}",
                         {"decision": "approve"}, token=USER_TOKEN)
        self.assertEqual(code, 200)
        self.assertEqual(out["by"], "user:keaton")


if __name__ == "__main__":
    unittest.main()
