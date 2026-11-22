"""Dark-current analysis on a synthetic archive with a known model: recovers T_d, finds the hot pixels,
validates near the noise floor, writes the report and leaves the archive untouched."""
import json
import os

import numpy as np
from astropy.io import fits

from sequencer import darkmodel as DM

H, W = 120, 240
TD, IREF, RN = 6.0, 0.05, 18.0
DAY = 86400.0
T0 = 1794000000.0


def _archive(root, rng, step_day=None, step=0.0):
    cdir = os.path.join(root, "darkarchive", "tele")
    os.makedirs(cdir)
    b0 = 1100 + rng.normal(0, 3, (H, W))
    b1 = np.full((H, W), 2.0)
    I = np.abs(rng.normal(IREF, 0.01, (H, W)))
    I[:, :60] *= 3                                          # glow: the left-most block column
    hot = [(int(y), int(x)) for y, x in zip(rng.integers(0, H, 12), rng.integers(60, W, 12))]
    for y, x in hot:
        I[y, x] = 40.0
    recs, k = [], 0
    for day in range(4):
        for T in range(6, 21, 2):
            for exp in (0.001, 10, 30, 60, 120):
                for i in range(6):
                    t = T + rng.uniform(-0.3, 0.3)
                    x = t - DM.T_REF
                    f = b0 + b1 * x + exp * I * 2 ** (x / TD) + rng.normal(0, RN, (H, W))
                    if step_day is not None and day >= step_day:
                        f += step
                    f = np.clip(np.round(f / 4) * 4, 0, 65535).astype(np.uint16)
                    unix = T0 + day * DAY + k * 10
                    rel = f"{day:02d}/f{k:05d}.fits"
                    os.makedirs(os.path.join(cdir, f"{day:02d}"), exist_ok=True)
                    fits.PrimaryHDU(f).writeto(os.path.join(cdir, rel))
                    recs.append({"file": rel, "camera": "tele", "exposure": exp, "gain": 200, "offset": 0, "binning": 1,
                                 "start_unix": unix, "start": f"day{day}", "temp_start": t - 0.05, "temp_end": t + 0.05,
                                 "temp_c": t, "mean": float(f.mean())})
                    k += 1
    with open(os.path.join(cdir, "index.jsonl"), "w") as fh:
        fh.write("\n".join(json.dumps(r) for r in recs) + "\n")
    return hot


def _snapshot(root):
    out = {}
    for d, _, fs in os.walk(os.path.join(root, "darkarchive")):
        for f in fs:
            p = os.path.join(d, f)
            out[p] = os.path.getsize(p), os.path.getmtime(p)
    return out


def test_model_recovers_known_sensor(tmp_path):
    rng = np.random.default_rng(1)
    hot = _archive(str(tmp_path), rng)
    before = _snapshot(str(tmp_path))
    out, s = DM.run(str(tmp_path), log=lambda *a: None, now=T0 + 5 * DAY)
    assert abs(s["Td"] - TD) < 0.6
    assert abs(s["read_noise_adu"] - RN) < 1.5
    assert s["classes"]["counts"]["hot"] >= len(hot)
    bad = fits.getdata(os.path.join(out, "badpixels.fits"))
    assert all(bad[y, x] & DM.BAD_HOT for y, x in hot)
    dark = [c for c in s["cells"] if c["exposure"] > DM.BIAS_MAX_EXP]
    assert np.median([c["ratio"] for c in dark]) < 1.5       # leave-one-out near the noise floor
    assert s["hot_td"]["fitted"] >= len(hot) and abs(s["hot_td"]["td_median"] - TD) < 1.0
    assert s["glow"]["max_ratio"] > 2                        # the left-edge glow shows up
    assert s["ageing"]["days"] and max(abs(d["level_shift"]) for d in s["ageing"]["days"]) < 2
    for name in ("report.md", "summary.json", "model.fits", "dark_current.svg", "loo.svg", "iref_map.png"):
        assert os.path.exists(os.path.join(out, name)), name
    assert _snapshot(str(tmp_path)) == before                # read-only on the archive
    # a rerun reuses the cached cells
    out2, s2 = DM.run(str(tmp_path), log=lambda *a: None, now=T0 + 5 * DAY)
    assert s2["Td"] == s["Td"]


def test_ageing_sees_a_level_step(tmp_path):
    rng = np.random.default_rng(2)
    _archive(str(tmp_path), rng, step_day=3, step=12.0)
    _, s = DM.run(str(tmp_path), log=lambda *a: None, now=T0 + 5 * DAY)
    days = s["ageing"]["days"]
    old = [d for d in days if d["age_days"] > 0.5]
    assert old and all(d["level_shift"] < -8 for d in old)   # older masters sit ~12 ADU below the newest
