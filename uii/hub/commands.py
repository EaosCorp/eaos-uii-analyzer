"""Command gateway — every command is validated here before a module sees it.

Rejections are instant and logged as audit envelopes; nothing reaches the
southbound gateway unvalidated.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from ..protocol import now_iso, uuid7
from .evidence import EvidenceStore
from .southbound import SouthboundHub


class CommandGateway:
    def __init__(self, store: EvidenceStore, southbound: SouthboundHub,
                 loop: asyncio.AbstractEventLoop):
        self.store = store
        self.southbound = southbound
        self.loop = loop

    def submit(self, module_id: str, cmd_type: str, params: Optional[dict],
               actor: str = "user:local",
               expires_in_s: int = 300) -> tuple[Optional[dict], Optional[dict]]:
        """Returns (command_envelope, problem)."""
        session = self.southbound.sessions.get(module_id)
        if not session or session.state != "OPERATIONAL":
            return None, self._reject(module_id, cmd_type, actor,
                                      "urn:uii:problem:module-lost",
                                      f"module '{module_id}' is not operational")
        declared = {c["type"] for c in session.manifest.get("commands", [])}
        if cmd_type not in declared:
            return None, self._reject(module_id, cmd_type, actor,
                                      "urn:uii:problem:validation",
                                      f"'{cmd_type}' not in module manifest {sorted(declared)}")
        if params is not None and not isinstance(params, dict):
            return None, self._reject(module_id, cmd_type, actor,
                                      "urn:uii:problem:validation", "params must be an object")

        command_id = uuid7()
        expires_at = time.time() + expires_in_s
        env = self.store.append(
            "command",
            {"command_id": command_id, "type": cmd_type, "params": params or {},
             "target": module_id, "expires_at": now_iso()},
            "urn:uii:schema:command:0.1", module=module_id, actor=actor,
            trace={"command_id": command_id, "correlation_id": command_id})

        future = asyncio.run_coroutine_threadsafe(
            self.southbound.dispatch(module_id, env), self.loop)
        if not future.result(timeout=5):
            self.store.append(
                "result", {"status": "failed",
                           "problem": "urn:uii:problem:module-lost"},
                "urn:uii:schema:result:0.1", module=module_id,
                trace={"command_id": command_id, "causation_id": env["id"],
                       "correlation_id": command_id})
        return env, None

    def status(self, command_id: str) -> Optional[dict]:
        envs = self.store.query(command_id=command_id, limit=500)
        if not envs:
            return None
        by_kind: dict[str, list] = {}
        for e in envs:
            by_kind.setdefault(e["kind"], []).append(e)
        result = (by_kind.get("result") or [None])[-1]
        state = "done" if result else (
            "running" if by_kind.get("progress") or by_kind.get("ack") else "queued")
        return {
            "command_id": command_id, "state": state,
            "command": (by_kind.get("command") or [None])[0],
            "ack": (by_kind.get("ack") or [None])[-1],
            "progress": [p["data"] for p in by_kind.get("progress", [])],
            "result": result,
            "derived": [o for o in by_kind.get("observation", [])
                        if o.get("schema", "").startswith("urn:uii:schema:observation.concentration")],
        }

    def _reject(self, module_id, cmd_type, actor, problem, detail) -> dict:
        self.store.append(
            "audit", {"event": "command-rejected", "type": cmd_type,
                      "problem": problem, "detail": detail},
            "urn:uii:schema:audit:0.1", module=module_id, actor=actor)
        return {"type": problem, "title": "command rejected", "detail": detail}
