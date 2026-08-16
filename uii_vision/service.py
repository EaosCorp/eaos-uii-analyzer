"""FoamService — the telemetry half of the vision interpreter.

Interpreters fire only on a command RESULT, so the streamed-frame path needs
its own driver. FoamService subscribes to the evidence fan-out (the same
mechanism the detections engine uses) and, for every raw `frames` observation
that arrives on its own (no command_id, i.e. the cadence stream), runs foam
CV and appends foam_coverage. Command-tied frames are skipped here; the
vision interpreter owns those. One frame, one interpretation, either way.
"""
from __future__ import annotations

import queue
import threading

from .interpreter import interpret_frame_env


class FoamService(threading.Thread):
    def __init__(self, store, southbound, blobs, state, tick_s=0.2):
        super().__init__(daemon=True)
        self.store = store
        self.southbound = southbound
        self.blobs = blobs
        self.state = state
        self.tick_s = tick_s
        self.stop = threading.Event()      # required by Hub.stop()
        self.interpreted = 0
        self.errors = 0
        self.q = store.subscribe()

    def _lookup(self, module_id):
        return self.southbound.sessions.get(module_id)

    def run(self):
        try:
            while not self.stop.is_set():
                try:
                    env = self.q.get(timeout=self.tick_s)
                except queue.Empty:
                    continue
                try:
                    cv = interpret_frame_env(self.store, self.blobs, self.state,
                                             env, session_lookup=self._lookup)
                    if cv is not None:
                        self.interpreted += 1
                except Exception:
                    # a bad frame is never fatal to the stream
                    self.errors += 1
        finally:
            self.store.unsubscribe(self.q)
