"""Dark-current analysis over the dark archive (dark harvest plan, section 6). Offline, read-only on the
archive: it never changes the library, the pool or a frame.

    python -m sequencer darks analyse [--data DIR] [--out DIR] [--camera tele] [--gain 200]

Steps:
  1. **Cells.** Steady frames (temperature change <= 0.5 C during the exposure) are grouped by exposure
     and 1 C of temperature; each cell with >= min_frames gets a median master (newest max_frames
     frames), cached as .npy under <out>/cells/ so a rerun only builds new cells.
  2. **Noise.** Read noise from bias-frame pairs; a per-pixel standard deviation over up to
     max_bias_frames bias frames (noisy and random-telegraph pixels).
  3. **Model, per pixel** (row tiles, memory stays ~100 MB):
         dark(T, t) = b0 + b1 (T - T_ref) + t * I_ref * 2^((T - T_ref) / T_d)
     bias b0, b1 from the bias cells (weighted LS); one global doubling temperature T_d from the
     clipped mean dark current of every cell; I_ref per pixel (weighted LS, closed form).
     Leave-one-out residuals come from the hat matrix (r / (1 - h)): no refits. Hot pixels get their
     own T_d (log-linear fit).
  4. **Glow:** I_ref averaged in blocks; per-block dark current per exposure (linear in t or not).
  5. **Temperature lag:** each archived frame's mean (index) against the model, by temperature change.
  6. **Ageing:** daily masters of one key (the sentinel exposure) in its most populated 1 C bin; level
     shift, pattern correlation, hot-pixel turnover and the extra residual of calibrating a fresh frame
     with an older master, after correcting the temperature difference with the model.
  7. **Outputs** in <out>/<YYYYMMDD>/: report.md, *.svg plots, maps *.png, model.fits (b0, b1, I_ref,
     T_d in the header), badpixels.fits (bits: 1 hot, 2 warm, 4 noisy, 8 low flat response),
     summary.json. <out>/latest.json points at the newest run.

All values are ADU on the 16-bit scale (the absolute e-/ADU isn't measured yet).
"""
import datetime as dt
import json
import math
import os
import time

import numpy as np
from astropy.io import fits

from .darklib import read_temp
from .darkpool import _read_jsonl, median_stack

T_REF = 10.0
BIAS_MAX_EXP = 0.01
BAD_HOT, BAD_WARM, BAD_NOISY, BAD_LOWFLAT = 1, 2, 4, 8
MEDIAN_EFF = math.sqrt(math.pi / 2)    # noise of a median of n Gaussian frames: 1.25 sigma / sqrt n


# ---- index ---------------------------------------------------------------------------------------
def _frame_temp(r):
    t = read_temp(r.get("temp_c"))
    if t is None:
        ts = [x for x in (read_temp(r.get("temp_start")), read_temp(r.get("temp_end"))) if x is not None]
        t = sum(ts) / len(ts) if ts else None
    return t


def _temp_change(r):
    a, b = read_temp(r.get("temp_start")), read_temp(r.get("temp_end"))
    return None if a is None or b is None else b - a


def load_index(archive, camera="tele", gain=200, offset=0, binning=1, max_temp_change=0.5):
    """Index records of one camera and gain whose FITS exists, with T (frame temperature), dT (change
    during the exposure), steady and path added."""
    cdir = os.path.join(archive, camera)
    out, seen = [], set()
    for r in _read_jsonl(os.path.join(cdir, "index.jsonl")):
        if not isinstance(r, dict) or not r.get("file") or r["file"] in seen:
            continue
        try:
            if (int(r.get("gain", -1)), int(r.get("offset", 0)), int(r.get("binning", 1))) != (gain, offset, binning):
                continue
            exp = float(r["exposure"])
        except (TypeError, ValueError, KeyError):
            continue
        p = os.path.join(cdir, r["file"])
        T = _frame_temp(r)
        if T is None or not os.path.exists(p):
            continue
        seen.add(r["file"])
        d = _temp_change(r)
        out.append({**r, "exposure": exp, "path": p, "T": T, "dT": d,
                    "steady": d is None or abs(d) <= max_temp_change + 1e-9})
    out.sort(key=lambda r: float(r.get("start_unix", 0)))
    return out


def _read(path):
    return np.asarray(fits.getdata(path), np.float32)


# ---- 1. cells --------------------------------------------------------------------------------------
def make_cells(recs, cache, min_frames=5, max_frames=15, log=print):
    """Median masters per (exposure, 1 C) of steady frames. Returns a list of dicts with the master as a
    read-only memmap (cell["data"])."""
    os.makedirs(cache, exist_ok=True)
    groups = {}
    for r in recs:
        if r["steady"]:
            groups.setdefault((r["exposure"], int(round(r["T"]))), []).append(r)
    cells = []
    for (exp, tc), rs in sorted(groups.items()):
        if len(rs) < min_frames:
            continue
        rs = rs[-max_frames:]                                  # newest
        name = f"e{exp:g}_t{tc:+d}"
        files = [r["file"] for r in rs]
        npy, meta_p = os.path.join(cache, name + ".npy"), os.path.join(cache, name + ".json")
        meta = None
        if os.path.exists(npy) and os.path.exists(meta_p):
            try:
                with open(meta_p) as fh:
                    meta = json.load(fh)
            except ValueError:
                meta = None
        if not meta or meta.get("files") != files:
            t0 = time.time()
            m = median_stack([_read(r["path"]) for r in rs])
            np.save(npy + ".tmp.npy", m)
            os.replace(npy + ".tmp.npy", npy)
            meta = {"exposure": exp, "tcell": tc, "files": files, "n": len(rs),
                    "T": float(np.mean([r["T"] for r in rs])),
                    "T_min": float(min(r["T"] for r in rs)), "T_max": float(max(r["T"] for r in rs)),
                    "unix_first": float(rs[0].get("start_unix", 0)), "unix_last": float(rs[-1].get("start_unix", 0))}
            with open(meta_p, "w") as fh:
                json.dump(meta, fh)
            log(f"cell {name}: {len(rs)} frames, {time.time() - t0:.0f} s")
        cells.append({**meta, "name": name, "data": np.load(npy, mmap_mode="r")})
    return cells


# ---- 2. noise ------------------------------------------------------------------------------------
def read_noise(bias_recs, crop=600):
    """Read noise (ADU) from pairs of consecutive bias frames: std(a - b) / sqrt 2 over a central crop."""
    vals = []
    for a, b in zip(bias_recs[0::2], bias_recs[1::2]):
        if abs(a["T"] - b["T"]) > 0.5:
            continue
        fa, fb = _read(a["path"]), _read(b["path"])
        h, w = fa.shape
        y0, x0 = max(0, h // 2 - crop // 2), max(0, w // 2 - crop // 2)
        d = (fa - fb)[y0:y0 + crop, x0:x0 + crop]
        med = np.median(d)
        mad = 1.4826 * np.median(np.abs(d - med)) + 1e-9
        d = d[np.abs(d - med) < 6 * mad]
        vals.append(float(np.std(d)) / math.sqrt(2))
        if len(vals) >= 20:
            break
    return float(np.median(vals)) if vals else None, len(vals)


def bias_std_map(bias_recs, max_frames=200):
    """Per-pixel std over bias frames, after removing each frame's own mean level (bias drifts with
    temperature; that's not pixel noise)."""
    rs = bias_recs[-max_frames:]
    n, mean, m2 = 0, None, None
    for r in rs:
        f = _read(r["path"])
        f = f - np.float32(np.mean(f))
        n += 1
        if mean is None:
            mean, m2 = np.zeros_like(f), np.zeros_like(f)
        d = f - mean
        mean += d / n
        m2 += d * (f - mean)
    if n < 3:
        return None, n
    return np.sqrt(m2 / (n - 1)).astype(np.float32), n


# ---- 3. model ------------------------------------------------------------------------------------
def _clipped(v, k=5.0):
    v = v.ravel()
    med = np.median(v)
    mad = 1.4826 * np.median(np.abs(v - med)) + 1e-9
    m = np.abs(v - med) < k * mad
    return v[m]


def _wls_line(x, y, w):
    """Weighted LS y = a + b x; returns a, b, stderr_b."""
    x, y, w = (np.asarray(v, float) for v in (x, y, w))
    sw = w.sum()
    xm, ym = (w * x).sum() / sw, (w * y).sum() / sw
    sxx = (w * (x - xm) ** 2).sum()
    b = (w * (x - xm) * (y - ym)).sum() / sxx if sxx > 0 else 0.0
    a = ym - b * xm
    res = y - a - b * x
    dof = max(1, len(x) - 2)
    s2 = (w * res ** 2).sum() / dof
    seb = math.sqrt(s2 / sxx) if sxx > 0 else float("nan")
    return a, b, seb


def fit_model(cells, rn, tile=32, block=60, log=print):
    """Per-pixel bias and dark-current model. Returns a dict of maps and per-cell statistics."""
    bias = [c for c in cells if c["exposure"] <= BIAS_MAX_EXP]
    dark = [c for c in cells if c["exposure"] > BIAS_MAX_EXP]
    if len(bias) < 2 or not dark:
        raise ValueError(f"need >= 2 bias cells and >= 1 dark cell (have {len(bias)} and {len(dark)})")
    h, w = bias[0]["data"].shape
    xb = np.array([c["T"] - T_REF for c in bias])
    wb = np.array([c["n"] for c in bias], float)
    swb = wb.sum()
    xbm = (wb * xb).sum() / swb
    sxxb = (wb * (xb - xbm) ** 2).sum()
    hb = wb * (1 / swb + (xb - xbm) ** 2 / sxxb)          # leverage of each bias cell
    td = np.array([c["exposure"] for c in dark])
    xd = np.array([c["T"] - T_REF for c in dark])
    nd = np.array([c["n"] for c in dark], float)
    b0 = np.zeros((h, w), np.float32)
    b1 = np.zeros((h, w), np.float32)

    # pass 1: bias maps and the clipped mean dark signal of every dark cell
    sums, cnts = np.zeros(len(dark)), np.zeros(len(dark))
    bias_loo = [[] for _ in bias]
    for y0 in range(0, h, tile):
        y1 = min(h, y0 + tile)
        Mb = np.stack([c["data"][y0:y1] for c in bias]).astype(np.float64)
        ym = np.tensordot(wb, Mb, 1) / swb
        sl = np.tensordot(wb * (xb - xbm), Mb - ym, 1) / sxxb
        a = ym - sl * xbm
        b0[y0:y1], b1[y0:y1] = a, sl
        for i in range(len(bias)):
            r = (Mb[i] - a - sl * xb[i]) / (1 - hb[i])
            bias_loo[i].append(_clipped(r[::3, ::3]))
        for j, c in enumerate(dark):
            S = c["data"][y0:y1] - (a + sl * xd[j])
            v = _clipped(S[::2, ::2])
            sums[j] += v.sum()
            cnts[j] += v.size
    smean = sums / np.maximum(cnts, 1)

    # global dark current against temperature (cells with >= 10 s and a positive signal)
    sel = (td >= 10) & (smean > 0)
    if sel.sum() < 3:
        raise ValueError("too few dark cells with a measurable signal for the temperature fit")
    rate = smean / td
    a, slope, se = _wls_line(xd[sel], np.log2(rate[sel]), nd[sel] * td[sel] ** 2 * rate[sel] ** 2)
    Td = 1.0 / slope if slope > 0 else float("inf")
    Td_se = se / slope ** 2 if slope > 0 else float("nan")
    I0 = 2.0 ** a
    log(f"global: I({T_REF:g} C) = {I0:.4f} ADU/s, T_d = {Td:.2f} +- {Td_se:.2f} C")
    g = 2.0 ** (xd / Td)
    xs = td * g                                            # regressor per dark cell
    sxx = (nd * xs ** 2).sum()
    hd = nd * xs ** 2 / sxx

    # pass 2: per-pixel I_ref, leave-one-out residuals, block sums for the glow map
    Iref = np.zeros((h, w), np.float32)
    loo = [[] for _ in dark]
    nby, nbx = h // block, w // block
    bsum = np.zeros((len(dark), nby, nbx))
    bcnt = np.zeros((nby, nbx))
    for y0 in range(0, h, tile):
        y1 = min(h, y0 + tile)
        S = np.stack([c["data"][y0:y1] for c in dark]).astype(np.float64) \
            - (b0[y0:y1].astype(np.float64)[None] + b1[y0:y1].astype(np.float64)[None] * xd[:, None, None])
        I = np.tensordot(nd * xs, S, 1) / sxx
        Iref[y0:y1] = I
        for j in range(len(dark)):
            r = (S[j] - I * xs[j]) / (1 - hd[j])
            loo[j].append(_clipped(r[::3, ::3]))
        iv = I[::2, ::2].ravel()
        imed = np.median(iv)
        keep = I < imed + 10 * 1.4826 * np.median(np.abs(iv - imed)) + 1e-9   # hot pixels out of the glow blocks
        for by in range(y0 // block, min(nby, -(-y1 // block))):
            ya, yb = max(y0, by * block), min(y1, (by + 1) * block)
            if ya >= yb:
                continue
            km = keep[ya - y0:yb - y0, :nbx * block]
            seg = (S[:, ya - y0:yb - y0, :nbx * block] * km[None]).reshape(len(dark), yb - ya, nbx, block)
            bsum[:, by] += seg.sum(axis=(1, 3))
            bcnt[by] += km.reshape(yb - ya, nbx, block).sum(axis=(0, 2))

    def _rms(parts):
        v = np.concatenate(parts)
        return float(np.sqrt(np.mean(v ** 2)))

    cell_stats = []
    for i, c in enumerate(bias):
        cell_stats.append(_cell_stat(c, _rms(bias_loo[i]), rn, None))
    for j, c in enumerate(dark):
        cell_stats.append(_cell_stat(c, _rms(loo[j]), rn, smean[j]))
    blocks = bsum / np.maximum(bcnt, 1)[None]
    return {"b0": b0, "b1": b1, "Iref": Iref, "Td": Td, "Td_se": Td_se, "I0": I0, "T_ref": T_REF,
            "dark_cells": dark, "bias_cells": bias, "smean": smean, "cells": cell_stats,
            "blocks": blocks, "block": block}


def _cell_stat(c, loo_rms, rn, smean):
    floor = MEDIAN_EFF * rn / math.sqrt(c["n"]) if rn else None
    return {"name": c["name"], "exposure": c["exposure"], "T": round(c["T"], 2), "n": c["n"],
            "signal": None if smean is None else round(float(smean), 3),
            "loo_rms": round(loo_rms, 3), "floor": None if floor is None else round(floor, 3),
            "ratio": None if not floor else round(loo_rms / floor, 2)}


# ---- hot pixels, bad-pixel map ---------------------------------------------------------------------
def classify(Iref, std_map, rn, flat=None, hot_k=10.0, warm_k=5.0, noisy_k=3.0):
    v = _clipped(Iref[::4, ::4])
    med, mad = float(np.median(v)), float(1.4826 * np.median(np.abs(v - np.median(v))) + 1e-12)
    bad = np.zeros(Iref.shape, np.uint8)
    hot_thr = max(med + hot_k * mad, med + 5 * rn / 60.0)  # also >= 5 read noise in 60 s
    warm_thr = med + warm_k * mad
    bad[Iref > hot_thr] |= BAD_HOT
    bad[(Iref > warm_thr) & (Iref <= hot_thr)] |= BAD_WARM
    noisy_thr = None
    if std_map is not None:
        s = _clipped(std_map[::4, ::4])
        noisy_thr = float(np.median(s)) * noisy_k
        bad[std_map > noisy_thr] |= BAD_NOISY
    if flat is not None and flat.shape == Iref.shape:
        bad[flat < 0.5] |= BAD_LOWFLAT
    counts = {k: int(((bad & bit) > 0).sum()) for k, bit in
              (("hot", BAD_HOT), ("warm", BAD_WARM), ("noisy", BAD_NOISY), ("low_flat", BAD_LOWFLAT))}
    return bad, {"I_median": med, "I_mad": mad, "hot_threshold": hot_thr, "warm_threshold": warm_thr,
                 "noisy_threshold": noisy_thr, "counts": counts, "pixels": int(Iref.size)}


def hot_pixel_td(model, bad, rn, max_pixels=20000):
    """Per-pixel doubling temperature for hot pixels: log2(S/t) linear in T over cells >= 20 s."""
    ys, xs = np.nonzero(bad & BAD_HOT)
    if ys.size == 0:
        return {"n": 0}
    if ys.size > max_pixels:
        k = np.argsort(model["Iref"][ys, xs])[-max_pixels:]
        ys, xs = ys[k], xs[k]
    dark = [c for c in model["dark_cells"] if c["exposure"] >= 20]
    T = np.array([c["T"] - T_REF for c in dark])
    t = np.array([c["exposure"] for c in dark])
    S = np.stack([np.asarray(c["data"][ys, xs], np.float64) for c in dark]) \
        - (model["b0"][ys, xs][None] + model["b1"][ys, xs][None] * T[:, None])
    tds = []
    for k in range(ys.size):
        s = S[:, k]
        m = s > 5 * rn
        if m.sum() < 3 or np.ptp(T[m]) < 4:
            continue
        _, sl, _ = _wls_line(T[m], np.log2(s[m] / t[m]), np.ones(m.sum()))
        if sl > 0:
            tds.append(1.0 / sl)
    tds = np.array(tds)
    if tds.size == 0:
        return {"n": int(ys.size), "fitted": 0}
    return {"n": int(ys.size), "fitted": int(tds.size), "td_median": float(np.median(tds)),
            "td_p10": float(np.percentile(tds, 10)), "td_p90": float(np.percentile(tds, 90)),
            "td_values": tds}


# ---- 5. temperature lag ----------------------------------------------------------------------------
def temperature_lag(recs, model):
    """Each frame's mean (from the index, no FITS reads) against the model's prediction, grouped by the
    temperature change during the exposure."""
    b0m, b1m, Im = float(np.mean(model["b0"])), float(np.mean(model["b1"])), float(np.mean(model["Iref"]))
    rows = []
    for r in recs:
        if r["exposure"] < 30 or r.get("mean") is None or r["dT"] is None:
            continue
        x = r["T"] - T_REF
        pred = b0m + b1m * x + r["exposure"] * Im * 2 ** (x / model["Td"])
        rows.append((r["dT"], (float(r["mean"]) - pred) / r["exposure"], r["exposure"]))
    edges = [-9, -0.5, -0.1, 0.1, 0.5, 1.0, 2.0, 9]
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        v = [res for d, res, _ in rows if lo <= d < hi]
        if v:
            out.append({"dT_from": lo, "dT_to": hi, "frames": len(v), "residual_adu_s": float(np.median(v)),
                        "spread_adu_s": float(np.percentile(v, 84) - np.percentile(v, 16)) / 2})
    return {"bins": out, "points": rows}


# ---- 6. ageing -------------------------------------------------------------------------------------
def ageing(recs, model, rn, exposure=60.0, min_frames=5, max_frames=15, log=print):
    rs = [r for r in recs if r["steady"] and abs(r["exposure"] - exposure) < 1e-6]
    if not rs:
        return {"exposure": exposure, "days": []}
    by = {}
    for r in rs:
        day = dt.datetime.fromtimestamp(float(r.get("start_unix", 0)), dt.timezone.utc).strftime("%Y-%m-%d")
        by.setdefault(int(round(r["T"])), {}).setdefault(day, []).append(r)
    tc, days = max(by.items(), key=lambda kv: sum(len(v) >= min_frames for v in kv[1].values()))
    days = {d: v[-max_frames:] for d, v in sorted(days.items()) if len(v) >= min_frames + (0 if d != max(days) else 1)}
    if len(days) < 2:
        return {"exposure": exposure, "tcell": tc, "days": []}
    ref_day = max(days)
    test = days[ref_day][-1]                               # fresh frame, kept out of the reference master
    ref_rs = days[ref_day][:-1]
    F = _read(test["path"])
    b0, b1, I, Td = model["b0"], model["b1"], model["Iref"], model["Td"]

    def bias_at(T):
        return b0 + b1 * np.float32(T - T_REF)

    def master(rr):
        return median_stack([_read(r["path"]) for r in rr]), float(np.mean([r["T"] for r in rr]))

    Mref, Tref_day = master(ref_rs)
    Sref = Mref - bias_at(Tref_day)
    v = _clipped(Sref[::4, ::4])
    thr = float(np.median(v) + 10 * 1.4826 * np.median(np.abs(v - np.median(v)))) + 5 * rn / math.sqrt(len(ref_rs))
    hot_ref = Sref > thr

    def to_T(M, Tm, Tt):                                   # move a master from Tm to Tt with the model
        return M + (b1 * np.float32(Tt - Tm)) + exposure * I * np.float32(2 ** ((Tt - T_REF) / Td) - 2 ** ((Tm - T_REF) / Td))

    def hp(M):
        k = 64
        hh, ww = (M.shape[0] // k) * k, (M.shape[1] // k) * k
        A = M[:hh, :ww]
        blk = A.reshape(hh // k, k, ww // k, k).mean(axis=(1, 3))
        return A - np.repeat(np.repeat(blk, k, 0), k, 1)

    floor = MEDIAN_EFF * rn / math.sqrt(len(ref_rs))
    base = _clipped((F - to_T(Mref, Tref_day, test["T"]))[::2, ::2])
    rms_fresh = float(np.sqrt(np.mean((base - base.mean()) ** 2)))
    hp_ref = hp(Mref)
    ref_unix = float(np.mean([float(r.get("start_unix", 0)) for r in ref_rs]))
    out = []
    for d, rr in days.items():
        M, Tm = (Mref, Tref_day) if d == ref_day else master(rr)
        unix = ref_unix if d == ref_day else float(np.mean([float(r.get("start_unix", 0)) for r in rr]))
        Mc = to_T(M, Tm, Tref_day)
        diff = _clipped((Mc - Mref)[::2, ::2])
        a, b = hp(Mc).ravel()[::7], hp_ref.ravel()[::7]
        m = (np.abs(a - np.median(a)) < 50) & (np.abs(b - np.median(b)) < 50)
        corr = float(np.corrcoef(a[m], b[m])[0, 1]) if d != ref_day else 1.0
        hot = (M - bias_at(Tm)) > thr
        res = _clipped((F - to_T(M, Tm, test["T"]))[::2, ::2])
        rms = float(np.sqrt(np.mean((res - res.mean()) ** 2)))
        miss = int((hot_ref & (np.abs(F - to_T(M, Tm, test["T"])) > 5 * rn)).sum())
        out.append({"day": d, "age_days": round((ref_unix - unix) / 86400.0, 2), "frames": len(rr), "T": round(Tm, 2),
                    "level_shift": round(float(np.mean(diff)), 3), "pattern_corr": round(corr, 4),
                    "hot": int(hot.sum()), "hot_new_vs_ref": int((hot_ref & ~hot).sum()),
                    "hot_gone_vs_ref": int((hot & ~hot_ref).sum()),
                    "residual_rms": round(rms, 3),
                    "extra_rms": round(math.sqrt(max(0.0, rms ** 2 - rms_fresh ** 2)), 3),
                    "hot_misses": miss})
        log(f"ageing {d}: shift {out[-1]['level_shift']:+.2f}, corr {corr:.3f}, extra {out[-1]['extra_rms']:.2f}")
    out.sort(key=lambda x: x["age_days"])
    return {"exposure": exposure, "tcell": tc, "ref_day": ref_day, "test_frame": test["file"],
            "floor": round(floor, 3), "rms_fresh": round(rms_fresh, 3), "hot_threshold": round(thr, 2), "days": out}


def bias_by_day(recs):
    out = {}
    for r in recs:
        if r["exposure"] > BIAS_MAX_EXP or r.get("mean") is None:
            continue
        day = dt.datetime.fromtimestamp(float(r.get("start_unix", 0)), dt.timezone.utc).strftime("%Y-%m-%d")
        o = out.setdefault(day, {"means": [], "T": [], "ident": set()})
        o["means"].append(float(r["mean"]))
        o["T"].append(r["T"])
        o["ident"].add(json.dumps(r.get("identity") or {}, sort_keys=True))
    return [{"day": d, "frames": len(o["means"]), "mean": round(float(np.median(o["means"])), 2),
             "T": round(float(np.median(o["T"])), 2), "identities": len(o["ident"])} for d, o in sorted(out.items())]


# ---- tolerance -----------------------------------------------------------------------------------
def tolerance_table(model, rn, exposures, temps=(10.0, 20.0), n=15, pct=(50, 99.9)):
    I = model["Iref"][::4, ::4]
    q = {p: float(np.percentile(I, p)) for p in pct}
    floor = MEDIAN_EFF * rn / math.sqrt(n)
    rows = []
    for T in temps:
        for t in exposures:
            for p in pct:
                g = 2 ** ((T - T_REF) / model["Td"])
                errs = [t * q[p] * g * (2 ** (d / model["Td"]) - 1) for d in (1, 2, 3)]
                rows.append({"T": T, "exposure": t, "pixel": f"p{p:g}", "err_1C": round(errs[0], 3),
                             "err_2C": round(errs[1], 3), "err_3C": round(errs[2], 3), "floor": round(floor, 3)})
    return rows


# ---- plots (dependency-free SVG and PNG) -----------------------------------------------------------
PALETTE = ["#3b6fd4", "#d4623b", "#2f9e6e", "#9b59b6", "#c9a227", "#16a2b8", "#7f8c8d", "#e84393"]


def svg_plot(series, xlabel, ylabel, title, logy=False, w=720, h=400, hline=None):
    """series: [{"x": [...], "y": [...], "label": str, "kind": "dots"|"line"}]."""
    pts = [(x, y) for s in series for x, y in zip(s["x"], s["y"]) if y is not None and (not logy or y > 0)]
    if not pts:
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="60"><text x="10" y="30">{title}: no data</text></svg>'
    xs = [p[0] for p in pts]
    ys = [math.log10(p[1]) if logy else p[1] for p in pts]
    if hline is not None:
        ys.append(math.log10(hline) if logy else hline)
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if x1 == x0:
        x0, x1 = x0 - 1, x1 + 1
    if y1 == y0:
        y0, y1 = y0 - 1, y1 + 1
    pad = (y1 - y0) * 0.06
    y0, y1 = y0 - pad, y1 + pad
    L, R, T, B = 70, 20, 34, 50
    W, H = w - L - R, h - T - B

    def X(v):
        return L + (v - x0) / (x1 - x0) * W

    def Y(v):
        v = math.log10(v) if logy else v
        return T + H - (v - y0) / (y1 - y0) * H

    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" font-family="sans-serif" font-size="12">',
         '<rect width="100%" height="100%" fill="#fff"/>',
         f'<text x="{L}" y="20" font-size="14" font-weight="bold">{title}</text>',
         f'<rect x="{L}" y="{T}" width="{W}" height="{H}" fill="none" stroke="#888"/>']
    for i in range(6):
        xv = x0 + (x1 - x0) * i / 5
        o.append(f'<text x="{X(xv):.1f}" y="{T + H + 16}" text-anchor="middle">{xv:.3g}</text>')
        yv = y0 + (y1 - y0) * i / 5
        lab = f"{10 ** yv:.3g}" if logy else f"{yv:.3g}"
        yy = T + H - (yv - y0) / (y1 - y0) * H
        o.append(f'<text x="{L - 6}" y="{yy + 4:.1f}" text-anchor="end">{lab}</text>')
        o.append(f'<line x1="{L}" x2="{L + W}" y1="{yy:.1f}" y2="{yy:.1f}" stroke="#eee"/>')
    o.append(f'<text x="{L + W / 2}" y="{h - 12}" text-anchor="middle">{xlabel}</text>')
    o.append(f'<text x="16" y="{T + H / 2}" text-anchor="middle" transform="rotate(-90 16 {T + H / 2})">{ylabel}</text>')
    if hline is not None:
        o.append(f'<line x1="{L}" x2="{L + W}" y1="{Y(hline):.1f}" y2="{Y(hline):.1f}" stroke="#c00" stroke-dasharray="5,4"/>')
    for k, s in enumerate(series):
        col = s.get("color") or PALETTE[k % len(PALETTE)]
        p = [(X(x), Y(y)) for x, y in zip(s["x"], s["y"]) if y is not None and (not logy or y > 0)]
        if s.get("kind") == "line" and len(p) > 1:
            o.append('<polyline fill="none" stroke="%s" stroke-width="2" points="%s"/>' % (col, " ".join(f"{a:.1f},{b:.1f}" for a, b in p)))
        else:
            o += [f'<circle cx="{a:.1f}" cy="{b:.1f}" r="3" fill="{col}"/>' for a, b in p]
        o.append(f'<rect x="{L + W - 190}" y="{T + 8 + 16 * k}" width="10" height="10" fill="{col}"/>')
        o.append(f'<text x="{L + W - 175}" y="{T + 17 + 16 * k}">{s.get("label", "")}</text>')
    o.append("</svg>")
    return "\n".join(o)


def png_map(a, path, lo_pct=1, hi_pct=99, max_w=960):
    from PIL import Image
    a = np.asarray(a, np.float32)
    step = max(1, int(math.ceil(a.shape[1] / max_w)))
    if step > 1:
        hh, ww = (a.shape[0] // step) * step, (a.shape[1] // step) * step
        a = a[:hh, :ww].reshape(hh // step, step, ww // step, step).mean(axis=(1, 3))
    lo, hi = np.percentile(a, lo_pct), np.percentile(a, hi_pct)
    v = np.clip((a - lo) / (hi - lo + 1e-12), 0, 1)
    stops = np.array([[68, 1, 84], [59, 82, 139], [33, 145, 140], [94, 201, 98], [253, 231, 37]], float)
    idx = v * (len(stops) - 1)
    i0 = np.clip(np.floor(idx).astype(int), 0, len(stops) - 2)
    f = (idx - i0)[..., None]
    rgb = stops[i0] * (1 - f) + stops[i0 + 1] * f
    Image.fromarray(rgb.astype(np.uint8)).save(path)
    return float(lo), float(hi)


# ---- run -----------------------------------------------------------------------------------------
def _flat_master(data_dir, camera):
    root = os.path.join(data_dir, "flatlib", camera)
    if not os.path.isdir(root):
        return None
    best = None
    for d in os.listdir(root):
        p = os.path.join(root, d, "master.fits")
        if d.startswith("_") or not os.path.exists(p):
            continue
        if best is None or os.path.getmtime(p) > os.path.getmtime(best):
            best = p
    return _read(best) if best else None


def run(data_dir, out_root=None, camera="tele", gain=200, sentinel_exposure=60.0, log=print, now=None):
    t_start = time.time()
    archive = os.path.join(data_dir, "darkarchive")
    out_root = out_root or os.path.join(data_dir, "darkmodel")
    stamp = dt.datetime.fromtimestamp(now or time.time(), dt.timezone.utc).strftime("%Y%m%d")
    out = os.path.join(out_root, stamp)
    os.makedirs(out, exist_ok=True)
    recs = load_index(archive, camera, gain)
    log(f"{len(recs)} archived frames ({sum(r['steady'] for r in recs)} steady)")
    cells = make_cells(recs, os.path.join(out_root, "cells", camera), log=log)
    bias_recs = [r for r in recs if r["exposure"] <= BIAS_MAX_EXP and r["steady"]]
    rn, rn_pairs = read_noise(bias_recs)
    log(f"read noise {rn:.2f} ADU from {rn_pairs} bias pairs")
    std_map, n_std = bias_std_map(bias_recs)
    model = fit_model(cells, rn, log=log)
    flat = _flat_master(data_dir, camera)
    bad, cls = classify(model["Iref"], std_map, rn, flat)
    hot = hot_pixel_td(model, bad, rn)
    lag = temperature_lag(recs, model)
    age = ageing(recs, model, rn, sentinel_exposure, log=log)
    biasd = bias_by_day(recs)
    exps = sorted({c["exposure"] for c in model["dark_cells"]})
    tol = tolerance_table(model, rn, exps)

    # files
    hdr = fits.Header()
    hdr["T_REF"], hdr["T_D"], hdr["T_D_SE"], hdr["I0"] = T_REF, model["Td"], model["Td_se"], model["I0"]
    hdr["COMMENT"] = "dark(T,t) = B0 + B1 (T - T_REF) + t * IREF * 2^((T - T_REF) / T_D), ADU"
    fits.HDUList([fits.PrimaryHDU(header=hdr), fits.ImageHDU(model["b0"], name="B0"),
                  fits.ImageHDU(model["b1"], name="B1"), fits.ImageHDU(model["Iref"], name="IREF")]
                 ).writeto(os.path.join(out, "model.fits"), overwrite=True)
    bh = fits.Header()
    bh["COMMENT"] = "bits: 1 hot, 2 warm, 4 noisy (bias std), 8 low flat response (< 0.5)"
    fits.PrimaryHDU(bad, header=bh).writeto(os.path.join(out, "badpixels.fits"), overwrite=True)
    _plots(out, model, rn, hot, lag, age, biasd, cells)
    png_map(model["Iref"], os.path.join(out, "iref_map.png"))
    png_map(model["blocks"].mean(axis=0) if model["blocks"].size else model["Iref"], os.path.join(out, "glow_blocks.png"), 0, 100)
    if std_map is not None:
        png_map(std_map, os.path.join(out, "bias_std_map.png"))

    summary = {"created": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "camera": camera, "gain": gain,
               "frames": len(recs), "steady": sum(r["steady"] for r in recs),
               "span": [recs[0].get("start"), recs[-1].get("start")] if recs else None,
               "T_range": [round(min(r["T"] for r in recs), 2), round(max(r["T"] for r in recs), 2)] if recs else None,
               "read_noise_adu": round(rn, 3), "read_noise_pairs": rn_pairs, "bias_std_frames": n_std,
               "T_ref": T_REF, "Td": round(model["Td"], 3), "Td_se": round(model["Td_se"], 3),
               "I_ref_global_adu_s": round(model["I0"], 5), "cells": model["cells"], "classes": cls,
               "hot_td": {k: v for k, v in hot.items() if k != "td_values"},
               "temperature_lag": lag["bins"], "ageing": age, "bias_by_day": biasd, "tolerance": tol,
               "glow": _glow_summary(model), "runtime_s": round(time.time() - t_start, 1)}
    with open(os.path.join(out, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1, default=float)
    with open(os.path.join(out, "report.md"), "w") as fh:
        fh.write(report_md(summary))
    with open(os.path.join(out_root, "latest.json"), "w") as fh:
        json.dump({"dir": out, "stamp": stamp, "created": summary["created"]}, fh)
    log(f"report: {out}/report.md ({summary['runtime_s']:.0f} s)")
    return out, summary


def _glow_summary(model):
    I = model["blocks"]
    dark = model["dark_cells"]
    if not I.size:
        return {}
    # dark current per block, averaged over cells of >= 30 s, normalised by exposure and temperature
    sel = [j for j, c in enumerate(dark) if c["exposure"] >= 30]
    if not sel:
        return {}
    rate = np.mean([I[j] / (dark[j]["exposure"] * 2 ** ((dark[j]["T"] - T_REF) / model["Td"])) for j in sel], axis=0)
    med = float(np.median(rate))
    iy, ix = np.unravel_index(np.argmax(rate), rate.shape)
    # linearity: the brightest block against the median block, per exposure, at the most populated 1 C cell
    tcs = {}
    for j, c in enumerate(dark):
        tcs.setdefault(c["tcell"], []).append(j)
    tc, js = max(tcs.items(), key=lambda kv: len(kv[1]))
    lin = [{"exposure": dark[j]["exposure"], "max_block": round(float(I[j][iy, ix]), 3),
            "median_block": round(float(np.median(I[j])), 3)} for j in sorted(js, key=lambda j: dark[j]["exposure"])]
    return {"block_px": model["block"], "median_rate": round(med, 5), "max_rate": round(float(rate.max()), 5),
            "max_ratio": round(float(rate.max() / med), 2) if med > 0 else None,
            "max_at_px": [int(iy * model["block"]), int(ix * model["block"])], "linearity_tcell": tc, "linearity": lin}


def _plots(out, model, rn, hot, lag, age, biasd, cells):
    dark = model["dark_cells"]
    by_exp = {}
    for j, c in enumerate(dark):
        by_exp.setdefault(c["exposure"], []).append((c["T"], model["smean"][j] / c["exposure"]))
    Tline = np.linspace(min(c["T"] for c in dark), max(c["T"] for c in dark), 40)
    series = [{"x": [p[0] for p in v], "y": [p[1] for p in v], "label": f"{e:g} s"} for e, v in sorted(by_exp.items())]
    series.append({"x": list(Tline), "y": list(model["I0"] * 2 ** ((Tline - T_REF) / model["Td"])), "kind": "line",
                   "label": f"fit T_d {model['Td']:.1f} C", "color": "#000"})
    _w(out, "dark_current.svg", svg_plot(series, "sensor temperature (C)", "mean dark current (ADU/s)",
                                          "Dark current against temperature (clipped mean per cell)", logy=True))
    bc = model["bias_cells"]
    _w(out, "bias_vs_temp.svg", svg_plot([{"x": [c["T"] for c in bc], "y": [float(np.mean(c["data"][::8, ::8])) for c in bc],
                                           "label": "bias cells"}], "sensor temperature (C)", "mean bias (ADU)",
                                         "Bias level against temperature"))
    if biasd:
        _w(out, "bias_by_day.svg", svg_plot([{"x": list(range(len(biasd))), "y": [b["mean"] for b in biasd], "label": "median bias frame mean",
                                              "kind": "line"}], "day index (" + biasd[0]["day"] + " ...)", "ADU", "Bias level by day"))
    cs = model["cells"]
    _w(out, "loo.svg", svg_plot([{"x": [c["T"] for c in cs if c["exposure"] == e], "y": [c["ratio"] for c in cs if c["exposure"] == e],
                                  "label": f"{e:g} s"} for e in sorted({c["exposure"] for c in cs})],
                                "cell temperature (C)", "leave-one-out rms / noise floor", "Model validation (1 = at the noise floor)",
                                hline=1.0))
    if hot.get("fitted"):
        v = hot["td_values"]
        edges = np.linspace(max(0, np.percentile(v, 1)), np.percentile(v, 99), 25)
        hcnt, _ = np.histogram(v, edges)
        _w(out, "hot_td.svg", svg_plot([{"x": list((edges[:-1] + edges[1:]) / 2), "y": list(hcnt), "kind": "line", "label": "hot pixels"}],
                                       "doubling temperature (C)", "pixels", f"Hot-pixel doubling temperature (global {model['Td']:.1f} C)"))
    if lag["points"]:
        p = lag["points"][::max(1, len(lag["points"]) // 3000)]
        _w(out, "temp_lag.svg", svg_plot([{"x": [a for a, _, _ in p], "y": [b for _, b, _ in p], "label": "frames >= 30 s"}],
                                         "temperature change during the exposure (C)", "frame mean - model (ADU/s)",
                                         "Temperature lag: residual against warming"))
    if age.get("days"):
        d = age["days"]
        _w(out, "ageing.svg", svg_plot([{"x": [x["age_days"] for x in d], "y": [x["extra_rms"] for x in d], "label": "extra residual rms", "kind": "line"},
                                        {"x": [x["age_days"] for x in d], "y": [x["level_shift"] for x in d], "label": "level shift"}],
                                       "master age (days)", "ADU", f"Ageing: {age['exposure']:g} s masters at {age['tcell']:+d} C",
                                       hline=age["floor"]))
        _w(out, "ageing_hot.svg", svg_plot([{"x": [x["age_days"] for x in d], "y": [x["hot_new_vs_ref"] for x in d], "label": "hot now, not then"},
                                            {"x": [x["age_days"] for x in d], "y": [x["hot_gone_vs_ref"] for x in d], "label": "hot then, not now"}],
                                           "master age (days)", "pixels", "Hot-pixel turnover against the newest master"))


def _w(out, name, text):
    with open(os.path.join(out, name), "w") as fh:
        fh.write(text)


def report_md(s):
    L = [f"# Dark model, {s['camera']} gain {s['gain']} ({s['created'][:10]})", "",
         f"From `python -m sequencer darks analyse`. Frames: {s['frames']} ({s['steady']} steady), "
         f"{s['span'][0] if s['span'] else '?'} … {s['span'][1] if s['span'] else '?'}, sensor {s['T_range'][0]} … {s['T_range'][1]} °C. "
         f"Values in ADU (16-bit scale); e⁻/ADU not measured yet. Runtime {s['runtime_s']:.0f} s.", "",
         "## Headline", "",
         f"- **Doubling temperature T_d = {s['Td']:.2f} ± {s['Td_se']:.2f} °C** (global, normal pixels).",
         f"- Mean dark current at {s['T_ref']:g} °C: **{s['I_ref_global_adu_s']:.4f} ADU/s** "
         f"({s['I_ref_global_adu_s'] * 120:.2f} ADU in 120 s).",
         f"- Read noise **{s['read_noise_adu']:.2f} ADU** ({s['read_noise_pairs']} bias pairs).",
         f"- Bad pixels: hot {s['classes']['counts']['hot']}, warm {s['classes']['counts']['warm']}, "
         f"noisy {s['classes']['counts']['noisy']}, low flat {s['classes']['counts']['low_flat']} of {s['classes']['pixels']}.",
         "", "![dark current](dark_current.svg)", "",
         "## Validation: leave one out", "",
         "Each cell's master predicted from the model fitted to all the other cells, against the master's own noise floor "
         "(1.25 × read noise / √frames, a median master's noise). Ratio ≈ 1: the model is as good as a real master there.", "",
         "| Cell | Exposure (s) | T (°C) | Frames | Signal (ADU) | LOO rms | Floor | Ratio |", "|---|---|---|---|---|---|---|---|"]
    for c in s["cells"]:
        L.append(f"| {c['name']} | {c['exposure']:g} | {c['T']} | {c['n']} | {'' if c['signal'] is None else c['signal']} | "
                 f"{c['loo_rms']} | {c['floor']} | {c['ratio']} |")
    L += ["", "![loo](loo.svg)", "", "## Matching tolerance from the model", "",
          "Error of using a master 1, 2 or 3 °C off, for the median and the 99.9th-percentile pixel, against the floor of a 15-frame master.", "",
          "| T (°C) | Exposure (s) | Pixel | 1 °C | 2 °C | 3 °C | Floor |", "|---|---|---|---|---|---|---|"]
    for r in s["tolerance"]:
        L.append(f"| {r['T']:g} | {r['exposure']:g} | {r['pixel']} | {r['err_1C']} | {r['err_2C']} | {r['err_3C']} | {r['floor']} |")
    g = s.get("glow") or {}
    L += ["", "## Glow", ""]
    if g:
        L += [f"Dark current in {g['block_px']} px blocks: median {g['median_rate']} ADU/s at {s['T_ref']:g} °C, brightest block "
              f"{g['max_rate']} ({g['max_ratio']}× the median) at row {g['max_at_px'][0]}, column {g['max_at_px'][1]}.", "",
              f"Linearity at {g['linearity_tcell']:+d} °C (signal in ADU; linear in t means the ratio stays constant):", "",
              "| Exposure (s) | Brightest block | Median block |", "|---|---|---|"]
        L += [f"| {x['exposure']:g} | {x['max_block']} | {x['median_block']} |" for x in g["linearity"]]
        L += ["", "![glow](glow_blocks.png)", "", "Per-pixel I_ref map: ![iref](iref_map.png)"]
    h = s["hot_td"]
    L += ["", "## Hot pixels", "", f"{h.get('n', 0)} hot pixels; {h.get('fitted', 0)} with their own T_d fit"
          + (f": median {h['td_median']:.2f} °C (10–90 %: {h['td_p10']:.2f}–{h['td_p90']:.2f})." if h.get("fitted") else "."), ""]
    if h.get("fitted"):
        L.append("![hot td](hot_td.svg)")
    L += ["", "## Temperature lag", "", "Frame mean against the model, by the sensor reading's change during the exposure. "
          "If warming frames sit off zero, the reading lags the die.", "",
          "| ΔT during exposure (°C) | Frames | Median residual (ADU/s) | ± (ADU/s) |", "|---|---|---|---|"]
    L += [f"| {b['dT_from']} … {b['dT_to']} | {b['frames']} | {b['residual_adu_s']:.4f} | {b['spread_adu_s']:.4f} |" for b in s["temperature_lag"]]
    L += ["", "![lag](temp_lag.svg)", "", "## Ageing", ""]
    a = s["ageing"]
    if a.get("days"):
        L += [f"{a['exposure']:g} s masters per day at {a['tcell']:+d} °C, against the newest ({a['ref_day']}), corrected to the same "
              f"temperature with the model. Fresh test frame `{a['test_frame']}`; floor {a['floor']} ADU, fresh-master residual {a['rms_fresh']} ADU.", "",
              "| Day | Age (d) | Frames | T (°C) | Level shift | Pattern corr | Hot | New hot | Gone | Residual rms | Extra rms | Hot misses |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        L += [f"| {d['day']} | {d['age_days']} | {d['frames']} | {d['T']} | {d['level_shift']} | {d['pattern_corr']} | {d['hot']} | "
              f"{d['hot_new_vs_ref']} | {d['hot_gone_vs_ref']} | {d['residual_rms']} | {d['extra_rms']} | {d['hot_misses']} |" for d in a["days"]]
        L += ["", "![ageing](ageing.svg)", "", "![ageing hot](ageing_hot.svg)"]
    else:
        L.append("Not enough days with sentinel frames yet.")
    L += ["", "## Bias by day", "", "| Day | Frames | Median frame mean (ADU) | T (°C) | Camera identities |", "|---|---|---|---|---|"]
    L += [f"| {b['day']} | {b['frames']} | {b['mean']} | {b['T']} | {b['identities']} |" for b in s["bias_by_day"]]
    L += ["", "![bias by day](bias_by_day.svg)", "", "![bias vs temp](bias_vs_temp.svg)", ""]
    L += ["Per-pixel bias std (noisy / RTS candidates): ![bias std](bias_std_map.png)", ""]
    return "\n".join(L)
