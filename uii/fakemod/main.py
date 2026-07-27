"""fakemod — compatibility shim.

The fake module is no longer separate code: it is pimod (the real module
agent) with UII_SIM=1, so the software bench exercises the exact code that
ships to the integration bench. This entrypoint just sets sim defaults.

  python3 -m uii.fakemod.main   ==   UII_SIM=1 UII_SPEED=30 python3 -m uii.pimod.main
"""
from __future__ import annotations

import os


def main():
    os.environ.setdefault("UII_SIM", "1")
    os.environ.setdefault("UII_SPEED", "30")
    os.environ.setdefault("UII_MODULE_ID", os.environ.get("UII_MODULE_ID", "fm-0001"))
    from ..pimod.main import main as pimod_main
    pimod_main()


if __name__ == "__main__":
    main()
