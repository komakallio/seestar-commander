"""Pointing memory: remember where the mount really points, so the first slew after an unpark lands
close to the target instead of 1.5-2 deg off.

The SeeStar firmware forgets every SyncToCoordinates when it parks. Its raw pointing has a large
constant part (one wedge: Dec about +1.5 deg) and an HA-dependent part from the polar-axis error. So:

- every successful centring solve is stored (data/pointing/solves.jsonl): time, target, the mount's
  reported HA/Dec, the *raw* mount HA/Dec (reported minus the sync shift, below), the solved HA/Dec
  (JNow, frame centre) and the offset solved - raw;
- the **sync shift** is tracked (data/pointing/state.json): zero after a park (the firmware drops its
  sync), and every SyncToCoordinates adds (synced-to - reported) in HA/Dec. With it, solves taken
  while the mount is synced still give raw offsets. If the shift is unknown (service started with the
  scope unparked and no saved state, or another tool had the scope), nothing is recorded or corrected
  until the next park;
- a model of the raw offset: the median of the latest solves (constant), or once there are enough
  solves spread in HA, a least-squares fit of the classic equatorial terms (below);
- the service does *not* sync after an unpark. It offsets the commanded position of each target's
  first slew by (model at the target - sync shift); the centring solve then syncs as before. See
  docs/design.md, "Pointing memory", for why.

Model (offset = true - raw mount, degrees; h = HA, d = Dec):
    dH = IH + CH sec d + NP tan d - MA cos h tan d - ME sin h tan d
    dD = ID              + MA sin h          - ME cos h
  IH, ID   index offsets in HA and Dec
  CH, NP   collimation and HA/Dec non-perpendicularity (optional, off by default)
  MA       polar axis azimuth error, + = axis east of the pole (same sign as sequencer/polar.py's az)
  ME       polar axis elevation error, + = axis too high (same sign as polar.py's alt)
The HA rows are fitted as sky arc (dH cos d), so high-Dec points don't dominate. Refraction is not
modelled (under 2' above 30 deg altitude).
"""
import json
import math
import os
import threading

import numpy as np

from . import astro

ALL_TERMS = ("IH", "ID", "CH", "NP", "MA", "ME")
DEFAULT_TERMS = ("IH", "ID", "MA", "ME")


class PointingConfig:
    """[pointing] in config.toml."""

    def __init__(self, enabled=True, max_age_days=30.0, min_solves_for_fit=8, min_ha_span_deg=30.0,
                 window=60, constant_window=5, terms=DEFAULT_TERMS, max_correction_deg=10.0):
        self.enabled = bool(enabled)
        self.max_age_days = float(max_age_days)
        self.min_solves_for_fit = int(min_solves_for_fit)
        self.min_ha_span_deg = float(min_ha_span_deg)
        self.window = int(window)
        self.constant_window = int(constant_window)
        self.terms = tuple(t.upper() for t in terms)
        bad = [t for t in self.terms if t not in ALL_TERMS]
        if bad:
            raise ValueError(f"[pointing] terms: unknown {bad} (use {', '.join(ALL_TERMS)})")
        self.max_correction_deg = float(max_correction_deg)

    @classmethod
    def from_dict(cls, d):
        known = {k: v for k, v in (d or {}).items() if k in cls.__init__.__code__.co_varnames}
        return cls(**known)

    def view(self):
        return {"enabled": self.enabled, "max_age_days": self.max_age_days,
                "min_solves_for_fit": self.min_solves_for_fit, "min_ha_span_deg": self.min_ha_span_deg,
                "window": self.window, "constant_window": self.constant_window, "terms": list(self.terms),
                "max_correction_deg": self.max_correction_deg}


def _rows(h, d, terms):
    """Design-matrix rows (HA as sky arc, Dec) for one point."""
    hr, dr = math.radians(h), math.radians(d)
    ch, sh, cd, sd = math.cos(hr), math.sin(hr), math.cos(dr), math.sin(dr)
    col = {"IH": (cd, 0.0), "ID": (0.0, 1.0), "CH": (1.0, 0.0), "NP": (sd, 0.0),
           "MA": (-ch * sd, sh), "ME": (-sh * sd, -ch)}
    return [col[t][0] for t in terms], [col[t][1] for t in terms]


def fit(points, terms=DEFAULT_TERMS):
    """Least-squares fit of `terms` to points [(h, d, dH, dD)] in degrees.
    Returns {"terms": {name: deg}, "rms_arcmin", "residuals": [(rH_sky_arcmin, rD_arcmin)]}."""
    A, y = [], []
    for h, d, dh, dd in points:
        rh, rd = _rows(h, d, terms)
        A += [rh, rd]
        y += [dh * math.cos(math.radians(d)), dd]
    A, y = np.asarray(A), np.asarray(y)
    x, *_ = np.linalg.lstsq(A, y, rcond=None)
    r = (y - A @ x) * 60.0
    res = [(float(r[2 * i]), float(r[2 * i + 1])) for i in range(len(points))]
    rms = float(np.sqrt(np.mean([a * a + b * b for a, b in res]))) if res else 0.0
    return {"terms": {t: float(v) for t, v in zip(terms, x)}, "rms_arcmin": rms, "residuals": res,
            "rank": int(np.linalg.matrix_rank(A))}


def predict(terms, h, d):
    """(dH, dD) in degrees from fitted terms at (h, d)."""
    names = tuple(terms)
    rh, rd = _rows(h, d, names)
    cd = max(math.cos(math.radians(d)), 0.05)
    sh = sum(c * terms[n] for c, n in zip(rh, names))
    return sh / cd, sum(c * terms[n] for c, n in zip(rd, names))


def sky_arcmin(dh, dd, d):
    return math.hypot(dh * math.cos(math.radians(d)), dd) * 60.0


class PointingMemory:
    """Solve log, sync-shift bookkeeping and the model. Thread-safe; all angles in degrees."""

    def __init__(self, directory, cfg: PointingConfig, clock, log=print, since=None):
        self.dir, self.cfg, self.clock, self.log = directory, cfg, clock, log
        self.since = since                      # callable -> unix time; solves before it are ignored
        self._lock = threading.RLock()
        os.makedirs(directory, exist_ok=True)
        self.solves_path = os.path.join(directory, "solves.jsonl")
        self.state_path = os.path.join(directory, "state.json")
        self.records = self._load_records()
        self.shift = None                       # (dH, dD) baked into the firmware by syncs; None = unknown
        self.shift_note = "unknown"
        self._model = None
        self.last_correction = None

    # ---- persistence ------------------------------------------------------------------------------
    def _load_records(self):
        out = []
        try:
            with open(self.solves_path) as fh:
                for line in fh:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
        except FileNotFoundError:
            pass
        return out

    def _save_state(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"shift_ha": None if self.shift is None else round(self.shift[0], 5),
                       "shift_dec": None if self.shift is None else round(self.shift[1], 5),
                       "note": self.shift_note, "unix": round(self.clock.time(), 1)}, fh)
        os.replace(tmp, self.state_path)

    def load_state(self, at_park):
        """At service start: parked = no sync in the firmware; else trust the saved shift, if any."""
        with self._lock:
            if at_park:
                return self.note_parked("service start: parked")
            try:
                with open(self.state_path) as fh:
                    st = json.load(fh)
                if st.get("shift_ha") is not None:
                    self.shift = (float(st["shift_ha"]), float(st["shift_dec"]))
                    self.shift_note = f"restored ({st.get('note')})"
                    return
            except (FileNotFoundError, ValueError, KeyError, TypeError):
                pass
            self.note_unknown("service start: not parked, no saved sync state")

    # ---- sync bookkeeping -------------------------------------------------------------------------
    def note_parked(self, why="parked"):
        """The firmware Park drops its sync: the reported frame is the raw one again."""
        with self._lock:
            self.shift, self.shift_note = (0.0, 0.0), why
            self._save_state()

    def note_unknown(self, why):
        with self._lock:
            if self.shift is not None or self.shift_note != why:
                self.log(f"pointing memory: sync state unknown ({why}); no solves recorded or slews "
                         "corrected until the next park")
            self.shift, self.shift_note = None, why
            self._save_state()

    def note_sync(self, reported, synced):
        """SyncToCoordinates at reported (ha, dec) to synced (ha, dec)."""
        with self._lock:
            if self.shift is None:
                return
            dh = astro.wrap180(synced[0] - reported[0])
            self.shift = (self.shift[0] + dh, self.shift[1] + (synced[1] - reported[1]))
            self.shift_note = "synced"
            self._save_state()

    # ---- solves -----------------------------------------------------------------------------------
    def add_solve(self, t, target, reported, solved, extra=None):
        """reported/solved: (ha, dec) JNow at time t. Returns the record (None if the shift is unknown)."""
        with self._lock:
            if self.shift is None:
                return None
            raw = (astro.wrap180(reported[0] - self.shift[0]), reported[1] - self.shift[1])
            off = (astro.wrap180(solved[0] - raw[0]), solved[1] - raw[1])
            rec = {"unix": round(t, 1), "time": _iso(t), "target": target,
                   "mount_ha": round(reported[0], 5), "mount_dec": round(reported[1], 5),
                   "raw_ha": round(raw[0], 5), "raw_dec": round(raw[1], 5),
                   "solved_ha": round(solved[0], 5), "solved_dec": round(solved[1], 5),
                   "shift_ha": round(self.shift[0], 5), "shift_dec": round(self.shift[1], 5),
                   "off_ha": round(off[0], 5), "off_dec": round(off[1], 5),
                   "off_arcmin": round(sky_arcmin(off[0], off[1], raw[1]), 2), **(extra or {})}
            self.records.append(rec)
            with open(self.solves_path, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
            self._model = None
            return rec

    def usable(self, now=None):
        """Records inside the age limit, after the last polar alignment, newest last, at most `window`."""
        now = self.clock.time() if now is None else now
        t0 = now - self.cfg.max_age_days * 86400.0
        if self.since:
            try:
                t0 = max(t0, float(self.since() or 0.0))
            except Exception:  # noqa: BLE001
                pass
        recs = [r for r in self.records if t0 <= r.get("unix", 0) <= now + 60]
        return recs[-self.cfg.window:] if self.cfg.window > 0 else recs

    # ---- model ------------------------------------------------------------------------------------
    def invalidate(self, why=None):
        with self._lock:
            self._model = None
        if why:
            self.log(f"pointing memory: {why}")

    def model(self):
        with self._lock:
            if self._model is None or self.clock.time() - self._model.get("_t", 0) > 600:
                self._model = self._build()
            return self._model

    def _build(self):
        recs = self.usable()
        m = {"_t": self.clock.time(), "n": len(recs)}
        if not recs:
            return {**m, "kind": "none"}
        has = [astro.wrap180(r["raw_ha"]) for r in recs]
        span = max(has) - min(has)
        m["ha_span_deg"] = round(span, 1)
        if len(recs) >= self.cfg.min_solves_for_fit and span >= self.cfg.min_ha_span_deg:
            pts = [(r["raw_ha"], r["raw_dec"], r["off_ha"], r["off_dec"]) for r in recs]
            f = fit(pts, self.cfg.terms)
            sane = f["rank"] == len(self.cfg.terms) and all(abs(v) < 15.0 for v in f["terms"].values())
            if sane:
                return {**m, "kind": "fit", "terms": f["terms"], "rms_arcmin": f["rms_arcmin"],
                        "residuals": [{"target": r.get("target"), "time": r.get("time"), "ha": r["raw_ha"],
                                       "dec": r["raw_dec"], "dha_arcmin": round(a, 2), "ddec_arcmin": round(b, 2)}
                                      for r, (a, b) in zip(recs, f["residuals"])]}
            m["fit_rejected"] = f"fit not sane (rank {f['rank']}, terms {f['terms']})"
        last = recs[-max(1, self.cfg.constant_window):]
        dh = float(np.median([r["off_ha"] for r in last]))
        dd = float(np.median([r["off_dec"] for r in last]))
        res = [{"target": r.get("target"), "time": r.get("time"), "ha": r["raw_ha"], "dec": r["raw_dec"],
                "dha_arcmin": round((r["off_ha"] - dh) * math.cos(math.radians(r["raw_dec"])) * 60, 2),
                "ddec_arcmin": round((r["off_dec"] - dd) * 60, 2)} for r in recs]
        rms = float(np.sqrt(np.mean([x["dha_arcmin"] ** 2 + x["ddec_arcmin"] ** 2 for x in res])))
        why = (m.get("fit_rejected") or f"{len(recs)}/{self.cfg.min_solves_for_fit} solves, HA span {span:.0f}"
               f"/{self.cfg.min_ha_span_deg:.0f} deg")
        return {**m, "kind": "constant", "dha": dh, "ddec": dd, "rms_arcmin": rms, "residuals": res,
                "why_constant": why}

    def predict(self, ha, dec):
        """Raw offset (dH, dD) at (ha, dec) in degrees, or None without a model."""
        m = self.model()
        if m["kind"] == "fit":
            return predict(m["terms"], ha, dec)
        if m["kind"] == "constant":
            return m["dha"], m["ddec"]
        return None

    def correction(self, ha, dec):
        """What to take off a target's (HA, Dec) so the mount, in its current (synced) frame, points at
        it: model(target) - sync shift. Returns (dH, dD, why) with dH/dD None when nothing applies."""
        with self._lock:
            if not self.cfg.enabled:
                return None, None, "disabled"
            if self.shift is None:
                return None, None, f"sync state unknown ({self.shift_note})"
            p = self.predict(ha, dec)
            if p is None:
                return None, None, "no solves yet"
            dh, dd = p[0] - self.shift[0], p[1] - self.shift[1]
            size = sky_arcmin(dh, dd, dec) / 60.0
            if size > self.cfg.max_correction_deg:
                return None, None, f"correction {size:.1f} deg > {self.cfg.max_correction_deg:g} deg: not applied"
            return dh, dd, self.model()["kind"]

    # ---- status -----------------------------------------------------------------------------------
    def summary(self, n_residuals=8):
        m = self.model()
        out = {"enabled": self.cfg.enabled, "kind": m["kind"], "n": m["n"], "ha_span_deg": m.get("ha_span_deg"),
               "shift": None if self.shift is None else {"ha_arcmin": round(self.shift[0] * 60, 1),
                                                          "dec_arcmin": round(self.shift[1] * 60, 1)},
               "shift_note": self.shift_note, "config": self.cfg.view(),
               "last_correction": self.last_correction}
        if m["kind"] == "fit":
            out["terms_arcmin"] = {k: round(v * 60, 1) for k, v in m["terms"].items()}
        elif m["kind"] == "constant":
            out["terms_arcmin"] = {"dHA": round(m["dha"] * 60, 1), "dDec": round(m["ddec"] * 60, 1)}
            out["why_constant"] = m.get("why_constant")
        if m["kind"] != "none":
            out["rms_arcmin"] = round(m["rms_arcmin"], 2)
            out["residuals"] = m["residuals"][-n_residuals:]
        if self.records:
            r = self.records[-1]
            out["last"] = {k: r.get(k) for k in ("time", "target", "off_arcmin", "off_ha", "off_dec")}
        return out


def _iso(t):
    import datetime as dt
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
