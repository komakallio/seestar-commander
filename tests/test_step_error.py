"""An unexpected error in a step (here a driver refusal on the slew) ends the plan in state error with the
step marked failed, not left "running" for a client to wait on; resume retries it."""
from sequencer import plan as P

from conftest import ra_at_ha, wait_for

PLAN = f"""
[plan]
park_at_end = false
[[step]]
name = "field"
ra = {ra_at_ha(-20)}
dec = 45
exposure = 10
count = 1
centre = "none"
"""


def test_error_marks_step_failed(make_service):
    svc = make_service(speed=300)
    real = svc.scope.move_radec

    def refuse(*a, **k):
        raise RuntimeError("SET_SCOPE_SET_TRACK_STATE fail: below the horizon (Error Code: 0x4ff)")
    svc.scope.move_radec = refuse
    assert svc.cmd_start(P.loads(PLAN))["ok"]
    assert wait_for(lambda: svc.status()["state"] == "error" and not svc.busy, 60)
    st = svc.status()["plan"]["steps"][0]
    assert st["status"] == "failed" and "0x4ff" in st["reason"]
    svc.scope.move_radec = real
    assert svc.cmd_resume()["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done", 90)
    assert svc.status()["plan"]["steps"][0]["status"] == "done"


class _Clock:
    def __init__(self):
        self.t = 0.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


class _Mount:
    """Coasts on for a few reads after the stop; refuses tracking on while it moves (as the S30 did)."""
    def __init__(self, coast_reads):
        self.coast, self.u, self.Declination, self.sets = coast_reads, 0.0, 6.5, []

    def read(self):
        if self.coast > 0:
            self.coast -= 1
            self.u -= 2.0
        return self.u

    @property
    def Tracking(self):
        return bool(self.sets and self.sets[-1])

    @Tracking.setter
    def Tracking(self, v):
        if v and self.coast > 0:
            self.sets.append(None)
            raise RuntimeError("SET_SCOPE_SET_TRACK_STATE fail (Error Code: 0x4ff)")
        self.sets.append(v)


def _fake(coast_reads):
    from types import SimpleNamespace
    from tools.scope import Scope
    m = _Mount(coast_reads)
    f = SimpleNamespace(t=m, clock=_Clock(), ha_unwrapped=m.read)
    f._stopped = Scope._stopped.__get__(f)
    f.track_on = Scope.track_on.__get__(f)
    return f, m


def test_track_on_waits_for_the_axis_to_stop():
    f, m = _fake(coast_reads=3)
    assert f._stopped() is False                            # it moved on after the stop
    f.track_on()
    assert m.sets == [True]                                 # sent once, after the coast: no refusal


def test_track_on_is_immediate_when_stopped():
    f, m = _fake(coast_reads=0)
    f.track_on()
    assert m.sets == [True] and f.clock.t < 1.0
