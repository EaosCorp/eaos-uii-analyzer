"""faceplate extension — the local screen serves and reflects the hub."""
import unittest
import urllib.request

from ..helpers import Bench, wait_for


class TestFaceplate(unittest.TestCase):
    def test_page_serves_and_is_a_pure_client(self):
        b = Bench(extensions=["faceplate"])
        try:
            b.spawn("ref-a", "slot-1")
            wait_for(lambda: b.state_of("ref-a") == "OPERATIONAL",
                     what="adoption")
            with urllib.request.urlopen(b.base + "/faceplate", timeout=10) as r:
                self.assertEqual(r.status, 200)
                self.assertIn("text/html", r.headers["Content-Type"])
                body = r.read().decode()
            self.assertIn("UII FACEPLATE", body)
            # a pure client: talks to /v1 like everyone else, no private data
            self.assertIn("/v1/observations/latest", body)
            self.assertIn("/v1/events", body)
        finally:
            b.close()


if __name__ == "__main__":
    unittest.main()
