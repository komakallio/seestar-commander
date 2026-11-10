"""Dark library: keys, temperature matching, expiry, missing detection, build and use."""
import numpy as np
from astropy.io import fits

from sequencer import plan as P
import os

from sequencer.darklib import DarkLibrary, DarkPolicy, Key, history, read_temp, tbin

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


def test_tbin_edges_go_up():
    # every edge (odd values for 2 C bins) goes to the upper bin, whatever its parity
    for edge in (-5.0, -3.0, -1.0, 1.0, 3.0, 5.0, 7.0, 9.0, 11.0, 13.0, 15.0):
        assert tbin(edge, 2.0) == edge + 1.0, edge
        assert tbin(edge - 0.0625, 2.0) == edge - 1.0            # one 1/16 C step below: lower bin
    assert tbin(10.0, 2.0) == 10.0 and tbin(10.99, 2.0) == 10.0 and tbin(9.0, 2.0) == 10.0
    assert tbin(-0.4, 2.0) == 0.0 and str(tbin(-0.4, 2.0)) == "0.0"     # no -0.0 label
    assert tbin(4.5, 1.0) == 5.0 and tbin(5.5, 1.0) == 6.0
    assert tbin(None, 2.0) is None


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
    assert lib.match(K60, 8.0, fallback=False) is None
    fb = lib.match(K60, 8.0)                                 # soft expiry: newest expired one, flagged
    assert fb["fallback"] is True and fb["status"] == "expired"
    assert lib.match(K60, 13.0) is None                      # still within the temperature window only
    assert [m["exposure"] for m in lib.missing([K60], 8.0)] == [60.0]    # but it counts as missing
    lib.policy.use_expired_fallback = False
    assert lib.match(K60, 8.0) is None


def test_missing(tmp_path):
    lib, _ = _lib(tmp_path)
    lib.add(K60, _frames(), [8.0])
    keys = [K60, Key("tele", 120.0, 80), K60]
    miss = lib.missing(keys, 8.3)
    assert [(m["exposure"], m["status"]) for m in miss] == [(120.0, "missing")]
    assert len(lib.missing(keys, 14.0)) == 2                 # too warm for the 8 C master


def test_rebuild_same_bin_replaces(tmp_path):
    lib, clock = _lib(tmp_path)
    lib.add(K60, _frames(500), [8.0])
    clock.t += 60
    lib.add(K60, _frames(700), [8.4])
    clock.t += 60
    lib.add(K60, _frames(800), [8.2])
    assert len(lib.entries()) == 1
    assert float(np.median(lib.load_master(lib.match(K60, 8.0)))) == 800
    old = history(os.path.join(lib.root, "tele"), "e60_g80_o0_b1_tp8.0")     # nothing deleted
    assert [float(np.median(fits.getdata(os.path.join(h["dir"], "master.fits")))) for h in old] == [500, 700]
    assert all(os.path.exists(os.path.join(h["dir"], "meta.json")) for h in old)


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


def test_match_by_actual_temperature(tmp_path):
    lib, clock = _lib(tmp_path)
    a = lib.add(K60, _frames(500), [12.9], tb=12.0)          # bin +12, taken at its top
    b = lib.add(K60, _frames(600), [14.6], tb=14.0)          # bin +14, taken high in it
    # by bin centre 13.4 would go to +14 (0.6 vs 1.4); by the frames' own temperature +12 is nearer (0.5 vs 1.2)
    assert lib.match(K60, 13.4)["id"] == a["id"]
    assert lib.match(K60, 14.2)["id"] == b["id"]
    assert lib.match(K60, 16.7) is None and lib.match(K60, 16.5)["id"] == b["id"]   # window 2 C from 14.6
    lib.policy.match_tolerance_c = 0.5
    assert lib.match(K60, 13.6) is None and lib.match(K60, 13.3)["id"] == a["id"]
    lib.policy.match_tolerance_c = None
    # an older master without temp_mean is matched by its bin
    import json, os
    meta = os.path.join(a["dir"], "meta.json")
    with open(meta) as fh:
        d = json.load(fh)
    d.pop("temp_mean")
    with open(meta, "w") as fh:
        json.dump(d, fh)
    assert lib.match(K60, 13.4)["id"] == b["id"]             # 12.0 is 1.4 away, 14.6 is 1.2
    assert lib.match(K60, 12.1)["id"] == a["id"]
