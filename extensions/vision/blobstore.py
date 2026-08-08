"""Frame blob store — content-addressed pixels beside the evidence log.

Frames are large, so they do not go inside evidence envelopes; the envelope
carries the frame's SHA-256 and metadata, and the bytes live here, addressed
by that hash. Co-located hub and campod on one Jetson share this directory
(UII_FRAMES_DIR), so the module writes a frame and the hub interpreter reads
it back by hash. In a future distributed split, a real upload channel
replaces the shared directory; the hash contract does not change.

Retention (see docs/vision-profile.md §11) tiers the blob, never the
envelope: reference images, event frames, and periodic keyframes are kept,
routine frames are aged out. `sweep()` is the hook for that policy.
"""
from __future__ import annotations

import hashlib
import os
import time


class FrameBlobStore:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _dir(self, digest: str) -> str:
        return os.path.join(self.root, digest[:2])

    def put(self, data: bytes, ext: str = "png") -> str:
        """Write bytes, return their SHA-256 hex. Idempotent (content-addressed)."""
        digest = hashlib.sha256(data).hexdigest()
        d = self._dir(digest)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{digest}.{ext}")
        if not os.path.exists(path):
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        return digest

    def path(self, digest: str) -> str | None:
        d = self._dir(digest)
        if not os.path.isdir(d):
            return None
        for name in os.listdir(d):
            if name.startswith(digest + "."):
                return os.path.join(d, name)
        return None

    def get(self, digest: str) -> bytes | None:
        p = self.path(digest)
        if not p:
            return None
        with open(p, "rb") as f:
            return f.read()

    def exists(self, digest: str) -> bool:
        return self.path(digest) is not None

    def sweep(self, keep_hashes: set[str], older_than_s: float) -> int:
        """Delete blobs not in keep_hashes and older than the cutoff. Returns
        count removed. The retention policy decides keep_hashes (reference and
        event frames); this is only the mechanism."""
        cutoff = time.time() - older_than_s
        removed = 0
        for sub in os.listdir(self.root):
            d = os.path.join(self.root, sub)
            if not os.path.isdir(d):
                continue
            for name in os.listdir(d):
                digest = name.split(".", 1)[0]
                if digest in keep_hashes:
                    continue
                fp = os.path.join(d, name)
                try:
                    if os.path.getmtime(fp) < cutoff:
                        os.remove(fp)
                        removed += 1
                except OSError:
                    pass
        return removed
