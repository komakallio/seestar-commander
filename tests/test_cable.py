"""Cable window: the RA axis only moves by MoveAxis inside the window, tracking is stopped before the
limit, and firmware gotos are never used."""
import pytest

from sequencer import astro
from sequencer import plan as P
from sequencer.service import HA_RATE_MIN

from conftest import ra_at_ha, wait_for


def test_map_ha_inside_window(make_service):
    sc = make_service().scope
    assert (sc.ha_u_min, sc.ha_u_max) == (-175.0, 175.0)
    assert sc.map_ha(170) == 170
    assert sc.map_ha(-170) == -170
    assert sc.map_ha(178) is None                   # the 10 deg behind the cable stop
    with pytest.raises(ValueError, match="not reachable"):
        sc.move_hadec(178, 30)


def test_moves_never_leave_the_window(make_service):
    svc = make_service(speed=400)
    sc, tel = svc.scope, svc.rig.sim.telescope
    tel.Unpark()
    for ha, dec in ((170, 60), (-170, 60), (0, 20), (-90, -80)):
        sc.move_hadec(ha, dec)
        assert abs(astro.wrap180(sc.position()["ha"] - ha)) < 0.5
    assert tel.firmware_gotos == 0
    assert not tel.cable_violation
    assert tel.phys_min < tel.u_min_seen and tel.u_max_seen < tel.phys_max
    # going from +170 to -170 must have gone the long (legal) way round through 0, not across 180
    assert tel.u_min_seen < -169 and tel.u_max_seen > 169


def test_target_stops_before_cable_limit(make_service):
    # window ends 6 minutes of tracking past HA 0 (cw limit 96.5 - margin 5 => u_max = 1.5)
    svc = make_service(cw_limit=96.5)
    pl = P.loads(f'[plan]\npark_at_end = false\n[[step]]\nname = "edge"\nra = {ra_at_ha(-0.0)}\ndec = 50\n'
                 'exposure = 10\ncount = 40\ncentre = "none"\n')
    assert svc.cmd_start(pl)["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done", 60)
    st = svc.status()["plan"]["steps"][0]
    assert st["status"] == "stopped" and "cable window" in st["reason"]
    assert 0 < st["frames"] < 40
    tel = svc.rig.sim.telescope
    assert not tel.Tracking
    assert svc.scope.ha_unwrapped() < svc.scope.ha_u_max
    assert not tel.cable_violation and tel.firmware_gotos == 0


def test_target_skipped_without_cable_room(make_service):
    svc = make_service(cw_limit=96.5)
    # HA about +1 (J2000 vs JNow) at the start: ~0.5 deg = 2 min of tracking, less than one frame plus the reserve
    pl = P.loads(f'[plan]\npark_at_end = false\n[[step]]\nra = {ra_at_ha(1.4)}\ndec = 50\nexposure = 60\n')
    svc.cmd_start(pl)
    assert wait_for(lambda: svc.status()["state"] == "done", 30)
    st = svc.status()["plan"]["steps"][0]
    assert st["status"] == "skipped" and "cable" in st["reason"]
    assert svc.rig.sim.telescope.mech_u == pytest.approx(-90, abs=0.01)    # never moved


def test_watchdog_stops_tracking_at_limit(make_service):
    svc = make_service(speed=600, cw_limit=96.5)
    sc, tel = svc.scope, svc.rig.sim.telescope
    tel.Unpark()
    sc.move_hadec(1.0, 50)
    tel.Tracking = True                      # tracking outside any job: only the watchdog can stop it
    assert wait_for(lambda: not tel.Tracking, 20)
    assert sc.ha_unwrapped() < sc.ha_u_max + 0.5 * HA_RATE_MIN
    assert any("CABLE WATCHDOG" in line for line in svc.status()["log"])


def test_guarded_park_aborts_if_firmware_takes_the_long_way(make_service):
    svc = make_service(speed=400)
    sc, tel = svc.scope, svc.rig.sim.telescope
    tel.Unpark()
    sc.move_hadec(-120, 0)        # 30 deg CCW of home: the sim firmware would turn ~330 deg the wrong way
    sc.move_hadec = lambda *a, **k: sc.position()   # simulate skipping the axis move to home
    r = sc.park_guarded(guard_deg=10)
    assert not r["parked"] and r["aborted"]
    assert not tel.cable_violation and tel.u_min_seen > -135


def test_guarded_park_from_target(make_service):
    svc = make_service(speed=400)
    sc, tel = svc.scope, svc.rig.sim.telescope
    tel.Unpark()
    sc.move_hadec(40, 30)
    tel.Tracking = True
    r = sc.park_guarded()
    assert r["parked"] and not r["aborted"] and r["max_dev"] < 3
    assert tel.AtPark and tel.firmware_gotos == 0
