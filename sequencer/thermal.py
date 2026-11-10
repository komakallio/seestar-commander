"""Thermal model of the uncooled sensor for the dark harvest (sequencer/harvest.py): where the idle sensor
settles (the idle floor), where it settles under continuous exposure (the warm plateau), how fast it gets
there, and what temperature a dark frame will have before it is taken.

Model: relaxation towards a floor F (idle) and a plateau P (exposing), both fitted from data, never from
where the harvester happened to start frames:
  - floor: idle stretches of the temperature log (activity "idle", nothing exposing, >= min_stretch_min),
    the recent ones (fit_hours) and the longest of the whole history (history_days). The idle sensor cools
    on two time scales (the die in minutes, the body in an hour or more), so the fit has a fast and a slow
    component, y = F + A_i exp(-t/tau_fast) + B_i exp(-t/tau_slow), and each stretch counts by its length:
    the many short gaps between frames mostly show the fast part, the long stretches the floor;
  - clamp: the floor is never above the lowest idle offset (sensor - ambient) already seen in the history
    (without an ambient: the lowest idle reading of fit_hours), nor more than MAX_BELOW_SEEN_C below it
    (ambient-relative; the slow time constant is also capped at 1.5 x the longest stretch, so short
    stretches can't extrapolate the floor far down); the plateau is never below the highest frame-end
    offset already seen;
  - plateau and tau_heat: an exponential over runs of back-to-back dark frames from the frame archive (the
    readings before and after each frame); runs too short for a fit give P from the frames' own warming
    rates with the default tau_heat.
With an ambient source in the temperature log (the first [templog.sources] entry), the fits are done on
sensor - ambient, so floor and plateau follow the ambient (re-evaluated at every refresh). reach_range()
widens [F, P] to the readings already seen at a similar ambient (+-1 C): a bin the sensor has been in at
this ambient is never "unreachable" or "edge" because of the fit.

A frame's temperature is the mean of the readings before and after it (sequencer/darkpool.py), so for an
exposure e started at T:  T_end = P + (T - P) exp(-e / tau_heat),  frame mean = (T + T_end) / 2.

Defaults before there is data: rise P - F = 4 C, tau_heat 5 min, tau_cool 15 min (S30 Pro telephoto:
~1 C/min warming right after idle, ~6 C above ambient idle).
"""
import datetime as dt
import math
import os
import time

import numpy as np

DEFAULT_RISE_C = 4.0
DEFAULT_TAU_HEAT_S = 300.0
DEFAULT_TAU_COOL_S = 900.0
TAU_GRID_S = np.geomspace(60.0, 4 * 3600.0, 48)


def _unix(s):
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def fit_relax(stretches, min_points=4, flat_c=0.15):
    """Shared asymptote and time constant for several stretches [(ts, ys)] that each relax towards it:
    y = asym + A_i exp(-(t - t0_i) / tau). Returns {asym, tau_s, rms, n, spread} or None. Stretches that
    barely move (spread < flat_c) give the asymptote as their late mean and tau_s None."""
    pts = [(np.asarray(t, float), np.asarray(y, float)) for t, y in stretches if len(t) >= 3]
    n = sum(len(t) for t, _ in pts)
    if n < min_points:
        return None
    spread = max(float(y.max() - y.min()) for _, y in pts)
    if spread < flat_c:
        late = np.concatenate([y[len(y) * 2 // 3:] for _, y in pts])
        return {"asym": float(np.mean(late)), "tau_s": None, "rms": float(np.std(late)), "n": n,
                "spread": round(spread, 3)}
    t_all = np.concatenate([t - t[0] for t, _ in pts])
    y_all = np.concatenate([y for _, y in pts])
    owner = np.concatenate([np.full(len(t), i) for i, (t, _) in enumerate(pts)])
    best = None
    for tau in TAU_GRID_S:
        A = np.zeros((n, 1 + len(pts)))
        A[:, 0] = 1.0
        for i in range(len(pts)):
            m = owner == i
            A[m, 1 + i] = np.exp(-t_all[m] / tau)
        coef, *_ = np.linalg.lstsq(A, y_all, rcond=None)
        sse = float(np.sum((A @ coef - y_all) ** 2))
        if best is None or sse < best[0]:
            best = (sse, tau, coef)
    sse, tau, coef = best
    lo, hi = float(y_all.min()), float(y_all.max())
    asym = float(min(max(coef[0], lo - 3 * (hi - lo) - 1.0), hi + 3 * (hi - lo) + 1.0))
    return {"asym": asym, "tau_s": float(tau), "rms": math.sqrt(sse / n), "n": n, "spread": round(spread, 3)}


TAU_FAST_S = np.geomspace(60.0, 1200.0, 8)
MAX_BELOW_SEEN_C = 1.0      # ambient-relative floor at most this far below the lowest idle offset seen
TAU_SLOW_S = np.geomspace(900.0, 6 * 3600.0, 10)


def fit_relax2(stretches, weights=None, min_points=6, flat_c=0.15):
    """Like fit_relax, with a fast and a slow component (the die cools in minutes, the body in an hour or
    more): y = asym + A_i exp(-(t - t0_i) / tau_fast) + B_i exp(-(t - t0_i) / tau_slow), one asymptote and one
    pair of time constants for all stretches, amplitudes per stretch. A single component is tried too; two
    win only when they cut the weighted error by more than 20 %. weights: one per stretch (default its duration in minutes), so a long
    idle stretch counts far more than the short gaps between frames. Returns {asym, tau_s (slow),
    tau_fast_s, rms, n, spread, components} or None."""
    pts = [(np.asarray(t, float), np.asarray(y, float)) for t, y in stretches if len(t) >= 3]
    n = sum(len(t) for t, _ in pts)
    if n < min_points:
        return None
    spread = max(float(y.max() - y.min()) for _, y in pts)
    if spread < flat_c:
        f = fit_relax([(t, y) for t, y in pts], flat_c=flat_c)
        return f and {**f, "tau_fast_s": None, "components": 0}
    if weights is None:
        weights = [max(1.0, (t[-1] - t[0]) / 60.0) for t, _ in pts]
    t_all = np.concatenate([t - t[0] for t, _ in pts])
    y_all = np.concatenate([y for _, y in pts])
    owner = np.concatenate([np.full(len(t), i) for i, (t, _) in enumerate(pts)])
    sw = np.sqrt(np.concatenate([np.full(len(t), float(w)) for (t, _), w in zip(pts, weights)]))
    k = len(pts)
    # a time constant much longer than the longest stretch can't be seen in the data (it would only
    # extrapolate the floor): at most 1.5 x the longest stretch
    tmax = max(900.0, 1.5 * max(float(t[-1] - t[0]) for t, _ in pts))
    combos = [(None, ts) for ts in TAU_GRID_S if ts <= tmax] + \
        [(tf, ts) for tf in TAU_FAST_S for ts in TAU_SLOW_S if 3 * tf <= ts <= tmax]
    best = {1: None, 2: None}
    for tf, ts in combos:
        A = np.zeros((n, 1 + k * (1 if tf is None else 2)))
        A[:, 0] = 1.0
        for i in range(k):
            m = owner == i
            A[m, 1 + i] = np.exp(-t_all[m] / ts)
            if tf is not None:
                A[m, 1 + k + i] = np.exp(-t_all[m] / tf)
        coef, *_ = np.linalg.lstsq(A * sw[:, None], y_all * sw, rcond=None)
        res = A @ coef - y_all
        sse = float(np.sum((res * sw) ** 2))
        c = 1 if tf is None else 2
        if best[c] is None or sse < best[c][0] * (1 - 1e-6):
            best[c] = (sse, tf, ts, coef, res)
    # two components only when they explain clearly more than one
    one, two = best[1], best[2]
    pick = two if two is not None and (one is None or two[0] < 0.8 * one[0]) else one
    sse, tf, ts, coef, res = pick
    lo, hi = float(y_all.min()), float(y_all.max())
    asym = float(min(max(coef[0], lo - 3 * (hi - lo) - 1.0), hi + 3 * (hi - lo) + 1.0))
    return {"asym": asym, "tau_s": float(ts), "tau_fast_s": None if tf is None else float(tf),
            "rms": float(np.sqrt(np.mean(res ** 2))), "n": n, "spread": round(spread, 3),
            "components": 1 if tf is None else 2}


def _rolling_min(vals, k=3):
    """Lowest k-point running median (one stray reading doesn't count)."""
    if len(vals) < k:
        return min(vals) if vals else None
    return min(float(np.median(vals[i:i + k])) for i in range(len(vals) - k + 1))


class ThermalModel:
    """Fitted from the service's temperature log (idle stretches) and frame pool (dark frame runs)."""

    def __init__(self, svc, fit_hours=12.0, min_stretch_min=10.0, refresh_s=120.0, history_days=14.0,
                 long_stretch_min=45.0):
        self.svc = svc
        self.fit_hours = float(fit_hours)
        self.history_days = float(history_days)
        self.min_stretch_s = float(min_stretch_min) * 60.0
        self.long_stretch_s = float(long_stretch_min) * 60.0
        self.refresh_s = float(refresh_s)
        self._t = None                 # last refresh (service clock)
        self._mono = 0.0               # and in real time (an accelerated sim clock refreshes less often)
        self._files = {}               # templog day file -> ((size, mtime), rows)
        self.s = self._empty()

    @staticmethod
    def _empty():
        return {"floor": None, "plateau": None, "tau_heat_s": DEFAULT_TAU_HEAT_S, "tau_cool_s": DEFAULT_TAU_COOL_S,
                "tau_cool_fast_s": None, "ambient": None, "ambient_source": None, "floor_fit": None,
                "heat_fit": None, "floor_offset": None, "plateau_offset": None, "floor_clamped": False,
                "plateau_clamped": False, "observed_low": None, "observed_high": None, "fitted_unix": None}

    # ---- data -----------------------------------------------------------------------------------
    def _templog(self, now):
        """(rows of the last history_days with "_t" unix, ambient column, interval). Day files are cached
        until they change."""
        tl = getattr(self.svc, "templog", None)
        if tl is None:
            return [], None, 60.0
        src = tl.cfg.sources[0][0] if tl.cfg.sources else None
        t_min = now - self.history_days * 86400.0
        rows = []
        try:
            if hasattr(tl, "path_for"):
                from .templog import read_csv
                seen = []
                for i in range(int(math.ceil(self.history_days)) + 1, -1, -1):
                    path = tl.path_for(now - i * 86400.0)
                    if path in seen:
                        continue
                    seen.append(path)
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue
                    key = (st.st_size, st.st_mtime)
                    c = self._files.get(path)
                    if c is None or c[0] != key:
                        rs = []
                        for r in read_csv(path):
                            t = _unix(r.get("time"))
                            if t is not None:
                                rs.append({**r, "_t": t})
                        c = self._files[path] = (key, rs)
                    rows.extend(r for r in c[1] if t_min <= r["_t"] <= now)
                for p in [p for p in self._files if p not in seen]:
                    del self._files[p]
            else:
                for r in tl.rows(self.history_days * 24.0, now):
                    t = _unix(r.get("time"))
                    if t is not None:
                        rows.append({**r, "_t": t})
        except Exception:  # noqa: BLE001
            rows = []
        return rows, src, tl.cfg.interval_s

    def _frames(self, now, hours=None):
        P = getattr(self.svc, "darkpool", None)
        if P is None:
            return []
        hours = self.fit_hours if hours is None else hours
        with P._lock:
            recs = list(P.frames.values())
        out = []
        for r in recs:
            try:
                t0 = float(r["start_unix"])
                t1 = float(r.get("downloaded_unix") or r.get("end_unix") or t0 + float(r["exposure"]))
                a, b = float(r["temp_start"]), float(r["temp_end"])
            except (KeyError, TypeError, ValueError):
                continue
            if now - hours * 3600.0 <= t0 <= now and t1 > t0 and a and b:
                out.append((t0, a, t1, b))
        return sorted(out)

    def _stretches(self, idle, interval, now):
        """Contiguous idle runs (a gap, or a jump no idle sensor makes, > 1 C a row, splits): the recent ones
        (last fit_hours) and the longest ones of the whole history, which carry the slow component."""
        out, cur = [], []
        for p in idle:
            if cur and (p[0] - cur[-1][0] > 2.5 * interval or abs(p[1] - cur[-1][1]) > 1.0):
                out.append(cur)
                cur = []
            cur.append(p)
        if cur:
            out.append(cur)
        out = [s for s in out if s[-1][0] - s[0][0] >= self.min_stretch_s]
        recent = [s for s in out if s[-1][0] >= now - self.fit_hours * 3600.0][-8:]
        longest = sorted((s for s in out if s[-1][0] - s[0][0] >= self.long_stretch_s),
                         key=lambda s: s[-1][0] - s[0][0])[-6:]
        chosen = sorted({id(s): s for s in recent + longest}.values(), key=lambda s: s[0][0])
        thin = []
        for s in chosen:                   # at most ~120 points a stretch (the fit's cost)
            step = max(1, len(s) // 120)
            thin.append(s[::step] if s[-1] in s[::step] else s[::step] + [s[-1]])
        return thin

    # ---- fit --------------------------------------------------------------------------------------
    def refresh(self, now=None, force=False):
        now = self.svc.clock.time() if now is None else now
        if not force and self._t is not None and (now - self._t < self.refresh_s or time.monotonic() - self._mono < 1.0):
            return self.s
        self._t, self._mono = now, time.monotonic()
        rows, src, interval = self._templog(now)
        amb_t, amb_v = [], []
        idle, seen = [], []                  # idle points; every reading (t, T, amb)
        for r in rows:
            t = r["_t"]
            a = r.get(src) if src else None
            a = float(a) if isinstance(a, (int, float)) else None
            if a is not None:
                amb_t.append(t)
                amb_v.append(a)
            T = r.get("sensor_c")
            if isinstance(T, (int, float)) and T != 0:
                seen.append((t, float(T), a))
                if r.get("activity") == "idle" and not r.get("exposing"):
                    idle.append((t, float(T), a))
        amb_now = amb_v[-1] if amb_t and now - amb_t[-1] < 3 * 3600 else None

        def amb_at(t):
            if not amb_t or t < amb_t[0] - 1800 or t > amb_t[-1] + 1800:
                return None
            return float(np.interp(t, amb_t, amb_v))

        stretches = self._stretches(idle, interval, now)
        use_amb = amb_now is not None and stretches and all(p[2] is not None for s in stretches for p in s)
        ff = fit_relax2([([p[0] for p in s], [p[1] - (p[2] if use_amb else 0.0) for p in s]) for s in stretches])
        out = self._empty()
        out["ambient"], out["ambient_source"] = amb_now, src
        # the floor can't be above an idle reading already seen: the lowest idle offset (sensor - ambient) of
        # the history, or without an ambient the lowest idle reading of the last fit_hours
        if use_amb:
            low_idle = _rolling_min([p[1] - p[2] for p in idle if p[2] is not None])
        else:
            low_idle = _rolling_min([p[1] for p in idle if p[0] >= now - self.fit_hours * 3600.0])
        if ff:
            out["floor_fit"] = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in ff.items()}
            out["floor_fit"]["relative_to_ambient"] = bool(use_amb)
            out["floor_fit"]["stretches"] = len(stretches)
            fl = ff["asym"]
            if low_idle is not None and fl > low_idle:
                fl, out["floor_clamped"] = low_idle, True
            if use_amb and low_idle is not None and fl < low_idle - MAX_BELOW_SEEN_C:
                fl, out["floor_clamped"] = low_idle - MAX_BELOW_SEEN_C, True
            out["floor_fit"]["lowest_idle"] = None if low_idle is None else round(low_idle, 3)
            if use_amb:
                out["floor_offset"] = fl
                out["floor"] = amb_now + fl
            else:
                out["floor"] = fl
            if ff["tau_s"]:
                out["tau_cool_s"] = ff["tau_s"]
                out["tau_cool_fast_s"] = ff["tau_fast_s"]
        # the range already seen at a similar ambient (+-1 C; without an ambient: the last fit_hours)
        if amb_now is not None:
            sim = [p[1] for p in seen if p[2] is not None and abs(p[2] - amb_now) <= 1.0]
        else:
            sim = [p[1] for p in seen if p[0] >= now - self.fit_hours * 3600.0]
        if len(sim) >= 3:
            out["observed_low"], out["observed_high"] = _rolling_min(sim), -_rolling_min([-v for v in sim])

        # heating: runs of back-to-back frames (gap <= 60 s)
        fr = self._frames(now)
        runs, cur = [], []
        for f in fr:
            if cur and f[0] - cur[-1][2] > 60.0:
                runs.append(cur)
                cur = []
            cur.append(f)
        if cur:
            runs.append(cur)
        heat_st = []
        for run in runs:
            ts, ys, ok = [], [], True
            for t0, a, t1, b in run:
                for t, T in ((t0, a), (t1, b)):
                    am = amb_at(t) if amb_now is not None else None
                    if amb_now is not None and am is None:
                        ok = False
                    ts.append(t)
                    ys.append(T - (am or 0.0))
            if ts[-1] - ts[0] >= 120.0 and len(ts) >= 4:
                heat_st.append((ts, ys, ok))
        h_amb = amb_now is not None and heat_st and all(ok for *_, ok in heat_st)
        if amb_now is not None and not h_amb:          # mixed: fit absolute temperatures
            heat_st = []
            for run in runs:
                ts = [t for t0, a, t1, b in run for t in (t0, t1)]
                ys = [T for t0, a, t1, b in run for T in (a, b)]
                if ts[-1] - ts[0] >= 120.0 and len(ts) >= 4:
                    heat_st.append((ts, ys, True))
        heat_st = heat_st[-6:]
        hf = fit_relax([(ts, ys) for ts, ys, _ in heat_st])
        off = amb_now if h_amb else 0.0
        plateau = None
        if hf and hf["tau_s"] and hf["asym"] > max(y for _, ys, _ in heat_st for y in ys) - 0.05:
            plateau = hf["asym"] + off
            out["tau_heat_s"] = hf["tau_s"]
        elif fr:
            # frame warming rates, default tau: P = T_mid + rate * tau (weighted by duration)
            tau = out["tau_heat_s"]
            num = den = 0.0
            for t0, a, t1, b in fr[-40:]:
                d = t1 - t0
                num += d * ((a + b) / 2 + (b - a) / d * tau)
                den += d
            plateau = num / den if den else None
        if hf:
            out["heat_fit"] = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in hf.items()}
            out["heat_fit"]["relative_to_ambient"] = bool(h_amb)
        # the plateau can't be below an exposing reading already seen: the highest frame-end offset of the
        # history (or the highest frame-end reading of the last fit_hours)
        if amb_now is not None:
            offs = [b - amb_at(t1) for t0, a, t1, b in self._frames(now, self.history_days * 24.0)
                    if amb_at(t1) is not None]
            high = amb_now + max(offs) if offs else None
        else:
            high = max((b for *_, b in fr), default=None)
        if plateau is not None and high is not None and plateau < high:
            plateau, out["plateau_clamped"] = high, True
        if plateau is not None and out["floor"] is not None:
            plateau = max(plateau, out["floor"])
        if plateau is None and out["floor"] is not None:
            plateau = out["floor"] + DEFAULT_RISE_C
        out["plateau"] = plateau
        if plateau is not None and amb_now is not None:
            out["plateau_offset"] = plateau - amb_now
        out["fitted_unix"] = now
        self.s = out
        return out

    # ---- use -------------------------------------------------------------------------------------
    def plateau_for(self, T):
        s = self.s
        if s["plateau"] is not None:
            return s["plateau"]
        base = s["floor"] if s["floor"] is not None else T
        return base + DEFAULT_RISE_C

    def reach_range(self):
        """(low, high) the sensor can be at now: the fitted floor and plateau, widened to what has already
        been seen at a similar ambient. None when the floor isn't known."""
        s = self.s
        if s["floor"] is None:
            return None
        lo, hi = s["floor"], self.plateau_for(s["floor"])
        if s["observed_low"] is not None:
            lo, hi = min(lo, s["observed_low"]), max(hi, s["observed_high"])
        return lo, hi

    def predict(self, T, exposure):
        """(frame mean, reading at the end) for a dark of `exposure` s started at sensor temperature T."""
        P = self.plateau_for(T)
        end = P + (T - P) * math.exp(-float(exposure) / self.s["tau_heat_s"])
        return (T + end) / 2.0, end

    def warming_rate(self, T):
        """C/min while exposing at T."""
        return (self.plateau_for(T) - T) / self.s["tau_heat_s"] * 60.0

    def cooling_eta_s(self, T, target):
        """Seconds of idle until T falls to target (None: the floor is above it or unknown)."""
        F = self.s["floor"]
        if F is None or target <= F + 0.02 or T <= target:
            return None if T > target else 0.0
        return self.s["tau_cool_s"] * math.log((T - F) / (target - F))

    def view(self, T=None):
        s = self.s
        r = lambda v, n=2: None if v is None else round(float(v), n)  # noqa: E731
        return {"idle_floor": r(s["floor"]), "plateau": r(s["plateau"]),
                "warming_rate": r(self.warming_rate(T)) if T is not None else None,
                "tau_heat_min": r(s["tau_heat_s"] / 60.0, 1), "tau_cool_min": r(s["tau_cool_s"] / 60.0, 1),
                "ambient": r(s["ambient"]), "ambient_source": s["ambient_source"],
                "floor_offset": r(s["floor_offset"]), "plateau_offset": r(s["plateau_offset"]),
                "tau_cool_fast_min": r(s["tau_cool_fast_s"] / 60.0, 1) if s["tau_cool_fast_s"] else None,
                "floor_clamped": s["floor_clamped"], "plateau_clamped": s["plateau_clamped"],
                "observed_low": r(s["observed_low"]), "observed_high": r(s["observed_high"]),
                "floor_fit": s["floor_fit"], "heat_fit": s["heat_fit"]}
