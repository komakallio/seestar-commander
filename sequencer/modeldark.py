"""Model darks: a dark frame computed from the dark-current model (sequencer/darkmodel.py,
`darks analyse`) at a light's own exposure and sensor temperature, plus its bad-pixel map.

    dark(T, t) = B0 + B1 x + pedestal(t) + t 2^(x / T_D) (A + B x),   x = T - T_REF
    hot pixels (HOT_K finite): t 2^(HOT_A + HOT_K x) instead of the A, B term

pedestal(t): the black-level clamp's level relative to the bias model, a line in exposure fitted to the
model's per-master pedestals (the light's own pedestal can't be measured; a constant error only shifts
the stack's zero, and after the flat it leaves a faint vignetting-shaped residual of that size).

Config, [darks]:
    source = "master"    calibrate with dark-library masters (the default; nothing changes)
             "model"     calibrate with model darks; a light outside the model's range falls back to a master
             "compare"   masters as now, plus a second stack with model darks (stack_model.fits) to compare
    model_dir = ""       default <data>/darkmodel (uses latest.json there)
    bad_pixels = true    model darks: replace bad-map pixels by the median of their same-colour neighbours

Nothing here changes or deletes a master, a model or a frame: model darks are computed on the fly, the raw
lights are saved as before, and switching source back to "master" restores the old pipeline.
"""
import json
import os
import warnings

import numpy as np
from astropy.io import fits

SOURCES = ("master", "model", "compare")


class ModelDark:
    def __init__(self, model_dir, log=print, camera="tele", gain=200):
        self.dir, self.log, self.camera, self.gain = model_dir, log, camera, gain
        self._stamp = None
        self.m = None
        self._cache = {}

    # ---- loading ---------------------------------------------------------------------------------
    def _latest(self):
        p = os.path.join(self.dir, "latest.json")
        try:
            with open(p) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def load(self):
        """(Re)load when latest.json points at another run. Returns True when a model is available."""
        lt = self._latest()
        if not lt:
            self.m = None
            return False
        key = (lt.get("dir"), lt.get("created"))
        if key == self._stamp and self.m is not None:
            return True
        d = lt["dir"]
        try:
            with fits.open(os.path.join(d, "model.fits")) as hd:
                h = hd[0].header
                maps = {n: np.asarray(hd[n].data, np.float32) for n in ("B0", "B1", "A", "B")}
                for n in ("HOT_A", "HOT_K"):
                    maps[n] = np.asarray(hd[n].data, np.float32) if n in hd and hd[n].data is not None else None
            with open(os.path.join(d, "summary.json")) as fh:
                s = json.load(fh)
            bad = np.asarray(fits.getdata(os.path.join(d, "badpixels.fits")), np.uint8)
        except Exception as e:  # noqa: BLE001
            self.log(f"model darks: can't load {d}: {e}")
            self.m = None
            return False
        if s.get("camera") != self.camera or int(s.get("gain", -1)) != self.gain:
            self.log(f"model darks: {d} is {s.get('camera')} gain {s.get('gain')}, not {self.camera} gain {self.gain}")
            self.m = None
            return False
        cells = [c for c in s.get("cells", []) if c.get("pedestal") is not None]
        t = np.array([c["exposure"] for c in cells], float)
        p = np.array([c["pedestal"] for c in cells], float)
        if t.size >= 2 and np.ptp(t) > 0:
            p1, p0 = np.polyfit(t, p, 1)
        else:
            p0, p1 = (float(p.mean()) if p.size else 0.0), 0.0
        own = maps["HOT_K"] is not None and np.isfinite(maps["HOT_K"]).any()
        exps = sorted({c["exposure"] for c in s.get("cells", []) if c["exposure"] > 0.01})
        self.m = {**maps, "T_REF": float(h["T_REF"]), "T_D": float(h["T_D"]), "bad": bad > 0,
                  "own": np.isfinite(maps["HOT_K"]) if own else None, "ped": (float(p0), float(p1)),
                  "T_range": tuple(s.get("T_range") or (None, None)),
                  "exp_range": (min(exps), max(exps)) if exps else (None, None),
                  "id": f"MODEL:{lt.get('stamp')}", "dir": d}
        self._stamp, self._cache = key, {}
        self.log(f"model darks: loaded {d} (T_d {self.m['T_D']:.2f} C, exposures {self.m['exp_range']}, "
                 f"sensor {self.m['T_range']} C, bad pixels {int(self.m['bad'].sum())})")
        return True

    # ---- use -------------------------------------------------------------------------------------
    def covers(self, exposure, temp, gain=None, margin_c=1.0):
        """None when the model can make this dark, else why not (outside what it was fitted on)."""
        if self.m is None:
            return "no model"
        if gain is not None and int(gain) != self.gain:
            return f"gain {gain} (the model is gain {self.gain})"
        lo, hi = self.m["exp_range"]
        if lo is None or not (lo - 1e-6 <= exposure <= hi + 1e-6):
            return f"exposure {exposure:g} s outside the model's {lo:g}-{hi:g} s"
        tlo, thi = self.m["T_range"]
        if temp is None:
            return "no sensor temperature"
        if tlo is None or not (tlo - margin_c <= temp <= thi + margin_c):
            return f"sensor {temp:.1f} C outside the model's {tlo:.1f}-{thi:.1f} C"
        return None

    def dark(self, exposure, temp, gain=None):
        """(entry, array) for a light, or (None, None) when the model can't cover it."""
        if self.m is None or self.covers(exposure, temp, gain):
            return None, None
        key = (round(float(exposure), 3), round(float(temp), 2))
        if key in self._cache:
            return self._cache[key]
        m = self.m
        x = np.float32(temp - m["T_REF"])
        d = np.float32(exposure * 2 ** (float(x) / m["T_D"])) * (m["A"] + m["B"] * x)
        if m["own"] is not None:
            d[m["own"]] = exposure * 2.0 ** (m["HOT_A"][m["own"]] + m["HOT_K"][m["own"]] * x)
        d += m["B0"] + m["B1"] * x + np.float32(m["ped"][0] + m["ped"][1] * exposure)
        entry = {"id": m["id"], "age_days": 0.0, "model_dir": m["dir"], "exposure": exposure, "temp": temp}
        if len(self._cache) > 4:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = (entry, d)
        return entry, d

    @property
    def bad(self):
        return None if self.m is None else self.m["bad"]


def fill_bad(raw, bad):
    """Bad pixels replaced by the median of their four same-colour Bayer neighbours (+-2 px); a copy."""
    if bad is None or bad.shape != raw.shape or not bad.any():
        return raw
    d = np.asarray(raw, np.float32).copy()
    p = np.pad(d, 2, mode="reflect")
    ys, xs = np.nonzero(bad)
    nb = np.stack([p[ys, xs + 2], p[ys + 4, xs + 2], p[ys + 2, xs], p[ys + 2, xs + 4]])   # up, down, left, right
    pb = np.pad(bad, 2, mode="reflect")
    ok = ~np.stack([pb[ys, xs + 2], pb[ys + 4, xs + 2], pb[ys + 2, xs], pb[ys + 2, xs + 4]])
    nb = np.where(ok, nb, np.nan)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)       # all four neighbours bad: keep the pixel
        v = np.nanmedian(nb, axis=0)
    d[ys, xs] = np.where(np.isfinite(v), v, d[ys, xs])
    return d
