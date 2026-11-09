"""Dark frame pool: every archived dark frame is binned by its own sensor temperature, and a master is
built into the dark library once a (camera, exposure, gain, offset, binning, temperature bin) pool holds
enough eligible frames. The SeeStar is uncooled and warms up while it exposes, so a series of darks can
cross a bin boundary: binning by the series mean would put frames at the wrong temperature into a master.

The pool is a view over the dark archive, nothing is copied:
  <data>/darkarchive/<camera>/<YYYYMMDD>/*.fits     the frames (written by the service, see _archive_dark)
  <data>/darkarchive/<camera>/index.jsonl           one record per frame (temps start/end/mean, times, settings)
  <data>/darkarchive/<camera>/used.jsonl            {"file", "master", "unix"}: frames already in a master

A frame's temperature is the mean of the readings before and after the exposure (`temp_c`). A frame is
eligible for its pool when
  - it is steady: the reading changed by at most `max_temp_change_c` (0.5 C) during the exposure
    (fast warm-up after idle: the dark current isn't steady). Unsteady frames stay in the archive,
    flagged (`steady: false` in the index), and never go into a master;
  - it is younger than `max_age_days` (14 d), so a master never mixes stale frames;
  - it hasn't been used in a master yet.
Frames stay archived after a master is built from them (raw material for the dark-current analysis).

Config: [darks.harvest] pool_max_age_days = 14, max_temp_change_c = 0.5 (shared with the harvester).
"""
import datetime as dt
import json
import os
import threading

import numpy as np
from astropy.io import fits

from .darklib import Key, read_temp, tbin


def _iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def median_stack(frames, rows=256):
    """Median of equally shaped 2-D frames, in row chunks (15 full frames are 250 MB as uint16; the chunked
    median keeps the extra memory small)."""
    frames = [np.asarray(f) for f in frames]
    out = np.empty(frames[0].shape, np.float32)
    for r in range(0, out.shape[0], rows):
        out[r:r + rows] = np.median(np.stack([f[r:r + rows] for f in frames]), axis=0)
    return out


class DarkPool:
    def __init__(self, root, darklib, clock, log=print, max_age_days=14.0, max_temp_change_c=0.5):
        self.root = root
        self.lib = darklib
        self.clock = clock
        self.log = log
        self.max_age_days = float(max_age_days)
        self.max_temp_change_c = float(max_temp_change_c)
        self._lock = threading.RLock()
        self.frames = {}          # (camera, file) -> index record
        self.used = {}            # (camera, file) -> master id
        self.load()

    # ---- index --------------------------------------------------------------------------------
    def load(self):
        """(Re)read every camera's index and used list. Idempotent: records are keyed by file, a frame
        listed twice counts once, records whose FITS file is gone are skipped."""
        frames, used = {}, {}
        if os.path.isdir(self.root):
            for cam in sorted(os.listdir(self.root)):
                cdir = os.path.join(self.root, cam)
                if not os.path.isdir(cdir):
                    continue
                for rec in _read_jsonl(os.path.join(cdir, "index.jsonl")):
                    if not isinstance(rec, dict) or not rec.get("file"):
                        continue
                    if not os.path.exists(os.path.join(cdir, rec["file"])):
                        continue
                    rec.setdefault("camera", cam)
                    frames[(cam, rec["file"])] = rec
                for u in _read_jsonl(os.path.join(cdir, "used.jsonl")):
                    if isinstance(u, dict) and u.get("file"):
                        used[(cam, u["file"])] = u.get("master")
        with self._lock:
            self.frames, self.used = frames, used
        return len(frames)

    def register(self, rec):
        """A frame the service has just archived (its index line is already written)."""
        with self._lock:
            self.frames[(rec["camera"], rec["file"])] = rec
        return self.info(rec)

    # ---- per frame ---------------------------------------------------------------------------
    def temp_change(self, rec):
        a, b = read_temp(rec.get("temp_start")), read_temp(rec.get("temp_end"))
        return None if a is None or b is None else round(b - a, 3)

    def steady(self, rec):
        d = self.temp_change(rec)
        return d is None or abs(d) <= self.max_temp_change_c + 1e-9

    def frame_temp(self, rec):
        t = read_temp(rec.get("temp_c"))
        if t is None:
            ts = [x for x in (read_temp(rec.get("temp_start")), read_temp(rec.get("temp_end"))) if x is not None]
            t = sum(ts) / len(ts) if ts else None
        return t

    def frame_tbin(self, rec):
        return tbin(self.frame_temp(rec), self.lib.policy.temp_bin_c)

    @staticmethod
    def key_of(rec):
        return Key(rec["camera"], float(rec["exposure"]), int(rec["gain"]), int(rec.get("offset", 0)),
                   int(rec.get("binning", 1)))

    def age_days(self, rec, now=None):
        now = self.clock.time() if now is None else now
        return (now - float(rec.get("start_unix", 0))) / 86400.0

    def why_not(self, rec, now=None):
        """None when the frame is eligible for its pool, else the reason."""
        if (rec["camera"], rec["file"]) in self.used:
            return "used"
        if not self.steady(rec):
            return "unsteady"
        if self.age_days(rec, now) > self.max_age_days:
            return "too old"
        return None

    def info(self, rec):
        key, tb = self.key_of(rec), self.frame_tbin(rec)
        return {"key": key, "temp_bin": tb, "steady": self.steady(rec), "temp_change": self.temp_change(rec),
                "pool_n": len(self.eligible(key, tb))}

    # ---- pools -------------------------------------------------------------------------------
    def eligible(self, key, tb, now=None):
        """Eligible frames of one pool, oldest first."""
        now = self.clock.time() if now is None else now
        with self._lock:
            recs = [r for r in self.frames.values()
                    if self.key_of(r) == key and self.frame_tbin(r) == tb and self.why_not(r, now) is None]
        return sorted(recs, key=lambda r: r.get("start_unix", 0))

    def count(self, key, tb, now=None):
        return len(self.eligible(key, tb, now))

    def pools(self, now=None):
        """Every non-empty pool: key, bin, n, oldest/newest frame and when the oldest drops out."""
        now = self.clock.time() if now is None else now
        groups = {}
        with self._lock:
            recs = list(self.frames.values())
        for r in recs:
            if self.why_not(r, now) is not None:
                continue
            groups.setdefault((self.key_of(r), self.frame_tbin(r)), []).append(r)
        out = []
        for (k, tb), rs in sorted(groups.items(), key=lambda kv: (kv[0][0].camera, kv[0][0].exposure,
                                                                    kv[0][0].gain, kv[0][1] is None,
                                                                    kv[0][1] or 0)):
            t0, t1 = min(r["start_unix"] for r in rs), max(r["start_unix"] for r in rs)
            temps = [t for t in (self.frame_temp(r) for r in rs) if t is not None]
            out.append({"camera": k.camera, "exposure": k.exposure, "gain": k.gain, "offset": k.offset,
                        "binning": k.binning, "temp_bin": tb, "label": k.label(), "n": len(rs),
                        "oldest": _iso(t0), "newest": _iso(t1),
                        "oldest_age_days": round((now - t0) / 86400.0, 2),
                        "drops_in_days": round(self.max_age_days - (now - t0) / 86400.0, 2),
                        "temp_min": round(min(temps), 2) if temps else None,
                        "temp_max": round(max(temps), 2) if temps else None})
        return out

    def stats(self, now=None):
        now = self.clock.time() if now is None else now
        with self._lock:
            recs = list(self.frames.values())
        n = {"frames": len(recs), "used": 0, "unsteady": 0, "too_old": 0, "eligible": 0}
        for r in recs:
            w = self.why_not(r, now)
            n[{"used": "used", "unsteady": "unsteady", "too old": "too_old", None: "eligible"}[w]] += 1
        return n

    def summary(self, now=None):
        return {"max_age_days": self.max_age_days, "max_temp_change_c": self.max_temp_change_c,
                "pools": self.pools(now), "frames": self.stats(now)}

    # ---- masters -----------------------------------------------------------------------------
    def build(self, key, tb, min_count, identity=None):
        """Build the pool's master into the library when it holds at least min_count eligible frames;
        every eligible frame goes in. Marks them used. Returns the library entry or None."""
        recs = self.eligible(key, tb)
        if len(recs) < max(1, int(min_count)):
            return None
        cdir = os.path.join(self.root, key.camera)
        frames = [fits.getdata(os.path.join(cdir, r["file"])) for r in recs]
        temps = [self.frame_temp(r) for r in recs]
        ident = identity or recs[-1].get("identity") or {}
        m = self.lib.add(key, frames, temps, ident, tb=tb, extra=self.master_meta(recs), master=median_stack(frames))
        now = self.clock.time()
        with self._lock, open(os.path.join(cdir, "used.jsonl"), "a") as fh:
            for r in recs:
                self.used[(key.camera, r["file"])] = m["id"]
                fh.write(json.dumps({"file": r["file"], "master": m["id"], "unix": round(now, 3)}) + "\n")
        return m

    def master_meta(self, recs):
        t0 = min(r["start_unix"] for r in recs)
        t1 = max(r.get("end_unix", r["start_unix"]) for r in recs)
        days = sorted({r["file"].split("/")[0] for r in recs})
        return {"source": "pool", "frame_files": [r["file"] for r in recs], "date_first": _iso(t0),
                "date_last": _iso(t1), "days": days,
                "sources": sorted({r.get("source", "?") for r in recs})}


def _read_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out
