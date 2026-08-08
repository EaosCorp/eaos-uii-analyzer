"""Frame sources for campod, and the sim rule.

Two real jobs and one bench job behind one tiny interface:

  grab() -> (rgb ndarray HxWx3 uint8, meta dict)

* HTTPSnapshotSource — pulls a still from the Reolink over the HTTP API
  (cmd=Snap), the same call we verified on the edge boxes. One frame per
  grab is exactly the "almost telemetry" cadence foam monitoring needs; a
  full RTSP decode path (opencv) is a later add for high-rate work.
* SimFrameSource — synthetic basin scene with a controllable foam fraction.
  UII_SIM=1 selects it. campod and the interpreter are byte-identical
  against sim and real (architecture §7): a foam model proven on recorded /
  synthetic frames ships by git tag, never as an edit on the box.

PNG encode/decode go through Pillow (the extension's imaging dependency);
the CV itself is pure numpy.
"""
from __future__ import annotations

import io
import os
import urllib.parse
import urllib.request

import numpy as np


# ---- encode / decode (Pillow) ------------------------------------------------

def to_png_bytes(rgb: np.ndarray) -> bytes:
    from PIL import Image
    arr = np.asarray(rgb)
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr, "RGB").save(buf, format="PNG")
    return buf.getvalue()


def decode(data: bytes) -> np.ndarray:
    from PIL import Image
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))


# ---- sim ---------------------------------------------------------------------

class SimFrameSource:
    """Dark water with bright foam patches covering ~coverage_pct of the frame.
    Deterministic per frame index; `coverage_pct` and `foam_type` are settable
    so a test can drive the hidden truth the CV must recover."""

    def __init__(self, w=320, h=180, coverage_pct=25.0, foam_type="nuisance_white",
                 quality=1.0, seed=1):
        self.w, self.h = w, h
        self.coverage_pct = coverage_pct
        self.foam_type = foam_type
        self.quality = quality       # 1.0 sharp/lit; lower = darker/blurrier
        self.i = 0
        self._seed = seed

    def set(self, coverage_pct=None, foam_type=None, quality=None):
        if coverage_pct is not None: self.coverage_pct = coverage_pct
        if foam_type is not None: self.foam_type = foam_type
        if quality is not None: self.quality = quality

    def grab(self, command=None):
        rng = np.random.default_rng(self._seed + self.i)
        self.i += 1
        h, w = self.h, self.w
        # water: dark, slightly green, moderately saturated
        img = np.zeros((h, w, 3), dtype=np.float32)
        img[..., 0] = 0.10; img[..., 1] = 0.16; img[..., 2] = 0.12
        img += rng.normal(0, 0.01, img.shape)
        # paint foam rows from the top to hit the target coverage fraction
        frac = max(0.0, min(1.0, self.coverage_pct / 100.0))
        rows = int(round(frac * h))
        if rows > 0:
            if self.foam_type == "biological_brown":
                fg = np.array([0.62, 0.44, 0.28])    # warm tan, moderate sat
            else:
                fg = np.array([0.92, 0.93, 0.94])    # bright near-white
            img[:rows, :, :] = fg
            img[:rows, :, :] += rng.normal(0, 0.02, (rows, w, 3))
        # quality knob: dim + blur when < 1
        q = float(self.quality)
        img *= (0.25 + 0.75 * q)
        if q < 0.6:  # cheap blur = 3x3 box via shifts
            img = (img + np.roll(img, 1, 0) + np.roll(img, -1, 0) +
                   np.roll(img, 1, 1) + np.roll(img, -1, 1)) / 5.0
        rgb = np.clip(img * 255.0, 0, 255).astype(np.uint8)
        meta = {"source": "sim", "w": w, "h": h, "exposure": round(q, 3),
                "true_coverage_pct": self.coverage_pct, "frame_index": self.i}
        return rgb, meta


# ---- real Reolink HTTP snapshot ---------------------------------------------

class HTTPSnapshotSource:
    """Reolink HTTP API still capture (cmd=Snap). Reads host/user/password
    from the on-device credential env already provisioned at
    /etc/eaos/camera-credentials.env."""

    def __init__(self, host, user, password, timeout=12):
        self.host, self.user, self.password, self.timeout = host, user, password, timeout

    @classmethod
    def from_env(cls, env=None):
        e = env or os.environ
        return cls(e.get("CAMERA_HOST", "192.168.3.160"),
                   e.get("CAMERA_USER", "admin"),
                   e.get("CAMERA_PASSWORD", ""))

    def grab(self, command=None):
        q = urllib.parse.urlencode({
            "cmd": "Snap", "channel": 0, "rs": "campod",
            "user": self.user, "password": self.password})
        url = f"http://{self.host}/cgi-bin/api.cgi?{q}"
        with urllib.request.urlopen(url, timeout=self.timeout) as r:
            data = r.read()
        rgb = decode(data)
        h, w = rgb.shape[0], rgb.shape[1]
        return rgb, {"source": "reolink-http", "w": w, "h": h}


def make_source(env=None):
    """UII_SIM=1 -> sim; else the Reolink HTTP snapshot from the env creds."""
    e = env or os.environ
    if e.get("UII_SIM") in ("1", "true", "yes"):
        return SimFrameSource(coverage_pct=float(e.get("UII_SIM_COVERAGE", 25.0)),
                              foam_type=e.get("UII_SIM_FOAM", "nuisance_white"),
                              quality=float(e.get("UII_SIM_QUALITY", 1.0)))
    return HTTPSnapshotSource.from_env(e)
