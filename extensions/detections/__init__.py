"""detections extension — the hub's own configurable status engine.

Rules declared in hub.json ("detections": [...]) are evaluated
continuously over the evidence stream; alerts come back OUT as evidence,
acknowledge is audited, and each module's active alerts roll up to one
NAMUR NE107-style status. Includes the health watchdog (quiet module ->
DEGRADED). Full design + research grounding: docs/detections.md.

Hooks used: hub.store.subscribe() (the engine watches the stream),
hub.add_service (background thread), API routes (/v1/alerts,
/v1/alerts/ack, /v1/health), module-row enricher (NE107 "status" column).

Enable: "extensions": ["detections"] in hub.json.
"""
from .engine import Detections


def setup(hub):
    engine = Detections(hub.store, hub.southbound,
                        hub.config.raw.get("detections") or [],
                        speed=hub.speed)
    hub.add_service(engine)
    hub.detections = engine   # embeddable access (tests, demos)

    hub.module_row_enrichers.append(
        lambda s: {"status": engine.ne107_status(s.module_id)})

    def get_alerts(handler, m, q):
        handler._json({"items": engine.active_alerts()})

    def get_health(handler, m, q):
        handler._json({
            "hub": hub.store.hub_id,
            "modules": [
                {"id": s.module_id, "state": s.state,
                 "status": engine.ne107_status(s.module_id),
                 "alerts": [a for a in engine.active_alerts()
                            if a["module"] == s.module_id]}
                for s in hub.southbound.sessions.values()]})

    def post_ack(handler, m, body):
        ok = engine.ack(body.get("rule", ""), body.get("module", ""),
                        actor=body.get("_actor") or body.get("actor", "user:api"))
        if not ok:
            return handler._problem(404, "urn:uii:problem:not-found",
                                    "no such active un-acked alert")
        handler._json({"acked": body.get("rule"), "module": body.get("module")})

    hub.api_get_routes.append((r"/v1/alerts", get_alerts))
    hub.api_get_routes.append((r"/v1/health", get_health))
    hub.api_post_routes.append((r"/v1/alerts/ack", post_ack))
