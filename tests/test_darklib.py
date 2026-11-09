"""Dark library: keys, temperature matching, expiry, missing detection, build and use."""
import numpy as np
from astropy.io import fits

from sequencer import plan as P
from sequencer.darklib import DarkLibrary, DarkPolicy, Key, read_temp

from conftest import NIGHT, ra_at_ha, wait_for

K60 = Key("tele", 60.0, 80)


class FixedClock:
    def __init__(self, t):
        self.t = t

    def time(self):
        return self.t


def _lib(tmp_path, t=NIGHT, **pol):
    clock = FixedClock(t)
    return DarkLibrary(str(tmp_path / "lib"), DarkPolicy(**pol), clock), clock


def _frames(v=500):
    return [np.full((8, 8), v, np.uint16) for _ in range(3)]


def test_read_temp():
    assert read_temp(8.2) == 8.2
    assert read_temp(0) is None and read_temp(0.0) is None      # wide camera: 0 = no reading
    assert read_temp(None) is None and read_temp("x") is None


def test_match_nearest_temperature(tmp_path):
    lib, _ = _lib(tmp_path)
    lib.add(K60, _frames(500), [7.9, 8.1])          # bin 8
    lib.add(K60, _frames(600), [12.0, 12.2])        # bin 12
    assert lib.match(K60, 8.4)["temp_bin"] == 8.0
    assert lib.match(K60, 11.0)["temp_bin"] == 12.0
    assert lib.match(K60, 10.5)["temp_bin"] == 12.0   # 1.5 from 12 < 2.5 from 8
    assert lib.match(K60, 15.5) is None               # 3.5 > bin/2 + tolerance = 2
    assert lib.match(Key("tele", 60.0, 100), 8.0) is None   # other gain
    assert lib.match(Key("tele", 60.0, 80, offset=10), 8.0) is None
    assert lib.match(Key("wide", 60.0, 80), 8.0) is None
    m = lib.match(K60, 8.0)
    assert m["frames"] == 3 and m["temp_min"] == 7.9 and m["temp_max"] == 8.1
    assert float(np.median(lib.load_master(m))) == 500


def test_no_temperature_reading(tmp_path):
    lib, _ = _lib(tmp_path)
    lib.add(Key("wide", 10.0, 80), _frames(), [0, 0])
    m = lib.match(Key("wide", 10.0, 80), 0)
    assert m is not None and m["temp_bin"] is None
    assert lib.match(Key("wide", 10.0, 80), 8.0) is None        # a real reading needs a real bin


def test_expiry(tmp_path):
    lib, clock = _lib(tmp_path, max_age_days=60, expiring_days=7)
    lib.add(K60, _frames(), [8.0])
    assert lib.entries()[0]["status"] == "fresh"
    clock.t += 55 * 86400
    assert lib.entries()[0]["status"] == "expiring" and lib.match(K60, 8.0) is not None
    clock.t += 6 * 86400
    assert lib.entries()[0]["status"] == "expired"
    assert lib.match(K60, 8.0) is None


def test_missing(tmp_path):
    lib, _ = _lib(tmp_path)
    lib.add(K60, _frames(), [8.0])
    keys = [K60, Key("tele", 120.0, 80), K60]
    miss = lib.missing(keys, 8.3)
    assert [(m["exposure"], m["status"]) for m in miss] == [(120.0, "missing")]
    assert len(lib.missing(keys, 14.0)) == 2                 # too warm for the 8 C master


def test_rebuild_same_bin_replaces(tmp_path):
    lib, _ = _lib(tmp_path)
    lib.add(K60, _frames(500), [8.0])
    lib.add(K60, _frames(700), [8.4])
    assert len(lib.entries()) == 1
    assert float(np.median(lib.load_master(lib.match(K60, 8.0)))) == 700


PLAN = f"""
[plan]
park_at_end = false
[[step]]
name = "field"
ra = {ra_at_ha(-20)}
dec = 45
exposure = 20
gain = 80
count = 2
centre = "none"
"""


def test_build_missing_and_use(make_service):
    svc = make_service(speed=300)
    cam = svc.rig.sim.camera
    cam.set_temp(8.3)
    pl = P.loads(PLAN)
    # nothing in the library: lights stack uncalibrated and are flagged
    svc.cmd_start(pl)
    assert wait_for(lambda: svc.status()["state"] == "done", 60)
    st = svc.status()
    assert [f["dark"] for f in st["frames"]] == [None, None]
    assert st["stack"]["uncalibrated"] == 2
    assert [m["exposure"] for m in st["darklib"]["missing"]] == [20.0]

    # build what the plan is missing: wheel to Dark and back, master stored with temperatures
    r = svc.cmd_darks_build(missing=True, count=3)
    assert r["ok"] and r["items"] == ["tele 20s g80 o0 b1"]
    assert wait_for(svc.idle, 60)
    assert svc.imager.filter_name() == "IR"
    st = svc.status()
    assert st["darklib"]["missing"] == []
    e = st["darklib"]["entries"][0]
    assert e["frames"] == 3 and e["temp_bin"] == 8.0 and e["status"] == "fresh"
    assert any("re-stacked 2 lights (0 uncalibrated," in line for line in st["log"])

    # new lights now pick the master and record it
    svc.cmd_start(pl)
    assert wait_for(lambda: svc.status()["state"] == "done", 60)
    st = svc.status()
    assert all(f["dark"] == e["id"] for f in st["frames"])
    assert st["stack"]["uncalibrated"] == 0
    import glob
    import os
    lights = sorted(glob.glob(os.path.join(svc.s.data_dir, "sessions", "*", "field", "lights", "*.fits")))
    assert fits.getheader(lights[-1])["DARKUSED"] == e["id"]

    # the sensor warms up by 4 C: the master no longer matches
    cam.set_temp(12.5)
    assert wait_for(lambda: svc.status()["darklib"]["missing"] != [], 10)


def test_build_explicit_and_refused_while_running(make_service):
    svc = make_service(speed=300)
    assert not svc.cmd_darks_build()["ok"]                       # nothing specified
    assert svc.cmd_darks_build(exposure=5, gain=120, count=2)["ok"]
    assert wait_for(svc.idle, 30)
    assert svc.darklib.match(Key("tele", 5.0, 120), svc.imager.temperature()) is not None
    svc.cmd_start(P.loads(PLAN.replace("count = 2", "count = 50")))
    assert wait_for(lambda: svc.busy == "run", 10)
    assert not svc.cmd_darks_build(exposure=5, gain=120)["ok"]
    svc.cmd_stop()
    assert wait_for(svc.idle, 20)
