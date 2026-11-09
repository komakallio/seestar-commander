"""Wide camera alongside the telephoto: plan options, cadence, timing against the telephoto and the
dithers, stop / unsafe / night limit / scope lock, headers and WCS, solver failures, the projections
solver wrapper (subprocess mocked) and the status page."""
import glob
import json
import os
import statistics
import time
import urllib.request

import numpy as np
import pytest
from astropy.io import fits

from sequencer import astro
from sequencer import plan as P
from sequencer import wide as W
from sequencer.httpapi import serve

from conftest import LAT, LON, NIGHT, ra_at_ha, wait_for


def _plan(extra="", count=6, exposure=30, dither=0, centre="none", park=False, sun=None, t=NIGHT):
    d = f"dither_every = {dither}\ndither_settle_s = 2\n" if dither else ""
    c = f"[constraints]\nsun_max_alt = {sun}\n" if sun is not None else ""
    return P.loads(f'[plan]\npark_at_end = {str(park).lower()}\n{c}[[step]]\nname = "T"\nra = {ra_at_ha(-30, t)}\n'
                   f'dec = 45\nexposure = {exposure}\ncount = {count}\ncentre = "{centre}"\n{d}{extra}', "toml")


def _done(svc, timeout=300):
    assert wait_for(lambda: svc.status(log=False, darklib=False)["state"] in ("done", "stopped", "error")
                    and svc.idle(), timeout)
    return svc.status()


def _wide_files(svc, pat="*.fits"):
    return sorted(glob.glob(os.path.join(svc.s.data_dir, "sessions", "*", "T", "wide", pat)))


def _record_moves(tel):
    """Times of every nonzero MoveAxis (dither pulses, slews) on the simulated mount."""
    moves, orig = [], tel.MoveAxis

    def rec(axis, rate):
        if rate:
            moves.append(tel.clock.time())
        return orig(axis, rate)
    tel.MoveAxis = rec
    return moves


def _wide_state(svc):
    return svc.status(log=False, darklib=False)["wide"]["state"]


# ---- plan and config -----------------------------------------------------------------------------

def test_plan_wide_options():
    s = _plan("wide_every = 3\nwide_exposure = 15\nwide_gain = 120\n").steps[0]
    assert (s.wide_every, s.wide_exposure, s.wide_gain) == (3, 15.0, 120)
    assert "wide every 3" in s.describe()
    s = _plan().steps[0]
    assert (s.wide_every, s.wide_exposure, s.wide_gain) == (None, None, None)     # -> [wide] defaults
    for bad in ("wide_every = -1\n", "wide_exposure = 61\n", "wide_exposure = 0\n", "wide_gain = -5\n"):
        with pytest.raises(P.PlanError):
            _plan(bad)
    pl = P.loads('[defaults]\nwide_every = 2\n[[step]]\ntarget = "M31"\n', "toml")
    assert pl.steps[0].wide_every == 2


def test_config_defaults_and_plan_override(make_service):
    svc = make_service(speed=60, extra={"wide": {"every": 2, "exposure": 5, "gain": 90}})
    assert svc.wide.settings(_plan().steps[0]) == (2, 5.0, 90)
    assert svc.wide.settings(_plan("wide_every = 0\n").steps[0])[0] == 0
    assert svc.cmd_start(_plan(count=5))["ok"]
    st = _done(svc)
    files = _wide_files(svc)
    assert len(files) == 3                                           # lights 1, 3, 5
    assert [fits.getheader(f)["TELEFRM"] for f in files] == [1, 3, 5]
    h = fits.getheader(files[0])
    assert h["EXPTIME"] == 5.0 and h["GAIN"] == 90
    assert st["wide"]["count"] == 3


# ---- cadence, headers, WCS -----------------------------------------------------------------------

def test_wide_frames_headers_and_wcs(make_service):
    svc = make_service(speed=60)
    assert svc.cmd_start(_plan("wide_every = 2\nwide_exposure = 10\nwide_gain = 110\n", count=6, dither=2))["ok"]
    st = _done(svc)
    assert st["state"] == "done" and st["plan"]["steps"][0]["frames"] == 6
    files = _wide_files(svc)
    assert len(files) == 3
    assert wait_for(lambda: svc.status()["wide"]["solved"] == 3, 30)
    h = fits.getheader(files[1])
    assert h["CAMERA"] == "wide" and h["CAMNUM"] == 1 and h["INSTRUME"] == "SeeStar S30 Pro wide"
    assert h["FOCALLEN"] == 6.0 and h["XPIXSZ"] == 1.6 and h["FOCUSPOS"] == 512       # its own focuser 1
    assert h["EXPTIME"] == 10.0 and h["GAIN"] == 110 and h["IMAGETYP"] == "LIGHT" and h["TELEFRM"] == 3
    step = svc.plan.steps[0]
    assert h["OBJECT"] == "T" and h["OBJCTRA"] == astro.ra_sexagesimal(step.ra)
    assert h["OBJCTDEC"] == astro.dec_sexagesimal(step.dec) and h["RA"] == pytest.approx(step.ra)
    assert h["EQUINOX"] == 2000.0 and "MNT_RA" in h and "MNT_HA" in h and "CABLE_U" in h
    assert h["DATE-OBS"] < h["DATE-AVG"] < h["DATE-END"] and "DITHN" in h
    assert "FILTER" not in h
    # WCS: header (local TAN at the centre) and sidecar, close to the simulated truth
    assert h["WCSSOLVD"] is True and h["CTYPE1"] == "RA---TAN"
    side = json.load(open(files[1][:-5] + ".wcs.json"))
    assert side["solved"] and side["frame"] == os.path.basename(files[1]) and "header_wcs" in side
    from astropy.wcs import WCS
    data = fits.getdata(files[1])
    ra_c, dec_c = WCS(h).wcs_pix2world([[(data.shape[1] - 1) / 2, (data.shape[0] - 1) / 2]], 0)[0]
    assert astro.separation(ra_c, dec_c, side["ra"], side["dec"]) < 0.2
    assert astro.separation(side["ra"], side["dec"], step.ra, step.dec) < 2.5     # mount pointing error
    # the thumbnail and the status block
    assert os.path.exists(os.path.join(svc.out, "wide.jpg")) and "wide" in st["images"]
    w = svc.status()["wide"]
    assert w["count"] == 3 and w["last"]["solved"] is True and w["last"]["light"] == 5
    assert svc.rig.sim.telescope.firmware_gotos == 0


def _cadence(make_service, wide):
    svc = make_service(speed=20)
    tel = svc.rig.sim.telescope
    moves = _record_moves(tel)
    extra = "wide_every = 1\nwide_exposure = 10\n" if wide else "wide_every = 0\n"
    assert svc.cmd_start(_plan(extra, count=6, exposure=30, dither=2))["ok"]
    st = _done(svc)
    assert st["state"] == "done"
    starts = svc.rig.sim.camera.starts
    return svc, moves, [b - a for a, b in zip(starts, starts[1:])]


def test_wide_does_not_delay_the_telephoto_and_avoids_dithers(make_service):
    off_svc, _, off = _cadence(make_service, wide=False)
    svc, moves, on = _cadence(make_service, wide=True)
    cam = svc.rig.sim.wide_camera
    assert len(_wide_files(svc)) == 6 and cam.aborts == 0
    # no wide exposure ever overlapped a mount move (dither pulses)
    assert moves
    for t0, t1, how in cam.intervals:
        assert how == "done"
        assert not any(t0 - 0.5 <= t <= t1 + 0.5 for t in moves), (t0, t1)
    # every wide exposure lay inside its telephoto exposure
    tele = svc.rig.sim.camera.starts
    for (t0, t1, _), ts in zip(cam.intervals, tele):
        assert ts <= t0 < ts + 1.5 and t1 <= ts + 30
    # downloads never overlapped: the telephoto never waited for the wide camera, the worker never blocked
    assert svc.imager.bus_waits == 0 and svc.rig.wide.bus_waits == 0
    assert svc.wide.worker_block_s < 1.0
    tele_end = [t + 30 for t in tele]
    for d0, d1 in cam.downloads:                                 # inside a telephoto exposure
        assert any(ts <= d0 and d1 <= te for ts, te in zip(tele, tele_end)), (d0, d1)
    # the telephoto cadence is the same with the wide camera on (sim timing noise aside)
    assert statistics.median(on) <= statistics.median(off) * 1.25 + 3.0, (on, off)


def test_wide_frame_waits_for_room_between_dithers(make_service):
    """A wide exposure longer than the telephoto one starts only where it ends before the next dither."""
    svc = make_service(speed=60)
    tel = svc.rig.sim.telescope
    moves = _record_moves(tel)
    assert svc.cmd_start(_plan("wide_every = 1\nwide_exposure = 50\n", count=6, exposure=30, dither=2))["ok"]
    _done(svc)
    cam = svc.rig.sim.wide_camera
    assert [fits.getheader(f)["TELEFRM"] for f in _wide_files(svc)] == [1, 3, 5]     # after each dither
    for t0, t1, how in cam.intervals:
        assert how == "done" and not any(t0 <= t <= t1 for t in moves)


def test_wide_frame_that_never_fits_is_skipped(make_service):
    svc = make_service(speed=60)
    assert svc.cmd_start(_plan("wide_every = 1\nwide_exposure = 45\n", count=3, exposure=30, dither=1))["ok"]
    st = _done(svc)
    assert st["plan"]["steps"][0]["frames"] == 3 and not _wide_files(svc)
    assert svc.rig.sim.wide_camera.exposures == 0
    assert any("doesn't fit between dithers" in line for line in st["log"])


# ---- stop, unsafe, night limit, scope lock ---------------------------------------------------------

def _start_and_wait_exposing(svc, plan):
    assert svc.cmd_start(plan)["ok"]
    assert wait_for(lambda: _wide_state(svc) == "exposing", 60, poll=0.01)


def test_stop_aborts_the_wide_exposure(make_service):
    svc = make_service(speed=30)
    _start_and_wait_exposing(svc, _plan("wide_every = 1\nwide_exposure = 40\n", count=4, exposure=60))
    svc.cmd_stop()
    st = _done(svc, 30)
    assert st["state"] == "stopped"
    cam = svc.rig.sim.wide_camera
    assert cam.aborts == 1 and cam.intervals[-1][2] == "aborted" and cam._exp is None
    assert not _wide_files(svc) and st["wide"]["state"] == "idle" and st["wide"]["aborted"] == 1


def test_stop_ends_stopped_when_the_worker_beats_the_aborts(make_service):
    """The worker reacts to the abort flag at once; if it sets "stopped" while cmd_stop is still in the
    camera aborts (here: held in the wide cancel until it has), the state must not be left at "stopping"."""
    svc = make_service(speed=30)
    _start_and_wait_exposing(svc, _plan("wide_every = 1\nwide_exposure = 40\n", count=4, exposure=60))
    orig = svc.wide.cancel

    def slow_cancel(why):
        ok = orig(why)
        wait_for(lambda: svc.status(log=False, darklib=False)["state"] == "stopped", 5)
        return ok
    svc.wide.cancel = slow_cancel
    svc.cmd_stop()
    assert _done(svc, 30)["state"] == "stopped"


def test_unsafe_aborts_the_wide_exposure_and_parks(make_service):
    svc = make_service(speed=30)
    _start_and_wait_exposing(svc, _plan("wide_every = 1\nwide_exposure = 40\n", count=4, exposure=60))
    svc.rig.sim.safety.set(False, ["RAIN"])
    assert wait_for(lambda: svc.rig.sim.telescope.AtPark and svc.idle(), 120)
    cam = svc.rig.sim.wide_camera
    assert cam.aborts == 1 and cam.intervals[-1][2] == "aborted"
    assert not _wide_files(svc) and _wide_state(svc) == "idle"


def test_night_limit_ends_wide_frames_cleanly(make_service):
    from test_night_limit import DAWN8
    svc = make_service(start=DAWN8 - 400, speed=100)
    pl = _plan("wide_every = 1\nwide_exposure = 50\n", count=50, exposure=60, sun=-2, t=DAWN8)
    assert svc.cmd_start(pl)["ok"]
    st = _done(svc, 120)
    assert "night limit" in st["plan"]["steps"][0]["reason"] and svc.rig.sim.telescope.AtPark
    cam = svc.rig.sim.wide_camera
    assert cam.exposures >= 1 and st["wide"]["state"] == "idle" and svc.wide.job is None
    done = [iv for iv in cam.intervals if iv[2] == "done"]
    # every completed wide exposure was saved, or dropped with the night's end (never left running)
    assert len(_wide_files(svc)) + st["wide"]["aborted"] >= len(done)
    assert len(_wide_files(svc)) == st["wide"]["count"]


def test_scope_lock_drops_the_wide_frame(make_service):
    svc = make_service(speed=30)
    _start_and_wait_exposing(svc, _plan("wide_every = 1\nwide_exposure = 40\n", count=3, exposure=60))
    svc.hwlock = {"owner": "test", "reason": "", "since": time.time(), "until": time.time() + 600, "ttl_s": 600}
    assert wait_for(lambda: _wide_state(svc) == "idle", 30)
    svc.hwlock = None
    st = _done(svc)
    assert st["state"] == "done" and st["plan"]["steps"][0]["frames"] == 3
    assert svc.rig.sim.wide_camera.intervals[0][2] == "aborted"
    assert any("frame aborted (scope locked)" in line for line in st["log"])


# ---- solver failures --------------------------------------------------------------------------------

@pytest.mark.parametrize("fail", ["raise", "none"])
def test_solver_failure_does_not_affect_the_plan(make_service, fail):
    svc = make_service(speed=60)
    svc.rig.wide_solver.fail = fail
    assert svc.cmd_start(_plan("wide_every = 1\nwide_exposure = 10\n", count=4, dither=2))["ok"]
    st = _done(svc)
    assert st["state"] == "done" and st["plan"]["steps"][0]["frames"] == 4
    files = _wide_files(svc)
    assert len(files) == 4
    assert wait_for(lambda: svc.status()["wide"]["failed"] == 4, 30)
    h = fits.getheader(files[0])
    assert h["WCSSOLVD"] is False and "CTYPE1" not in h
    side = json.load(open(files[0][:-5] + ".wcs.json"))
    assert side["solved"] is False and side["why"]
    w = svc.status()["wide"]
    assert w["solved"] == 0 and w["last"]["solved"] is False


def test_no_wide_solver_just_saves(make_service):
    svc = make_service(speed=60, world={"wide_solver": False})
    assert svc.cmd_start(_plan("wide_every = 2\nwide_exposure = 10\n", count=4))["ok"]
    st = _done(svc)
    assert len(_wide_files(svc)) == 2 and not _wide_files(svc, "*.json")
    assert st["wide"]["solver"]["available"] is False and st["wide"]["last"]["solved"] is None


def test_no_wide_camera(make_service):
    svc = make_service(speed=60, world={"wide": False})
    assert svc.cmd_start(_plan("wide_every = 1\n", count=2))["ok"]
    st = _done(svc)
    assert st["state"] == "done" and st["wide"]["available"] is False and not _wide_files(svc)


# ---- the projections solver wrapper and the WCS helpers --------------------------------------------

def _tan_grid(w, h, ra0, dec0, scale_deg, step):
    """Synthetic solution_grid.ts output: a TAN camera, 'scale_deg' per green px, north up, east left."""
    pts = []
    a0, d0 = np.radians(ra0), np.radians(dec0)

    def vec(x, y):
        xi, eta = np.radians(-(x - w / 2) * scale_deg), np.radians(-(y - h / 2) * scale_deg)
        rho = np.hypot(xi, eta)
        c = np.arctan(rho)
        dec = np.arcsin(np.cos(c) * np.sin(d0) + (eta * np.sin(c) * np.cos(d0) / rho if rho else 0))
        ra = a0 + np.arctan2(xi * np.sin(c), rho * np.cos(d0) * np.cos(c) - eta * np.sin(d0) * np.sin(c))
        return [float(np.cos(dec) * np.cos(ra)), float(np.cos(dec) * np.sin(ra)), float(np.sin(dec))]
    for y in range(0, h + 1, step):
        for x in range(0, w + 1, step):
            pts.append([x, y, *vec(x, y)])
    return {"width": w, "height": h, "step": step, "center": vec(w / 2, h / 2), "fit": {}, "pts": pts}


def test_projections_solver_unavailable(tmp_path):
    assert not W.ProjectionsSolver("").available
    s = W.ProjectionsSolver(str(tmp_path))
    assert not s.available and "solve-blind.ts" in s.error
    assert s.solve({"image": np.zeros((8, 8), np.uint16)}) is None
    assert isinstance(W.make_wide_solver(W.WideConfig(solve=False)), W.NoWideSolver)


def test_projections_solver_with_mocked_node(tmp_path, monkeypatch):
    proj = tmp_path / "projections"
    (proj / "scripts").mkdir(parents=True)
    (proj / "scripts" / "solve-blind.ts").write_text("")
    monkeypatch.setattr(W.shutil, "which", lambda name: "/usr/bin/node")
    grid = _tan_grid(1080, 1920, 120.0, 50.0, 110.0 / 3600, 60)
    calls = []

    class R:
        def __init__(self, out):
            self.stdout, self.stderr, self.returncode = out, "", 0

    def fake_run(cmd, cwd=None, capture_output=True, text=True, timeout=None, env=None):
        calls.append((cmd, cwd, env))
        if cmd[1] == "scripts/solve-blind.ts":
            assert cwd == str(proj) and os.path.exists(cmd[2])
            with open(cmd[4], "w") as fh:
                json.dump({"model": {"lens": {"width": 1080, "height": 1920}}}, fh)
            return R("1234 detections\nWinner: 412 stars, RMS 0.61 px\n  Solved.\n")
        assert cmd[1].endswith("tools/solution_grid.ts") and env["PROJECTIONS_DIR"] == str(proj)
        return R(json.dumps(grid))
    monkeypatch.setattr(W.subprocess, "run", fake_run)
    s = W.ProjectionsSolver(str(proj))
    assert s.available
    img = np.random.default_rng(0).integers(300, 400, (3840, 2160)).astype(np.uint16)
    sol = s.solve({"image": img, "solution_path": str(tmp_path / "f.solution.json")})
    assert sol and sol["stars"] == 412 and sol["rms_px"] == 0.61
    assert (sol["ra"], sol["dec"]) == pytest.approx((120.0, 50.0), abs=1e-6)
    assert os.path.exists(tmp_path / "f.solution.json")
    cd = np.asarray(sol["tan"]["cd_green"])
    assert cd[0, 0] == pytest.approx(-110.0 / 3600, rel=0.02) and abs(cd[0, 1]) < 1e-5
    hw = W.header_wcs(sol, 2160, 3840)
    assert hw["CD1_1"] == pytest.approx(-55.0 / 3600, rel=0.02)          # full-resolution px
    assert hw["CRPIX1"] == pytest.approx(2 * 540 + 1.5) and hw["CRPIX2"] == pytest.approx(2 * 960 + 1.5)
    # not solved
    monkeypatch.setattr(W.subprocess, "run", lambda *a, **k: R("no match\n"))
    assert s.solve({"image": img}) is None


# ---- status page -------------------------------------------------------------------------------------

def test_status_page_has_the_wide_block(make_service):
    svc = make_service(speed=60)
    srv = serve(svc, host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    html = urllib.request.urlopen(base + "/").read().decode()
    assert 'id="widecard"' in html and "/img/${k}.jpg" in html
    assert svc.cmd_start(_plan("wide_every = 2\nwide_exposure = 5\n", count=2))["ok"]
    _done(svc)
    st = json.loads(urllib.request.urlopen(base + "/api/status").read())
    assert st["wide"]["count"] == 1 and st["headline"] == "Done"
    assert urllib.request.urlopen(base + "/img/wide.jpg").read()[:2] == b"\xff\xd8"
    srv.shutdown()
