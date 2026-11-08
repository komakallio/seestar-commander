"""Safety loop: unsafe -> abort exposure -> guarded park -> on_parked hook -> roof close *requested*;
the roof only closes on the operator's command, and only with the scope parked."""
from sequencer import plan as P

from conftest import ra_at_ha, wait_for

LONG = f"""
[plan]
park_at_end = false
[[step]]
name = "long"
ra = {ra_at_ha(-20)}
dec = 45
exposure = 120
count = 30
centre = "none"
"""


def _in_exposure(svc):
    return svc.status()["phase"] == "lights" and svc.imager.exposing


def test_unsafe_parks_and_requests_roof(make_service):
    svc = make_service(speed=100)
    sim = svc.rig.sim
    assert svc.cmd_start(P.loads(LONG))["ok"]
    assert wait_for(lambda: _in_exposure(svc), 30)
    sim.safety.set(False, ["RAIN"])
    assert wait_for(lambda: svc.status()["state"] == "parked" and svc.idle(), 60)
    st = svc.status()
    assert sim.camera.aborts >= 1                              # exposure aborted
    assert sim.telescope.AtPark and sim.telescope.firmware_gotos == 0
    assert "http://hooks.invalid/parked" in svc.rig.hooks_posted
    assert st["roof"]["close_requested"] and "RAIN" in st["roof"]["reason"]
    assert sim.dome.close_calls == 0                           # never closes by itself
    assert st["plan"]["steps"][0]["status"] == "interrupted"
    assert st["safety"]["safe"] is False

    # supervised: resume refused while unsafe
    assert not svc.cmd_resume()["ok"]
    # operator closes the roof
    assert svc.cmd_roof_close()["ok"]
    assert wait_for(lambda: svc.idle() and sim.dome.ShutterStatus == 1, 30)
    assert sim.dome.close_calls == 1
    assert not svc.status()["roof"]["close_requested"]

    # safe again: still nothing restarts until resume
    sim.safety.set(True)
    assert wait_for(lambda: svc.status()["safety"]["safe"] is True, 20)
    assert svc.status()["state"] == "parked"
    sim.dome.OpenShutter()
    assert svc.cmd_resume()["ok"]
    assert wait_for(lambda: _in_exposure(svc), 30)              # unparked, moved, imaging again
    assert "http://hooks.invalid/unparked" in svc.rig.hooks_posted
    svc.cmd_stop()
    assert wait_for(svc.idle, 20)


def test_roof_close_refused_unless_parked(make_service):
    svc = make_service()
    svc.rig.sim.telescope.Unpark()
    r = svc.cmd_roof_close()
    assert not r["ok"] and "not parked" in r["msg"]
    assert svc.rig.sim.dome.close_calls == 0


def test_no_roof_configured(make_service):
    svc = make_service(roof=False, speed=100)
    svc.cmd_start(P.loads(LONG))
    assert wait_for(lambda: _in_exposure(svc), 30)
    svc.rig.sim.safety.set(False, ["WIND"])
    assert wait_for(lambda: svc.status()["state"] == "parked" and svc.idle(), 60)
    assert not svc.status()["roof"]["close_requested"]
    assert not svc.cmd_roof_close()["ok"]


def test_operator_park_and_stop(make_service):
    svc = make_service(speed=100)
    svc.cmd_start(P.loads(LONG))
    assert wait_for(lambda: _in_exposure(svc), 30)
    svc.cmd_stop()
    assert wait_for(lambda: svc.status()["state"] == "stopped" and svc.idle(), 20)
    assert not svc.rig.sim.telescope.AtPark                 # stop does not park
    assert svc.cmd_park()["ok"]
    assert wait_for(lambda: svc.status()["state"] == "parked" and svc.idle(), 60)
    assert svc.rig.sim.telescope.AtPark


def test_unsafe_while_parked_just_requests_roof(make_service):
    svc = make_service()
    svc.rig.sim.safety.set(False, ["RAIN"])
    assert wait_for(lambda: svc.status()["roof"]["close_requested"], 20)
    assert wait_for(svc.idle, 20)
    assert svc.rig.hooks_posted == []                       # already parked: no new park, no hook
    assert svc.rig.sim.dome.close_calls == 0
