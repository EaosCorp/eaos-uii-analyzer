"""Authority — who may run what, arriving how.

The rule (reference architecture §2): **effective authority is
min(actor class, ingress path ceiling)**. Both dimensions are config, not
code, and neither is agent-specific: the same matrix protects against a
fat-fingered human, a confused scheduler, and an over-eager agent
identically.

Dimension 1 — actor class, from the trace.actor prefix:

  user:ali            a human
  agent:eddy-om@site  an AI agent
  system:scheduler    configured automation (hub.json IS its standing
                      human approval: an engineer wrote auto_calibrate)
  plc:blue-north      the plant, via the OT adapter

Each class has a maximum risk it may execute ALONE (defaults below,
overridable per site in hub.json "authority"). Above that, the command
needs an approval envelope from a human (spec §10). `hazardous` always
needs approval, no matter who asks — that is structural, not config.

Dimension 2 — ingress path ceiling ("permissions by connectivity"):

  local     the hub's own API/CLI (bench, SSH, on-prem network)
  ot        request tags from the PLC plane
  cloud     northbound command channel
  cellular  remote service path

A path's ceiling is ABSOLUTE: a command whose risk exceeds the ceiling of
the path it arrived on is rejected outright — approval cannot launder it.
(Reference architecture: cellular is service/agent authority, never plant
control, regardless of credential.) Today every API request is "local";
the API server is the place that will stamp real ingress (which NIC /
overlay a request arrived on) when the other paths exist.
"""
from __future__ import annotations

from typing import Optional

RISK_ORDER = {"info": 0, "routine": 1, "disruptive": 2, "hazardous": 3}

# max risk an actor class may execute without an approval envelope
DEFAULT_AUTHORITY = {
    "user": "disruptive",
    "agent": "routine",
    "system": "disruptive",
    "plc": "routine",
}

# max risk that may even be REQUESTED via an ingress path (absolute)
DEFAULT_PATH_CEILINGS = {
    "local": "hazardous",
    "ot": "routine",
    "cloud": "disruptive",
    "cellular": "routine",
}


def actor_class(actor: str) -> str:
    cls = (actor or "").split(":", 1)[0]
    return cls if cls in DEFAULT_AUTHORITY else "agent"   # least privilege


class AuthorityPolicy:
    def __init__(self, authority: Optional[dict] = None,
                 path_ceilings: Optional[dict] = None):
        self.authority = {**DEFAULT_AUTHORITY, **(authority or {})}
        self.path_ceilings = {**DEFAULT_PATH_CEILINGS, **(path_ceilings or {})}

    def decide(self, actor: str, risk: str, ingress: str = "local") -> str:
        """Returns 'allow' | 'approval' | 'reject-path'."""
        risk_rank = RISK_ORDER.get(risk, RISK_ORDER["routine"])
        ceiling = self.path_ceilings.get(ingress, "routine")
        if risk_rank > RISK_ORDER.get(ceiling, 1):
            return "reject-path"
        if risk == "hazardous":
            return "approval"                    # always, structurally
        allowed = self.authority.get(actor_class(actor), "routine")
        if risk_rank <= RISK_ORDER.get(allowed, 1):
            return "allow"
        return "approval"

    def may_approve(self, approver: str, requester: str) -> tuple[bool, str]:
        """Approvals come from humans; a human cannot approve their own
        request (two-person rule for anything a user couldn't run alone)."""
        if actor_class(approver) != "user":
            return False, f"approver must be a human (user:*), got '{approver}'"
        if actor_class(requester) == "user" and approver == requester:
            return False, "requester cannot approve their own command"
        return True, ""
