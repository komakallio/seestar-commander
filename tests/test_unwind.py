"""Unwind and re-acquire: at the cable limit the RA axis turns a full turn back (closed-loop MoveAxis,
never a goto) to the same sky position, re-centres and the target goes on."""
import pytest

from sequencer import statusview
from sequencer import plan as P
from sequencer import unwind as unw
from sequencer.service import HA_RATE_MIN

from conftest import ra_at_ha, wait_for

R = HA_RATE_MIN


def _plan(extra="", ha=-1.0, count=30, centre="auto", dec=50):
    return P.loads(f'[plan]\npark_at_end = false\n[[step]]\nname = "edge"\nra = {ra_at_ha(ha)}\ndec = {dec}\n'
                   f'exposure = 10\ncount = {count}\ncentre = "{centre}"\n{extra}')


def _step(svc):
    return svc.status()["plan"]["steps"][0]


def _window_kept(svc):
    sc, tel = svc.scope, svc.rig.sim.telescope
    assert not tel.cable_violation and tel.firmware_gotos == 0
    # the mechanical angle differs from the bookkeeping by the sim's index error (1.2 deg, hidden by a sync)
    assert sc.ha_u_min - 2 < tel.u_min_seen and tel.u_max_seen < sc.ha_u_max + 2


# ---- window maths ---------------------------------------------------------------------------------
def _all_sky(t, n):
    return n


def test_options_window_wider_than_a_turn():
    # window -385..1.5: one turn back is inside straight away
    o = unw.options(0.8, -385, 1.5, R, need_min=25 / 60, reserve_min=2, todo=10, now=0, max_wait_min=60,
                    sky_frames=_all_sky)
    assert len(o) == 1 and o[0].turns == 1 and o[0].u_now == pytest.approx(-359.2)
    assert o[0].wait_s == 0 and o[0].frames == 10
    assert o[0].move_s == pytest.approx(360 / 5 + unw.MOVE_EXTRA_S)
    assert unw.choose(o, 10) is o[0]


def test_options_window_narrower_than_a_turn_waits_for_the_gap():
    # -175..175 (350 deg): u - 360 = -185.8 is 10.8 deg below the CCW end, plus the slack
    u = 174.2
    o = unw.options(u, -175, 175, R, need_min=75 / 60, reserve_min=2, todo=40, now=0, max_wait_min=60,
                    sky_frames=_all_sky)
    assert len(o) == 1
    gap = -175 + unw.SLACK_DEG - (u - 360)
    assert o[0].wait_s == pytest.approx(gap / R * 60)
    assert o[0].frames == 40
    assert unw.options(u, -175, 175, R, 75 / 60, 2, 40, 0, max_wait_min=30, sky_frames=_all_sky) == []
    assert unw.choose([], 40) is None


def test_choose_fewest_turns_that_fit_else_most_room():
    # a 900 deg window: one turn back has ~1430 min of tracking (143 frames of 10 min), two turns back ~2870
    kw = dict(u_min=-800, u_max=100, rate_deg_min=R, need_min=10, reserve_min=2, now=0, max_wait_min=0,
              sky_frames=_all_sky)
    o = unw.options(99, todo=200, **kw)
    assert [x.turns for x in o] == [1, 2] and [x.frames for x in o] == [143, 200]
    assert unw.choose(o, 200).turns == 2                 # all 200 only fit two turns back
    o = unw.options(99, todo=100, **kw)
    assert unw.choose(o, 100).turns == 1                 # 100 fit one turn back: the shorter move
    # the sky allows only 3 frames whatever the cable room: still the option with the most frames
    o = unw.options(99, todo=30, **{**kw, "sky_frames": lambda t, n: min(n, 3)})
    assert unw.choose(o, 30).frames == 3


def test_map_ha_near_picks_the_turn():
    from scope import Scope                               # tools/ is on sys.path (conftest)
    sc = Scope.__new__(Scope)
    sc.ha_u_min, sc.ha_u_max = -385.0, 1.5
    assert sc.map_ha(0.5) == 0.5
    assert sc.map_ha(0.5, near=-360) == -359.5
    assert sc.map_ha(0.5, near=0) == 0.5
    sc.ha_u_min = -175.0
    assert sc.map_ha(0.5, near=-360) is None             # outside the window: refused before moving


def test_plan_options_parse():
    st = _plan().steps[0]
    assert st.unwind and st.unwind_min_frames == 3 and st.unwind_max_wait_min == 60
    st = _plan("unwind = false\nunwind_min_frames = 5\nunwind_min_minutes = 20\nunwind_max_wait_min = 0\n").steps[0]
    assert (st.unwind, st.unwind_min_frames, st.unwind_min_minutes, st.unwind_max_wait_min) == (False, 5, 20, 0)
    with pytest.raises(P.PlanError):
        _plan("unwind_min_frames = 0\n")


def test_headline():
    a = {"kind": "unwind", "target": "M45", "turns": 1}
    assert statusview.headline({"action": a, "state": "running"}, now=0) == "Unwinding cable (M45)"
    s = statusview.headline({"action": {**a, "until": 600}, "state": "running"}, now=0)
    assert s.startswith("Unwinding cable (M45): waiting 10 min")


# ---- simulator -----------------------------------------------------------------------------------
def test_unwind_and_reacquire(make_service):
    # window -385..1.5 (cw 96.5, ccw 300): the target hits the CW end after a few minutes, one turn back fits
    svc = make_service(cw_limit=96.5, ccw_limit=300)
    assert svc.cmd_start(_plan("dither_every = 2\ndither_px = 10\n"))["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done", 90)
    st = _step(svc)
    assert st["status"] == "done" and st["frames"] == 30 and st.get("unwinds") == 1
    log = svc.status()["log"]
    assert any("UNWINDING CABLE" in ln for ln in log)
    assert any("unwound to unwrapped HA" in ln for ln in log)
    # back on target after the turn: re-centred by plate solve, the bookkeeping a turn back
    assert st["centre"]["method"] == "solve" and st["centre"]["solved"]
    assert st["centre"]["offset_arcmin"] <= 3
    assert svc.scope.ha_unwrapped() < -350
    _window_kept(svc)
    tel = svc.rig.sim.telescope
    assert tel.u_min_seen < -355                          # it really went the full turn back


def test_unwind_waits_for_the_gap_in_a_narrow_window(make_service):
    # window -345..1.5 (346.5 deg): one turn back lands 15 deg below the CCW end; wait for the sky
    svc = make_service(speed=600, cw_limit=96.5, ccw_limit=260)
    assert svc.cmd_start(_plan("unwind_max_wait_min = 90\nunwind_min_minutes = 2\n", count=24, centre="none"))["ok"]
    assert wait_for(lambda: svc.status()["headline"].startswith("Unwinding cable (edge): waiting"), 60)
    assert not svc.rig.sim.telescope.Tracking            # waiting with tracking off at the CCW end
    assert wait_for(lambda: svc.status()["state"] == "done", 90)
    st = _step(svc)
    assert st["status"] == "done" and st["frames"] == 24 and st.get("unwinds") == 1
    assert any("waiting" in ln and "UNWINDING" in ln for ln in svc.status()["log"])
    assert svc.scope.ha_u_min < svc.scope.ha_unwrapped() < -330
    _window_kept(svc)


def test_no_unwind_when_the_gap_is_too_long(make_service):
    svc = make_service(cw_limit=96.5, ccw_limit=260)
    assert svc.cmd_start(_plan("unwind_max_wait_min = 0\n", centre="none"))["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done", 60)
    st = _step(svc)
    assert st["status"] == "stopped" and "cable window" in st["reason"] and "no unwind" in st["reason"]
    assert "unwinds" not in st and not svc.rig.sim.telescope.Tracking
    _window_kept(svc)


def test_no_unwind_for_too_few_frames(make_service):
    svc = make_service(cw_limit=96.5, ccw_limit=300)
    assert svc.cmd_start(_plan("unwind_min_frames = 100\n", centre="none"))["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done", 60)
    st = _step(svc)
    assert st["status"] == "stopped" and "unwind_min_frames" in st["reason"]
    assert svc.scope.ha_unwrapped() > -10                  # never turned back


def test_unwind_false_stops_as_before(make_service):
    svc = make_service(cw_limit=96.5, ccw_limit=300)
    assert svc.cmd_start(_plan("unwind = false\n", centre="none"))["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done", 60)
    st = _step(svc)
    assert st["status"] == "stopped" and "unwind = false" in st["reason"]


def test_stop_during_unwind(make_service):
    svc = make_service(speed=600, cw_limit=96.5, ccw_limit=260)
    assert svc.cmd_start(_plan("unwind_max_wait_min = 90\nunwind_min_minutes = 2\n", centre="none"))["ok"]
    assert wait_for(lambda: svc.status()["headline"].startswith("Unwinding cable"), 60)
    svc.cmd_stop()
    assert wait_for(lambda: svc.status()["state"] == "stopped", 30)
    assert _step(svc)["status"] == "interrupted"
    tel = svc.rig.sim.telescope
    assert not tel.Tracking and not tel.Slewing
    _window_kept(svc)
