"""Hub entrypoint.

  UII_HUB_ID     hub identity           (default hub-dev-001)
  UII_DB         evidence sqlite path   (default ./data/evidence.db)
  UII_SB_PORT    southbound TCP port    (default 7300)
  UII_API_PORT   HTTP API port          (default 8400)
  UII_ALLOWED    comma list of adoptable module types
                 (default fake-nh4,fake-po4,jarbalyzer-nh4)
"""
from __future__ import annotations

import asyncio
import os

from .api import serve_api
from .commands import CommandGateway
from .evidence import EvidenceStore
from .southbound import SouthboundHub


def main():
    hub_id = os.environ.get("UII_HUB_ID", "hub-dev-001")
    db = os.environ.get("UII_DB", "./data/evidence.db")
    sb_port = int(os.environ.get("UII_SB_PORT", 7300))
    api_port = int(os.environ.get("UII_API_PORT", 8400))
    allowed = set(os.environ.get(
        "UII_ALLOWED", "fake-nh4,fake-po4,jarbalyzer-nh4").split(","))

    os.makedirs(os.path.dirname(db) or ".", exist_ok=True)
    store = EvidenceStore(db, hub_id)
    southbound = SouthboundHub(store, allowed)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    gateway = CommandGateway(store, southbound, loop)
    serve_api(store, southbound, gateway, "0.0.0.0", api_port)

    store.append("event", {"event": "hub-started", "api_port": api_port,
                           "southbound_port": sb_port},
                 "urn:uii:schema:event:0.1", actor="system:hub")
    print(f"[hub] {hub_id} · api :{api_port} · southbound :{sb_port} · db {db}")
    loop.run_until_complete(southbound.serve("0.0.0.0", sb_port))


if __name__ == "__main__":
    main()
