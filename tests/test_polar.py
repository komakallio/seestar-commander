"""Polar alignment against the simulator's polar-misalignment model: three-point measurement, the live
adjust loop with simulated knob turns, the cable window, safety and the API."""
import json
import os

import numpy as np
import pytest

from sequencer import polar as pm
from sequencer.httpapi import serve

from conftest import LAT, wait_for
from test_http import _base, _req

ERR = (45.0, -70.0)          # arcmin: axis too high and west of the pole (about the first real run's 1.4 deg)


def _polar_service(make_service, err=ERR, **settings):
    polar = settings.pop("polar", {})
    # a gentler speed-up than the other tests: a thread stall of 50 ms is 6 s of simulated time here
    settings.setdefault("speed", 120.0)
    return make_service(world={"polar_error": err}, extra={"polar": {"adjust_timeout_min": 30, **polar}}, **settings)


def _wait_frames(svc, n, timeout=60):
    assert wait_for(lambda: (svc.polar.status().get("live") or {}).get("frame", 0) >= n
                    or svc.polar.status()["state"] in ("error", "stopped"), timeout), svc.polar.status()["msg"]
    st = svc.polar.status()
    assert st["state"] == "adjusting", (st["state"], st["msg"])
    return st


def _record_moves(svc):
    tel, moves = svc.rig.sim.telescope, []
    orig = tel.MoveAxis

    def move(axis, rate):
        moves.append((int(axis), float(rate), len(svc.polar.st["points"])))
        return orig(axis, rate)
    tel.MoveAxis = move
    return moves


# ---- geometry ------------------------------------------------------------------------------------

def test_error_roundtrip_and_correction():
    for alt, az in ((45.0, -70.0), (-12.0, 30.0), (0.0, 0.0)):
        A = pm.axis_from_error(alt, az, LAT)
        e = pm.axis_error(A, LAT)
        assert e["alt_arcmin"] == pytest.approx(alt, abs=1e-6)
        assert e["az_arcmin"] == pytest.approx(az, abs=1e-6)
        assert pm.mount_rotation(alt, az, LAT) @ pm.pole_vector(LAT) == pytest.approx(A, abs=1e-12)
        assert pm.correction_rotation(A, LAT) @ A == pytest.approx(pm.pole_vector(LAT), abs=1e-12)


def test_refracted_pole_and_words():
    assert float(pm.altaz_of(pm.pole_vector(60.0))[0]) == pytest.approx(60.0 + 0.55 / 60, abs=0.1 / 60)
    assert float(pm.true_alt(pm.apparent_alt(20.0))) == pytest.approx(20.0, abs=1e-9)
    cfg = pm.PolarConfig(LAT)
    w = pm.describe({"alt_arcmin": 30.0, "az_arcmin": -20.0}, cfg)
    assert (w["alt"]["word"], w["az"]["word"]) == ("lower", "turn east") and w["alt"]["knob"] is None
    cfg.alt_knob_sign, cfg.az_knob_sign = 1, -1
    w = pm.describe({"alt_arcmin": 30.0, "az_arcmin": -20.0}, cfg)
    assert (w["alt"]["knob"], w["az"]["knob"]) == ("counter-clockwise", "counter-clockwise")
    assert pm.describe({"alt_arcmin": 1.0, "az_arcmin": -1.5}, cfg)["az"]["word"] == "ok"


def test_fit_axis_and_knobs_exact():
    A = pm.axis_from_error(*ERR, LAT)
    v0 = pm.rot(np.cross(A, pm.UP), 25.0) @ A            # a direction 25 deg from the axis
    du = [-30.0, 0.0, 30.0]
    vs = [pm.rot(A, -u) @ v0 for u in du]
    f = pm.fit_axis(vs, du, LAT)
    assert np.degrees(np.arccos(min(1.0, f["axis"] @ A))) * 60 < 1e-6
    assert f["sign"] == -1 and f["angle_residual_arcmin"] < 1e-6
    Q = pm.knob_rotation(-0.4, 0.9, A)
    x, smin = pm.fit_knobs(vs[-1], Q @ vs[-1], A)
    assert x == pytest.approx([-0.4, 0.9], abs=1e-7) and smin > 0.1


# ---- in the simulator ----------------------------------------------------------------------------

@pytest.mark.parametrize("err", [ERR, (-25.0, 35.0)])
def test_measures_known_error(make_service, err):
    svc = _polar_service(make_service, err)
    moves = _record_moves(svc)
    r = svc.polar.cmd_start()
    assert r["ok"], r
    assert wait_for(lambda: svc.polar.status()["measured"] is not None or svc.polar.status()["state"] == "error", 60)
    m = svc.polar.status()["measured"]
    assert m["alt_arcmin"] == pytest.approx(err[0], abs=2.0)
    assert m["az_arcmin"] == pytest.approx(err[1], abs=2.0)
    assert m["angle_residual_arcmin"] < 1.0 and m["span_deg"] == pytest.approx(60, abs=1)
    assert m["words"]["alt"]["move"] == ("lower" if err[0] > 0 else "raise")
    assert m["words"]["az"]["move"] == ("turn west" if err[1] > 0 else "turn east")
    svc.polar.cmd_stop()
    assert wait_for(svc.idle, 20)
    tel = svc.rig.sim.telescope
    # cable safety: MoveAxis only, inside +-30 deg of the start (home, -90), only RA after the first point
    assert tel.firmware_gotos == 0 and not tel.cable_violation and tel.calls == []
    assert -121 < tel.u_min_seen and tel.u_max_seen < -59
    assert moves and not [m for m in moves if m[0] == 1 and m[1] != 0 and m[2] >= 1]
    assert svc.status()["state"] == "idle"


@pytest.mark.parametrize("track", [True, False])
def test_knobs_reduce_live_error(make_service, track):
    svc = _polar_service(make_service, polar={"track_during_adjust": track})
    tel = svc.rig.sim.telescope
    assert svc.polar.cmd_start()["ok"]
    st = _wait_frames(svc, 2)
    live0, tgt0 = st["live"], st["target"]
    assert live0["tracking"] is track
    assert live0["alt_arcmin"] == pytest.approx(ERR[0], abs=2.5) and live0["az_arcmin"] == pytest.approx(ERR[1], abs=2.5), \
        (st, svc.status()["log"][-30:])
    assert tgt0 and tgt0["distance_px"] > 20 and os.path.exists(os.path.join(svc.out, "polar_crop.jpg"))
    # the operator follows the words part of the way: lower by 30', turn east by 50'
    tel.adjust_polar(-30.0, 50.0)
    st = _wait_frames(svc, live0["frame"] + 2)
    live1 = st["live"]
    assert live1["alt_arcmin"] == pytest.approx(15.0, abs=2.5) and live1["az_arcmin"] == pytest.approx(-20.0, abs=2.5), \
        (live0, live1, st["history"], [x for x in svc.status()["log"] if "polar" in x or "SIM" in x])
    assert live1["total_arcmin"] < 0.4 * live0["total_arcmin"]
    assert st["history"][-1]["total"] == live1["total_arcmin"] and len(st["history"]) >= 4
    if track:
        # the star moves onto the reticle predicted before the turn (sky fixed in the frame while tracking)
        tel.set_polar(0.0, 0.0)
        st = _wait_frames(svc, live1["frame"] + 2)
        sx, sy = st["target"]["star"]
        rx, ry = tgt0["reticle"]
        assert np.hypot(sx - rx, sy - ry) < 0.2 * tgt0["distance_px"]
        assert st["live"]["total_arcmin"] < 2.5, (st["history"], st["live"], st["msg"])
        assert st["live"]["words"]["alt"]["ok"] or abs(st["live"]["alt_arcmin"]) < 2.5
    assert svc.polar.cmd_done()["ok"]
    assert wait_for(svc.idle, 20)
    st = svc.polar.status()
    assert st["state"] == "done" and st["final"]["total_arcmin"] == st["live"]["total_arcmin"]
    with open(os.path.join(svc.s.data_dir, "polar", "results.jsonl")) as fh:
        rec = json.loads(fh.readlines()[-1])
    assert rec["time"].endswith("Z") and rec["initial"]["total_arcmin"] > 50
    assert not tel.Tracking and tel.firmware_gotos == 0 and not tel.cable_violation


def test_too_narrow_window_is_refused(make_service):
    svc = _polar_service(make_service, cw_limit=96.5)          # unwrapped HA window -175 .. +1.5
    sc = svc.scope
    svc.rig.sim.telescope.Unpark()
    sc.move_hadec(-20, 60)
    r = svc.polar.cmd_start()
    assert not r["ok"] and "cable window" in r["msg"] and "+-30" in r["msg"]
    assert svc.idle()
    r = svc.polar.cmd_start({"span_deg": 8})                     # -28 .. -12 fits
    assert r["ok"] and max(r["u"]) <= sc.ha_u_max
    assert wait_for(lambda: svc.polar.status()["measured"] is not None, 60)
    svc.polar.cmd_stop()
    assert wait_for(svc.idle, 20)
    tel = svc.rig.sim.telescope
    assert tel.u_max_seen < sc.ha_u_max and tel.firmware_gotos == 0


def test_low_points_refused(make_service):
    svc = _polar_service(make_service)
    r = svc.polar.cmd_start({"dec": 5})
    assert not r["ok"] and "Dec" in r["msg"]


def test_exclusive_with_plans_and_unsafe_parks(make_service):
    from sequencer import plan as P
    svc = _polar_service(make_service)
    assert svc.polar.cmd_start()["ok"]
    assert not svc.polar.cmd_start()["ok"]                     # one at a time
    assert wait_for(lambda: svc.busy == "polar", 10)
    pl = P.loads('[[step]]\nra = 10\ndec = 40\nexposure = 10\ncount = 1\ncentre = "none"\n')
    assert not svc.cmd_start(pl)["ok"]
    _wait_frames(svc, 1)
    svc.rig.sim.safety.set(False, ["RAIN"])
    assert wait_for(lambda: svc.status()["state"] == "parked" and svc.idle(), 60)
    assert svc.polar.status()["state"] == "stopped"
    assert svc.rig.sim.telescope.AtPark and svc.rig.sim.telescope.firmware_gotos == 0


def test_polar_api(make_service):
    svc = _polar_service(make_service, api_token="tok")
    srv, base = _base(svc)
    try:
        code, page = _req(base, "GET", "/polar")
        assert code == 200 and b"Polar alignment" in page and b"reticle" in page
        assert _req(base, "POST", "/api/polar/start")[0] == 401
        code, body = _req(base, "POST", "/api/polar/start", {"span_deg": 25}, token="tok")
        assert code == 200 and json.loads(body)["ok"]
        assert wait_for(lambda: (json.loads(_req(base, "GET", "/api/polar")[1]).get("live") or {}).get("frame", 0) >= 1, 60)
        st = json.loads(_req(base, "GET", "/api/polar")[1])
        assert st["state"] == "adjusting" and st["sim_truth"]["alt_arcmin"] == ERR[0]
        assert isinstance(st["target"]["star"][0], float)
        assert _req(base, "GET", "/img/polar_crop.jpg")[0] == 200
        code, body = _req(base, "POST", "/api/sim/polar", {"dalt": -5, "daz": 5}, token="tok")
        assert json.loads(body)["alt_arcmin"] == ERR[0] - 5
        code, body = _req(base, "POST", "/api/polar/done", token="tok")
        assert json.loads(body)["ok"] and json.loads(body)["final"]["source"] == "live"
        assert wait_for(svc.idle, 20)
        code, body = _req(base, "POST", "/api/polar/stop", token="tok")
        assert json.loads(body)["ok"]
        assert json.loads(_req(base, "GET", "/api/polar")[1])["state"] == "done"
        assert _req(base, "POST", "/api/polar/bogus", token="tok")[0] == 404
    finally:
        srv.shutdown()
