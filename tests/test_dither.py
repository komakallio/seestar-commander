"""Dithering: plan options, the pattern, timed MoveAxis pulses (cable window, interrupt, command delays),
the measured response, FITS headers, re-centring, and hot pixels rejected by the stack."""
import glob
import math
import os

import numpy as np
import pytest
from astropy.io import fits

from sequencer import astro
from sequencer import dither as D
from sequencer import plan as P
from sequencer.statusview import headline

from conftest import NIGHT, ra_at_ha, wait_for
from scope import PRIMARY, SECONDARY, CableLimit, Interrupted

DELAYS = ((0.28, 0.26), (0.23, 0.12))       # MoveAxis start/stop delays measured on one S30 Pro


def _plan(extra="", count=6, exposure=30, centre="auto", dec=45):
    return P.loads(f'[plan]\npark_at_end = false\n[[step]]\nname = "T"\nra = {ra_at_ha(-30)}\ndec = {dec}\n'
                   f'exposure = {exposure}\ncount = {count}\ncentre = "{centre}"\n{extra}', "toml")


# ---- plan ----------------------------------------------------------------------------------------

def test_plan_dither_options():
    s = _plan("dither_every = 2\ndither_px = 18\ndither_settle_s = 3\ndither_recentre_every = 5\n").steps[0]
    assert (s.dither, s.dither_every, s.dither_px, s.dither_settle_s, s.dither_recentre_every) == (True, 2, 18, 3, 5)
    assert "dither 18 px every 2" in s.describe()
    assert _plan().steps[0].dither is False
    assert _plan("dither = true\n").steps[0].dither_every == 1          # the v1 boolean still works
    assert _plan("dither = false\ndither_every = 3\n").steps[0].dither_every == 0
    with pytest.raises(P.PlanError):
        _plan("dither_every = 1\ndither_px = 0\n")
    with pytest.raises(P.PlanError):
        _plan("dither_jitter = 1\n")


# ---- pattern, pulses, response (pure) -----------------------------------------------------------

def test_pattern_stays_in_the_disc_and_moves_every_time():
    p = D.Pattern(15.0, seed=3)
    cur, pts = (0.0, 0.0), []
    for _ in range(300):
        nxt = p.next(cur)
        assert math.hypot(*nxt) <= 15.0 + 1e-9                          # around the centre, no random walk
        assert math.hypot(nxt[0] - cur[0], nxt[1] - cur[1]) >= 7.5 - 1e-9   # every dither moves >= r/2
        cur = nxt
        pts.append(nxt)
    a = np.array(pts)
    assert abs(a[:, 0].mean()) < 2 and abs(a[:, 1].mean()) < 2           # no bias: it fills the disc
    assert (np.hypot(a[:, 0], a[:, 1]) > 10).mean() > 0.3


def test_pulses_signs_and_no_ra_reversal():
    cfg = D.DitherConfig()
    du, dd = D.axis_moves(36.0, -18.0, 60.0)                   # 36" east, 18" south at Dec 60
    assert du == pytest.approx(-0.02) and dd == pytest.approx(-0.005)   # east = -HA, scaled by 1/cos(dec)
    (ax0, r0, s0, l0, b0), (ax1, r1, s1, l1, b1) = D.pulses(cfg, du, dd)
    sid = astro.SIDEREAL_DEG_S
    assert ax0 == 0 and -sid < r0 < 0 and r0 == pytest.approx(-0.9 * sid)   # slows RA, never reverses it
    assert s0 * abs(r0) == pytest.approx(0.02) and l0 == cfg.ra_lead_s and b0 == sid
    assert ax1 == 1 and r1 == -cfg.rate_deg_s and s1 * cfg.rate_deg_s == pytest.approx(0.005)
    (_, r, s, _, b), = D.pulses(cfg, 0.01, 0.0)
    assert r == cfg.rate_deg_s and s == pytest.approx(1.0)
    rep = D.DitherConfig(ra_moveaxis="replaces")
    (_, r, s, _, b), = D.pulses(rep, -0.01, 0.0)                # MoveAxis replaces tracking: add sidereal
    assert r == pytest.approx(sid - 0.9 * sid) and b == 0.0
    bl = D.DitherConfig(dec_backlash_as=18.0)
    (_, _, s, _, _), = D.pulses(bl, 0.0, 0.005, last_dec_sign=-1)
    assert s == pytest.approx((0.005 + 0.005) / bl.rate_deg_s)


def test_response_recovers_scale_and_drift():
    a0 = D.shift_matrix([[-1e-3, 0.0], [0.0, -1e-3]])          # 3.6"/px, no rotation
    K = np.array([[0.9, 0.0], [0.0, 1.05]])                    # RA moves 10 % short, Dec 5 % long
    v = np.array([-2.0, 0.5])                                  # px/min drift
    r = D.Response(a0)
    rng = np.random.default_rng(1)
    cmd = np.zeros(2)
    for i in range(10):
        if i:
            cmd = rng.uniform(-50, 50, 2)
        r.add(i * 1.2, cmd, a0 @ K @ cmd + v * i * 1.2, 0)
    sc = r.scale()
    assert sc["east"] == pytest.approx(0.9, abs=1e-6) and sc["north"] == pytest.approx(1.05, abs=1e-6)
    assert r.fit["v_px_min"] == pytest.approx(v)
    path = r.drift_path()
    assert path[-1] == pytest.approx(v * 9 * 1.2)


# ---- Scope.pulse_axis and the simulated command delays -------------------------------------------

def _tracking_scope(make_service, **world):
    svc = make_service(speed=50, world=world)
    sc, tel = svc.scope, svc.rig.sim.telescope
    tel.Unpark()
    sc.move_radec((astro.lst_deg(svc.clock.time(), 25.0) + 30) % 360, 45.0, tol=0.05)
    return svc, sc, tel


def test_pulse_moves_by_rate_times_time_with_delays(make_service):
    svc, sc, tel = _tracking_scope(make_service, axis_delays=DELAYS)
    for axis, lead in ((SECONDARY, 0.11), (PRIMARY, 0.02)):
        sc.clock.sleep(1.0)
        u0, d0, t0 = tel.mech_u, tel.Declination, sc.clock.time()
        sc.pulse_axis(axis, 0.01, 3.0, lead_s=lead, base_rate=astro.SIDEREAL_DEG_S)
        sc.clock.sleep(1.0)                                   # past the stop delay
        dt = sc.clock.time() - t0
        if axis == SECONDARY:
            got = (tel.Declination - d0) * 3600
        else:
            got = (tel.mech_u - u0 - astro.SIDEREAL_DEG_S * (1 + tel.track_error) * dt) * 3600
        assert got == pytest.approx(108.0, abs=4.0), (axis, got)   # 0.01 deg/s x 3 s = 108"
    # without the lead the Dec move comes out short by rate x (start - stop delay)
    d0 = tel.Declination
    sc.pulse_axis(SECONDARY, 0.01, 3.0, lead_s=0.0)
    sc.clock.sleep(1.0)
    assert (tel.Declination - d0) * 3600 == pytest.approx(108.0 - 0.11 * 36, abs=2.0)


def test_pulse_refused_outside_the_window_and_interruptible(make_service):
    svc, sc, tel = _tracking_scope(make_service)
    u = sc.ha_unwrapped()
    sc.ha_u_max = u + 0.1                                     # pretend the cable limit is 0.1 deg away
    with pytest.raises(CableLimit):
        sc.pulse_axis(PRIMARY, 0.01, 2.0, runaway_s=60.0)    # held 60 s it would pass the limit
    assert tel._rate == [0.0, 0.0] and tel.mech_u < sc.ha_u_max
    sc.ha_u_max = 175.0
    with pytest.raises(CableLimit):
        sc.pulse_axis(SECONDARY, 0.01, 2.0, runaway_s=60000.0)   # would leave the Dec range
    sc.interrupt = lambda: True
    with pytest.raises(Interrupted):
        sc.pulse_axis(PRIMARY, 0.01, 5.0)
    assert tel._rate == [0.0, 0.0] and not tel._pending
    assert tel.firmware_gotos == 0 and not tel.cable_violation


# ---- the service ---------------------------------------------------------------------------------

def test_dithered_run_achieves_the_pattern(make_service):
    svc = make_service(speed=60, world={"axis_delays": DELAYS})
    seen = []
    pl = _plan("dither_every = 1\ndither_px = 12\ndither_settle_s = 2\n", count=6)
    assert svc.cmd_start(pl)["ok"]

    def watch():
        st = svc.status(log=False, darklib=False)
        if (st.get("action") or {}).get("kind") == "dither":
            seen.append(st["headline"])
        return st["state"] == "done" and svc.idle()
    assert wait_for(watch, 300, poll=0.01)
    st = svc.status()
    assert any(h.startswith("Dithering T (#") for h in seen), seen
    d = st["dither"]
    assert d["n"] == 5 and d["fitted"] and d["oriented"]
    assert d["scale"]["east"] == pytest.approx(1.0, abs=0.05) and d["scale"]["north"] == pytest.approx(1.0, abs=0.05)
    assert d["rms_px"] < 0.5
    fr = st["frames"]
    assert all(math.hypot(f["dither_e_px"], f["dither_n_px"]) <= 12.0 + 0.1 for f in fr)
    files = sorted(glob.glob(os.path.join(svc.s.data_dir, "sessions", "*", "T", "lights", "*.fits")))
    assert len(files) == 6
    h = fits.getheader(files[-1])
    assert h["DITHN"] == 5 and "DITHERX" in h and "DITHERY" in h and "DITHRA" in h
    moves = [math.hypot(b["dither_x_px"] - a["dither_x_px"], b["dither_y_px"] - a["dither_y_px"])
             for a, b in zip(fr, fr[1:])]
    assert min(moves) > 4.0                                   # every dither really moved the field
    tel = svc.rig.sim.telescope
    assert tel.firmware_gotos == 0 and not tel.cable_violation
    assert st["drift"] is not None and st["drift"]["total"] < 30   # the dithers are out of the drift rate


def test_recentre_every_n_dithers(make_service):
    svc = make_service(speed=80)
    pl = _plan("dither_every = 1\ndither_px = 12\ndither_settle_s = 1\ndither_recentre_every = 2\n", count=5)
    assert svc.cmd_start(pl)["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done" and svc.idle(), 300)
    log = "\n".join(svc.status()["log"])
    assert log.count("re-centre (every 2 dithers)") == 2
    assert svc.status()["dither"]["n"] == 4


def test_stop_during_dither_settle(make_service):
    svc = make_service(speed=50)
    pl = _plan("dither_every = 1\ndither_px = 12\ndither_settle_s = 3000\n", count=4, centre="none")
    assert svc.cmd_start(pl)["ok"]
    assert wait_for(lambda: (svc.status(log=False)["action"] or {}).get("kind") == "dither", 120)
    svc.cmd_stop()
    assert wait_for(lambda: svc.idle(), 30)
    assert svc.status()["state"] == "stopped"
    tel = svc.rig.sim.telescope
    assert tel._rate == [0.0, 0.0] and tel.firmware_gotos == 0


def _hot_residual(svc):
    """Median excess of the stack at the brightest hot pixels (frame-1 coordinates) over their surroundings."""
    from scipy import ndimage as ndi
    stack = fits.getdata(glob.glob(os.path.join(svc.s.data_dir, "sessions", "*", "T", "stack.fits"))[0])
    cam = svc.rig.sim.camera
    hy, hx, amp, _ = cam._hot
    sel = np.argsort(amp)[-60:]
    out = []
    for y, x in zip(hy[sel], hx[sel]):
        ch = {(0, 0): 1, (1, 1): 1, (0, 1): 0, (1, 0): 2}[(y % 2, x % 2)]
        im = stack[ch]
        yy, xx = y // 2, x // 2
        if 3 <= yy < im.shape[0] - 3 and 3 <= xx < im.shape[1] - 3:
            out.append(im[yy, xx] - np.median(im[yy - 3:yy + 4, xx - 3:xx + 4]))
    return float(np.median(out))


def test_dithering_removes_hot_pixels_from_the_stack(make_service):
    world = {"track_error": 0.0}                               # no drift: without dithers hot pixels stay put
    res = {}
    for name, extra in (("still", ""), ("dither", "dither_every = 1\ndither_px = 12\ndither_settle_s = 1\n")):
        svc = make_service(speed=100, world=world)
        assert svc.cmd_start(_plan(extra, count=7, exposure=60))["ok"]
        assert wait_for(lambda: svc.status()["state"] == "done" and svc.idle(), 300)
        assert svc.status()["stack"]["frames"] == 7
        res[name] = _hot_residual(svc)
    assert res["still"] > 1000                                 # uncalibrated: hot pixels are in the stack
    assert res["dither"] < 0.01 * res["still"], res          # sim: ~9800 -> < 1 count


def test_headline_dithering():
    s = {"state": "running", "busy": "run", "time_unix": NIGHT, "safety": {"safe": True},
         "action": {"kind": "dither", "target": "M45", "n": 3, "px": 14.2}}
    assert headline(s) == "Dithering M45 (#3, 14 px)"


def test_stack_drops_an_outlier_in_the_first_three_frames():
    from sequencer.imaging import RunningStack
    rng = np.random.default_rng(0)
    frames = [rng.normal(1000, 10, (64, 64)).astype(np.float32) for _ in range(5)]
    frames[0][20, 21] += 5000                                 # a hot pixel in frame 1 only (R channel)
    st = RunningStack()
    for f in frames:
        st.add(f, offset=(0.0, 0.0))
    assert abs(st.image()[10, 10, 0] - 1000) < 30
    assert abs(st.image()[5, 5, 1] - 1000) < 30               # normal pixels unaffected
