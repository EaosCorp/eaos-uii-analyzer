"""Command gateway — every command is validated here before a module sees it.

THE SEAM, half three: one chokepoint for ALL actors (humans, agents,
schedulers, PLCs). Manifest validation, policy decision, dispatch — and
ONE RESULT PER COMMAND, ALWAYS (spec §6.3): success, failure, rejection,
or module loss each terminate in exactly one `result` envelope. There is
no transport that bypasses this path.

Idempotency: submissions may carry an idempotency key; retries return the
original response instead of executing twice. Agents retry — this is what
makes that safe.

Extension hook — `policy`. The core default allows everything (open
bench). The authority extension swaps in a real policy whose decide() can
answer:
    "allow"                          -> dispatch now
    ("reject", problem_urn, detail)  -> instant audited rejection
    ("defer", extra_data)            -> command stored as evidence but NOT
                                        dispatched; policy.on_defer() takes
                                        over (the approval flow)
plus command_state() so /v1/commands/{id} can report "pending-approval".
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Optional

from ..protocol import now_iso, uuid7
from .evidence import EvidenceStore
from .southbound import SouthboundHub


class AllowAllPolicy:
    """Open-bench default: every declared command runs for every actor.
    The risk field is still read and recorded so evidence is complete."""

    def decide(self, actor: str, risk: str, ingress: str):
        return "allow"

    def on_defer(self, gateway, env):  # pragma: no cover — never deferred
        pass

    def command_state(self, command_id: str) -> Optional[str]:
        return None


class CommandGateway:
    def __init__(self, store: EvidenceStore, southbound: SouthboundHub,
                 loop: asyncio.AbstractEventLoop, policy=None):
        self.store = store
        self.southbound = southbound
        self.loop = loop
        self.policy = policy or AllowAllPolicy()
        self._idem: OrderedDict[str, tuple] = OrderedDict()

    # -- submission ---------------------------------------------------------

    def submit(self, module_id: str, cmd_type: str, params: Optional[dict],
               actor: str = "user:local", expires_in_s: int = 300,
               ingress: str = "local",
               idempotency_key: Optional[str] = None
               ) -> tuple[Optional[dict], Optional[dict]]:
        """Returns (command_envelope, problem)."""
        if idempotency_key and idempotency_key in self._idem:
            return self._idem[idempotency_key]
        result = self._submit(module_id, cmd_type, params, actor,
                              expires_in_s, ingress)
        if idempotency_key:
            self._idem[idempotency_key] = result
            while len(self._idem) > 500:
                self._idem.popitem(last=False)
        return result

    def _submit(self, module_id, cmd_type, params, actor, expires_in_s,
                ingress) -> tuple[Optional[dict], Optional[dict]]:
        session = self.southbound.sessions.get(module_id)
        if not session or session.state != "OPERATIONAL":
            return None, self._reject(module_id, cmd_type, actor,
                                      "urn:uii:problem:module-lost",
                                      f"module '{module_id}' is not operational")
        declared = {c["type"]: c for c in session.manifest.get("commands", [])}
        if cmd_type not in declared:
            return None, self._reject(
                module_id, cmd_type, actor, "urn:uii:problem:validation",
                f"'{cmd_type}' not in module manifest {sorted(declared)}")
        if params is not None and not isinstance(params, dict):
            return None, self._reject(module_id, cmd_type, actor,
                                      "urn:uii:problem:validation",
                                      "params must be an object")

        risk = declared[cmd_type].get("risk", "routine")
        decision = self.policy.decide(actor, risk, ingress)
        if isinstance(decision, tuple) and decision[0] == "reject":
            return None, self._reject(module_id, cmd_type, actor,
                                      decision[1], decision[2])

        command_id = uuid7()
        data = {"command_id": command_id, "type": cmd_type,
                "params": params or {}, "target": module_id, "risk": risk,
                "ingress": ingress, "expires_at": now_iso()}
        deferred = isinstance(decision, tuple) and decision[0] == "defer"
        if deferred:
            data.update(decision[1] or {})
        env = self.store.append(
            "command", data, "urn:uii:schema:command:0.1",
            module=module_id, actor=actor,
            trace={"command_id": command_id, "correlation_id": command_id})

        if deferred:
            self.policy.on_defer(self, env)
        else:
            self.dispatch_env(module_id, env)
        return env, None

    def dispatch_env(self, module_id: str, env: dict):
        """Send a stored command to its module; a lost module still gets
        its one terminal result. Public: the authority extension dispatches
        approved commands through this."""
        future = asyncio.run_coroutine_threadsafe(
            self.southbound.dispatch(module_id, env), self.loop)
        if not future.result(timeout=5):
            self.store.append(
                "result", {"status": "failed",
                           "problem": "urn:uii:problem:module-lost"},
                "urn:uii:schema:result:0.1", module=module_id,
                trace={"command_id": env["data"]["command_id"],
                       "causation_id": env["id"],
                       "correlation_id": env["data"]["command_id"]})

    # -- status -------------------------------------------------------------------

    def status(self, command_id: str) -> Optional[dict]:
        envs = self.store.query(command_id=command_id, limit=500)
        if not envs:
            return None
        by_kind: dict[str, list] = {}
        for e in envs:
            by_kind.setdefault(e["kind"], []).append(e)
        result = (by_kind.get("result") or [None])[-1]
        if result:
            state = "done"
        else:
            state = (self.policy.command_state(command_id)
                     or ("running" if by_kind.get("progress") or by_kind.get("ack")
                         else "queued"))
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
