"""Dark frame pool (sequencer/darkpool.py): per-frame temperature binning, the steadiness and age rules,
pool masters and their metadata, import of an existing archive; the sim sensor thermal model."""
import json
import os

import numpy as np
import pytest
from astropy.io import fits

from sequencer.clock import ScaledClock
from sequencer.darklib import DarkLibrary, DarkPolicy, Key
from sequencer.darkpool import DarkPool, median_stack
from sequencer.sim import SimThermal

from conftest import NIGHT, wait_for

K20 = Key("tele", 20.0, 200)


class FixedClock:
    def __init__(self, t):
        self.t = t

    def time(self):
        return self.t


def _write(root, key, t0, temp0, temp1, value=500, source="test"):
    """One archived frame + its index line, like the service writes them."""
    cdir = os.path.join(root, key.camera)
    day = "20261108"
    rel = f"{day}/{t0:.0f}_e{key.exposure:g}.fits"
    os.makedirs(os.path.join(cdir, day), exist_ok=True)
    fits.writeto(os.path.join(cdir, rel), np.full((6, 4), value, np.uint16), overwrite=True)
    rec = {"file": rel, "camera": key.camera, "exposure": key.exposure, "gain": key.gain, "offset": key.offset,
           "binning": key.binning, "start_unix": t0, "end_unix": t0 + key.exposure, "temp_start": temp0,
           "temp_end": temp1, "temp_c": round((temp0 + temp1) / 2, 3), "source": source}
    with open(os.path.join(cdir, "index.jsonl"), "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec


def _pool(tmp_path, t=NIGHT):
    clock = FixedClock(t)
    lib = DarkLibrary(str(tmp_path / "lib"), DarkPolicy(), clock)
    return DarkPool(str(tmp_path / "arch"), lib, clock), lib, clock


def test_per_frame_bins_steadiness_and_age(tmp_path):
    root = str(tmp_path / "arch")
    for i, (a, b) in enumerate([(10.6, 10.7), (10.8, 10.9), (10.95, 11.1), (11.2, 11.3), (11.3, 11.9)]):
        _write(root, K20, NIGHT - 3600 + 30 * i, a, b)
    _write(root, K20, NIGHT - 15 * 86400, 10.0, 10.1)              # older than 14 days
    P, lib, clock = _pool(tmp_path)
    assert P.count(K20, 10.0) == 2            # 10.65, 10.85
    assert P.count(K20, 12.0) == 2            # 11.025 and 11.25; 11.6 changed 0.6 C: not pooled
    st = P.stats()
    assert st == {"frames": 6, "used": 0, "unsteady": 1, "too_old": 1, "eligible": 4}
    pools = {p["temp_bin"]: p for p in P.summary()["pools"]}
    assert pools[10.0]["n"] == 2 and pools[12.0]["temp_min"] == 11.03
    assert pools[10.0]["drops_in_days"] == pytest.approx(14 - 1 / 24, abs=0.01)
    assert P.build(K20, 10.0, 3) is None                             # not enough yet
    clock.t += 14 * 86400 - 1800                                     # the first frames age out
    assert P.count(K20, 10.0) == 0


def test_pool_master_metadata_and_used(tmp_path):
    root = str(tmp_path / "arch")
    for i, v in enumerate((500, 510, 520)):
        _write(root, K20, NIGHT - 86400 * (2 - i), 9.6 + 0.1 * i, 9.7 + 0.1 * i, v, source="harvest")
    P, lib, _ = _pool(tmp_path)
    m = P.build(K20, 10.0, 3, {"Name": "x"})
    assert m["source"] == "pool" and m["frames"] == 3 and m["temp_bin"] == 10.0
    assert m["frame_files"] == sorted(m["frame_files"]) and len(m["frame_files"]) == 3
    assert m["date_first"] == "2026-11-06T20:00:00Z" and m["date_last"].startswith("2026-11-08T20:00")
    assert m["temp_min"] == 9.65 and m["temp_max"] == 9.85 and m["sources"] == ["harvest"]
    assert float(np.median(lib.load_master(lib.match(K20, 10.0)))) == 510
    assert P.count(K20, 10.0) == 0 and P.stats()["used"] == 3
    P2 = DarkPool(root, lib, FixedClock(NIGHT))                      # used.jsonl survives a restart
    assert P2.count(K20, 10.0) == 0 and P2.stats()["used"] == 3
    assert os.path.exists(os.path.join(root, "tele", m["frame_files"][0]))   # frames stay archived


def test_import_is_idempotent(tmp_path):
    root = str(tmp_path / "arch")
    rec = _write(root, K20, NIGHT - 60, 9.9, 10.0)
    with open(os.path.join(root, "tele", "index.jsonl"), "a") as fh:
        fh.write(json.dumps(rec) + "\n")                             # listed twice
        fh.write("not json\n")
        fh.write(json.dumps({**rec, "file": "20261108/gone.fits"}) + "\n")   # file deleted
    P, _, _ = _pool(tmp_path)
    assert P.stats()["frames"] == 1 and P.load() == 1 and P.count(K20, 10.0) == 1


def test_median_stack_chunks():
    rng = np.random.default_rng(1)
    fr = [rng.integers(0, 1000, (700, 9)).astype(np.uint16) for _ in range(5)]
    assert np.array_equal(median_stack(fr, rows=64), np.median(np.stack(fr), axis=0).astype(np.float32))


def test_sim_thermal_model():
    clock = ScaledClock(1.0, NIGHT)
    th = SimThermal(clock, ambient_c=5.0)
    assert th.read(None) == pytest.approx(11.0)
    t0 = th.t
    # 6 minutes exposing: ~1.3 C like the real telephoto, then an hour idle: back to ~ambient + 6
    th._step(t0, t0 + 360, True)
    assert th.temp - 11.0 == pytest.approx(1.3, abs=0.15)
    th._step(t0 + 360, t0 + 4000, False)
    assert th.temp == pytest.approx(11.0, abs=0.05)


def test_darks_build_splits_at_a_bin_boundary(make_service):
    svc = make_service(speed=300)
    svc.rig.sim.camera.set_temp(10.8, rate_c_h=12.0)                # warming through 11 (bin 10 | bin 12)
    assert svc.cmd_darks_build(exposure=20, count=8)["ok"]
    assert wait_for(lambda: "darks_result" in svc.st, 90) and wait_for(svc.idle, 30)
    res = svc.st["darks_result"]
    P = svc.darkpool
    n10, n12 = P.count(svc.dark_key(20, 200), 10.0), P.count(svc.dark_key(20, 200), 12.0)
    assert n10 >= 1 and n12 >= 1 and n10 + n12 == 8
    assert res["built"] == [] and sorted(p["temp_bin"] for p in res["partial"]) == [10.0, 12.0]
    assert any("partial pools" in line for line in svc.st["log"])
    recs = [json.loads(x) for x in open(os.path.join(svc.s.data_dir, "darkarchive", "tele", "index.jsonl"))]
    assert all(r["steady"] for r in recs) and len(recs) == 8
    # the next build at the warmer temperature completes bin 12 from both builds' frames
    svc.rig.sim.camera.set_temp(11.6)
    assert svc.cmd_darks_build(exposure=20, count=8)["ok"]
    assert wait_for(lambda: svc.st["darks_result"]["built"], 90) and wait_for(svc.idle, 30)
    m = svc.darklib.match(svc.dark_key(20, 200), 11.6)
    assert m["source"] == "pool" and m["frames"] == 8 + n12 and m["temp_bin"] == 12.0
    assert svc.dark_summary()["pool"]["pools"][0]["temp_bin"] == 10.0     # bin 10 still waits


def test_unsteady_frames_not_pooled(make_service):
    svc = make_service(speed=300)
    svc.rig.sim.camera.set_temp(10.0, rate_c_h=120.0)                # 0.67 C per 20 s frame
    assert svc.cmd_darks_build(exposure=20, count=3)["ok"]
    assert wait_for(lambda: "darks_result" in svc.st, 60) and wait_for(svc.idle, 30)
    res = svc.st["darks_result"]
    assert res["built"] == [] and res["unsteady"] == 3 and res["partial"] == []
    assert svc.darkpool.stats()["unsteady"] == 3 and svc.darklib.entries() == []
    assert any("NOT POOLED" in line for line in svc.st["log"])


def test_restart_imports_the_archive(make_service):
    svc = make_service(speed=300)
    svc.rig.sim.camera.set_temp(9.9)
    assert svc.cmd_darks_build(exposure=5, count=4)["ok"]
    assert wait_for(lambda: "darks_result" in svc.st, 60) and wait_for(svc.idle, 30)
    assert svc.st["darks_result"]["built"]
    svc.rig.sim.camera.set_temp(14.0)
    svc.st.pop("darks_result")
    assert svc.cmd_darks_build(exposure=5, count=4) and wait_for(lambda: "darks_result" in svc.st, 60)
    svc.shutdown()
    again = DarkPool(os.path.join(svc.s.data_dir, "darkarchive"), svc.darklib, svc.clock)
    assert again.stats()["frames"] == 8 and again.stats()["used"] == 8
