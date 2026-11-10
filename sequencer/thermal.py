"""Thermal model of the uncooled sensor for the dark harvest (sequencer/harvest.py): where the idle sensor
settles (the idle floor), where it settles under continuous exposure (the warm plateau), how fast it gets
there, and what temperature a dark frame will have before it is taken.

Model: first-order relaxation. Idle, the sensor relaxes towards the floor F with time constant tau_cool;
exposing, towards the plateau P with tau_heat. Both are fitted from data, never from where the harvester
happened to start frames:
  - floor and tau_cool: an exponential decay fitted to the idle stretches of the temperature log
    (rows with activity "idle" and nothing exposing, at least min_stretch_min long, last fit_hours),
    T(t) = F + A_i exp(-(t - t_i) / tau) with one F and tau shared by all stretches and one amplitude per
    stretch (a grid over tau, linear least squares for the rest);
  - plateau and tau_heat: the same fit over runs of back-to-back dark frames from the frame archive (the
    readings before and after each frame); a run too short for a fit gives P from the frames' own warming
    rates with the default tau_heat.
With an ambient source in the temperature log (the first [templog.sources] entry), both fits are done on
sensor - ambient, so the floor and plateau follow the ambient as it changes (re-evaluated at every refresh).

A frame's temperature is the mean of the readings before and after it (sequencer/darkpool.py), so for an
exposure e started at T:  T_end = P + (T - P) exp(-e / tau_heat),  frame mean = (T + T_end) / 2.

Defaults before there is data: rise P - F = 4 C, tau_heat 5 min, tau_cool 15 min (S30 Pro telephoto:
~1 C/min warming right after idle, ~6 C above ambient idle).
"""
import datetime as dt
import math
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


class ThermalModel:
    """Fitted from the service's temperature log (idle stretches) and frame pool (dark frame runs)."""

    def __init__(self, svc, fit_hours=12.0, min_stretch_min=10.0, refresh_s=120.0):
        self.svc = svc
        self.fit_hours = float(fit_hours)
        self.min_stretch_s = float(min_stretch_min) * 60.0
        self.refresh_s = float(refresh_s)
        self._t = None                 # last refresh (service clock)
        self._mono = 0.0               # and in real time (an accelerated sim clock refreshes less often)
        self.s = self._empty()

    @staticmethod
    def _empty():
        return {"floor": None, "plateau": None, "tau_heat_s": DEFAULT_TAU_HEAT_S, "tau_cool_s": DEFAULT_TAU_COOL_S,
                "ambient": None, "ambient_source": None, "floor_fit": None, "heat_fit": None,
                "floor_offset": None, "plateau_offset": None, "fitted_unix": None}

    # ---- data -----------------------------------------------------------------------------------
    def _templog(self, now):
        tl = getattr(self.svc, "templog", None)
        if tl is None:
            return [], None, 60.0
        try:
            rows = tl.rows(self.fit_hours, now)
        except Exception:  # noqa: BLE001
            rows = []
        src = tl.cfg.sources[0][0] if tl.cfg.sources else None
        return rows, src, tl.cfg.interval_s

    def _frames(self, now):
        P = getattr(self.svc, "darkpool", None)
        if P is None:
            return []
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
            if now - self.fit_hours * 3600.0 <= t0 <= now and t1 > t0 and a and b:
                out.append((t0, a, t1, b))
        return sorted(out)

    # ---- fit --------------------------------------------------------------------------------------
    def refresh(self, now=None, force=False):
        now = self.svc.clock.time() if now is None else now
        if not force and self._t is not None and (now - self._t < self.refresh_s or time.monotonic() - self._mono < 1.0):
            return self.s
        self._t, self._mono = now, time.monotonic()
        rows, src, interval = self._templog(now)
        amb_t, amb_v = [], []
        idle = []
        for r in rows:
            t = _unix(r.get("time"))
            if t is None:
                continue
            a = r.get(src) if src else None
            if isinstance(a, (int, float)):
                amb_t.append(t)
                amb_v.append(float(a))
            T = r.get("sensor_c")
            if isinstance(T, (int, float)) and T != 0 and r.get("activity") == "idle" and not r.get("exposing"):
                idle.append((t, float(T), float(a) if isinstance(a, (int, float)) else None))
        amb_now = amb_v[-1] if amb_t and now - amb_t[-1] < 3 * 3600 else None

        def amb_at(t):
            if not amb_t or t < amb_t[0] - 1800 or t > amb_t[-1] + 1800:
                return None
            return float(np.interp(t, amb_t, amb_v))

        # idle stretches: contiguous idle rows
        stretches, cur = [], []
        for p in idle:                         # a gap, or a jump no idle sensor makes (> 1 C a row), splits
            if cur and (p[0] - cur[-1][0] > 2.5 * interval or abs(p[1] - cur[-1][1]) > 1.0):
                stretches.append(cur)
                cur = []
            cur.append(p)
        if cur:
            stretches.append(cur)
        stretches = [s for s in stretches if s[-1][0] - s[0][0] >= self.min_stretch_s][-8:]
        use_amb = amb_now is not None and stretches and all(p[2] is not None for s in stretches for p in s)
        ff = fit_relax([([p[0] for p in s], [p[1] - (p[2] if use_amb else 0.0) for p in s]) for s in stretches])
        out = self._empty()
        out["ambient"], out["ambient_source"] = amb_now, src
        if ff:
            out["floor_fit"] = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in ff.items()}
            out["floor_fit"]["relative_to_ambient"] = bool(use_amb)
            if use_amb:
                out["floor_offset"] = ff["asym"]
                out["floor"] = amb_now + ff["asym"]
            else:
                out["floor"] = ff["asym"]
            if ff["tau_s"]:
                out["tau_cool_s"] = ff["tau_s"]

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
                "floor_fit": s["floor_fit"], "heat_fit": s["heat_fit"]}
