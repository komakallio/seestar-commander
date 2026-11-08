"""Optimised panorama grid for the SeeStar wide camera on the EQ wedge (offline, no SeeStar contact).

Picks pointings by set cover (scipy MILP / HiGHS) over a numerical sky grid, then orders
them for minimum slew time inside the cable window.

Strategies
  hadec   candidates on constant-Dec levels (the camera's long side lies along Dec, so a
          level is an HA/Dec strip). Inside a level only the RA axis moves; the MILP pays
          one Dec change per level used. Default.
  altrows candidates on constant-alt rows (like plan_grid.py); every step moves both axes.
  free    hadec candidates without the per-level cost: minimum frame count.

Coverage rule: every sky point with alt >= alt_min must lie at least min_overlap/2 inside
some frame, so adjacent frames overlap by >= min_overlap and the bottom edge reaches
min_overlap/2 below alt_min.

Ordering: levels are visited in the cheapest order (brute force over level order and
sweep direction); inside a level frames are swept monotonically in unwrapped HA, so no
move ever crosses the cable seam (all targets lie inside [ha_min, ha_max]).

usage:
  plan_optimal.py [--strategy hadec] [--alt-min -10] [--min-overlap 4] [--ha-min -175 --ha-max 175]
                  [--home-ha -90] [--exp 20] [--download 10] [--out plan.json] [--install [--force]]
  plan_optimal.py --compare [--figure ../docs/grid-optimization.png]   # table + figure for the report
"""
import argparse
import itertools
import json
import math
import os
import sys
import time

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix, csr_matrix, hstack, vstack

sys.path.insert(0, os.path.dirname(__file__))
import panolib as pl
import pano_sim as ps


# ---- candidates -----------------------------------------------------------
def cand_hadec(ha_min, ha_max, ha_step=2.5, dec_step=2.5, dec_min=-45.0, dec_max=89.5):
    decs = np.arange(dec_max, dec_min - 1e-9, -dec_step)[::-1]
    has = np.arange(ha_min, ha_max + 1e-9, ha_step)
    H, D = np.meshgrid(has, decs)
    lvl = np.repeat(np.arange(len(decs)), len(has))
    return H.ravel(), D.ravel(), lvl


def cand_altrows(ha_min, ha_max, alt_min, az_step=3.0, alt_step=2.5):
    H, D, L = [], [], []
    alts = list(np.arange(alt_min + 5, 85.0 + 1e-9, alt_step)) + [88.0]
    for k, alt in enumerate(alts):
        step = az_step / max(math.cos(math.radians(alt)), 0.1)
        for az in np.arange(0, 360, step):
            ha, dec = pl.altaz_to_hadec(alt, az)
            if ha_min <= ha <= ha_max and -85 <= dec <= 89.5:
                H.append(ha); D.append(dec); L.append(k)
    return np.array(H), np.array(D), np.array(L)


# ---- selection ------------------------------------------------------------
def coverage_matrix(P, H, D, cam, need):
    rows, cols = [], []
    for i in range(0, len(H), 256):
        m = ps.margins(P, H[i:i + 256], D[i:i + 256], cam) >= need
        f, p = np.nonzero(m)
        rows.append(p); cols.append(f + i)
    r, c = np.concatenate(rows), np.concatenate(cols)
    return csr_matrix((np.ones(len(r)), (r, c)), shape=(len(P), len(H)))


def select(H, D, L, cam, alt_min, min_overlap, c_frame, c_level, grid=1.5, pad=0.35, time_limit=120,
           log=print):
    """Weighted set cover with optional per-level opening cost. Returns chosen indices, info."""
    P, alt = ps.sky_grid(alt_min, grid)
    A = coverage_matrix(P, H, D, cam, min_overlap / 2 + pad)
    reach = np.asarray(A.sum(axis=1)).ravel() > 0
    useful = np.asarray(A.sum(axis=0)).ravel() > 0
    A = A[reach][:, useful]
    idx = np.nonzero(useful)[0]
    Lu = L[idx]
    levels, lvl_of = np.unique(Lu, return_inverse=True)
    nf, nl = len(idx), len(levels) if c_level > 0 else 0
    cons = [LinearConstraint(hstack([A, csr_matrix((A.shape[0], nl))]).tocsr() if nl else A, lb=1)]
    c = np.concatenate([np.full(nf, c_frame), np.full(nl, c_level)])
    if nl:
        link = coo_matrix((np.concatenate([np.ones(nf), -np.ones(nf)]),
                           (np.concatenate([np.arange(nf)] * 2), np.concatenate([np.arange(nf), nf + lvl_of]))),
                          shape=(nf, nf + nl)).tocsr()
        cons.append(LinearConstraint(link, ub=0))
    log(f"  MILP: {A.shape[0]} sky points ({100 * reach.mean():.1f}% reachable), {nf} candidates, {nl} levels")
    t0 = time.time()
    r = milp(c, constraints=cons, integrality=np.ones(len(c)), bounds=Bounds(0, 1),
             options={"time_limit": time_limit, "disp": False, "mip_rel_gap": 1e-4})
    if r.x is None:
        raise RuntimeError(f"MILP failed: {r.message}")
    chosen = idx[np.nonzero(r.x[:nf] > 0.5)[0]]
    info = {"status": r.message, "seconds": round(time.time() - t0, 1), "objective": r.fun,
            "bound": getattr(r, "mip_dual_bound", None), "reachable_pct": round(100 * reach.mean(), 2)}
    log(f"  MILP: {len(chosen)} frames, {info['seconds']} s, {r.message}")
    return chosen, info


def prune(H, D, cam, alt_min, min_overlap, grid=0.75):
    """Drop frames that the rest already cover (greedy, least-unique first)."""
    P, _ = ps.sky_grid(alt_min, grid)
    M = ps.margins(P, H, D, cam) >= min_overlap / 2
    need = M.any(axis=0)
    keep = np.ones(len(H), bool)
    for i in np.argsort(M.sum(axis=1)):
        keep[i] = False
        if not (M[keep].any(axis=0) >= need).all():
            keep[i] = True
    return keep


# ---- ordering -------------------------------------------------------------
def order(H, D, L, exp, tm, home, max_perm_levels=6):
    """Visit levels in the cheapest order; each level is one monotonic HA sweep."""
    groups = {}
    for h, d, l in zip(H, D, L):
        groups.setdefault(int(l), []).append((float(h), float(d)))
    lv = list(groups)
    for l in lv:
        groups[l].sort()
    best = None
    perms = itertools.permutations(lv) if len(lv) <= max_perm_levels else [sorted(lv, key=lambda l: -np.mean([d for _, d in groups[l]]))]
    for perm in perms:
        for dirs in itertools.product((False, True), repeat=len(perm)) if len(perm) <= max_perm_levels else [None]:
            seq = []
            pos = home
            for k, l in enumerate(perm):
                g = groups[l]
                if dirs is None:  # greedy: start at the nearer end
                    rev = abs(g[-1][0] - pos[0]) < abs(g[0][0] - pos[0])
                else:
                    rev = dirs[k]
                g = g[::-1] if rev else g
                seq += g
                pos = g[-1]
            t = ps.timeline(seq, exp, tm, home)["total_s"]
            if best is None or t < best[0]:
                best = (t, seq)
    return best[1]


def to_frames(seq, cam, row_of=None):
    out = []
    for i, (u, dec) in enumerate(seq):
        alt, az = pl.hadec_to_altaz(u, dec)
        out.append({
            "id": i, "row": (row_of or {}).get((u, dec), 0), "status": "planned",
            "planned": {"alt": round(alt, 2), "az": round(az, 2), "ha": round(((u + 180) % 360) - 180, 3),
                        "dec": round(dec, 3), "ha_u": round(u, 3)},
            "planned_footprint": ps.outline(u, dec, cam, 8),
            "actual": None, "actual_footprint": None, "file": None, "time": None, "note": None,
        })
    return out


# ---- structured levels (strips / rows) --------------------------------------
def level_frames(kind, c, n, A, ha_min, ha_max):
    """Frames of one level. kind 'dec': constant Dec c, n frames evenly over HA [-A, A]
    (A >= 180: full circle, offset half a step so none sits on the seam).
    kind 'alt': constant alt c, n frames evenly in azimuth, HA clamped into the window
    (like panolib.plan_grid). Returns (ha, dec) arrays."""
    if kind == "dec":
        if A >= 180:
            h = -180 + 360 / n * (np.arange(n) + 0.5)
        else:
            h = np.linspace(-A, A, n) if n > 1 else np.zeros(1)
        h = np.clip(h, ha_min, ha_max)
        return h, np.full(n, float(c))
    if c >= 87:
        ha, dec = pl.altaz_to_hadec(c, 180.0)
        return np.array([ha]), np.array([dec])
    hs, ds = [], []
    for k in range(n):
        ha, dec = pl.altaz_to_hadec(c, ((k + 0.5) * 360 / n) % 360)
        hs.append(min(max(ha, ha_min), ha_max)); ds.append(dec)
    return np.array(hs), np.array(ds)


def best_level(kind, P, centres, need, ha_min, ha_max, cam, n_max=36):
    """Fewest frames of one level covering all points P (margin >= need).
    Returns (n, c, A, worst_margin) or None."""
    def worst(c, n, A):
        h, d = level_frames(kind, c, n, A, ha_min, ha_max)
        return float(ps.margins(P, h, d, cam).max(axis=0).min())

    if kind == "dec":
        ha = np.degrees(np.arctan2(-P[:, 1], P[:, 0]))
        ext = float(np.abs(ha).max())
        spans = (180,) if ext > 150 else tuple(np.arange(ext, max(ext - 30, 0) - 1e-9, -2.5))
    else:
        spans = (180,)
    best = None
    for c in centres:
        if worst(c, n_max, spans[0]) < need and (len(spans) == 1 or worst(c, n_max, spans[-1]) < need):
            continue  # this centre can't cover the band at any density
        top = best[0] - 1 if best else n_max
        for n in range(1, top + 1):
            ok = [(A, w) for A in spans for w in [worst(c, n, A)] if w >= need]
            if ok:
                A, wm = max(ok, key=lambda t: t[1])
                best = (n, float(c), float(A), wm)
                break
    return best


def search_levels(kind, alt_min, min_overlap, ha_min, ha_max, cam, c_frame, c_level, grid=1.5, pad=0.3,
                  n_levels=(3, 4), step=2.5, log=print):
    """Stack levels bottom-up; level k is responsible for coord band [b_{k-1}, b_k)
    (coord = Dec or alt). Search boundaries on a grid, best centre/count per band."""
    P, alt = ps.sky_grid(alt_min, grid)
    coord = np.degrees(np.arcsin(np.clip(P[:, 2], -1, 1))) if kind == "dec" else alt
    lo_all, hi_all = float(coord.min()), 90.0
    need = min_overlap / 2 + pad
    memo = {}

    def band(lo, hi):
        key = (lo, hi)
        if key not in memo:
            sel = (coord >= lo) & (coord < hi) if hi < 90 else coord >= lo
            if not sel.any():
                memo[key] = (0, None, 0, 90.0)
            else:
                mid = (max(lo, lo_all) + hi) / 2
                if hi >= 90:
                    cs = np.arange(min(89.5, mid + 12), max(lo, mid - 8) - 1e-9, -1.0)
                else:
                    cs = np.arange(mid - 6, mid + 6.01, 1.0)
                if kind == "alt" and hi >= 90:
                    cs = np.r_[88.0, cs[cs < 87]]
                memo[key] = best_level(kind, P[sel], cs, need, ha_min, ha_max, cam)
        return memo[key]

    edges = np.arange(math.ceil(lo_all / step) * step, 90 - 1e-9, step)
    best = None
    t0 = time.time()
    for K in n_levels:
        for bs in itertools.combinations(edges, K - 1):
            bounds = [-91.0] + list(bs) + [90.0]
            if any(bounds[k + 1] - bounds[k] > cam.fov_long - min_overlap for k in range(1, K)):
                continue
            cost, lv = 0.0, []
            for k in range(K):
                r = band(bounds[k], bounds[k + 1])
                if r is None:
                    cost = None
                    break
                if r[0]:
                    lv.append(r)
                    cost += r[0] * c_frame + c_level
                if best and cost >= best[0]:
                    break
            if cost is not None and (best is None or cost < best[0]):
                best = (cost, lv, bounds)
    if best is None:
        raise RuntimeError(f"no {kind}-level plan covers alt >= {alt_min} inside HA [{ha_min}, {ha_max}]; "
                           "widen the cable window (or add Dec-over-the-pole pointings)")
    log(f"  level search ({kind}): {len(memo)} bands in {time.time() - t0:.0f} s -> "
        f"{sum(r[0] for r in best[1])} frames, bounds {[round(float(b), 1) for b in best[2][1:-1]]}")
    H, D, L = [], [], []
    for k, (n, c, A, wm) in enumerate(best[1]):
        h, d = level_frames(kind, c, n, A, ha_min, ha_max)
        H += list(h); D += list(d); L += [k] * n
        log(f"    level {k}: {kind} {c:5.1f}  {n:2d} frames  HA span ±{A:.0f}  worst margin {wm:.1f}")
    return np.array(H), np.array(D), np.array(L)


def refine_on_levels(H, D, L, cam, alt_min, min_overlap, ha_min, ha_max, time_limit=30, log=print):
    """MILP over all HA positions (1.25 deg) on the chosen Dec levels; keep if it saves frames."""
    decs = sorted(set(D))
    has = np.arange(ha_min, ha_max + 1e-9, 1.25)
    CH = np.tile(has, len(decs))
    CD = np.repeat(decs, len(has))
    CL = np.repeat(np.arange(len(decs)), len(has))
    try:
        ch, info = select(CH, CD, CL, cam, alt_min, min_overlap, 1.0, 0.0, time_limit=time_limit, log=log)
    except RuntimeError as e:
        log(f"  refine: {e}")
        return H, D, L
    if len(ch) < len(H):
        rank = {d: k for k, d in enumerate(decs)}
        return CH[ch], CD[ch], np.array([rank[d] for d in CD[ch]])
    return H, D, L


def plan(strategy="hadec", alt_min=-10.0, min_overlap=4.0, ha_min=-175.0, ha_max=175.0, home_ha=-90.0,
         home_dec=-90.0, exp=20.0, download=10.0, cam=ps.Camera(), tm=None, time_limit=60, log=print):
    tm = tm or ps.TimeModel(download=download)
    typical_step = 25.0
    log(f"{strategy}: alt_min {alt_min}, overlap {min_overlap}, HA [{ha_min}, {ha_max}], exp {exp}s")
    info = {}
    if strategy == "altrows":
        c_frame = tm.frame(exp) + 2 * tm.axis_overhead + tm.sec_per_deg * (typical_step + 5)
        H, D, L = search_levels("alt", alt_min, min_overlap, ha_min, ha_max, cam, c_frame, 0.0,
                                n_levels=(3, 4, 5), step=5.0, log=log)
    elif strategy == "hadec":
        c_frame = tm.frame(exp) + tm.axis_overhead + tm.sec_per_deg * typical_step
        c_level = tm.axis_overhead + tm.sec_per_deg * 40
        H, D, L = search_levels("dec", alt_min, min_overlap, ha_min, ha_max, cam, c_frame, c_level, log=log)
        H, D, L = refine_on_levels(H, D, L, cam, alt_min, min_overlap, ha_min, ha_max, time_limit, log=log)
    else:  # free: unconstrained set cover on a 5 x 5 deg candidate grid (gives a lower bound too)
        CH, CD, CL = cand_hadec(ha_min, ha_max, 5.0, 5.0)
        ch, info = select(CH, CD, CL, cam, alt_min, min_overlap, 1.0, 0.0, grid=2.0, time_limit=time_limit, log=log)
        H, D, L = CH[ch], CD[ch], CL[ch]
    keep = prune(H, D, cam, alt_min, min_overlap)
    if not keep.all():
        log(f"  pruned {int((~keep).sum())} redundant frames")
    H, D, L = H[keep], D[keep], L[keep]
    seq = order(H, D, L, exp, tm, (home_ha, home_dec))
    lvl_rank = {l: r for r, l in enumerate(dict.fromkeys(L[np.argsort([seq.index((h, d)) for h, d in zip(H, D)])]))}
    row_of = {(float(h), float(d)): lvl_rank[l] for h, d, l in zip(H, D, L)}
    frames = to_frames(seq, cam, row_of)
    cv = ps.coverage([u for u, _ in seq], [d for _, d in seq], cam, alt_min, min_overlap)
    info["verified"] = {"covered": cv["covered"], "min_overlap": cv["min_overlap"]}
    if cv["covered"] < 100 or cv["min_overlap"] < min_overlap - 0.5:
        log(f"  WARNING: on the 0.5 deg check grid: covered {cv['covered']}%, min overlap {cv['min_overlap']} deg")
    return frames, info


# ---- report ---------------------------------------------------------------
def fmt_min(s):
    return f"{s / 60:.1f}"


def table(results):
    hdr = ("| plan | frames | covered alt≥−10 | min overlap | −10..0 | 0..15 | 15..30 | 30..50 | 50..70 | 70..90 |"
           " night (meas.) | day (meas.) | night (nominal) | day (nominal) | largest RA move | RA extremes vs home |")
    sep = "|" + "|".join(["---"] * (hdr.count("|") - 1)) + "|"
    lines = [hdr, sep]
    for r in results:
        cv = r["coverage"]
        bands = {b["band"]: b for b in cv["bands"]}
        bcells = [f"{bands[k]['with_overlap']:.0f}% / {bands[k]['min_overlap']:.1f}°" if k in bands else "–"
                  for k in ("-10..0", "0..15", "15..30", "30..50", "50..70", "70..90")]
        lo, hi = r["ra_extremes_vs_home"]
        lines.append(f"| {r['name']} | {r['frames']} | {cv['covered']:.1f}% | {cv['min_overlap']:.1f}° | "
                     + " | ".join(bcells) + " | "
                     + " | ".join(fmt_min(r[k]["total_s"]) + " min" for k in ("night_measured", "day_measured", "night_nominal", "day_nominal"))
                     + f" | {r['night_measured']['largest_ra_move']:.0f}° | {lo:+.0f}° / {hi:+.0f}° |")
    return "\n".join(lines)


def figure(plans, path, alt_min=-10.0):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    rmax = 90 - alt_min + 40  # show frames down to alt ~-50 (low strip reaches far below the horizon)
    fig, axs = plt.subplots(1, len(plans), figsize=(6.2 * len(plans), 6.6))
    for ax, (title, frames) in zip(np.atleast_1d(axs), plans):
        ax.set_aspect("equal"); ax.axis("off")
        for a, ls in ((0, "-"), (alt_min, "--"), (30, ":"), (60, ":")):
            t = np.linspace(0, 2 * np.pi, 361)
            ax.plot((90 - a) * np.sin(t), (90 - a) * np.cos(t), color="0.5" if a else "0.2", lw=0.8, ls=ls)
        for az, lab in ((0, "N"), (90, "E"), (180, "S"), (270, "W")):
            r = 90 - alt_min + 7
            ax.text(-r * math.sin(math.radians(az)), r * math.cos(math.radians(az)), lab, ha="center", va="center", fontsize=11)
        cmap = plt.get_cmap("viridis")
        n = len(frames)
        cx, cy = [], []
        for i, f in enumerate(frames):
            u, dec = f["planned"]["ha_u"], f["planned"]["dec"]
            pts = np.array(ps.outline(u, dec, ps.Camera(), 24))
            r = np.minimum(90 - pts[:, 0], rmax)
            x, y = -r * np.sin(np.radians(pts[:, 1])), r * np.cos(np.radians(pts[:, 1]))
            col = cmap(i / max(n - 1, 1))
            ax.fill(x, y, color=col, alpha=0.10, lw=0)
            ax.plot(np.r_[x, x[0]], np.r_[y, y[0]], color=col, lw=0.9)
            p = f["planned"]
            r0 = 90 - p["alt"]
            cx.append(-r0 * math.sin(math.radians(p["az"]))); cy.append(r0 * math.cos(math.radians(p["az"])))
        ax.plot(cx, cy, color="0.25", lw=0.7, alpha=0.8)
        ax.scatter(cx, cy, c=[cmap(i / max(n - 1, 1)) for i in range(n)], s=14, zorder=3)
        ax.annotate("start", (cx[0], cy[0]), xytext=(4, 4), textcoords="offset points", fontsize=8)
        ax.set_xlim(-rmax, rmax); ax.set_ylim(-rmax, rmax)
        ax.set_title(title, fontsize=11)
    fig.suptitle("Planned footprints, allsky (N up, E left); rings: alt 60/30 dotted, 0 solid, alt_min dashed; colour = capture order",
                 fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=110)


def compare(a):
    kw = dict(alt_min=a.alt_min, min_overlap=a.min_overlap, home=(a.home_ha, -90.0))
    res, plans, out = [], [], {}
    cur = pl.plan_grid()
    res.append(ps.summarise("current alt rows (plan_grid)", cur, **kw)); plans.append(("current: 52 frames, alt rows", cur))
    runs = [("altrows", a.min_overlap), ("free", a.min_overlap), ("hadec", a.min_overlap), ("hadec", 3.0)]
    for strat, ov in runs:
        fr, info = plan(strat, a.alt_min, ov, a.ha_min, a.ha_max, a.home_ha, exp=a.exp,
                        download=a.download, time_limit=a.time_limit)
        name = f"optimal {strat}" + (f" (overlap {ov:g}°)" if ov != a.min_overlap else "")
        res.append(ps.summarise(name, fr, **kw)); res[-1]["milp"] = info
        out.setdefault(strat, fr)
        plans.append((f"{name}: {len(fr)} frames", fr))
    print(table(res))
    for r in res:
        print(r["name"], "MILP", r.get("milp"))
    if a.figure:
        figure([plans[0], plans[3]], a.figure, a.alt_min)
        print("figure:", a.figure)
    if a.out:
        json.dump(pl.new_state(out["hadec"]), open(a.out, "w"), indent=0)
    if a.json:
        json.dump(res, open(a.json, "w"), indent=1, default=float)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategy", choices=("hadec", "altrows", "free"), default="hadec")
    ap.add_argument("--alt-min", type=float, default=-10.0)
    ap.add_argument("--min-overlap", type=float, default=4.0)
    ap.add_argument("--ha-min", type=float, default=-175.0, help="unwrapped HA window (cable), deg")
    ap.add_argument("--ha-max", type=float, default=175.0)
    ap.add_argument("--home-ha", type=float, default=-90.0, help="RA-axis angle at home (cable neutral)")
    ap.add_argument("--home-dec", type=float, default=-90.0, help="Dec at the start position (home = folded)")
    ap.add_argument("--exp", type=float, default=20.0, help="exposure used for the time weights")
    ap.add_argument("--download", type=float, default=10.0)
    ap.add_argument("--roll", type=float, default=0.0, help="camera roll about boresight vs the Dec axis, deg")
    ap.add_argument("--time-limit", type=float, default=120)
    ap.add_argument("--out", help="write a state JSON (panolib format) here")
    ap.add_argument("--json", help="--compare: write the raw results here")
    ap.add_argument("--install", action="store_true", help="write data/pano/state.json + status.js")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--figure")
    a = ap.parse_args()
    if a.compare:
        return compare(a)
    cam = ps.Camera(roll=a.roll)
    frames, info = plan(a.strategy, a.alt_min, a.min_overlap, a.ha_min, a.ha_max, a.home_ha, a.home_dec,
                        a.exp, a.download, cam=cam, time_limit=a.time_limit)
    s = ps.summarise(f"optimal {a.strategy}", frames, cam, a.alt_min, a.min_overlap, (a.home_ha, a.home_dec))
    print(json.dumps({k: v for k, v in s.items()}, indent=1))
    for f in frames:
        p = f["planned"]
        print(f"  {f['id']:3d} row {f['row']} alt {p['alt']:6.1f} az {p['az']:6.1f}  HA_u {p['ha_u']:7.1f} Dec {p['dec']:6.1f}")
    st = pl.new_state(frames)
    st["plan"] = {"strategy": a.strategy, "alt_min": a.alt_min, "min_overlap": a.min_overlap,
                  "ha_window": [a.ha_min, a.ha_max], "home_ha": a.home_ha,
                  "info": json.loads(json.dumps(info, default=float))}
    if a.out:
        json.dump(st, open(a.out, "w"), indent=0)
    if a.install:
        if os.path.exists(pl.STATE) and not a.force:
            old = pl.load_state()
            if any(f["status"] != "planned" for f in old["frames"]):
                sys.exit("state has acquired frames; use --force to replace")
        pl.save_state(st, f"plan_optimal {a.strategy}: {len(frames)} frames")


if __name__ == "__main__":
    main()
