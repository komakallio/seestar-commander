"""Dark library: master darks keyed by camera, exposure, gain, offset, binning and a sensor
temperature bin, with expiry. The SeeStar is uncooled, so darks are matched to the light's sensor
temperature (Camera.CCDTemperature) instead of being taken at a set point.

Layout:  <data>/darklib/<camera>/e<exp>_g<gain>_o<offset>_b<bin>_t<tbin>/master.fits + meta.json
A temperature reading of exactly 0 counts as "no reading" (the wide camera returns 0); entries made
without a reading get tbin "na" and only match lights without a reading.
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
    if temp is None:
        return None
    return round(round(temp / width) * width, 2)


def _tb_str(tb):
    return "na" if tb is None else f"{tb:+.1f}".replace("+", "p").replace("-", "m")


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

    def match(self, key: Key, temp):
        """Best usable master for a light: same settings, not expired, nearest temperature within
        bin/2 + tolerance (or both without a reading), newest on ties. Returns the entry dict or None."""
        temp = read_temp(temp)
        p = self.policy
        best = None
        for m in self.entries():
            if self.key_of(m) != key or m["status"] == "expired":
                continue
            if temp is None or m.get("temp_bin") is None:
                if (temp is None) != (m.get("temp_bin") is None):
                    continue
                d = 0.0
            else:
                d = abs(temp - m["temp_bin"])
                if d > p.temp_bin_c / 2 + p.temp_tolerance_c:
                    continue
            score = (d, -m["created_unix"])
            if best is None or score < best[0]:
                best = (score, m)
        return best[1] if best else None

    def load_master(self, m):
        return fits.getdata(os.path.join(m["dir"], "master.fits")).astype(np.float32)

    def missing(self, keys, temp):
        """Keys (from plans) with no usable master at sensor temperature temp."""
        out, seen = [], set()
        for k in keys:
            if k in seen:
                continue
            seen.add(k)
            if self.match(k, temp) is None:
                tb = tbin(read_temp(temp), self.policy.temp_bin_c)
                out.append({"camera": k.camera, "exposure": k.exposure, "gain": k.gain, "offset": k.offset,
                            "binning": k.binning, "temp_bin": tb, "label": k.label(), "status": "missing"})
        return out

    def add(self, key: Key, frames, temps, identity=None):
        """Store a master (median of frames) for key; returns the entry dict."""
        temps_r = [t for t in (read_temp(x) for x in temps) if t is not None]
        tmean = float(np.mean(temps_r)) if temps_r else None
        tb = tbin(tmean, self.policy.temp_bin_c)
        d = self.entry_dir(key, tb)
        if os.path.isdir(d):
            shutil.rmtree(d)          # rebuilding the same bin replaces it
        os.makedirs(d)
        master = np.median(np.stack(frames), axis=0).astype(np.float32)
        now = self.clock.time()
        meta = {"camera": key.camera, "exposure": key.exposure, "gain": key.gain, "offset": key.offset,
                "binning": key.binning, "temp_bin": tb, "frames": len(frames),
                "temp_mean": round(tmean, 2) if tmean is not None else None,
                "temp_min": round(min(temps_r), 2) if temps_r else None,
                "temp_max": round(max(temps_r), 2) if temps_r else None,
                "created_unix": now,
                "created": dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "identity": identity or {}, "label": key.label() + (f" {tb:+.0f}C" if tb is not None else " no-temp"),
                "id": f"{key.camera}/{os.path.basename(d)}"}
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
                                               "created", "age_days", "status")})
        return {"temp": read_temp(temp), "policy": vars(self.policy), "entries": ents,
                "missing": self.missing(keys, temp)}
