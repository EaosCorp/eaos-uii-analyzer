"""authority extension — who may run what, arriving how.

Replaces the core's open-bench AllowAllPolicy with real governance:

  * effective permission = min(actor class, ingress path ceiling)
    (policy.py; matrix + ceilings site-configurable in hub.json)
  * the approval flow (spec §10): above-authority commands are stored as
    evidence but DEFERRED; a human approves/denies via /v1/approvals,
    every step audited; hazardous always needs a human; nobody approves
    their own request
  * locked mode: hub.json "credentials" (token -> actor) makes identity
    come from the bearer token, never from a request body's claim

Hooks used: hub.gateway.policy (decide / on_defer / command_state),
hub.api_auth, API routes (/v1/approvals, /v1/approvals/{id}).

Enable: "extensions": ["authority"] in hub.json.
"""
from __future__ import annotations

import time
from typing import Optional

from uii.protocol import now_iso, uuid7

from .policy import AuthorityPolicy


class GatewayPolicy:
    """Adapter between the core gateway's policy hook and AuthorityPolicy,
    carrying the pending-approval machinery."""

    def __init__(self, hub, matrix: AuthorityPolicy):
        self.hub = hub
        self.matrix = matrix
        self.pending: dict[str, dict] = {}       # approval_id -> record
        self._by_command: dict[str, str] = {}    # command_id -> approval_id

    # -- the core gateway hook -------------------------------------------------

    def decide(self, actor: str, risk: str, ingress: str):
        decision = self.matrix.decide(actor, risk, ingress)
        if decision == "reject-path":
            return ("reject", "urn:uii:problem:path-ceiling",
                    f"risk '{risk}' exceeds the '{ingress}' path ceiling "
                    f"'{self.matrix.path_ceilings.get(ingress)}' — not "
                    f"approvable over this path")
        if decision == "approval":
            return ("defer", {"approval_id": uuid7()})
        return "allow"

    def on_defer(self, gateway, env):
        d = env["data"]
        self.pending[d["approval_id"]] = {
            "approval_id": d["approval_id"], "command_env": env,
            "module": d["target"], "type": d["type"], "risk": d["risk"],
            "requested_by": env["trace"]["actor"], "requested_at": now_iso(),
            "expires_at_mono": time.monotonic() + 300,
        }
        self._by_command[d["command_id"]] = d["approval_id"]
        gateway.store.append(
            "audit", {"event": "approval-pending",
                      "approval_id": d["approval_id"], "type": d["type"],
                      "risk": d["risk"], "requested_by": env["trace"]["actor"]},
            "urn:uii:schema:audit:0.1", module=d["target"],
            actor=env["trace"]["actor"],
            trace={"command_id": d["command_id"], "causation_id": env["id"],
                   "correlation_id": d["command_id"]})

    def command_state(self, command_id: str) -> Optional[str]:
        self._expire()
        return "pending-approval" if command_id in self._by_command else None

    # -- approvals ---------------------------------------------------------------

    def list_approvals(self) -> list[dict]:
        self._expire()
        return [{k: v for k, v in p.items()
                 if k not in ("command_env", "expires_at_mono")}
                | {"command_id": p["command_env"]["data"]["command_id"]}
                for p in self.pending.values()]

    def decide_approval(self, approval_id: str, decision: str,
                        approver: str) -> tuple[Optional[dict], Optional[dict]]:
        self._expire()
        gateway, store = self.hub.gateway, self.hub.store
        p = self.pending.get(approval_id)
        if not p:
            return None, {"type": "urn:uii:problem:not-found",
                          "detail": f"'{approval_id}' unknown, decided, or expired"}
        ok, why = self.matrix.may_approve(approver, p["requested_by"])
        if not ok:
            store.append("audit", {"event": "approval-refused",
                                   "approval_id": approval_id, "detail": why},
                         "urn:uii:schema:audit:0.1", module=p["module"],
                         actor=approver)
            return None, {"type": "urn:uii:problem:unauthorized-role",
                          "detail": why}
        env = p["command_env"]
        command_id = env["data"]["command_id"]
        self.pending.pop(approval_id, None)
        self._by_command.pop(command_id, None)
        granted = decision == "approve"
        store.append(
            "audit", {"event": "approval-granted" if granted else "approval-denied",
                      "approval_id": approval_id, "type": p["type"],
                      "risk": p["risk"], "requested_by": p["requested_by"]},
            "urn:uii:schema:audit:0.1", module=p["module"], actor=approver,
            trace={"command_id": command_id, "causation_id": env["id"],
                   "correlation_id": command_id})
        if granted:
            gateway.dispatch_env(p["module"], env)
        else:
            store.append(
                "result", {"status": "rejected",
                           "reason": f"approval denied by {approver}"},
                "urn:uii:schema:result:0.1", module=p["module"],
                trace={"command_id": command_id, "causation_id": env["id"],
                       "correlation_id": command_id})
        return {"approval_id": approval_id, "command_id": command_id,
                "decision": decision, "by": approver}, None

    def _expire(self):
        now = time.monotonic()
        for approval_id in [a for a, p in self.pending.items()
                            if now > p["expires_at_mono"]]:
            p = self.pending.pop(approval_id)
            self._by_command.pop(p["command_env"]["data"]["command_id"], None)
            self.hub.store.append(
                "result", {"status": "expired",
                           "reason": "approval not granted before expiry"},
                "urn:uii:schema:result:0.1", module=p["module"],
                trace={"command_id": p["command_env"]["data"]["command_id"],
                       "causation_id": p["command_env"]["id"],
                       "correlation_id": p["command_env"]["data"]["command_id"]})


def setup(hub):
    raw = hub.config.raw
    policy = GatewayPolicy(hub, AuthorityPolicy(raw.get("authority"),
                                                raw.get("path_ceilings")))
    hub.gateway.policy = policy
    hub.authority = policy   # embeddable access

    # -- locked mode: identity from credentials, not claims -----------------
    credentials = raw.get("credentials") or {}
    if credentials:
        def auth(handler):
            tok = handler.headers.get("Authorization", "")
            if tok.lower().startswith("bearer "):
                tok = tok[7:]
            actor = credentials.get(tok.strip())
            if not actor:
                handler._problem(401, "urn:uii:problem:unauthorized",
                                 "valid bearer token required (locked mode)")
                return None, False
            return actor, True
        hub.api_auth = auth

    # -- API routes ----------------------------------------------------------

    def get_approvals(handler, m, q):
        handler._json({"items": policy.list_approvals()})

    def post_approval(handler, m, body):
        decision = body.get("decision")
        if decision not in ("approve", "deny"):
            return handler._problem(400, "urn:uii:problem:validation",
                                    "decision must be approve|deny")
        summary, problem = policy.decide_approval(
            m.group(1), decision,
            body.get("_actor") or body.get("actor", "user:api"))
        if problem:
            code = 404 if problem["type"].endswith("not-found") else 403
            return handler._problem(code, problem["type"], problem["detail"])
        handler._json(summary)

    hub.api_get_routes.append((r"/v1/approvals", get_approvals))
    hub.api_post_routes.append((r"/v1/approvals/([\w-]+)", post_approval))
