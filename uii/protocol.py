"""UII southbound protocol v0.1 — JSON Lines over TCP.

One JSON object per line. CBOR replaces this framing at Stage 3 without
changing message shapes. Message types:

  module -> hub : HELLO, TELEM, ACK, PROGRESS, RESULT, PONG, BYE
  hub -> module : HELLO_OK, QUARANTINE, CMD, PING
"""
from __future__ import annotations

import json
import os
import time
import uuid

PROTO = "uii/0.1"
ENVELOPE_VERSION = "uii/0.1"


def uuid7() -> str:
    """Time-ordered UUID (48-bit unix-ms prefix, random tail)."""
    ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
    rand = int.from_bytes(os.urandom(10), "big")
    val = (ms << 80) | rand
    return str(uuid.UUID(int=val))


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time()*1000)%1000:03d}Z"


def encode(msg: dict) -> bytes:
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode()


def decode_line(line: bytes) -> dict:
    return json.loads(line.decode())
