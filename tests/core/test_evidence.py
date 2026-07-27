"""Evidence core — chain integrity, queries, lineage."""
import hashlib
import json
import os
import tempfile
import unittest

from uii.hub.evidence import EvidenceStore


class TestEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="uii-ev-")
        self.store = EvidenceStore(os.path.join(self.tmp, "e.db"), "hub-t")

    def test_hash_chain_and_survives_reopen(self):
        for i in range(5):
            self.store.append("event", {"n": i}, "urn:uii:schema:event:0.1")
        # reopen: chain continues from the stored tip
        store2 = EvidenceStore(os.path.join(self.tmp, "e.db"), "hub-t")
        store2.append("event", {"n": 5}, "urn:uii:schema:event:0.1")

        envs = store2.query(limit=100)
        self.assertEqual(len(envs), 6)
        prev = "genesis"
        for env in envs:
            body = {k: v for k, v in env.items()
                    if k not in ("integrity", "sequence")}
            payload = json.dumps(body, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256((prev + payload).encode()).hexdigest()
            self.assertEqual(env["integrity"]["prev_hash"], prev)
            self.assertEqual(env["integrity"]["hash"], digest)
            prev = digest

    def test_lineage_walk(self):
        cmd = self.store.append("command", {"command_id": "c1", "type": "sample"},
                                "urn:uii:schema:command:0.1", module="m1",
                                actor="user:t",
                                trace={"command_id": "c1", "correlation_id": "c1"})
        res = self.store.append("result", {"status": "succeeded"},
                                "urn:uii:schema:result:0.1", module="m1",
                                trace={"command_id": "c1",
                                       "causation_id": cmd["id"],
                                       "correlation_id": "c1"})
        obs = self.store.append("observation", {"value": 4.2},
                                "urn:uii:schema:observation.concentration:0.1",
                                module="m1", channel="nh4",
                                trace={"command_id": "c1",
                                       "causation_id": res["id"],
                                       "correlation_id": "c1"})
        lin = self.store.lineage(obs["id"])
        kinds = [u["kind"] for u in lin["upstream"]]
        self.assertEqual(kinds, ["result", "command"])
        # downstream from the command reaches the result
        lin2 = self.store.lineage(cmd["id"])
        self.assertIn("result", [d["kind"] for d in lin2["downstream"]])

    def test_query_filters(self):
        self.store.append("event", {"a": 1}, "s", module="m1")
        self.store.append("health", {"b": 2}, "s", module="m2")
        self.assertEqual(len(self.store.query(kind="event")), 1)
        self.assertEqual(len(self.store.query(module="m2")), 1)
        self.assertEqual(len(self.store.query(kind="event,health")), 2)


if __name__ == "__main__":
    unittest.main()
