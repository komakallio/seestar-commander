"""Model darks: computed from a dark model at the light's exposure and temperature; source = master stays the
default; compare adds a second stack without touching the first; model calibrates and falls back to a master
outside the model's range; bad pixels are filled from same-colour neighbours."""
import json
import math
import os

import numpy as np
from astropy.io import fits

from sequencer import plan as P
from sequencer.modeldark import ModelDark, fill_bad

from conftest import ra_at_ha, wait_for

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


def _sim_model(data_dir, H, W, hot, T_range=(-40.0, 60.0), exps=(10.0, 120.0), stamp="20991231"):
    """A model of the simulator's dark: bias 400, 1.5 t 2^((T - 8) / 6) ADU, plus its hot pixels."""
    d = os.path.join(data_dir, "darkmodel", stamp)
    os.makedirs(d)
    A = np.full((H, W), 1.5 * 2 ** (2 / 6), np.float32)
    hot_a = np.full((H, W), np.nan, np.float32)
    hot_k = np.full((H, W), np.nan, np.float32)
    ys, xs, amp, _ = hot
    hot_a[ys, xs] = np.log2(1.5 + amp) + 2 / 6
    hot_k[ys, xs] = 1 / 6
    h = fits.Header()
    h["T_REF"], h["T_D"] = 10.0, 6.0
    fits.HDUList([fits.PrimaryHDU(header=h), fits.ImageHDU(np.full((H, W), 400, np.float32), name="B0"),
                  fits.ImageHDU(np.zeros((H, W), np.float32), name="B1"), fits.ImageHDU(A, name="A"),
                  fits.ImageHDU(np.zeros((H, W), np.float32), name="B"), fits.ImageHDU(hot_a, name="HOT_A"),
                  fits.ImageHDU(hot_k, name="HOT_K")]).writeto(os.path.join(d, "model.fits"))
    bad = np.zeros((H, W), np.uint8)
    bad[ys, xs] = 1
    fits.PrimaryHDU(bad).writeto(os.path.join(d, "badpixels.fits"))
    cells = [{"exposure": e, "pedestal": 0.0, "T": 10.0} for e in exps]
    with open(os.path.join(d, "summary.json"), "w") as fh:
        json.dump({"camera": "tele", "gain": 200, "T_range": list(T_range), "cells": cells}, fh)
    with open(os.path.join(data_dir, "darkmodel", "latest.json"), "w") as fh:
        json.dump({"dir": d, "stamp": stamp, "created": stamp}, fh)
    return d


def _setup(make_service, tmp_path, source, **kw):
    svc0 = make_service(speed=300)                          # only to learn the sim's frame size and hot pixels
    cam = svc0.rig.sim.camera
    H, W = cam.CameraYSize, cam.CameraXSize
    hot = cam._hot_pixels(H, W)
    svc0.shutdown(timeout=5)
    _sim_model(str(tmp_path / "data"), H, W, hot, **kw)
    return make_service(speed=300, extra={"darks": {"source": source}})


def test_model_dark_matches_the_sim(make_service, tmp_path):
    svc0 = make_service(speed=300)
    cam = svc0.rig.sim.camera
    H, W = cam.CameraYSize, cam.CameraXSize
    hot = cam._hot_pixels(H, W)
    d = _sim_model(str(tmp_path / "m"), H, W, hot)
    md = ModelDark(os.path.join(str(tmp_path / "m"), "darkmodel"), log=lambda *a: None)
    assert md.load()
    e, dk = md.dark(60.0, 12.0, 200)
    assert e["id"] == "MODEL:20991231" and dk.shape == (H, W)
    expect = 400 + 1.5 * 60 * 2 ** ((12 - 8) / 6)
    assert abs(float(np.median(dk)) - expect) < 0.01
    ys, xs, amp, _ = hot
    assert np.allclose(dk[ys, xs], 400 + (1.5 + amp) * 60 * 2 ** ((12 - 8) / 6), rtol=1e-4)
    assert md.covers(5.0, 12.0) and md.covers(60.0, 12.0, 80) and md.covers(60.0, None)
    assert md.dark(200.0, 12.0) == (None, None)
    assert d                                                # files written by the test, untouched by the class


def test_fill_bad_uses_same_colour_neighbours():
    a = np.arange(64, dtype=np.float32).reshape(8, 8)
    bad = np.zeros((8, 8), bool)
    bad[4, 4] = True
    b = a.copy()
    b[4, 4] = 10000
    f = fill_bad(b, bad)
    assert f[4, 4] == np.median([a[2, 4], a[6, 4], a[4, 2], a[4, 6]])
    assert b[4, 4] == 10000                                 # a copy: the raw frame is untouched


def test_master_stays_the_default(make_service):
    svc = make_service(speed=300)
    assert svc.dark_source == "master" and svc.modeldark is None


def test_compare_adds_a_model_stack_and_leaves_the_master_stack(make_service, tmp_path):
    svc = _setup(make_service, tmp_path, "compare")
    assert svc.cmd_start(P.loads(PLAN))["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done", 90)
    st = svc.status()
    assert st["frames"][-1]["dark"] is None                 # primary: masters as before (none in the library)
    assert st["stack"]["uncalibrated"] == 3 and st["stack"]["model_frames"] == 3
    tdir = os.path.dirname(next(p for p in _walk(svc.s.data_dir) if p.endswith("stack.fits")))
    lights = sorted(p for p in _walk(tdir) if "/lights/" in p)
    assert fits.getheader(lights[-1])["DARKMODL"] == "MODEL:20991231"
    assert fits.getheader(lights[-1])["DARKUSED"] == "NONE"
    raw = fits.getdata(os.path.join(tdir, "stack.fits"))[1]
    mod = fits.getdata(os.path.join(tdir, "stack_model.fits"))[1]
    assert np.median(raw) - np.median(mod) > 400            # bias and dark taken out in the model stack only


def test_model_source_calibrates_and_falls_back_outside_its_range(make_service, tmp_path):
    svc = _setup(make_service, tmp_path, "model", exps=(60.0, 120.0))     # 30 s lights: outside the model
    assert svc.cmd_start(P.loads(PLAN))["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done", 90)
    assert svc.status()["frames"][-1]["dark"] is None
    assert any("outside the model" in x for x in svc.status()["log"])
    cam = svc.rig.sim.camera                                # a refit that covers 30 s: picked up without a restart
    _sim_model(svc.s.data_dir, cam.CameraYSize, cam.CameraXSize, cam._hot_pixels(cam.CameraYSize, cam.CameraXSize),
               stamp="20991232")
    assert svc.cmd_start(P.loads(PLAN))["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done" and (svc.status()["frames"] or [{}])[-1].get("dark"), 90)
    st = svc.status()
    assert st["frames"][-1]["dark"] == "MODEL:20991232" and st["stack"]["uncalibrated"] == 0


def _walk(root):
    for d, _, fs in os.walk(root):
        for f in fs:
            yield os.path.join(d, f)
