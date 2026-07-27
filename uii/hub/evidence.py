"""Evidence core — the hub's spine.

Every fact is an envelope: immutable, sequenced, hash-chained, stored in
SQLite (WAL). Services write through append() and subscribe for fan-out.
"""
from __future__ import annotations

import hashlib
import json
import queue
import sqlite3
import threading
from typing import Optional

from ..protocol import ENVELOPE_VERSION, now_iso, uuid7

_SCHEMA = """
CREATE TABLE IF NOT EXISTS envelopes (
  seq            INTEGER PRIMARY KEY AUTOINCREMENT,
  id             TEXT UNIQUE NOT NULL,
  kind           TEXT NOT NULL,
  time           TEXT NOT NULL,
  module         TEXT,
  channel        TEXT,
  actor          TEXT,
  command_id     TEXT,
  causation_id   TEXT,
  correlation_id TEXT,
  body           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kind ON envelopes(kind);
CREATE INDEX IF NOT EXISTS idx_module ON envelopes(module);
CREATE INDEX IF NOT EXISTS idx_command ON envelopes(command_id);
CREATE INDEX IF NOT EXISTS idx_causation ON envelopes(causation_id);
"""


class EvidenceStore:
    def __init__(self, path: str, hub_id: str):
        self.hub_id = hub_id
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        row = self._db.execute(
            "SELECT body FROM envelopes ORDER BY seq DESC LIMIT 1").fetchone()
        self._prev_hash = json.loads(row[0])["integrity"]["hash"] if row else "genesis"

    # -- write ---------------------------------------------------------------

    def append(self, kind: str, data: dict, schema: str, *,
               module: Optional[str] = None, channel: Optional[str] = None,
               actor: Optional[str] = None, trace: Optional[dict] = None,
               context: Optional[dict] = None, quality: Optional[dict] = None,
               time_: Optional[str] = None) -> dict:
        trace = trace or {}
        env = {
            "envelope": ENVELOPE_VERSION,
            "id": uuid7(),
            "kind": kind,
            "time": time_ or now_iso(),
            "source": {"hub": self.hub_id, "module": module, "channel": channel},
            "trace": {
                "actor": actor,
                "command_id": trace.get("command_id"),
                "causation_id": trace.get("causation_id"),
                "correlation_id": trace.get("correlation_id"),
            },
            "context": context,
            "quality": quality,
            "schema": schema,
            "data": data,
        }
        with self._lock:
            payload = json.dumps(env, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256((self._prev_hash + payload).encode()).hexdigest()
            env["integrity"] = {"prev_hash": self._prev_hash, "hash": digest}
            cur = self._db.execute(
                "INSERT INTO envelopes(id,kind,time,module,channel,actor,"
                "command_id,causation_id,correlation_id,body) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (env["id"], kind, env["time"], module, channel, actor,
                 env["trace"]["command_id"], env["trace"]["causation_id"],
                 env["trace"]["correlation_id"], json.dumps(env)))
            self._db.commit()
            env["sequence"] = cur.lastrowid
            self._db.execute("UPDATE envelopes SET body=? WHERE seq=?",
                             (json.dumps(env), env["sequence"]))
            self._db.commit()
            self._prev_hash = digest
            for q in list(self._subscribers):
                try:
                    q.put_nowait(env)
                except queue.Full:
                    pass
        return env

    # -- read ----------------------------------------------------------------

    def get(self, env_id: str) -> Optional[dict]:
        row = self._db.execute("SELECT body FROM envelopes WHERE id=?", (env_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def query(self, *, kind: Optional[str] = None, module: Optional[str] = None,
              channel: Optional[str] = None,
              command_id: Optional[str] = None,
              correlation_id: Optional[str] = None, since_seq: int = 0,
              since_time: Optional[str] = None, until_time: Optional[str] = None,
              limit: int = 200) -> list[dict]:
        sql, args = "SELECT body FROM envelopes WHERE seq>?", [since_seq]
        if kind:
            kinds = kind.split(",")
            sql += f" AND kind IN ({','.join('?'*len(kinds))})"
            args += kinds
        if module:
            sql += " AND module=?"
            args.append(module)
        if channel:
            sql += " AND channel=?"
            args.append(channel)
        if command_id:
            sql += " AND command_id=?"
            args.append(command_id)
        if correlation_id:
            sql += " AND correlation_id=?"
            args.append(correlation_id)
        if since_time:   # RFC3339 UTC strings compare lexicographically
            sql += " AND time>=?"
            args.append(since_time)
        if until_time:
            sql += " AND time<=?"
            args.append(until_time)
        sql += " ORDER BY seq LIMIT ?"
        args.append(min(limit, 5000))
        return [json.loads(r[0]) for r in self._db.execute(sql, args)]

    def children(self, env_id: str, limit: int = 50) -> list[dict]:
        rows = self._db.execute(
            "SELECT body FROM envelopes WHERE causation_id=? ORDER BY seq LIMIT ?",
            (env_id, limit))
        return [json.loads(r[0]) for r in rows]

    def latest_observation(self, module: str, channel: str) -> Optional[dict]:
        row = self._db.execute(
            "SELECT body FROM envelopes WHERE kind='observation' AND module=? "
            "AND channel=? ORDER BY seq DESC LIMIT 1", (module, channel)).fetchone()
        return json.loads(row[0]) if row else None

    def latest_calibration(self, module: str) -> Optional[dict]:
        row = self._db.execute(
            "SELECT body FROM envelopes WHERE kind='calibration' AND module=? "
            "ORDER BY seq DESC LIMIT 1", (module,)).fetchone()
        return json.loads(row[0]) if row else None

    def lineage(self, env_id: str) -> Optional[dict]:
        focus = self.get(env_id)
        if not focus:
            return None
        upstream, cursor, seen = [], focus, set()
        for _ in range(10):
            cause = (cursor.get("trace") or {}).get("causation_id")
            if not cause or cause in seen:
                break
            seen.add(cause)
            cursor = self.get(cause)
            if not cursor:
                break
            upstream.append(_summary(cursor))
        downstream = [_summary(c) for c in self.children(env_id)]
        context_refs = {}
        ctx = focus.get("context") or {}
        if ctx.get("calibration_id"):
            cal = self.get(ctx["calibration_id"])
            if cal:
                context_refs["calibration"] = _summary(cal)
        if ctx.get("method"):
            context_refs["method"] = ctx["method"]
        return {"focus": focus, "upstream": upstream,
                "downstream": downstream, "context_refs": context_refs}

    # -- fan-out -------------------------------------------------------------

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)


def _summary(env: dict) -> dict:
    return {
        "id": env["id"], "kind": env["kind"], "time": env["time"],
        "module": (env.get("source") or {}).get("module"),
        "actor": (env.get("trace") or {}).get("actor"),
        "data": env.get("data"),
    }
