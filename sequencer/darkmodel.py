"""Dark-current analysis over the dark archive (dark harvest plan, section 6). Offline, read-only on the
archive: it never changes the library, the pool or a frame.

    python -m sequencer darks analyse [--data DIR] [--out DIR] [--camera tele] [--gain 200]

What the first run (v1, 2026-11-22) taught, and v2 builds on:
  - The frame level doesn't rise with exposure: the sensor's black-level clamp removes the mean dark
    signal, so the mean of normal pixels is no measure of dark current. Each master gets its own level
    (the "pedestal"); the per-pixel model describes the pattern relative to it. A pedestal is a constant
    over the frame and doesn't matter for stacking.
  - The dark current is real: its shot noise adds variance proportional to exposure. The temporal noise of
    frame pairs gives the dark current's temperature dependence for normal pixels.
  - A master's own noise floor is 1.25 x (frame noise at its exposure and temperature) / sqrt(n), not
    read noise / sqrt(n).
  - The first frame of a series is off (level and banding): left out of the cells.

Steps:
  1. Cells: steady frames (temperature change <= 0.5 C), not first in a series, grouped by exposure and 1 C;
     median master per cell (newest max_frames), cached under <out>/cells/; also each cell's temporal
     noise from consecutive frame pairs.
  2. Noise: read noise from bias pairs; dark-shot-noise model var(t, T) - rn^2 = K t 2^((T - T_ref) / T_d);
     per-pixel bias std (noisy and random-telegraph pixels).
  3. Model per pixel (row tiles):
        master(T, t) = b0 + b1 (T - T_ref) + p_cell + t 2^((T - T_ref) / T_d) (A + B (T - T_ref))
     b0, b1 from the bias cells; p_cell the cell's clipped mean signal; A, B per pixel by weighted LS
     (B lets a pixel's own doubling temperature differ from T_d). Leave-one-out residuals from the hat
     matrix, on good pixels and on hot pixels separately, against each cell's own floor.
  4. Hot pixels: own T_d (log-linear). Glow: A in blocks, hot pixels left out.
  5. Temperature lag: hot-pixel signal in steady and warming frames against the model; as an equivalent
     temperature offset of the die from the reading.
  6. Ageing: daily masters of one key in its most populated 1 C bin, on good pixels: level shift, pattern
     correlation (with the correlation expected from noise alone), hot-pixel turnover, extra residual.
  7. Outputs in <out>/<YYYYMMDD>/: report.md, plots, model.fits (B0, B1, A, B; T_d), badpixels.fits
     (bits: 1 hot, 2 warm, 4 noisy, 8 low flat response), summary.json; <out>/latest.json.

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
CELL_VERSION = 2
CROP = 600


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


def load_index(archive, camera="tele", gain=200, offset=0, binning=1, max_temp_change=0.5, series_gap_s=120.0):
    """Index records of one camera and gain whose FITS exists, with T (frame temperature), dT (change during
    the exposure), steady, first (first frame of a series: the previous frame had another exposure or ended
    more than series_gap_s earlier) and path added. Sorted by start time."""
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
    prev = None
    for r in out:
        s = float(r.get("start_unix", 0))
        r["first"] = prev is None or prev["exposure"] != r["exposure"] or \
            s - (float(prev.get("start_unix", 0)) + prev["exposure"]) > series_gap_s
        prev = r
    return out


def _read(path):
    return np.asarray(fits.getdata(path), np.float32)


def _crop(a, crop=CROP):
    h, w = a.shape
    y0, x0 = max(0, h // 2 - crop // 2), max(0, w // 2 - crop // 2)
    return a[y0:y0 + crop, x0:x0 + crop]


def _clipped(v, k=5.0):
    v = np.asarray(v).ravel()
    med = np.median(v)
    mad = 1.4826 * np.median(np.abs(v - med)) + 1e-9
    return v[np.abs(v - med) < k * mad]


def _pair_noise(a, b):
    d = _clipped(_crop(a) - _crop(b), 6.0)
    return float(np.std(d)) / math.sqrt(2)


# ---- 1. cells --------------------------------------------------------------------------------------
def make_cells(recs, cache, min_frames=5, max_frames=15, log=print):
    """Median masters per (exposure, 1 C) of steady, non-first frames, and each cell's temporal noise.
    Returns dicts with the master as a read-only memmap (cell["data"])."""
    os.makedirs(cache, exist_ok=True)
    groups = {}
    for r in recs:
        if r["steady"] and not r["first"]:
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
        if not meta or meta.get("files") != files or meta.get("version") != CELL_VERSION:
            t0 = time.time()
            frames = [_read(r["path"]) for r in rs]
            noise = [_pair_noise(a, b) for a, b, ra, rb in zip(frames, frames[1:], rs, rs[1:])
                     if abs(ra["T"] - rb["T"]) < 0.5]
            m = median_stack(frames)
            del frames
            np.save(npy + ".tmp.npy", m)
            os.replace(npy + ".tmp.npy", npy)
            meta = {"version": CELL_VERSION, "exposure": exp, "tcell": tc, "files": files, "n": len(rs),
                    "T": float(np.mean([r["T"] for r in rs])),
                    "T_min": float(min(r["T"] for r in rs)), "T_max": float(max(r["T"] for r in rs)),
                    "noise": float(np.median(noise)) if noise else None, "noise_pairs": len(noise),
                    "unix_first": float(rs[0].get("start_unix", 0)), "unix_last": float(rs[-1].get("start_unix", 0))}
            with open(meta_p, "w") as fh:
                json.dump(meta, fh)
            log(f"cell {name}: {len(rs)} frames, noise {meta['noise']}, {time.time() - t0:.0f} s")
        cells.append({**meta, "name": name, "data": np.load(npy, mmap_mode="r")})
    return cells


# ---- 2. noise ------------------------------------------------------------------------------------
def read_noise(cells):
    """Read noise: the median temporal noise of the bias cells."""
    v = [c["noise"] for c in cells if c["exposure"] <= BIAS_MAX_EXP and c.get("noise")]
    return (float(np.median(v)) if v else None), len(v)


def _wls_line(x, y, w):
    """Weighted LS y = a + b x; returns a, b, stderr_b."""
    x, y, w = (np.asarray(v, float) for v in (x, y, w))
    sw = w.sum()
    xm, ym = (w * x).sum() / sw, (w * y).sum() / sw
    sxx = (w * (x - xm) ** 2).sum()
    b = (w * (x - xm) * (y - ym)).sum() / sxx if sxx > 0 else 0.0
    a = ym - b * xm
    res = y - a - b * x
    s2 = (w * res ** 2).sum() / max(1, len(x) - 2)
    seb = math.sqrt(s2 / sxx) if sxx > 0 else float("nan")
    return a, b, seb


def noise_model(cells, rn):
    """Dark shot noise: var - rn^2 = K t 2^((T - T_ref) / T_d) over dark cells with a noise measurement."""
    rows = [(c["exposure"], c["T"] - T_REF, c["noise"] ** 2 - rn ** 2, c.get("noise_pairs", 1))
            for c in cells if c["exposure"] >= 5 and c.get("noise")]
    rows = [r for r in rows if r[2] > 0]
    if len(rows) < 3 or np.ptp([r[1] for r in rows]) < 3:
        return {"ok": False, "cells": len(rows)}
    t, x, v, n = (np.array(z, float) for z in zip(*rows))
    a, sl, se = _wls_line(x, np.log2(v / t), n)
    if sl <= 0:
        return {"ok": False, "cells": len(rows), "slope": sl}
    K = 2.0 ** a
    pred = K * t * 2 ** (x * sl)
    return {"ok": True, "K_adu2_s": K, "Td": 1.0 / sl, "Td_se": se / sl ** 2, "cells": len(rows),
            "rel_rms": float(np.sqrt(np.mean((v / pred - 1) ** 2))),
            "points": [{"exposure": float(a_), "T": float(b_ + T_REF), "var_dark": float(c_)} for a_, b_, c_, _ in rows]}


def bias_std_map(bias_recs, max_frames=200):
    """Per-pixel std over bias frames, after removing each frame's own mean level."""
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
def fit_model(cells, rn, Td, tile=32, block=60, log=print):
    """Per-pixel bias and dark-current pattern. Td: the global doubling temperature (from the noise
    model). Returns maps and per-cell data; leave-one-out statistics come from loo_stats()."""
    bias = [c for c in cells if c["exposure"] <= BIAS_MAX_EXP]
    dark = [c for c in cells if c["exposure"] > BIAS_MAX_EXP]
    if len(bias) < 2 or len(dark) < 3:
        raise ValueError(f"need >= 2 bias cells and >= 3 dark cells (have {len(bias)} and {len(dark)})")
    h, w = bias[0]["data"].shape
    xb = np.array([c["T"] - T_REF for c in bias])
    wb = np.array([c["n"] for c in bias], float)
    swb = wb.sum()
    xbm = (wb * xb).sum() / swb
    sxxb = (wb * (xb - xbm) ** 2).sum()
    td = np.array([c["exposure"] for c in dark])
    xd = np.array([c["T"] - T_REF for c in dark])
    nd = np.array([c["n"] for c in dark], float)
    b0 = np.zeros((h, w), np.float32)
    b1 = np.zeros((h, w), np.float32)

    # pass 1: bias maps, and each dark cell's pedestal (clipped mean signal over the frame)
    sums, cnts = np.zeros(len(dark)), np.zeros(len(dark))
    for y0 in range(0, h, tile):
        y1 = min(h, y0 + tile)
        Mb = np.stack([c["data"][y0:y1] for c in bias]).astype(np.float64)
        ym = np.tensordot(wb, Mb, 1) / swb
        sl = np.tensordot(wb * (xb - xbm), Mb - ym, 1) / sxxb
        a = ym - sl * xbm
        b0[y0:y1], b1[y0:y1] = a, sl
        for j, c in enumerate(dark):
            v = _clipped((c["data"][y0:y1] - (a + sl * xd[j]))[::2, ::2])
            sums[j] += v.sum()
            cnts[j] += v.size
    ped = sums / np.maximum(cnts, 1)
    log("pedestals: " + ", ".join(f"{c['name']} {p:+.1f}" for c, p in zip(dark, ped)))

    g = 2.0 ** (xd / Td)
    Z = np.stack([td * g, td * g * xd], axis=1)             # regressors per cell: A and B
    G = (Z * nd[:, None]).T @ Z
    Gi = np.linalg.inv(G)
    P = Gi @ (Z * nd[:, None]).T                            # coefficients = P @ S
    hd = nd * np.einsum("ij,jk,ik->i", Z, Gi, Z)            # leverage of each cell

    # pass 2: per-pixel A, B; glow blocks
    A = np.zeros((h, w), np.float32)
    B = np.zeros((h, w), np.float32)
    nby, nbx = h // block, w // block
    bsum = np.zeros((nby, nbx))
    bcnt = np.zeros((nby, nbx))
    for y0 in range(0, h, tile):
        y1 = min(h, y0 + tile)
        S = _signal(dark, y0, y1, b0, b1, xd, ped)
        coef = np.tensordot(P, S, 1)
        A[y0:y1], B[y0:y1] = coef[0], coef[1]
        I = coef[0]
        iv = I[::2, ::2].ravel()
        imed = np.median(iv)
        keep = np.abs(I - imed) < 10 * 1.4826 * np.median(np.abs(iv - imed)) + 1e-9
        for by in range(y0 // block, min(nby, -(-y1 // block))):
            ya, yb = max(y0, by * block), min(y1, (by + 1) * block)
            if ya >= yb:
                continue
            km = keep[ya - y0:yb - y0, :nbx * block]
            seg = (I[ya - y0:yb - y0, :nbx * block] * km).reshape(yb - ya, nbx, block)
            bsum[by] += seg.sum(axis=(0, 2))
            bcnt[by] += km.reshape(yb - ya, nbx, block).sum(axis=(0, 2))
    return {"b0": b0, "b1": b1, "A": A, "B": B, "Td": Td, "T_ref": T_REF, "dark_cells": dark, "bias_cells": bias,
            "ped": ped, "Z": Z, "P": P, "hd": hd, "xd": xd, "xb": xb, "wb": wb, "glow": bsum / np.maximum(bcnt, 1),
            "block": block, "tile": tile}


def _signal(dark, y0, y1, b0, b1, xd, ped):
    return np.stack([c["data"][y0:y1] for c in dark]).astype(np.float64) \
        - (b0[y0:y1].astype(np.float64)[None] + b1[y0:y1].astype(np.float64)[None] * xd[:, None, None]) \
        - ped[:, None, None]


def loo_stats(model, bad, rn):
    """Leave-one-out residual rms per cell on good pixels (bad == 0) and on hot pixels, against each cell's
    own floor 1.25 x noise / sqrt(n)."""
    dark, bias, tile = model["dark_cells"], model["bias_cells"], model["tile"]
    b0, b1, xd, xb, wb = model["b0"], model["b1"], model["xd"], model["xb"], model["wb"]
    Z, P, hd, ped = model["Z"], model["P"], model["hd"], model["ped"]
    swb = wb.sum()
    xbm = (wb * xb).sum() / swb
    sxxb = (wb * (xb - xbm) ** 2).sum()
    hb = wb * (1 / swb + (xb - xbm) ** 2 / sxxb)
    h = b0.shape[0]
    good_d = [[] for _ in dark]
    hot_d = [[] for _ in dark]
    good_b = [[] for _ in bias]
    for y0 in range(0, h, tile):
        y1 = min(h, y0 + tile)
        gm = bad[y0:y1] == 0
        hm = (bad[y0:y1] & BAD_HOT) > 0
        sub = np.zeros_like(gm)
        sub[::3, ::3] = True
        S = _signal(dark, y0, y1, b0, b1, xd, ped)
        coef = np.tensordot(P, S, 1)
        for j in range(len(dark)):
            r = (S[j] - (Z[j, 0] * coef[0] + Z[j, 1] * coef[1])) / (1 - hd[j])
            good_d[j].append(r[gm & sub])
            hot_d[j].append(r[hm])
        for i, c in enumerate(bias):
            r = (c["data"][y0:y1] - b0[y0:y1] - b1[y0:y1] * xb[i]) / (1 - hb[i])
            good_b[i].append(np.asarray(r)[gm & sub])

    def rms(parts, clip=True):
        v = np.concatenate(parts) if parts else np.array([])
        if v.size == 0:
            return None
        v = _clipped(v, 6.0) if clip else v
        return float(np.sqrt(np.mean((v - v.mean()) ** 2)))

    out = []
    for i, c in enumerate(bias):
        out.append(_cell_stat(c, rms(good_b[i]), None, None))
    for j, c in enumerate(dark):
        out.append(_cell_stat(c, rms(good_d[j]), rms(hot_d[j], clip=False), float(ped[j])))
    return out


def _cell_stat(c, good, hot, ped):
    noise = c.get("noise")
    floor = MEDIAN_EFF * noise / math.sqrt(c["n"]) if noise else None
    return {"name": c["name"], "exposure": c["exposure"], "T": round(c["T"], 2), "n": c["n"],
            "noise": None if noise is None else round(noise, 2), "pedestal": None if ped is None else round(ped, 2),
            "loo_good": None if good is None else round(good, 3), "loo_hot": None if hot is None else round(hot, 2),
            "floor": None if floor is None else round(floor, 3),
            "ratio": None if not floor or good is None else round(good / floor, 2)}


# ---- hot pixels, bad-pixel map ---------------------------------------------------------------------
def classify(A, std_map, rn, flat=None, hot_k=10.0, warm_k=5.0, noisy_k=3.0):
    v = _clipped(A[::4, ::4])
    med, mad = float(np.median(v)), float(1.4826 * np.median(np.abs(v - np.median(v))) + 1e-12)
    bad = np.zeros(A.shape, np.uint8)
    hot_thr = max(med + hot_k * mad, med + 5 * rn / 60.0)  # also >= 5 read noise in 60 s
    warm_thr = med + warm_k * mad
    bad[A > hot_thr] |= BAD_HOT
    bad[(A > warm_thr) & (A <= hot_thr)] |= BAD_WARM
    noisy_thr = None
    if std_map is not None:
        s = _clipped(std_map[::4, ::4])
        noisy_thr = float(np.median(s)) * noisy_k
        bad[std_map > noisy_thr] |= BAD_NOISY
    if flat is not None and flat.shape == A.shape:
        bad[flat < 0.5] |= BAD_LOWFLAT
    counts = {k: int(((bad & bit) > 0).sum()) for k, bit in
              (("hot", BAD_HOT), ("warm", BAD_WARM), ("noisy", BAD_NOISY), ("low_flat", BAD_LOWFLAT))}
    counts["any"] = int((bad > 0).sum())
    return bad, {"A_median": med, "A_mad": mad, "hot_threshold": hot_thr, "warm_threshold": warm_thr,
                 "noisy_threshold": noisy_thr, "counts": counts, "pixels": int(A.size)}


def _hot_coords(model, bad, max_pixels):
    ys, xs = np.nonzero(bad & BAD_HOT)
    if ys.size > max_pixels:
        k = np.argsort(model["A"][ys, xs])[-max_pixels:]
        ys, xs = ys[k], xs[k]
    return ys, xs


def hot_pixel_td(model, bad, rn, max_pixels=20000, min_exp=10):
    """Hot pixels get their own exponential: log2(S / t) = a + (T - T_ref) / Td_pixel, fitted over cells
    >= min_exp s where the pixel's signal is > 5 read noise. Stores the per-pixel fit in model["hot_a"] and
    model["hot_k"] (1 / Td_pixel; NaN elsewhere), so _predict uses it. Leave-one-out relative error per
    pixel and cell (closed form, in log space), against the global model's error on the same pixels."""
    ys, xs = _hot_coords(model, bad, max_pixels)
    h, w = model["A"].shape
    model["hot_a"] = np.full((h, w), np.nan, np.float32)
    model["hot_k"] = np.full((h, w), np.nan, np.float32)
    if ys.size == 0:
        return {"n": 0}
    sel = [j for j, c in enumerate(model["dark_cells"]) if c["exposure"] >= min_exp]
    dark = [model["dark_cells"][j] for j in sel]
    X = model["xd"][sel]
    t = np.array([c["exposure"] for c in dark])
    S = np.stack([np.asarray(c["data"][ys, xs], np.float64) for c in dark]) \
        - (model["b0"][ys, xs][None] + model["b1"][ys, xs][None] * X[:, None]) - model["ped"][sel][:, None]
    glob = np.stack([_predict(model, ys, xs, c["T"], c["exposure"]) for c in dark])
    tds, own_err, glob_err = [], [], []
    for k in range(ys.size):
        s = S[:, k]
        m = s > 5 * rn
        if m.sum() < 4 or np.ptp(X[m]) < 4:
            continue
        x, y = X[m], np.log2(s[m] / t[m])
        a, sl, _ = _wls_line(x, y, np.ones(m.sum()))
        if sl <= 0:
            continue
        xm = x.mean()
        lev = 1 / x.size + (x - xm) ** 2 / ((x - xm) ** 2).sum()
        r = (y - a - sl * x) / (1 - lev)
        own_err.extend(np.abs(2 ** r - 1))
        glob_err.extend(np.abs(s[m] / np.maximum(glob[m, k], 1e-9) - 1))
        model["hot_a"][ys[k], xs[k]], model["hot_k"][ys[k], xs[k]] = a, sl
        tds.append(1.0 / sl)
    tds = np.array(tds)
    if tds.size == 0:
        return {"n": int(ys.size), "fitted": 0}
    return {"n": int(ys.size), "fitted": int(tds.size), "td_median": float(np.median(tds)),
            "td_p10": float(np.percentile(tds, 10)), "td_p90": float(np.percentile(tds, 90)),
            "loo_rel_err_median": float(np.median(own_err)), "loo_rel_err_p90": float(np.percentile(own_err, 90)),
            "global_rel_err_median": float(np.median(glob_err)), "global_rel_err_p90": float(np.percentile(glob_err, 90)),
            "td_values": tds}


def _predict(model, ys, xs, T, t):
    x = T - T_REF
    p = t * 2 ** (x / model["Td"]) * (model["A"][ys, xs] + model["B"][ys, xs] * x)
    if "hot_k" in model:
        hk, ha = model["hot_k"][ys, xs], model["hot_a"][ys, xs]
        own = np.isfinite(hk)
        p = np.where(own, t * 2.0 ** (np.nan_to_num(ha) + np.nan_to_num(hk) * x), p)
    return p


# ---- 5. temperature lag ----------------------------------------------------------------------------
def temperature_lag(recs, model, bad, max_pixels=2000, per_bin=40, min_exp=60):
    """The brightest hot pixels' signal in single frames against the model at the frame's reading,
    as an equivalent temperature offset Td * log2(measured / predicted), by the reading's change during the
    exposure. Steady frames near 0 and warming frames above 0 would mean the reading lags the die."""
    ys, xs = _hot_coords(model, bad, max_pixels)
    if ys.size < 10:
        return {"bins": [], "points": []}
    edges = [-9, -0.1, 0.1, 0.3, 0.5, 1.0, 2.0, 9]
    picked = {i: [] for i in range(len(edges) - 1)}
    for r in recs:
        if r["exposure"] < min_exp or r["dT"] is None or r["first"]:
            continue
        for i, (lo, hi) in enumerate(zip(edges[:-1], edges[1:])):
            if lo <= r["dT"] < hi:
                picked[i].append(r)
    pts, out = [], []
    for i, rs in picked.items():
        rs = rs[::max(1, len(rs) // per_bin)][:per_bin]
        offs = []
        for r in rs:
            f = _read(r["path"])
            x = r["T"] - T_REF
            base = model["b0"] + model["b1"] * np.float32(x)
            ped = float(np.median(_crop(f) - _crop(base)))
            s = f[ys, xs] - base[ys, xs] - ped
            p = _predict(model, ys, xs, r["T"], r["exposure"])
            ok = (p > 20) & (s > 0)
            if ok.sum() < 10:
                continue
            ratio = float(np.median(s[ok] / p[ok]))
            off = model["Td"] * math.log2(ratio) if ratio > 0 else None
            if off is not None:
                offs.append(off)
                pts.append((r["dT"], off))
        if offs:
            out.append({"dT_from": edges[i], "dT_to": edges[i + 1], "frames": len(offs),
                        "offset_c": round(float(np.median(offs)), 3),
                        "spread_c": round(float(np.percentile(offs, 84) - np.percentile(offs, 16)) / 2, 3)})
    return {"bins": out, "points": pts}


# ---- 6. ageing -------------------------------------------------------------------------------------
def ageing(recs, model, bad, cells, exposure=60.0, min_frames=5, max_frames=15, log=print):
    rs = [r for r in recs if r["steady"] and not r["first"] and abs(r["exposure"] - exposure) < 1e-6]
    if not rs:
        return {"exposure": exposure, "days": []}
    by = {}
    for r in rs:
        day = dt.datetime.fromtimestamp(float(r.get("start_unix", 0)), dt.timezone.utc).strftime("%Y-%m-%d")
        by.setdefault(int(round(r["T"])), {}).setdefault(day, []).append(r)
    tc, days = max(by.items(), key=lambda kv: sum(len(v) >= min_frames for v in kv[1].values()))
    days = {d: v[-max_frames:] for d, v in sorted(days.items()) if len(v) >= min_frames}
    if len(days) < 2:
        return {"exposure": exposure, "tcell": tc, "days": []}
    ref_day = max(days)
    test = days[ref_day][-1]
    ref_rs = days[ref_day][:-1]
    noise = [c["noise"] for c in cells if c["exposure"] == exposure and c.get("noise") and abs(c["tcell"] - tc) <= 1]
    sigma = float(np.median(noise)) if noise else None
    good = bad == 0
    hot = (bad & BAD_HOT) > 0
    gsub = good.copy()
    gsub[1::2, :] = False
    gsub[:, 1::2] = False
    F = _read(test["path"])
    b0, b1, A, B, Td = model["b0"], model["b1"], model["A"], model["B"], model["Td"]

    hk, ha = model.get("hot_k"), model.get("hot_a")
    own = np.isfinite(hk) if hk is not None else None

    def dark_at(T):
        x = np.float32(T - T_REF)
        d = np.float32(exposure * 2 ** (x / Td)) * (A + B * x)
        if own is not None and own.any():
            d[own] = exposure * 2.0 ** (ha[own] + hk[own] * x)
        return b0 + b1 * x + d

    def master(rr):
        return median_stack([_read(r["path"]) for r in rr]), float(np.mean([r["T"] for r in rr]))

    def level(M):
        return float(np.median(M[gsub]))

    def hp(M):                                             # pattern: master minus 64 px block means
        k = 64
        hh, ww = (M.shape[0] // k) * k, (M.shape[1] // k) * k
        X = M[:hh, :ww]
        blk = X.reshape(hh // k, k, ww // k, k).mean(axis=(1, 3))
        return X - np.repeat(np.repeat(blk, k, 0), k, 1)

    Mref, Tr = master(ref_rs)
    Mref = Mref - dark_at(Tr)                             # residual after the model: what ageing would change
    Fr = F - dark_at(test["T"])
    Fr -= level(Fr)
    Mref_l = Mref - level(Mref)
    hh, ww = (F.shape[0] // 64) * 64, (F.shape[1] // 64) * 64
    gcrop = gsub[:hh, :ww]
    hp_ref = hp(Mref)[gcrop]
    floor = MEDIAN_EFF * sigma / math.sqrt(len(ref_rs)) if sigma else None
    v_ref = float(np.var(_clipped(hp_ref, 6.0)))
    exp_corr = max(0.0, (v_ref - floor ** 2) / v_ref) if floor and v_ref > 0 else None
    base = _clipped((Fr - Mref_l)[gsub], 6.0)
    rms_fresh = float(np.std(base))
    thr = 10 * (sigma or 20)
    hot_ref = hot & (Mref_l + dark_at(Tr) - b0 - b1 * np.float32(Tr - T_REF) > thr)
    ref_unix = float(np.mean([float(r.get("start_unix", 0)) for r in ref_rs]))
    out = []
    for d, rr in days.items():
        if d == ref_day:
            M, Tm, unix = Mref + dark_at(Tr), Tr, ref_unix
        else:
            M, Tm = master(rr)
            unix = float(np.mean([float(r.get("start_unix", 0)) for r in rr]))
        Mr = M - dark_at(Tm)
        shift = level(Mr) - level(Mref)
        Mr_l = Mr - level(Mr)
        corr = 1.0 if d == ref_day else float(np.corrcoef(_clip_pair(hp(Mr)[gcrop], hp_ref))[0, 1])
        res = _clipped((Fr - Mr_l)[gsub], 6.0)
        rms = float(np.std(res))
        hot_d = hot & (M - b0 - b1 * np.float32(Tm - T_REF) - level(Mr) > thr)
        miss = int((hot & (np.abs(Fr - Mr_l) > 5 * (sigma or 20))).sum())
        out.append({"day": d, "age_days": round((ref_unix - unix) / 86400.0, 2), "frames": len(rr), "T": round(Tm, 2),
                    "level_shift": round(shift, 3), "pattern_corr": round(corr, 4),
                    "hot": int(hot_d.sum()), "hot_new_vs_ref": int((hot_ref & ~hot_d).sum()),
                    "hot_gone_vs_ref": int((hot_d & ~hot_ref).sum()), "residual_rms": round(rms, 3),
                    "extra_rms": round(math.sqrt(max(0.0, rms ** 2 - rms_fresh ** 2)), 3), "hot_misses": miss})
        log(f"ageing {d}: shift {shift:+.2f}, corr {corr:.3f}, extra {out[-1]['extra_rms']:.2f}")
    out.sort(key=lambda x: x["age_days"])
    return {"exposure": exposure, "tcell": tc, "ref_day": ref_day, "test_frame": test["file"],
            "frame_noise": None if sigma is None else round(sigma, 2), "floor": None if floor is None else round(floor, 3),
            "rms_fresh": round(rms_fresh, 3), "expected_corr": None if exp_corr is None else round(exp_corr, 3), "days": out}


def _clip_pair(a, b, k=6.0):
    def m(v):
        med = np.median(v)
        return np.abs(v - med) < k * 1.4826 * np.median(np.abs(v - med)) + 1e-9
    keep = m(a) & m(b)
    return np.stack([a[keep], b[keep]])


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
def tolerance_table(model, cells, exposures, temps=(10.0, 20.0), n=15, pct=(50, 99, 99.9)):
    """Error of using a master 1-3 C off, per pixel percentile of A (dark current above the pedestal),
    against the floor of an n-frame master at that exposure and temperature (measured noise)."""
    A = model["A"][::4, ::4]
    q = {p: float(np.percentile(A, p)) for p in pct}
    q50 = q.get(50, 0.0)
    rows = []
    for T in temps:
        for t in exposures:
            near = [c["noise"] for c in cells if c["exposure"] == t and c.get("noise")]
            nz = float(np.median(near)) if near else None
            floor = MEDIAN_EFF * nz / math.sqrt(n) if nz else None
            for p in pct:
                g = 2 ** ((T - T_REF) / model["Td"])
                errs = [t * (q[p] - q50) * g * (2 ** (d / model["Td"]) - 1) for d in (1, 2, 3)]
                rows.append({"T": T, "exposure": t, "pixel": f"p{p:g}", "err_1C": round(errs[0], 3),
                             "err_2C": round(errs[1], 3), "err_3C": round(errs[2], 3),
                             "floor": None if floor is None else round(floor, 3)})
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


# ---- (run below) -----------------------------------------------------------------------------------------
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



# ---- run -----------------------------------------------------------------------------------------
def run(data_dir, out_root=None, camera="tele", gain=200, sentinel_exposure=60.0, log=print, now=None):
    t_start = time.time()
    archive = os.path.join(data_dir, "darkarchive")
    out_root = out_root or os.path.join(data_dir, "darkmodel")
    stamp = dt.datetime.fromtimestamp(now or time.time(), dt.timezone.utc).strftime("%Y%m%d")
    out = os.path.join(out_root, stamp)
    os.makedirs(out, exist_ok=True)
    recs = load_index(archive, camera, gain)
    log(f"{len(recs)} archived frames ({sum(r['steady'] for r in recs)} steady, {sum(r['first'] for r in recs)} first of a series)")
    cells = make_cells(recs, os.path.join(out_root, "cells", camera), log=log)
    rn, rn_cells = read_noise(cells)
    if rn is None:
        raise ValueError("no bias cell with a noise measurement")
    nm = noise_model(cells, rn)
    log(f"read noise {rn:.2f} ADU; noise model: {nm}" if not nm.get("ok") else
        f"read noise {rn:.2f} ADU; dark shot noise K {nm['K_adu2_s']:.2f} ADU^2/s at {T_REF:g} C, T_d {nm['Td']:.2f} C")
    bias_recs = [r for r in recs if r["exposure"] <= BIAS_MAX_EXP and r["steady"]]
    std_map, n_std = bias_std_map(bias_recs)
    model = fit_model(cells, rn, nm["Td"] if nm.get("ok") else 6.0, log=log)
    flat = _flat_master(data_dir, camera)
    bad, cls = classify(model["A"], std_map, rn, flat)
    stats = loo_stats(model, bad, rn)
    hot = hot_pixel_td(model, bad, rn)
    lag = temperature_lag(recs, model, bad)
    age = ageing(recs, model, bad, cells, sentinel_exposure, log=log)
    biasd = bias_by_day(recs)
    exps = sorted({c["exposure"] for c in model["dark_cells"]})
    tol = tolerance_table(model, cells, exps)

    hdr = fits.Header()
    hdr["T_REF"], hdr["T_D"] = T_REF, model["Td"]
    hdr["COMMENT"] = "master(T,t) = B0 + B1 x + pedestal + t 2^(x / T_D) (A + B x), x = T - T_REF; ADU"
    hdr["COMMENT"] = "hot pixels (HOT_K finite): dark = t 2^(HOT_A + HOT_K x) instead of the A, B term"
    fits.HDUList([fits.PrimaryHDU(header=hdr), fits.ImageHDU(model["b0"], name="B0"), fits.ImageHDU(model["b1"], name="B1"),
                  fits.ImageHDU(model["A"], name="A"), fits.ImageHDU(model["B"], name="B"),
                  fits.ImageHDU(model.get("hot_a"), name="HOT_A"), fits.ImageHDU(model.get("hot_k"), name="HOT_K")]
                 ).writeto(os.path.join(out, "model.fits"), overwrite=True)
    bh = fits.Header()
    bh["COMMENT"] = "bits: 1 hot, 2 warm, 4 noisy (bias std), 8 low flat response (< 0.5)"
    fits.PrimaryHDU(bad, header=bh).writeto(os.path.join(out, "badpixels.fits"), overwrite=True)
    _plots(out, model, stats, nm, hot, lag, age, biasd)
    png_map(model["A"], os.path.join(out, "a_map.png"))
    png_map(model["glow"], os.path.join(out, "glow_blocks.png"), 0, 100)
    if std_map is not None:
        png_map(std_map, os.path.join(out, "bias_std_map.png"))

    summary = {"version": 2, "created": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "camera": camera, "gain": gain, "frames": len(recs), "steady": sum(r["steady"] for r in recs),
               "first_of_series": sum(r["first"] for r in recs),
               "span": [recs[0].get("start"), recs[-1].get("start")] if recs else None,
               "T_range": [round(min(r["T"] for r in recs), 2), round(max(r["T"] for r in recs), 2)] if recs else None,
               "read_noise_adu": round(rn, 3), "read_noise_cells": rn_cells, "bias_std_frames": n_std,
               "noise_model": {k: v for k, v in nm.items() if k != "points"}, "T_ref": T_REF, "Td": round(model["Td"], 3),
               "cells": stats, "classes": cls, "hot_td": {k: v for k, v in hot.items() if k != "td_values"},
               "temperature_lag": lag["bins"], "ageing": age, "bias_by_day": biasd, "tolerance": tol,
               "glow": _glow_summary(model, bad), "runtime_s": round(time.time() - t_start, 1)}
    with open(os.path.join(out, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1, default=float)
    with open(os.path.join(out, "report.md"), "w") as fh:
        fh.write(report_md(summary))
    with open(os.path.join(out_root, "latest.json"), "w") as fh:
        json.dump({"dir": out, "stamp": stamp, "created": summary["created"]}, fh)
    log(f"report: {out}/report.md ({summary['runtime_s']:.0f} s)")
    return out, summary


def _glow_summary(model, bad):
    g = model["glow"]
    if not g.size:
        return {}
    med = float(np.median(g))
    iy, ix = np.unravel_index(np.argmax(g), g.shape)
    good = model["A"][::4, ::4][bad[::4, ::4] == 0]
    spread = float(np.std(_clipped(good)))
    return {"block_px": model["block"], "median_A": round(med, 5), "max_A": round(float(g.max()), 5),
            "min_A": round(float(g.min()), 5), "max_minus_median": round(float(g.max()) - med, 5),
            "max_at_px": [int(iy * model["block"]), int(ix * model["block"])], "good_pixel_A_std": round(spread, 5)}


def _plots(out, model, stats, nm, hot, lag, age, biasd):
    if nm.get("ok"):
        pts = nm["points"]
        Tl = np.linspace(min(p["T"] for p in pts), max(p["T"] for p in pts), 40)
        series = [{"x": [p["T"] for p in pts if p["exposure"] == e], "y": [p["var_dark"] / e for p in pts if p["exposure"] == e],
                   "label": f"{e:g} s"} for e in sorted({p["exposure"] for p in pts})]
        series.append({"x": list(Tl), "y": list(nm["K_adu2_s"] * 2 ** ((Tl - T_REF) / nm["Td"])), "kind": "line",
                       "label": f"fit T_d {nm['Td']:.1f} C", "color": "#000"})
        _w(out, "dark_noise.svg", svg_plot(series, "sensor temperature (C)", "dark shot-noise variance / t (ADU^2/s)",
                                            "Dark current from its shot noise (frame pairs)", logy=True))
    ds = [s for s in stats if s["exposure"] > BIAS_MAX_EXP]
    _w(out, "pedestal.svg", svg_plot([{"x": [s["T"] for s in ds if s["exposure"] == e], "y": [s["pedestal"] for s in ds if s["exposure"] == e],
                                       "label": f"{e:g} s"} for e in sorted({s["exposure"] for s in ds})],
                                     "cell temperature (C)", "pedestal (ADU)", "Pedestal: cell level relative to the bias model"))
    _w(out, "loo.svg", svg_plot([{"x": [s["T"] for s in stats if s["exposure"] == e], "y": [s["ratio"] for s in stats if s["exposure"] == e],
                                  "label": f"{e:g} s"} for e in sorted({s["exposure"] for s in stats})],
                                "cell temperature (C)", "leave-one-out rms / floor (good pixels)",
                                "Model validation (1 = as good as a real master)", hline=1.0))
    if hot.get("fitted"):
        v = hot["td_values"]
        edges = np.linspace(max(0, np.percentile(v, 1)), np.percentile(v, 99), 25)
        hcnt, _ = np.histogram(v, edges)
        _w(out, "hot_td.svg", svg_plot([{"x": list((edges[:-1] + edges[1:]) / 2), "y": list(hcnt), "kind": "line", "label": "hot pixels"}],
                                       "doubling temperature (C)", "pixels", f"Hot-pixel doubling temperature (global {model['Td']:.1f} C)"))
    if lag["points"]:
        _w(out, "temp_lag.svg", svg_plot([{"x": [a for a, _ in lag["points"]], "y": [b for _, b in lag["points"]], "label": "frames >= 60 s"}],
                                         "reading change during the exposure (C)", "hot-pixel equivalent offset (C)",
                                         "Temperature lag: die against reading", hline=0.0))
    if age.get("days"):
        d = age["days"]
        _w(out, "ageing.svg", svg_plot([{"x": [x["age_days"] for x in d], "y": [x["extra_rms"] for x in d], "label": "extra residual rms", "kind": "line"},
                                        {"x": [x["age_days"] for x in d], "y": [x["level_shift"] for x in d], "label": "level shift"}],
                                       "master age (days)", "ADU", f"Ageing: {age['exposure']:g} s masters at {age['tcell']:+d} C",
                                       hline=age["floor"]))
        _w(out, "ageing_hot.svg", svg_plot([{"x": [x["age_days"] for x in d], "y": [x["hot_new_vs_ref"] for x in d], "label": "hot now, not then"},
                                            {"x": [x["age_days"] for x in d], "y": [x["hot_gone_vs_ref"] for x in d], "label": "hot then, not now"}],
                                           "master age (days)", "pixels", "Hot-pixel turnover against the newest master"))
    if biasd:
        _w(out, "bias_by_day.svg", svg_plot([{"x": list(range(len(biasd))), "y": [b["mean"] for b in biasd], "label": "median bias frame mean",
                                              "kind": "line"}], "day index (" + biasd[0]["day"] + " ...)", "ADU", "Bias level by day"))


def _w(out, name, text):
    with open(os.path.join(out, name), "w") as fh:
        fh.write(text)


def _f(v, fmt="{}"):
    return "" if v is None else fmt.format(v)


def report_md(s):
    nm, c = s["noise_model"], s["classes"]["counts"]
    L = [f"# Dark model v2, {s['camera']} gain {s['gain']} ({s['created'][:10]})", "",
         f"From `python -m sequencer darks analyse`. Frames: {s['frames']} ({s['steady']} steady, {s['first_of_series']} first of a series and "
         f"left out), {s['span'][0] if s['span'] else '?'} … {s['span'][1] if s['span'] else '?'}, sensor {s['T_range'][0]} … {s['T_range'][1]} °C. "
         f"ADU on the 16-bit scale; e⁻/ADU not measured yet. Runtime {s['runtime_s']:.0f} s.", "",
         "## Headline", "",
         f"- Read noise **{s['read_noise_adu']:.2f} ADU** (median of {s['read_noise_cells']} bias cells' frame pairs)."]
    if nm.get("ok"):
        L += [f"- **Dark current doubles every {nm['Td']:.2f} ± {nm['Td_se']:.2f} °C** (normal pixels, from shot noise; fit rms {nm['rel_rms'] * 100:.0f} %).",
              f"- Dark shot noise at {s['T_ref']:g} °C: variance **{nm['K_adu2_s']:.2f} ADU²/s**: at 120 s that's "
              f"{math.sqrt(nm['K_adu2_s'] * 120):.1f} ADU rms, against {s['read_noise_adu']:.1f} ADU read noise."]
    else:
        L.append(f"- Dark shot-noise model didn't fit: {nm}")
    h = s["hot_td"]
    L += [f"- Hot pixels {c['hot']}, warm {c['warm']}, noisy {c['noisy']}, low flat {c['low_flat']}; any {c['any']} of {s['classes']['pixels']} "
          f"({c['any'] / s['classes']['pixels'] * 100:.2f} %)."
          + (f" Hot pixels double every {h['td_median']:.2f} °C (10–90 %: {h['td_p10']:.2f}–{h['td_p90']:.2f})." if h.get("fitted") else ""),
          "", "![dark noise](dark_noise.svg)", "",
          "## Validation: leave one out", "",
          "Each cell's master predicted from the model fitted to all the other cells, on good pixels (not hot, warm or noisy), after its "
          "pedestal. Floor: 1.25 × the cell's own frame noise / √frames, the noise of a real median master. Ratio ≈ 1: the model is as good "
          "as a master there. Hot pixels: rms of the same residual on hot pixels (ADU).", "",
          "| Cell | Exposure (s) | T (°C) | Frames | Frame noise | Pedestal | LOO rms (good) | Floor | Ratio | LOO rms (hot) |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for x in s["cells"]:
        L.append(f"| {x['name']} | {x['exposure']:g} | {x['T']} | {x['n']} | {_f(x['noise'])} | {_f(x['pedestal'])} | {_f(x['loo_good'])} | "
                 f"{_f(x['floor'])} | {_f(x['ratio'])} | {_f(x['loo_hot'])} |")
    L += ["", "![loo](loo.svg)", "", "![pedestal](pedestal.svg)", "",
          "## Matching tolerance", "",
          "Error of using a master 1, 2 or 3 °C off, for pixels at the 50th, 99th and 99.9th percentile of dark current above the pedestal "
          "(ADU), against the floor of a 15-frame master at that exposure.", "",
          "| T (°C) | Exposure (s) | Pixel | 1 °C | 2 °C | 3 °C | Floor |", "|---|---|---|---|---|---|---|"]
    L += [f"| {r['T']:g} | {r['exposure']:g} | {r['pixel']} | {r['err_1C']} | {r['err_2C']} | {r['err_3C']} | {_f(r['floor'])} |" for r in s["tolerance"]]
    g = s.get("glow") or {}
    if g:
        L += ["", "## Glow", "", f"A (dark current above the pedestal at {s['T_ref']:g} °C, ADU/s) averaged in {g['block_px']} px blocks without "
              f"hot pixels: median {g['median_A']}, range {g['min_A']} … {g['max_A']} (brightest at row {g['max_at_px'][0]}, column "
              f"{g['max_at_px'][1]}). Pixel-to-pixel spread of good pixels: {g['good_pixel_A_std']} ADU/s.", "",
              "![glow](glow_blocks.png)", "", "Per-pixel A: ![A](a_map.png)"]
    L += ["", "## Hot pixels", ""]
    if h.get("fitted"):
        L += [f"{h['fitted']} of {h['n']} hot pixels have their own exponential (log-linear in T over cells ≥ 10 s). Leave-one-out "
              f"relative error of a hot pixel's signal: median **{h['loo_rel_err_median'] * 100:.1f} %** (90 %: {h['loo_rel_err_p90'] * 100:.1f} %), "
              f"against {h['global_rel_err_median'] * 100:.1f} % (90 %: {h['global_rel_err_p90'] * 100:.1f} %) with the global model.", "",
              "![hot td](hot_td.svg)"]
    L += ["", "## Temperature lag", "", "The brightest hot pixels in single frames ≥ 60 s against the model at the frame's reading, as an equivalent "
          "temperature offset (die minus reading). Steady frames near 0 and warming frames above 0 would mean the reading lags the die.", "",
          "| Reading change (°C) | Frames | Offset (°C) | ± (°C) |", "|---|---|---|---|"]
    L += [f"| {b['dT_from']} … {b['dT_to']} | {b['frames']} | {b['offset_c']} | {b['spread_c']} |" for b in s["temperature_lag"]]
    L += ["", "![lag](temp_lag.svg)", "", "## Ageing", ""]
    a = s["ageing"]
    if a.get("days"):
        L += [f"{a['exposure']:g} s masters per day at {a['tcell']:+d} °C against the newest ({a['ref_day']}), on good pixels, after the model "
              f"(so temperature differences between days are taken out). Fresh test frame `{a['test_frame']}`: frame noise {a['frame_noise']} ADU, "
              f"master floor {a['floor']} ADU, residual against the fresh master {a['rms_fresh']} ADU. Pattern correlation expected from noise "
              f"alone if nothing changed: {a['expected_corr']}.", "",
              "| Day | Age (d) | Frames | T (°C) | Level shift | Pattern corr | Hot | New hot | Gone | Residual rms | Extra rms | Hot misses |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        L += [f"| {d['day']} | {d['age_days']} | {d['frames']} | {d['T']} | {d['level_shift']} | {d['pattern_corr']} | {d['hot']} | "
              f"{d['hot_new_vs_ref']} | {d['hot_gone_vs_ref']} | {d['residual_rms']} | {d['extra_rms']} | {d['hot_misses']} |" for d in a["days"]]
        L += ["", "![ageing](ageing.svg)", "", "![ageing hot](ageing_hot.svg)"]
    else:
        L.append("Not enough days with frames of the ageing key yet.")
    L += ["", "## Bias by day", "", "| Day | Frames | Median frame mean (ADU) | T (°C) | Camera identities |", "|---|---|---|---|---|"]
    L += [f"| {b['day']} | {b['frames']} | {b['mean']} | {b['T']} | {b['identities']} |" for b in s["bias_by_day"]]
    L += ["", "![bias by day](bias_by_day.svg)", "", "Per-pixel bias std (noisy / RTS candidates): ![bias std](bias_std_map.png)", ""]
    return "\n".join(L)
