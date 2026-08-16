"""scheduler extension — schedules attach to ROLES, not serials.

The "bam, ready" half of auto-recognition: the moment a module is adopted
into a role, the role's cadence resumes against it with zero keyboard
work — take control if the role says so, never sample uncalibrated
(cal-required event or auto_calibrate), then samples on interval. All
submissions go through the command gateway as actor system:scheduler.

Hooks used: hub.add_service (background thread); everything else is a
plain client of hub.gateway + hub.southbound.sessions.

Enable: "extensions": ["scheduler"] in hub.json. Cadence/policy fields
live on each role (sample_interval_s, auto_take_control, auto_calibrate,
calibrate_std_conc, cal_max_age_s).
"""
from .scheduler import Scheduler


def setup(hub):
    hub.add_service(Scheduler(hub.store, hub.southbound, hub.gateway,
                              speed=hub.speed))
