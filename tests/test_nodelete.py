"""Nothing is deleted automatically: rebuilds move old masters to _history, expiry is soft (an expired master
is a flagged fallback for lights), pool ageing only stops frames being pooled, and the flat frames are
archived like the dark frames."""
import glob
import json
import os

import numpy as np
from astropy.io import fits

from sequencer import plan as P
from sequencer.darklib import DarkLibrary, DarkPolicy, Key
from sequencer.darkpool import DarkPool

from conftest import NIGHT, ra_at_ha, wait_for

PLAN = f"""
[plan]
park_at_end = false
[[step]]
name = "field"
ra = {ra_at_ha(-20)}
dec = 45
exposure = 20
gain = 200
count = 1
centre = "none"
"""


def _files(root):
    return {os.path.relpath(p, root) for p in glob.glob(os.path.join(root, "**", "*"), recursive=True)
            if os.path.isfile(p)}


class FixedClock:
    def __init__(self, t):
        self.t = t

    def time(self):
        return self.t


def _age(entry, days):
    p = os.path.join(entry["dir"], "meta.json")
    with open(p) as fh:
        m = json.load(fh)
    m["created_unix"] -= days * 86400
    with open(p, "w") as fh:
        json.dump(m, fh)


def test_rebuilds_keep_everything_and_flats_are_archived(make_service):
    svc = make_service(speed=300)
    svc.rig.sim.camera.set_temp(10.0)
    data = svc.s.data_dir
    for i in range(2):
        assert svc.cmd_darks_build(exposure=20, count=3)["ok"]
        assert wait_for(lambda: svc.st.get("darks_result", {}).get("built"), 60) and wait_for(svc.idle, 30)
        svc.st.pop("darks_result")
        assert svc.flats.cmd_build(["IR"], count=3)["ok"]
        assert wait_for(lambda: (svc.flats.state.get("last") or {}).get("built"), 90) and wait_for(svc.idle, 30)
        svc.flats.state["last"] = None
        if i == 0:
            before = _files(data)
    after = _files(data)
    assert before <= after, sorted(before - after)                       # nothing removed by the rebuilds
    assert glob.glob(os.path.join(data, "darklib", "tele", "_history", "e20_g200_o0_b1_tp10.0", "*", "master.fits"))
    assert glob.glob(os.path.join(data, "flatlib", "tele", "_history", "IR_g200_b1_f*", "*", "master.fits"))
    assert len(svc.darklib.entries()) == len({m["id"] for m in svc.darklib.entries()})   # history isn't listed
    with open(os.path.join(data, "flatarchive", "tele", "index.jsonl")) as fh:
        recs = [json.loads(x) for x in fh]
    kinds = {r["kind"] for r in recs}
    assert {"flat", "autoexposure", "darkflat"} <= kinds
    assert sum(r["kind"] == "flat" for r in recs) == 6
    r = next(x for x in recs if x["kind"] == "flat")
    assert r["filter"] == "IR" and r["gain"] == 200 and r["focus"] is not None and 0.3 < r["level_frac"] < 0.6
    assert r["temp_start"] is not None and r["exposure"] > 0
    h = fits.getheader(os.path.join(data, "flatarchive", "tele", r["file"]))
    assert h["IMAGETYP"] == "FLAT" and h["FLATKIND"] == "flat" and h["FILTER"] == "IR"
    d = next(x for x in recs if x["kind"] == "darkflat")
    assert d["filter"] == "Dark" and d["flat_filter"] == "IR"


def test_expiry_and_pool_age_never_delete(tmp_path):
    clock = FixedClock(NIGHT)
    lib = DarkLibrary(str(tmp_path / "lib"), DarkPolicy(), clock)
    k = Key("tele", 20.0, 200)
    root = tmp_path / "arch"
    cdir = root / "tele" / "20261108"
    cdir.mkdir(parents=True)
    with open(root / "tele" / "index.jsonl", "w") as fh:
        for i in range(3):
            fits.writeto(cdir / f"f{i}.fits", np.full((4, 4), 400, np.uint16))
            fh.write(json.dumps({"file": f"20261108/f{i}.fits", "camera": "tele", "exposure": 20.0, "gain": 200,
                                 "start_unix": NIGHT - 60 * i, "temp_start": 10.0, "temp_end": 10.1,
                                 "temp_c": 10.05}) + "\n")
    pool = DarkPool(str(root), lib, clock)
    m = pool.build(k, 10.0, 3)
    all_before = _files(str(tmp_path))
    clock.t += 400 * 86400                                    # far past every age limit
    assert lib.entries()[0]["status"] == "expired" and pool.count(k, 10.0) == 0
    assert lib.match(k, 10.0)["fallback"] is True
    pool.load()
    pool.build(k, 10.0, 1)                                    # nothing eligible: no-op
    lib.summary([k], 10.0)
    assert _files(str(tmp_path)) == all_before
    assert m["id"] == lib.entries()[0]["id"]


def test_expired_master_is_a_flagged_fallback_for_lights(make_service):
    svc = make_service(speed=300)
    svc.rig.sim.camera.set_temp(10.0)
    assert svc.cmd_darks_build(exposure=20, count=3)["ok"]
    assert wait_for(lambda: svc.darklib.entries(), 60) and wait_for(svc.idle, 30)
    m = svc.darklib.entries()[0]
    _age(m, 70)
    assert svc.darklib.entries()[0]["status"] == "expired" and os.path.exists(os.path.join(m["dir"], "master.fits"))
    assert svc.cmd_start(P.loads(PLAN))["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done", 60)
    st = svc.status()
    f = st["frames"][-1]
    assert f["dark"] == m["id"] and f["dark_expired"] is True
    assert any("using EXPIRED" in line for line in st["log"])
    assert st["darklib"]["entries"][0]["note"] == "expired, used as fallback"
    assert [x["exposure"] for x in st["darklib"]["missing"]] == [20.0]       # still asks for a fresh one
    light = sorted(glob.glob(os.path.join(svc.s.data_dir, "sessions", "*", "field", "lights", "*.fits")))[-1]
    h = fits.getheader(light)
    assert h["DARKUSED"] == m["id"] and h["DARKEXP"] is True and h["DARKAGE"] > 69
    cov = svc.harvest.coverage()
    row = next(r for r in cov["rows"] if r["exposure"] == 20.0)
    cell = row["cells"][cov["bins"].index(10.0)]
    assert cell["state"] == "expired" and cell["fallback"] is True and cell["note"] == "expired, used as fallback"
