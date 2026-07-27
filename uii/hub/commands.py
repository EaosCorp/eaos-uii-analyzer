"""Command gateway — every command is validated here before a module sees it.

The single chokepoint for ALL actors (humans, agents, schedulers, PLCs):
manifest validation, then AUTHORITY (actor class × declared risk × ingress
path ceiling, see authority.py), then dispatch. Rejections are instant and
logged as audit envelopes; nothing reaches the southbound gateway
unvalidated, and there is no transport that bypasses this path.

Approval flow (spec §10): a command above its actor's authority is stored
as evidence but NOT dispatched; an `audit{approval-pending}` envelope and a
pending-approval record are created. A human approves or denies via
/v1/approvals (audited either way); approval dispatches the original
command, denial or expiry emits its terminal result. Pending approvals are
in-memory + evidence: a hub restart drops the pending queue (the command
was never dispatched; resubmit), while the audit trail of what was
requested survives in the log.

Idempotency (spec §6.1): POST /v1/commands accepts an Idempotency-Key;
retries return the original response instead of executing twice. Agents
retry — this is what makes that safe.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Optional

from ..protocol import now_iso, uuid7
from .authority import AuthorityPolicy
from .evidence import EvidenceStore
from .southbound import SouthboundHub


class CommandGateway:
    def __init__(self, store: EvidenceStore, southbound: SouthboundHub,
                 loop: asyncio.AbstractEventLoop,
                 policy: Optional[AuthorityPolicy] = None):
        self.store = store
        self.southbound = southbound
        self.loop = loop
        self.policy = policy or AuthorityPolicy()
        # approval_id -> pending record; command_id -> approval_id
        self.pending: dict[str, dict] = {}
        self._by_command: dict[str, str] = {}
        self._idem: OrderedDict[str, tuple] = OrderedDict()

    # -- submission ---------------------------------------------------------

    def submit(self, module_id: str, cmd_type: str, params: Optional[dict],
               actor: str = "user:local", expires_in_s: int = 300,
               ingress: str = "local",
               idempotency_key: Optional[str] = None
               ) -> tuple[Optional[dict], Optional[dict]]:
        """Returns (command_envelope, problem). A pending-approval command
        has data.approval_id set and is not yet dispatched."""
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

        # -- authority: min(actor class, ingress path ceiling) --------------
        risk = declared[cmd_type].get("risk", "routine")
        decision = self.policy.decide(actor, risk, ingress)
        if decision == "reject-path":
            return None, self._reject(
                module_id, cmd_type, actor, "urn:uii:problem:path-ceiling",
                f"risk '{risk}' exceeds the '{ingress}' path ceiling "
                f"'{self.policy.path_ceilings.get(ingress)}' — not approvable "
                f"over this path")

        command_id = uuid7()
        data = {"command_id": command_id, "type": cmd_type,
                "params": params or {}, "target": module_id, "risk": risk,
                "expires_at": now_iso()}
        if decision == "approval":
            approval_id = uuid7()
            data["approval_id"] = approval_id
        env = self.store.append(
            "command", data, "urn:uii:schema:command:0.1",
            module=module_id, actor=actor,
            trace={"command_id": command_id, "correlation_id": command_id})

        if decision == "approval":
            self.pending[approval_id] = {
                "approval_id": approval_id, "command_env": env,
                "module": module_id, "type": cmd_type, "risk": risk,
                "requested_by": actor, "requested_at": now_iso(),
                "expires_at_mono": time.monotonic() + expires_in_s,
            }
            self._by_command[command_id] = approval_id
            self.store.append(
                "audit", {"event": "approval-pending",
                          "approval_id": approval_id, "type": cmd_type,
                          "risk": risk, "requested_by": actor},
                "urn:uii:schema:audit:0.1", module=module_id, actor=actor,
                trace={"command_id": command_id, "causation_id": env["id"],
                       "correlation_id": command_id})
            return env, None

        self._dispatch(module_id, env)
        return env, None

    def _dispatch(self, module_id: str, env: dict):
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

    # -- approvals ------------------------------------------------------------

    def list_approvals(self) -> list[dict]:
        self._expire_pending()
        return [{k: v for k, v in p.items()
                 if k not in ("command_env", "expires_at_mono")}
                | {"command_id": p["command_env"]["data"]["command_id"]}
                for p in self.pending.values()]

    def decide_approval(self, approval_id: str, decision: str,
                        approver: str) -> tuple[Optional[dict], Optional[dict]]:
        """Returns (summary, problem)."""
        self._expire_pending()
        p = self.pending.get(approval_id)
        if not p:
            return None, {"type": "urn:uii:problem:not-found",
                          "title": "no such pending approval",
                          "detail": f"'{approval_id}' unknown, decided, or expired"}
        ok, why = self.policy.may_approve(approver, p["requested_by"])
        if not ok:
            self.store.append(
                "audit", {"event": "approval-refused",
                          "approval_id": approval_id, "detail": why},
                "urn:uii:schema:audit:0.1", module=p["module"], actor=approver)
            return None, {"type": "urn:uii:problem:unauthorized-role",
                          "title": "cannot approve", "detail": why}

        env = p["command_env"]
        command_id = env["data"]["command_id"]
        self.pending.pop(approval_id, None)
        self._by_command.pop(command_id, None)
        granted = decision == "approve"
        self.store.append(
            "audit", {"event": "approval-granted" if granted else "approval-denied",
                      "approval_id": approval_id, "type": p["type"],
                      "risk": p["risk"], "requested_by": p["requested_by"]},
            "urn:uii:schema:audit:0.1", module=p["module"], actor=approver,
            trace={"command_id": command_id, "causation_id": env["id"],
                   "correlation_id": command_id})
        if granted:
            self._dispatch(p["module"], env)
        else:
            self.store.append(
                "result", {"status": "rejected",
                           "reason": f"approval denied by {approver}"},
                "urn:uii:schema:result:0.1", module=p["module"],
                trace={"command_id": command_id, "causation_id": env["id"],
                       "correlation_id": command_id})
        return {"approval_id": approval_id, "command_id": command_id,
                "decision": decision, "by": approver}, None

    def _expire_pending(self):
        now = time.monotonic()
        for approval_id in [a for a, p in self.pending.items()
                            if now > p["expires_at_mono"]]:
            p = self.pending.pop(approval_id)
            self._by_command.pop(p["command_env"]["data"]["command_id"], None)
            self.store.append(
                "result", {"status": "expired",
                           "reason": "approval not granted before expiry"},
                "urn:uii:schema:result:0.1", module=p["module"],
                trace={"command_id": p["command_env"]["data"]["command_id"],
                       "causation_id": p["command_env"]["id"],
                       "correlation_id": p["command_env"]["data"]["command_id"]})

    # -- status -------------------------------------------------------------------

    def status(self, command_id: str) -> Optional[dict]:
        self._expire_pending()
        envs = self.store.query(command_id=command_id, limit=500)
        if not envs:
            return None
        by_kind: dict[str, list] = {}
        for e in envs:
            by_kind.setdefault(e["kind"], []).append(e)
        result = (by_kind.get("result") or [None])[-1]
        if result:
            state = "done"
        elif command_id in self._by_command:
            state = "pending-approval"
        elif by_kind.get("progress") or by_kind.get("ack"):
            state = "running"
        else:
            state = "queued"
        return {
            "command_id": command_id, "state": state,
            "approval_id": self._by_command.get(command_id),
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
