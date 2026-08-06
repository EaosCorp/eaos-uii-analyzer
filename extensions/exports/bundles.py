"""Evidence export bundles (spec §8.1) — "the evidence for whatever you
need to make a call on", as one portable, self-describing, tamper-evident
artifact.

A bundle is a .tgz containing:

  manifest.json    what this is: hub identity, window, filters, counts,
                   first/last sequence, whether the slice is contiguous
  evidence.jsonl   the envelopes, sequence order, hash chain intact
  chain.json       first prev_hash + last hash, contiguity, verify recipe
  README.md        written for the next reader — a support engineer or an
                   AI agent dropped this bundle as context; it explains how
                   to read and verify what it is holding

Contiguity matters for verification: a pure time/sequence window is a
contiguous slice of the chain and internally verifiable end-to-end (each
envelope's prev_hash must equal its predecessor's hash). Filters by
module/kind/correlation produce an honest but non-contiguous selection;
each envelope still self-describes, but chain verification needs the full
log. The manifest says which you have — no bundle pretends.

Use cases: diagnostic bundle for support, regulatory evidence package,
refurb-center module history (module=X, kind=identity,calibration,health),
agent context for an investigation (correlation_id=<run>).
"""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
import time

from uii.hub.evidence import EvidenceStore

CONTIGUOUS_FILTERS = {"since_seq", "since_time", "until_time"}


def build_bundle(store: EvidenceStore, filters: dict) -> tuple[bytes, dict]:
    """Returns (tgz_bytes, manifest). Filters: module, kind,
    correlation_id, since_seq, since_time, until_time."""
    clean = {k: v for k, v in (filters or {}).items() if v not in (None, "")}
    contiguous = set(clean) <= CONTIGUOUS_FILTERS

    envs: list[dict] = []
    cursor = int(clean.get("since_seq", 0))
    while True:
        batch = store.query(
            kind=clean.get("kind"), module=clean.get("module"),
            correlation_id=clean.get("correlation_id"), since_seq=cursor,
            since_time=clean.get("since_time"),
            until_time=clean.get("until_time"), limit=5000)
        if not batch:
            break
        envs.extend(batch)
        cursor = batch[-1]["sequence"]

    manifest = {
        "bundle": "uii-evidence/0.1",
        "hub": store.hub_id,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "filters": clean,
        "count": len(envs),
        "first_seq": envs[0]["sequence"] if envs else None,
        "last_seq": envs[-1]["sequence"] if envs else None,
        "contiguous": contiguous,
        "kinds": sorted({e["kind"] for e in envs}),
        "modules": sorted({(e.get("source") or {}).get("module") or "-"
                           for e in envs}),
    }
    chain = {
        "contiguous": contiguous,
        "first": ({"seq": envs[0]["sequence"],
                   "prev_hash": envs[0]["integrity"]["prev_hash"]}
                  if envs else None),
        "last": ({"seq": envs[-1]["sequence"],
                  "hash": envs[-1]["integrity"]["hash"]} if envs else None),
        "verify": ("contiguous slice: for each envelope, sha256(prev_hash + "
                   "canonical_json(envelope minus integrity,sequence)) must "
                   "equal integrity.hash, and prev_hash must equal the "
                   "previous envelope's hash" if contiguous else
                   "non-contiguous selection: envelopes self-describe but "
                   "chain verification requires the full log window"),
    }

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add(name: str, text: str):
            data = text.encode()
            info = tarfile.TarInfo(f"bundle/{name}")
            info.size = len(data)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))

        add("manifest.json", json.dumps(manifest, indent=2))
        add("evidence.jsonl",
            "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in envs))
        add("chain.json", json.dumps(chain, indent=2))
        add("README.md", _readme(manifest))
    return buf.getvalue(), manifest


def verify_bundle(envs: list[dict]) -> tuple[bool, str]:
    """Verify a CONTIGUOUS slice: internal chain consistency."""
    prev = None
    for env in envs:
        body = {k: v for k, v in env.items() if k not in ("integrity", "sequence")}
        payload = json.dumps(body, sort_keys=True, separators=(",", ":"))
        expect = env["integrity"]["prev_hash"]
        digest = hashlib.sha256((expect + payload).encode()).hexdigest()
        if digest != env["integrity"]["hash"]:
            return False, f"hash mismatch at seq {env['sequence']}"
        if prev is not None and expect != prev:
            return False, f"chain break at seq {env['sequence']}"
        prev = env["integrity"]["hash"]
    return True, f"{len(envs)} envelopes verified"


def _readme(manifest: dict) -> str:
    return f"""# UII evidence bundle

Hub `{manifest['hub']}` · {manifest['count']} envelopes · sequences
{manifest['first_seq']}–{manifest['last_seq']} · filters
`{json.dumps(manifest['filters'])}` · contiguous: {manifest['contiguous']}

Every line of `evidence.jsonl` is one immutable evidence envelope: what
happened (`kind`, `data`), where (`source`), why (`trace.causation_id`
points at the envelope that caused it; `trace.actor` says who), and its
place in a hash chain (`integrity`). Kinds present: {', '.join(manifest['kinds'])}.

Reading it:
- Follow `trace.causation_id` backwards to answer "why does this record
  exist"; `correlation_id` groups one run/activity.
- `observation` envelopes carry `context.calibration_id` and
  `data.raw_refs` — the calibration and raw detector readings behind every
  derived value are in-band or fetchable from the hub.
- Verify integrity per `chain.json`. If `contiguous` is true, the whole
  slice re-verifies offline; if false, this is a filtered selection.

This bundle is safe to hand to a person or an AI agent as complete,
self-describing context: nothing in it can have been edited after the
fact without breaking the chain.
"""
