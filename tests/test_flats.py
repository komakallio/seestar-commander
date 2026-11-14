"""Flats: auto-exposure, normalisation, the flat library (focus band, expiry, missing), light sources,
the `flats build` job on the simulator, and that flat-fielding removes vignetting and dust donuts."""
import glob
import os

import numpy as np
import pytest
from astropy.io import fits

from sequencer import plan as P
from sequencer.flats import (FlatConfig, FlatError, FlatKey, FlatLibrary, HttpPanel, autoexpose, level,
                             make_master, normalise)

from sequencer.darklib import history

from conftest import NIGHT, ra_at_ha, wait_for

IR = FlatKey("tele", "IR", 80)


class FixedClock:
    def __init__(self, t):
        self.t = t

    def time(self):
        return self.t


# ---- auto-exposure ------------------------------------------------------------------------------------

def _source(rate, pedestal=400.0, fs=65535):
    taken = []

    def take(exp):
        taken.append(exp)
        return np.full((4, 4), min(fs, pedestal + rate * exp), np.float32)
    return take, taken


@pytest.mark.parametrize("rate", [40000.0, 2000.0, 900000.0, 150.0])
def test_autoexpose_converges(rate):
    cfg = FlatConfig(max_exposure_s=300.0, min_exposure_s=0.001)
    take, taken = _source(rate)
    exp, lvl, _, tries = autoexpose(take, cfg, log=lambda m: None)
    assert abs(lvl - cfg.target) <= cfg.tolerance
    assert 0.40 * 65535 <= lvl <= 0.50 * 65535
    assert len(tries) <= 5, tries
    assert exp == taken[-1]


def test_autoexpose_too_dim_and_too_bright():
    cfg = FlatConfig(max_exposure_s=10.0, min_exposure_s=0.01)
    with pytest.raises(FlatError, match="too dim"):
        autoexpose(_source(100.0)[0], cfg, log=lambda m: None)
    with pytest.raises(FlatError, match="too bright"):
        autoexpose(_source(1e8)[0], cfg, log=lambda m: None)


def test_normalise_per_bayer_channel():
    m = np.empty((4, 6), np.float32)
    m[0::2, 0::2], m[0::2, 1::2], m[1::2, 0::2], m[1::2, 1::2] = 1000, 800, 600, 1000
    m[0, 0] = 500                                 # a dust shadow on one G pixel
    n = normalise(m)
    assert n[0, 1] == 1.0 and n[1, 0] == 1.0 and n[1, 1] == 1.0     # colour of the panel taken out
    assert n[0, 0] == pytest.approx(0.5)
    n = normalise(np.zeros((4, 4)))
    assert np.all(n == 1.0)                       # nothing to divide by: no correction
    frames = [np.full((4, 4), 1400.0)] * 3
    assert np.all(make_master(frames, np.full((4, 4), 400.0)) == 1.0)


# ---- library ------------------------------------------------------------------------------------------

def _lib(tmp_path, **cfg):
    clock = FixedClock(NIGHT)
    return FlatLibrary(str(tmp_path / "flatlib"), FlatConfig(**cfg), clock), clock


def test_library_ignores_focus_by_default(tmp_path):
    lib, clock = _lib(tmp_path)
    m = lib.add(IR, np.ones((4, 4), np.float32), 1190)
    assert lib.match(IR, 1900)["id"] == m["id"]
    assert lib.match(IR, None)["id"] == m["id"]
    assert lib.missing([IR], {"tele": 2500}) == []


def test_library_focus_band_and_newest(tmp_path):
    lib, clock = _lib(tmp_path, focus_tolerance=100)
    a = lib.add(IR, np.ones((4, 4)), 1600, {"frames": 3, "exposure": 0.7})
    assert lib.match(IR, 1600)["id"] == a["id"] == "tele/IR_g80_b1_f1600"
    assert lib.match(IR, 1700)["id"] == a["id"]              # within the tolerance
    assert lib.match(IR, 1701) is None                       # dust shadows moved too far
    assert lib.match(IR, None) is None                       # a reading needs a reading
    assert lib.match(FlatKey("tele", "LP", 80), 1600) is None
    assert lib.match(FlatKey("tele", "IR", 200), 1600)["id"] == a["id"]     # a flat is valid at any gain
    assert lib.match(FlatKey("tele", "IR", 80, 2), 1600) is None
    assert lib.match(FlatKey("wide", "IR", 80), 1600) is None
    clock.t += 3600
    b = lib.add(IR, np.ones((4, 4)) * 0.9, 1650)
    assert lib.match(IR, 1600)["id"] == b["id"]              # newest within the band wins
    assert lib.match(IR, 1540)["id"] == a["id"]              # only the old one is close enough
    lib.add(IR, np.ones((4, 4)), 1650)                       # same position: replaced, the old one kept
    assert len(lib.entries()) == 2
    assert len(history(os.path.join(lib.root, "tele"), "IR_g80_b1_f1650")) == 1
    w = FlatKey("wide", "none", 100)
    lib.add(w, np.ones((4, 4)), None)
    assert lib.match(w, None) is not None and lib.match(w, 512) is None


def test_library_match_ignores_gain_prefers_same_gain(tmp_path):
    lib, clock = _lib(tmp_path, focus_tolerance=100)
    g80 = lib.add(IR, np.ones((4, 4)), 1600)
    clock.t += 3600
    g200 = lib.add(FlatKey("tele", "IR", 200), np.ones((4, 4)), 1600)
    assert os.path.basename(g80["dir"]) == "IR_g80_b1_f1600" and g80["gain"] == 80     # still recorded
    assert os.path.basename(g200["dir"]) == "IR_g200_b1_f1600"
    assert lib.match(IR, 1600)["id"] == g80["id"]                           # same gain beats newer
    assert lib.match(FlatKey("tele", "IR", 200), 1600)["id"] == g200["id"]
    assert lib.match(FlatKey("tele", "IR", 120), 1600)["id"] == g200["id"]  # no same gain: newest
    clock.t += 3600
    g80b = lib.add(IR, np.ones((4, 4)), 1650)
    assert lib.match(FlatKey("tele", "IR", 120), 1600)["id"] == g80b["id"]
    assert lib.match(FlatKey("tele", "IR", 200, 2), 1600) is None           # binning still counts
    assert lib.match(IR, 1540)["id"] == g80["id"]                           # g80b is out of the focus band
    clock.t += 30 * 86400 - 5400                                            # g80 expired, g200 not yet
    assert lib.match(IR, 1540)["id"] == g200["id"]                          # an expired same-gain one loses
    # missing: a filter with a usable master at any gain is present; one needed at two gains, listed once
    keys = [FlatKey("tele", "IR", 120), FlatKey("tele", "LP", 200), FlatKey("tele", "LP", 80)]
    miss = lib.missing(keys, {"tele": 1600})
    assert [(m["filter"], m["gain"]) for m in miss] == [("LP", 200)]


def test_library_expiry_and_missing(tmp_path):
    lib, clock = _lib(tmp_path, max_age_days=30, expiring_days=5)
    lib.add(IR, np.ones((4, 4)), 1600)
    assert lib.entries()[0]["status"] == "fresh"
    keys = [IR, FlatKey("tele", "LP", 80), IR]
    assert [m["filter"] for m in lib.missing(keys, {"tele": 1600})] == ["LP"]
    assert [m["filter"] for m in lib.missing(keys, {"tele": 1900})] == ["LP"]   # focus ignored by default
    clock.t += 26 * 86400
    assert lib.entries()[0]["status"] == "expiring" and lib.match(IR, 1600) is not None
    clock.t += 5 * 86400
    assert lib.entries()[0]["status"] == "expired" and lib.match(IR, 1600, fallback=False) is None
    fb = lib.match(IR, 1600)                                # soft expiry: the expired one as a fallback
    assert fb["fallback"] is True and fb["status"] == "expired"
    s = lib.summary(keys, {"tele": 1600})
    assert s["entries"][0]["status"] == "expired" and len(s["missing"]) == 2
    assert s["entries"][0]["note"] == "expired, used as fallback"
    lib2, _ = _lib(tmp_path, max_age_days=30, use_expired_fallback=False)
    lib2.clock = clock
    assert lib2.match(IR, 1600) is None


def test_http_panel_hooks():
    posted = []

    class Job:
        def log(self, m):
            pass

        def sleep(self, s):
            posted.append(("sleep", s))

    cfg = FlatConfig(source="http", panel_on_url="http://panel.invalid/on?b={brightness}",
                     panel_off_url="http://panel.invalid/off", brightness=100, brightness_filters={"LP": 255},
                     settle_s=3)
    p = HttpPanel(cfg, post=lambda url: posted.append(url) or {"ok": True})
    j = Job()
    p.on(j, "IR")
    p.filter_changed(j, "IR")                     # same brightness: nothing sent
    p.filter_changed(j, "LP")
    p.off(j)
    assert posted == ["http://panel.invalid/on?b=100", ("sleep", 3), "http://panel.invalid/on?b=255", ("sleep", 3),
                      "http://panel.invalid/off"]
    refuse = HttpPanel(cfg, post=lambda url: {"error": True, "message": "lid closed"})
    with pytest.raises(FlatError, match="lid closed"):
        refuse.on(j, "IR")


# ---- on the simulator ---------------------------------------------------------------------------------

PLAN = f"""
[plan]
park_at_end = false
[[step]]
name = "field"
ra = {ra_at_ha(-20)}
dec = 45
exposure = 30
gain = 200
count = 3
centre = "none"
"""


def _flatness(svc, stack_path):
    """Slope of the stack's sky background (green) against the optics' flat-field: about 1 when the
    vignetting and the dust donuts are still in, about 0 once they are divided out."""
    cam = svc.rig.sim.camera
    ff = cam.flatfield(cam.CameraYSize, cam.CameraXSize, svc.rig.sim.focuser.Position)
    x = (ff[0::2, 0::2] + ff[1::2, 1::2]) / 2
    g = fits.getdata(stack_path)[1]
    y = g / np.median(g)
    x = x / np.median(x)
    m = (y < 1.3) & (y > 0.5)                     # sky only, no stars
    m[:8], m[-8:], m[:, :8], m[:, -8:] = False, False, False, False   # registration edges
    xm, ym = x[m], y[m]
    return float(np.cov(xm, ym)[0, 1] / np.var(xm))


def _donut_depth(svc, stack_path):
    """Background in the deepest dust ring over the background just outside it (1 = no shadow)."""
    cam = svc.rig.sim.camera
    H, W = cam.CameraYSize // 2, cam.CameraXSize // 2
    fx, fy, rad, _ = cam.DUST[0]
    yy, xx = np.mgrid[0:H, 0:W]
    r = np.hypot(xx - fx * W, yy - fy * H) / (rad / 2)
    g = fits.getdata(stack_path)[1]
    return float(np.median(g[(r > 0.65) & (r < 0.85)]) / np.median(g[(r > 1.4) & (r < 2.0)]))


def test_flat_fielding_removes_vignetting_and_dust(make_service):
    svc = make_service(speed=300)
    svc.rig.sim.camera.set_temp(8.3)
    pl = P.loads(PLAN)
    assert svc.cmd_darks_build(plan=pl, count=3)["ok"]
    assert wait_for(svc.idle, 60)

    # lights with darks but no flats: the stack still shows the optics
    assert svc.cmd_start(pl)["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done", 90)
    st = svc.status()
    assert [f["flat"] for f in st["frames"]] == [None] * 3
    assert st["stack"]["noflat"] == 3 and st["stack"]["uncalibrated"] == 0
    assert [m["filter"] for m in st["flatlib"]["missing"]] == ["IR"]
    stack = glob.glob(os.path.join(svc.s.data_dir, "sessions", "*", "field", "stack.fits"))[0]
    assert _flatness(svc, stack) > 0.8
    assert _donut_depth(svc, stack) < 0.9

    # build what the plan is missing with the simulated panel
    r = svc.flats.cmd_build(missing=True, count=4)
    assert r["ok"] and r["items"] == ["tele IR g200 b1"], r
    assert wait_for(svc.idle, 90)
    assert svc.imager.filter_name() == "IR"
    assert svc.rig.sim.panel.on is False and svc.rig.sim.panel.switches[0] == (True, 255)
    st = svc.status()
    assert st["flatlib"]["missing"] == []
    e = st["flatlib"]["entries"][0]
    assert e["frames"] == 4 and e["focus"] == 1600 and e["status"] == "fresh" and e["source"] == "sim"
    assert 0.40 <= e["level_frac"] <= 0.50
    assert e["darkflat"] and e["darkflat"].startswith("tele/e")         # dark-flats taken and stored
    assert any(d["exposure"] == e["exposure"] for d in st["darklib"]["entries"])
    mf = fits.getdata(os.path.join(svc.s.data_dir, "flatlib", "tele", "IR_g200_b1_f1600", "master.fits"))
    assert 0.6 < mf.min() < 0.85 and abs(float(np.median(mf)) - 1) < 0.01
    assert any("re-stacked 3 lights (0 uncalibrated, 0 not flat-fielded)" in x for x in st["log"])
    restack = os.path.join(os.path.dirname(stack), "stack_darksub.fits")
    assert abs(_flatness(svc, restack)) < 0.15
    assert _donut_depth(svc, restack) > 0.97

    # new lights pick the master flat, record it and stack flat
    assert svc.cmd_start(pl)["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done", 90)
    st = svc.status()
    assert all(f["flat"] == e["id"] for f in st["frames"][-3:])
    assert st["stack"]["noflat"] == 0
    lights = sorted(glob.glob(os.path.join(svc.s.data_dir, "sessions", "*", "field", "lights", "*.fits")))
    assert fits.getheader(lights[-1])["FLATUSED"] == e["id"]
    assert abs(_flatness(svc, stack)) < 0.15
    assert _donut_depth(svc, stack) > 0.97

    # no focus_tolerance by default: refocusing keeps the master in use
    svc.rig.sim.focuser.Move(1800)
    svc.flats.poll_focus()
    assert svc.status()["flatlib"]["missing"] == []


def test_no_flat_without_a_dark(make_service, tmp_path):
    svc = make_service(speed=300)
    assert svc.flats.cmd_build(["IR"], count=3)["ok"]
    assert wait_for(svc.idle, 60)
    assert svc.cmd_start(P.loads(PLAN.replace("count = 3", "count = 1")))["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done", 60)
    st = svc.status()
    assert st["frames"][-1]["dark"] is None and st["frames"][-1]["flat"] is None
    assert st["stack"]["uncalibrated"] == 1 and st["stack"]["noflat"] == 1


def test_manual_panel_waits_for_confirmation(make_service):
    svc = make_service(speed=300, extra={"flats": {"source": "manual", "manual_timeout_s": 36000}})
    panel = svc.rig.sim.panel
    assert not svc.flats.cmd_confirm()["ok"]                 # nothing waiting
    r = svc.flats.cmd_build(["IR", "LP"], gain=80, count=3)
    assert r["ok"] and "confirm" in r["msg"]
    assert wait_for(lambda: svc.status()["flats"]["waiting"], 10)
    st = svc.status()
    assert st["state"] == "flats" and st["headline"] == "Flats: waiting for the panel (confirm)"
    panel.set(True, 255)                                     # the operator switches it on ...
    assert svc.flats.cmd_confirm()["ok"]                     # ... and confirms
    assert wait_for(svc.idle, 120)
    ents = {e["filter"]: e for e in svc.status()["flatlib"]["entries"]}
    assert set(ents) == {"IR", "LP"}
    assert ents["LP"]["exposure"] > 2.5 * ents["IR"]["exposure"]   # LP passes ~30 % of the panel light
    assert all(0.40 <= e["level_frac"] <= 0.50 for e in ents.values())
    assert svc.imager.filter_name() == "IR"
    assert any("it can be switched off" in x for x in svc.status()["log"])


def test_manual_panel_stop_and_timeout(make_service):
    svc = make_service(speed=300, extra={"flats": {"source": "manual", "manual_timeout_s": 600}})
    assert svc.flats.cmd_build(["IR"], count=2)["ok"]
    assert wait_for(lambda: svc.status()["flats"]["waiting"], 10)
    svc.cmd_stop()
    assert wait_for(svc.idle, 20)
    assert svc.status()["state"] == "stopped" and svc.status()["flatlib"]["entries"] == []
    assert svc.flats.cmd_build(["IR"], count=2)["ok"]
    assert wait_for(svc.idle, 30)                            # 600 sim s = 2 s here
    assert any("nobody confirmed the panel" in x for x in svc.status()["log"])


def test_too_dim_panel_fails_one_filter(make_service):
    svc = make_service(speed=300, extra={"flats": {"brightness_filters": {"LP": 2}, "max_exposure_s": 5}})
    assert svc.flats.cmd_build(["IR", "LP"], count=2)["ok"]
    assert wait_for(svc.idle, 120)
    st = svc.status()
    assert [e["filter"] for e in st["flatlib"]["entries"]] == ["IR"]
    assert st["flats"]["last"]["failed"] == ["tele LP g200 b1"]
    assert any("LP g200 b1 FAILED: too dim" in x for x in st["log"])


def test_refusals(make_service):
    svc = make_service(speed=300, extra={"flats": {"source": "sky"}})
    r = svc.flats.cmd_build(["IR"])
    assert not r["ok"] and "not implemented" in r["msg"]
    svc.flats.cfg.source = "sim"
    assert not svc.flats.cmd_build(["Dark"])["ok"]
    assert not svc.flats.cmd_build(["Ha"])["ok"]
    svc.cmd_start(P.loads(PLAN.replace("count = 3", "count = 50")))
    assert wait_for(lambda: svc.busy == "run", 10)
    assert not svc.flats.cmd_build(["IR"])["ok"]
    svc.cmd_stop()
    assert wait_for(svc.idle, 20)


def test_wide_flats(make_service):
    svc = make_service(speed=300)
    r = svc.flats.cmd_build(wide=True, count=3)
    assert r["ok"] and r["items"] == ["wide none g100 b1"], r
    assert wait_for(svc.idle, 120)
    e = svc.status()["flatlib"]["entries"][0]
    assert e["camera"] == "wide" and e["darkflat"] is None and 0.40 <= e["level_frac"] <= 0.50
    mf = fits.getdata(os.path.join(svc.s.data_dir, "flatlib", "wide", os.path.basename(e["id"]), "master.fits"))
    assert mf[:20, :20].mean() / mf[470:490, 260:280].mean() < 0.55       # corners ~46 % of the centre


def test_http_api_and_page(make_service):
    import json
    import urllib.request
    from sequencer.httpapi import serve
    svc = make_service(speed=300)
    srv = serve(svc, host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        def post(path, body):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            return json.loads(urllib.request.urlopen(req, timeout=10).read())
        assert post("/api/flats/build", {"filters": ["IR"], "count": 2})["ok"]
        assert wait_for(svc.idle, 60)
        fl = json.loads(urllib.request.urlopen(base + "/api/flats", timeout=10).read())
        assert fl["entries"][0]["filter"] == "IR" and fl["focus"]["tele"] == 1600
        assert post("/api/sim/panel", {"on": True, "brightness": 40})["ok"] and svc.rig.sim.panel.brightness == 40
        page = urllib.request.urlopen(base + "/", timeout=10).read().decode()
        assert "Flat library" in page and "flats/confirm" in page
    finally:
        srv.shutdown()
