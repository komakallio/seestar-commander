"""Dark library: master darks keyed by camera, exposure, gain, offset, binning and a sensor
temperature bin, with expiry. The SeeStar is uncooled, so darks are matched to the light's sensor
temperature (Camera.CCDTemperature) instead of being taken at a set point.

Layout:  <data>/darklib/<camera>/e<exp>_g<gain>_o<offset>_b<bin>_t<tbin>/master.fits + meta.json
A temperature reading of exactly 0 counts as "no reading" (the wide camera returns 0); entries made
without a reading get tbin "na" and only match lights without a reading.

Nothing is deleted: rebuilding an entry moves the old master to
<data>/darklib/<camera>/_history/<entry>/<created stamp>/ (see retire), and an expired master stays. Lights
prefer a fresh master; with use_expired_fallback (the default) a light with no fresh match uses the newest
expired one (flagged: entry["fallback"], DARKEXP = T in the light's header) rather than none.
"""
import datetime as dt
import json
import math
import os
import shutil
from dataclasses import dataclass

import numpy as np
from astropy.io import fits


def read_temp(value):
    """CCDTemperature -> float, or None when missing/0 (no reading)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v == 0.0 or not math.isfinite(v):
        return None
    return v


@dataclass
class DarkPolicy:
    temp_bin_c: float = 2.0          # width of a temperature bin
    temp_tolerance_c: float = 1.0    # a light may be this far outside the entry's bin
    max_age_days: float = 60.0
    expiring_days: float = 7.0       # "expiring soon" this long before max age
    default_count: int = 15
    use_expired_fallback: bool = True  # no fresh match: use the newest expired master (flagged)
    match_tolerance_c: float | None = None  # a light matches a master whose mean temperature is this close
                                            # (None: temp_bin_c / 2 + temp_tolerance_c)


@dataclass(frozen=True)
class Key:
    camera: str
    exposure: float
    gain: int
    offset: int = 0
    binning: int = 1

    def label(self):
        return f"{self.camera} {self.exposure:g}s g{self.gain} o{self.offset} b{self.binning}"


def tbin(temp, width):
    """Centre of temp's bin: bins are [c - width/2, c + width/2), so a reading exactly on an edge always
    goes to the upper bin (half-up; Python's round() rounds halves to even, which split the edges:
    11.0 went up to +12 but 9.0 down to +8)."""
    if temp is None:
        return None
    return round(math.floor(temp / width + 0.5) * width, 2) + 0.0


def _tb_str(tb):
    return "na" if tb is None else f"{tb:+.1f}".replace("+", "p").replace("-", "m")


HISTORY = "_history"


def retire(d):
    """Move an entry directory (master.fits + meta.json) into its camera's history instead of deleting it:
    <camera>/_history/<entry>/<created stamp>/. Every version is kept. Returns the new path or None."""
    if not os.path.isdir(d):
        return None
    created = None
    try:
        with open(os.path.join(d, "meta.json")) as fh:
            created = json.load(fh).get("created_unix")
    except Exception:  # noqa: BLE001
        pass
    stamp = dt.datetime.fromtimestamp(float(created) if created else os.path.getmtime(d),
                                      dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    base = os.path.join(os.path.dirname(d), HISTORY, os.path.basename(d))
    dest, i = os.path.join(base, stamp), 1
    while os.path.exists(dest):
        i += 1
        dest = os.path.join(base, f"{stamp}_{i}")
    os.makedirs(base, exist_ok=True)
    shutil.move(d, dest)
    return dest


def history(cam_dir, entry_name):
    """Earlier versions of an entry, oldest first: [{"dir", **meta}]."""
    base = os.path.join(cam_dir, HISTORY, entry_name)
    out = []
    if os.path.isdir(base):
        for v in sorted(os.listdir(base)):
            try:
                with open(os.path.join(base, v, "meta.json")) as fh:
                    out.append({**json.load(fh), "dir": os.path.join(base, v)})
            except Exception:  # noqa: BLE001
                continue
    return out


class DarkLibrary:
    def __init__(self, root, policy: DarkPolicy | None = None, clock=None):
        import time as _t
        self.root = root
        self.policy = policy or DarkPolicy()
        self.clock = clock or _t
        os.makedirs(root, exist_ok=True)

    # ---- entries -----------------------------------------------------------------------------
    def entry_dir(self, key: Key, tb):
        return os.path.join(self.root, key.camera,
                            f"e{key.exposure:g}_g{key.gain}_o{key.offset}_b{key.binning}_t{_tb_str(tb)}")

    def entry_id(self, key: Key, tb):
        return f"{key.camera}/{os.path.basename(self.entry_dir(key, tb))}"

    def entries(self):
        out = []
        if not os.path.isdir(self.root):
            return out
        for cam in sorted(os.listdir(self.root)):
            cdir = os.path.join(self.root, cam)
            if not os.path.isdir(cdir):
                continue
            for name in sorted(os.listdir(cdir)):
                mp = os.path.join(cdir, name, "meta.json")
                if not os.path.exists(mp):
                    continue
                try:
                    with open(mp) as fh:
                        m = json.load(fh)
                except Exception:  # noqa: BLE001
                    continue
                m["dir"] = os.path.join(cdir, name)
                m.update(self._status(m))
                out.append(m)
        return out

    def _status(self, m):
        age = (self.clock.time() - m["created_unix"]) / 86400.0
        p = self.policy
        st = "expired" if age > p.max_age_days else "expiring" if age > p.max_age_days - p.expiring_days else "fresh"
        return {"age_days": round(age, 2), "status": st}

    def key_of(self, m):
        return Key(m["camera"], float(m["exposure"]), int(m["gain"]), int(m.get("offset", 0)), int(m.get("binning", 1)))

    @staticmethod
    def master_temp(m):
        """The temperature a master was taken at: its frames' mean (temp_mean), else its bin (older masters)."""
        t = m.get("temp_mean")
        return float(t) if isinstance(t, (int, float)) else m.get("temp_bin")

    def match_window(self):
        p = self.policy
        return float(p.match_tolerance_c) if p.match_tolerance_c is not None else p.temp_bin_c / 2 + p.temp_tolerance_c

    def match(self, key: Key, temp, fallback=None):
        """Best usable master for a light: same settings, not expired, the nearest actual temperature (the
        master's temp_mean; its bin centre for masters without one) within match_window() (or both without a
        reading), newest on ties. Bins only organise the harvest. Returns the entry dict or None. With no fresh
        match and fallback (default: policy.use_expired_fallback), the nearest expired master in the same
        window (newest on ties), marked entry["fallback"] = True."""
        temp = read_temp(temp)
        p = self.policy
        fallback = p.use_expired_fallback if fallback is None else fallback
        win = self.match_window()
        best, old = None, None
        for m in self.entries():
            if self.key_of(m) != key:
                continue
            expired = m["status"] == "expired"
            if expired and not fallback:
                continue
            mt = None if m.get("temp_bin") is None else self.master_temp(m)
            if temp is None or mt is None:
                if (temp is None) != (mt is None):
                    continue
                d = 0.0
            else:
                d = abs(temp - mt)
                if d > win + 1e-9:
                    continue
            score = (round(d, 6), -m["created_unix"])
            if expired:
                if old is None or score < old[0]:
                    old = (score, m)
                continue
            if best is None or score < best[0]:
                best = (score, m)
        if best:
            return best[1]
        if old:
            return {**old[1], "fallback": True}
        return None

    def load_master(self, m):
        return fits.getdata(os.path.join(m["dir"], "master.fits")).astype(np.float32)

    def missing(self, keys, temp):
        """Keys (from plans) with no usable master at sensor temperature temp."""
        out, seen = [], set()
        for k in keys:
            if k in seen:
                continue
            seen.add(k)
            if self.match(k, temp, fallback=False) is None:
                tb = tbin(read_temp(temp), self.policy.temp_bin_c)
                out.append({"camera": k.camera, "exposure": k.exposure, "gain": k.gain, "offset": k.offset,
                            "binning": k.binning, "temp_bin": tb, "label": k.label(), "status": "missing"})
        return out

    def add(self, key: Key, frames, temps, identity=None, tb="mean", extra=None, master=None):
        """Store a master (median of frames) for key; returns the entry dict. The bin is the temps' mean's
        unless tb is given (the frame pool bins each frame by its own temperature and passes the pool's
        bin); extra goes into meta.json (pool masters: source, frame files, date span)."""
        temps_r = [t for t in (read_temp(x) for x in temps) if t is not None]
        tmean = float(np.mean(temps_r)) if temps_r else None
        if tb == "mean":
            tb = tbin(tmean, self.policy.temp_bin_c)
        d = self.entry_dir(key, tb)
        retire(d)                     # rebuilding the same bin: the old master goes to _history, never deleted
        os.makedirs(d)
        if master is None:
            master = np.median(np.stack(frames), axis=0)
        master = np.asarray(master, np.float32)
        now = self.clock.time()
        meta = {"camera": key.camera, "exposure": key.exposure, "gain": key.gain, "offset": key.offset,
                "binning": key.binning, "temp_bin": tb, "frames": len(frames),
                "temp_mean": round(tmean, 2) if tmean is not None else None,
                "temp_min": round(min(temps_r), 2) if temps_r else None,
                "temp_max": round(max(temps_r), 2) if temps_r else None,
                "created_unix": now,
                "created": dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "identity": identity or {}, "label": key.label() + (f" {tb:+.0f}C" if tb is not None else " no-temp"),
                "id": f"{key.camera}/{os.path.basename(d)}", **(extra or {})}
        h = fits.Header()
        h["IMAGETYP"] = "MASTER DARK"
        h["EXPTIME"], h["GAIN"], h["OFFSET"], h["XBINNING"] = key.exposure, key.gain, key.offset, key.binning
        h["NCOMBINE"] = len(frames)
        if tmean is not None:
            h["CCD-TEMP"] = round(tmean, 2)
        h["DATE"] = meta["created"]
        fits.writeto(os.path.join(d, "master.fits"), master, h, overwrite=True)
        with open(os.path.join(d, "meta.json"), "w") as fh:
            json.dump(meta, fh, indent=1)
        meta["dir"] = d
        meta.update(self._status(meta))
        return meta

    def summary(self, keys, temp):
        ents = []
        for m in self.entries():
            ents.append({k: m.get(k) for k in ("id", "label", "camera", "exposure", "gain", "offset", "binning",
                                               "temp_bin", "temp_mean", "temp_min", "temp_max", "frames",
                                               "created", "age_days", "status", "source", "date_first",
                                               "date_last")})
            ents[-1]["fallback_ok"] = m["status"] == "expired" and self.policy.use_expired_fallback
            if ents[-1]["fallback_ok"]:
                ents[-1]["note"] = "expired, used as fallback"
        return {"temp": read_temp(temp), "policy": vars(self.policy), "entries": ents,
                "missing": self.missing(keys, temp)}
