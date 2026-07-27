"""Evidence export bundles — contents, contiguity honesty, verification."""
import io
import json
import os
import tarfile
import tempfile
import unittest

from uii.hub.evidence import EvidenceStore
from extensions.exports.bundles import build_bundle, verify_bundle


def _mk_store():
    tmp = tempfile.mkdtemp(prefix="uii-exp-")
    store = EvidenceStore(os.path.join(tmp, "e.db"), "hub-x")
    for i in range(10):
        store.append("event", {"n": i}, "urn:uii:schema:event:0.1",
                     module="m1" if i % 2 else "m2")
    return store


def _read(blob):
    out = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for m in tar.getmembers():
            out[m.name.split("/", 1)[1]] = tar.extractfile(m).read().decode()
    return out


class TestExports(unittest.TestCase):
    def test_full_bundle_is_contiguous_and_verifies(self):
        store = _mk_store()
        blob, manifest = build_bundle(store, {})
        self.assertTrue(manifest["contiguous"])
        self.assertEqual(manifest["count"], 10)

        files = _read(blob)
        self.assertEqual(set(files), {"manifest.json", "evidence.jsonl",
                                      "chain.json", "README.md"})
        envs = [json.loads(l) for l in files["evidence.jsonl"].splitlines()]
        self.assertEqual(len(envs), 10)
        ok, msg = verify_bundle(envs)
        self.assertTrue(ok, msg)
        chain = json.loads(files["chain.json"])
        self.assertEqual(chain["first"]["prev_hash"], "genesis")
        self.assertIn("hub-x", files["README.md"])

    def test_window_slice_still_verifies(self):
        store = _mk_store()
        blob, manifest = build_bundle(store, {"since_seq": 3})
        self.assertTrue(manifest["contiguous"])
        envs = [json.loads(l) for l in
                _read(blob)["evidence.jsonl"].splitlines()]
        self.assertEqual(envs[0]["sequence"], 4)
        ok, msg = verify_bundle(envs)
        self.assertTrue(ok, msg)

    def test_filtered_bundle_is_honest_about_contiguity(self):
        store = _mk_store()
        blob, manifest = build_bundle(store, {"module": "m1"})
        self.assertFalse(manifest["contiguous"])
        self.assertEqual(manifest["count"], 5)
        chain = json.loads(_read(blob)["chain.json"])
        self.assertFalse(chain["contiguous"])

    def test_tamper_detected(self):
        store = _mk_store()
        blob, _ = build_bundle(store, {})
        envs = [json.loads(l) for l in
                _read(blob)["evidence.jsonl"].splitlines()]
        envs[4]["data"]["n"] = 999   # rewrite history
        ok, msg = verify_bundle(envs)
        self.assertFalse(ok)
        self.assertIn("seq 5", msg)


if __name__ == "__main__":
    unittest.main()
