"""Flat frames: light sources, auto-exposure, the flat library and the `flats build` job.

A flat master corrects what multiplies the light on its way to the sensor: vignetting and the dust
shadows ("donuts") on the filter and the sensor window. Lights are calibrated as (light - dark) / flat.

Library layout:  <data>/flatlib/<camera>/<filter>_g<gain>_b<bin>_f<focus>/master.fits + meta.json
Masters are stored by camera, filter, gain and binning, plus the focuser position the flats were taken
at: dust shadows move and change size with focus, so a master only matches lights whose focuser
position is within [flats] focus_tolerance steps of it. Matching ignores the gain (a normalised flat is
valid at any gain): a master at the light's gain is preferred, then the newest. They expire after
max_age_days (30). A master is stored normalised per Bayer (GRBG) channel to a median of 1, so it
corrects only the spatial pattern and leaves the colour balance of the lights alone.

Light sources ([flats] source):
  manual  the operator puts the panel on (or in front of) the scope, switches it on and confirms
          (`flats confirm`, POST /api/flats/confirm, the page's button); the job waits for that
  http    generic hooks: POST panel_on_url (with "{brightness}" replaced by the brightness for the
          filter), wait settle_s, ... POST panel_off_url at the end
  sim     the simulator's panel
  sky     twilight sky flats: planned, the interface is SkyFlats below (refused today)

The job never moves the mount: pointing at the panel is up to the operator or the panel's mounting.
Dark-flats: a matching master from the dark library (same exposure, gain, offset, binning and sensor
temperature), otherwise `dark_count` frames are taken right after the flats with the wheel on Dark and
stored in the dark library. The wide camera has no Dark position: its flats use a dark-library master
when one matches, otherwise none (flagged in the meta, about 1 % error from the pedestal).
"""
import datetime as dt
import json
import os
import shutil
import threading
import urllib.parse
from dataclasses import dataclass, field

import numpy as np
from astropy.io import fits

from .darklib import Key as DarkKey, read_temp


class FlatError(RuntimeError):
    """A flat set can't be made (panel too dim / too bright, no confirmation, ...)."""


@dataclass
class FlatConfig:
    """[flats] in config.toml."""
    source: str = "manual"            # manual | http | sim | sky (planned, refused)
    panel_on_url: str = ""            # POSTed to switch the panel on; "{brightness}" is replaced
    panel_off_url: str = ""
    brightness: int | None = None     # default panel brightness (None: the URL has none)
    brightness_filters: dict = field(default_factory=dict)   # per filter, e.g. {"IR": 60, "LP": 200}
    settle_s: float = 2.0             # wait after switching the panel on / changing brightness
    manual_timeout_s: float = 900.0   # manual: give up if nobody confirms within this
    target_frac: float = 0.45         # auto-exposure target: mean of the raw frame / full scale
    tolerance_frac: float = 0.05      # accept target +- this (0.40..0.50 by default)
    full_scale: int = 65535
    start_exposure_s: float = 1.0     # first test exposure (later: the last good one for that filter)
    min_exposure_s: float = 0.01      # shorter is unreliable (shutterless rolling readout); dim the panel
    max_exposure_s: float = 30.0
    max_iterations: int = 8
    count: int = 15                   # flats per master
    dark_count: int = 0               # dark-flats when none in the dark library (0 = same as count)
    max_age_days: float = 30.0
    expiring_days: float = 5.0
    focus_tolerance: int = 100        # focuser steps: a master matches lights within this of its position
    filters: list = field(default_factory=lambda: ["IR", "LP"])   # `flats build` with no filter/plan
    gain: int = 200                   # gain for `flats build` without a plan (200: HCG on the S30 Pro)
    wide: bool = False                # also flats for the wide camera (filter "none")

    @classmethod
    def from_dict(cls, d):
        d = dict(d or {})
        c = cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
        c.source = str(c.source).lower()
        c.count, c.dark_count, c.gain = int(c.count), int(c.dark_count), int(c.gain)
        c.focus_tolerance, c.max_iterations = int(c.focus_tolerance), int(c.max_iterations)
        c.brightness_filters = {str(k): int(v) for k, v in dict(c.brightness_filters).items()}
        return c

    @property
    def target(self):
        return self.target_frac * self.full_scale

    @property
    def tolerance(self):
        return self.tolerance_frac * self.full_scale

    def brightness_for(self, filt):
        return self.brightness_filters.get(filt, self.brightness)


@dataclass(frozen=True)
class FlatKey:
    camera: str            # "tele" or "wide"
    filter: str            # "IR" / "LP" (telephoto), "none" (wide)
    gain: int
    binning: int = 1

    def label(self):
        return f"{self.camera} {self.filter} g{self.gain} b{self.binning}"


# ---- pure helpers ------------------------------------------------------------------------------------

def level(frame):
    """Mean of the raw Bayer frame (ADU): what the auto-exposure aims at."""
    return float(np.mean(frame, dtype=np.float64))


def next_exposure(exp, lvl, cfg: FlatConfig):
    """One auto-exposure step. Exposure is scaled by target / level, which converges on the target even
    with a bias pedestal (the error shrinks by pedestal / target per step). A clipped frame (level near
    full scale) says nothing about the scale, so it is cut tenfold."""
    if lvl >= 0.97 * cfg.full_scale:
        return exp / 10.0
    ratio = cfg.target / max(lvl, 1.0)
    return exp * min(10.0, max(0.1, ratio))


def autoexpose(take, cfg: FlatConfig, start=None, log=print, check=None):
    """Find the exposure that puts the raw mean within target +- tolerance. take(exp) -> frame.
    Returns (exposure, level, frame, tries). Raises FlatError when the light is too dim at the longest or
    too bright at the shortest exposure, or it doesn't converge."""
    exp = min(cfg.max_exposure_s, max(cfg.min_exposure_s, float(start or cfg.start_exposure_s)))
    tries = []
    for _ in range(cfg.max_iterations):
        if check:
            check()
        frame = take(exp)
        lvl = level(frame)
        tries.append((round(exp, 4), round(lvl, 1)))
        log(f"auto-exposure {len(tries)}: {exp:.3f} s -> mean {lvl:.0f} ({100 * lvl / cfg.full_scale:.1f} %)")
        if abs(lvl - cfg.target) <= cfg.tolerance:
            return exp, lvl, frame, tries
        nxt = next_exposure(exp, lvl, cfg)
        if nxt > cfg.max_exposure_s:
            if exp >= cfg.max_exposure_s:
                raise FlatError(f"too dim: mean {100 * lvl / cfg.full_scale:.0f} % at the longest exposure "
                                f"{cfg.max_exposure_s:g} s (brighter panel, or raise max_exposure_s)")
            nxt = cfg.max_exposure_s
        if nxt < cfg.min_exposure_s:
            if exp <= cfg.min_exposure_s:
                raise FlatError(f"too bright: mean {100 * lvl / cfg.full_scale:.0f} % at the shortest exposure "
                                f"{cfg.min_exposure_s:g} s (dim the panel)")
            nxt = cfg.min_exposure_s
        exp = round(nxt, 4) if nxt >= 0.01 else nxt
    raise FlatError(f"auto-exposure did not converge in {cfg.max_iterations} tries: {tries}")


def normalise(master):
    """Divide each GRBG channel by its own median; non-positive or tiny values become 1 (no correction)."""
    out = np.asarray(master, np.float32).copy()
    for dy in (0, 1):
        for dx in (0, 1):
            c = out[dy::2, dx::2]
            med = float(np.median(c))
            out[dy::2, dx::2] = c / med if med > 0 else 1.0
    bad = ~np.isfinite(out) | (out < 0.05)
    out[bad] = 1.0
    return out


def make_master(frames, dark=None):
    """Median of (frame - dark-flat), normalised per Bayer channel."""
    st = np.stack([np.asarray(f, np.float32) for f in frames])
    if dark is not None:
        st = st - np.asarray(dark, np.float32)
    return normalise(np.median(st, axis=0))


# ---- library -----------------------------------------------------------------------------------------

def _focus_str(f):
    return "na" if f is None else str(int(f))


class FlatLibrary:
    def __init__(self, root, cfg: FlatConfig | None = None, clock=None):
        import time as _t
        self.root = root
        self.cfg = cfg or FlatConfig()
        self.clock = clock or _t
        os.makedirs(root, exist_ok=True)

    def entry_dir(self, key: FlatKey, focus):
        return os.path.join(self.root, key.camera, f"{key.filter}_g{key.gain}_b{key.binning}_f{_focus_str(focus)}")

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
        c = self.cfg
        st = "expired" if age > c.max_age_days else "expiring" if age > c.max_age_days - c.expiring_days else "fresh"
        return {"age_days": round(age, 2), "status": st}

    @staticmethod
    def key_of(m):
        return FlatKey(m["camera"], m["filter"], int(m["gain"]), int(m.get("binning", 1)))

    def match(self, key: FlatKey, focus):
        """Best usable master: same camera, filter and binning, not expired, focuser position within
        focus_tolerance (or both without a reading). Gain doesn't matter: a calibrated, normalised flat
        records the optics and the pixel response, which gain doesn't change. A master at the same gain is
        preferred, then the newest. Returns the entry dict or None."""
        best = None
        for m in self.entries():
            k = self.key_of(m)
            if (k.camera, k.filter, k.binning) != (key.camera, key.filter, key.binning) or m["status"] == "expired":
                continue
            mf = m.get("focus")
            if (focus is None) != (mf is None):
                continue
            if focus is not None and abs(int(focus) - int(mf)) > self.cfg.focus_tolerance:
                continue
            rank = (k.gain == key.gain, m["created_unix"])
            if best is None or rank > best[0]:
                best = (rank, m)
        return best[1] if best else None

    def load_master(self, m):
        return fits.getdata(os.path.join(m["dir"], "master.fits")).astype(np.float32)

    def missing(self, keys, focus):
        """Keys with no usable master at the current focuser positions; focus: {camera: position}. Gain
        doesn't count (see match): a filter needed at two gains is listed once, at the first one's gain."""
        out, seen = [], set()
        for k in keys:
            if (k.camera, k.filter, k.binning) in seen:
                continue
            seen.add((k.camera, k.filter, k.binning))
            f = focus.get(k.camera)
            if self.match(k, f) is None:
                out.append({"camera": k.camera, "filter": k.filter, "gain": k.gain, "binning": k.binning,
                            "focus": f, "label": k.label(), "status": "missing"})
        return out

    def add(self, key: FlatKey, master, focus, info=None):
        """Store a normalised master for key at focuser position focus; returns the entry dict. A master
        at exactly the same position replaces the old one."""
        d = self.entry_dir(key, focus)
        if os.path.isdir(d):
            shutil.rmtree(d)
        os.makedirs(d)
        now = self.clock.time()
        info = dict(info or {})
        meta = {"camera": key.camera, "filter": key.filter, "gain": key.gain, "binning": key.binning,
                "focus": None if focus is None else int(focus), "created_unix": now,
                "created": dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "label": key.label() + (f" @{int(focus)}" if focus is not None else ""),
                "id": f"{key.camera}/{os.path.basename(d)}", **info}
        h = fits.Header()
        h["IMAGETYP"] = "MASTER FLAT"
        h["FILTER"], h["GAIN"], h["XBINNING"] = key.filter, key.gain, key.binning
        h["CAMERA"] = key.camera
        if focus is not None:
            h["FOCUSPOS"] = int(focus)
        for k, card in (("frames", "NCOMBINE"), ("exposure", "EXPTIME"), ("level_frac", "FLATLVL")):
            if info.get(k) is not None:
                h[card] = info[k]
        h["DARKFLAT"] = str(info.get("darkflat") or "NONE")
        h["BAYERPAT"] = "GRBG"
        h["DATE"] = meta["created"]
        fits.writeto(os.path.join(d, "master.fits"), np.asarray(master, np.float32), h, overwrite=True)
        with open(os.path.join(d, "meta.json"), "w") as fh:
            json.dump(meta, fh, indent=1)
        meta["dir"] = d
        meta.update(self._status(meta))
        return meta

    def summary(self, keys, focus):
        ents = [{k: m.get(k) for k in ("id", "label", "camera", "filter", "gain", "binning", "focus", "exposure",
                                       "level_frac", "frames", "darkflat", "source", "created", "age_days",
                                       "status")} for m in self.entries()]
        c = self.cfg
        return {"focus": focus, "policy": {"max_age_days": c.max_age_days, "expiring_days": c.expiring_days,
                                           "focus_tolerance": c.focus_tolerance, "target_frac": c.target_frac,
                                           "tolerance_frac": c.tolerance_frac, "source": c.source},
                "entries": ents, "missing": self.missing(keys, focus)}


# ---- light sources -----------------------------------------------------------------------------------

class ManualPanel:
    """The operator switches the panel on and confirms; the job waits for that."""
    kind = "manual"

    def on(self, job, filt):
        job.wait_confirm("put the flat panel on the scope, switch it on and confirm")

    def filter_changed(self, job, filt):
        pass

    def off(self, job):
        job.log("flats: done with the panel, it can be switched off")


class HttpPanel:
    """Generic hooks: POST panel_on_url ("{brightness}" replaced) and panel_off_url."""
    kind = "http"

    def __init__(self, cfg: FlatConfig, post=None):
        self.cfg = cfg
        self.post = post or _post
        self._b = "unset"

    def _on(self, job, filt):
        b = self.cfg.brightness_for(filt)
        url = self.cfg.panel_on_url.replace("{brightness}", urllib.parse.quote(str(b if b is not None else "")))
        r = self.post(url)
        if isinstance(r, dict) and r.get("error"):
            raise FlatError(f"panel refused: {r.get('message')}")
        job.log(f"flats: panel on (brightness {b}) -> {r}")
        self._b = b
        job.sleep(self.cfg.settle_s)

    def on(self, job, filt):
        if not self.cfg.panel_on_url:
            raise FlatError("[flats] source = \"http\" needs panel_on_url")
        self._on(job, filt)

    def filter_changed(self, job, filt):
        if self.cfg.brightness_for(filt) != self._b:
            self._on(job, filt)

    def off(self, job):
        if self.cfg.panel_off_url:
            try:
                job.log(f"flats: panel off -> {self.post(self.cfg.panel_off_url)}")
            except Exception as e:  # noqa: BLE001
                job.log(f"flats: panel off failed: {e}")
        self._b = "unset"


class SimPanelSource:
    """The simulator's panel (sequencer/sim.py SimPanel)."""
    kind = "sim"

    def __init__(self, cfg: FlatConfig, panel):
        self.cfg, self.panel = cfg, panel

    def on(self, job, filt):
        self.filter_changed(job, filt, force=True)

    def filter_changed(self, job, filt, force=False):
        b = self.cfg.brightness_for(filt)
        b = 255 if b is None else int(b)
        if force or not self.panel.on or self.panel.brightness != b:
            self.panel.set(True, b)
            job.log(f"flats: sim panel on (brightness {b})")
            job.sleep(self.cfg.settle_s)

    def off(self, job):
        self.panel.set(False)
        job.log("flats: sim panel off")


class SkyFlats:
    """Planned: twilight sky flats. The interface the job will use:
      - window(t): (start, end) of the evening/morning window when the sky is in the exposure range
        (Sun between about -2 and -10 deg), and where to point (the anti-solar "flat spot", ~20 deg from
        the zenith away from the Sun);
      - on(job, filt): slew there (cable-safe move_hadec), tracking off so stars trail, wait for the window;
      - per frame: re-run the auto-exposure servo (the sky brightens or fades by ~x2 in a few minutes)
        and nudge the mount between frames so stars are median-rejected;
      - off(job): nothing (a park comes from the job's caller).
    Not implemented: `flats build` refuses source = "sky"."""
    kind = "sky"

    def on(self, job, filt):
        raise FlatError("sky flats are not implemented yet (planned); use source = \"manual\" or \"http\"")

    def filter_changed(self, job, filt):
        pass

    def off(self, job):
        pass


def _post(url):
    from .rig import _http
    return _http(url, "POST")


def make_source(cfg: FlatConfig, rig, post=None):
    if cfg.source == "manual":
        return ManualPanel()
    if cfg.source == "http":
        return HttpPanel(cfg, post)
    if cfg.source == "sim":
        if rig.sim is None:
            raise FlatError("[flats] source = \"sim\" needs the simulator")
        return SimPanelSource(cfg, rig.sim.panel)
    if cfg.source == "sky":
        return SkyFlats()
    raise FlatError(f"unknown [flats] source {cfg.source!r} (manual, http, sim; sky is planned)")


# ---- the service side --------------------------------------------------------------------------------

class FlatRunner:
    """Flat library + the `flats build` job + calibration lookups, for one Service."""

    def __init__(self, svc):
        self.svc = svc
        d = dict(svc.s.extra.get("flats") or {})
        if "source" not in d and svc.rig.sim is not None:
            d["source"] = "sim"
        self.cfg = FlatConfig.from_dict(d)
        self.lib = FlatLibrary(os.path.join(svc.s.data_dir, "flatlib"), self.cfg, svc.clock)
        self.post = None                       # tests: replace the HTTP POST of the http source
        self._confirm = threading.Event()
        self._masters = {}
        self._last_exp = {}                    # (camera, filter, gain) -> last good exposure
        self.focus = {"tele": None, "wide": None}
        self.state = {"state": "idle", "waiting": None, "current": None, "last": None}
        self._summary, self._gen = None, 0          # cached status summary, library generation

    def log(self, msg):
        self.svc.log(msg)

    def sleep(self, s):
        if s > 0:
            self.svc.clock.wait(self.svc.abort, s)
            self.svc._check_abort()

    # ---- focus (read by the monitor, so status never touches the SeeStar) ----
    def poll_focus(self):
        self.focus["tele"] = self.svc.imager.focus_pos()
        w = self.svc.rig.wide
        if w is not None and self.focus["wide"] is None:     # the wide focuser is never moved
            self.focus["wide"] = w.focus_pos()

    # ---- keys and lookups ----
    def _plans(self, plans=None):
        if plans is not None:
            return list(plans)
        from . import plan as planmod
        pls = [self.svc.plan] if self.svc.plan else []
        for path in self.svc.s.dark_plans:
            try:
                pls.append(planmod.load(path))
            except Exception:  # noqa: BLE001 - dark_keys already logs unreadable plans
                pass
        return pls

    def keys(self, plans=None):
        """Flat keys the active and planned plans' lights need (and the wide camera's, if [flats] wide)."""
        b = self.svc.imager.binning()
        out = []
        for pl in self._plans(plans):
            for st in pl.steps:
                if getattr(st, "kind", "light") != "light":
                    continue
                k = FlatKey("tele", st.filter, int(st.gain), b)
                if k not in out:
                    out.append(k)
                if self.cfg.wide and self.svc.wide.enabled(st):
                    _, _, wg = self.svc.wide.settings(st)
                    k = FlatKey("wide", "none", int(wg), self.svc.rig.wide.binning())
                    if k not in out:
                        out.append(k)
        return out

    def master_for(self, filt, gain, focus, camera="tele", binning=None):
        """(entry, array) of the best usable master flat, or (None, None)."""
        b = self.svc.imager.binning() if binning is None else int(binning)
        m = self.lib.match(FlatKey(camera, filt, int(gain), b), None if focus is None else int(focus))
        if m is None:
            return None, None
        if m["id"] not in self._masters or self._masters[m["id"]][0] != m["created_unix"]:
            self._masters[m["id"]] = (m["created_unix"], self.lib.load_master(m))
        return m, self._masters[m["id"]][1]

    def summary(self, max_age_s=2.0):
        """Library summary for the status; cached for max_age_s (real time) unless the inputs changed,
        because status() runs on every monitor tick."""
        import time as _t
        sig = (id(self.svc.plan), tuple(sorted(self.focus.items(), key=lambda kv: kv[0])), self._gen)
        c = self._summary
        if c is not None and c[0] == sig and _t.monotonic() - c[1] < max_age_s:
            return c[2]
        try:
            out = self.lib.summary(self.keys(), dict(self.focus))
        except Exception as e:  # noqa: BLE001
            out = {"error": str(e)}
        self._summary = (sig, _t.monotonic(), out)
        return out

    def view(self):
        return dict(self.state)

    # ---- commands ----
    def cmd_build(self, filters=None, gain=None, count=None, plan=None, missing=False, wide=None):
        """Build masters: the given filters (at gain), or everything missing for a plan (or the active and
        planned plans when missing=True) at the current focuser position; wide=True adds the wide camera."""
        svc = self.svc
        if svc.busy in ("run", "polar") or not svc.jobs.empty():
            return {"ok": False, "msg": "busy: stop the plan first"}
        if svc._locked_msg():
            return {"ok": False, "msg": svc._locked_msg()}
        if self.cfg.source == "sky":
            return {"ok": False, "msg": "sky flats are not implemented yet (planned); set [flats] source"}
        try:
            make_source(self.cfg, svc.rig)
        except FlatError as e:
            return {"ok": False, "msg": str(e)}
        n = int(count or self.cfg.count)
        b = svc.imager.binning()
        if filters:
            names = list(svc.imager.fw.Names)
            bad = [f for f in filters if f not in names or f == "Dark"]
            if bad:
                return {"ok": False, "msg": f"no flats for filter(s) {bad} (wheel: {names})"}
            g = int(gain if gain is not None else self.cfg.gain)
            keys = [FlatKey("tele", f, g, b) for f in filters]
        elif plan is not None or missing:
            self.poll_focus()
            ks = self.keys([plan] if plan is not None else None)
            keys = [FlatKey(m["camera"], m["filter"], m["gain"], m["binning"])
                    for m in self.lib.missing(ks, dict(self.focus))]
        elif wide:
            keys = []
        else:
            g = int(gain if gain is not None else self.cfg.gain)
            keys = [FlatKey("tele", f, g, b) for f in self.cfg.filters]
        if wide and svc.rig.wide is not None and not any(k.camera == "wide" for k in keys):
            from .wide import WideConfig
            wg = int(gain if gain is not None and not filters else WideConfig.from_dict(svc.s.extra.get("wide")).gain)
            keys.append(FlatKey("wide", "none", wg, svc.rig.wide.binning()))
        elif wide and svc.rig.wide is None:
            return {"ok": False, "msg": "no wide camera"}
        if not keys:
            return {"ok": True, "msg": "nothing missing", "items": []}
        items = [(k, n) for k in keys]
        self._confirm.clear()
        svc.jobs.put(("flats_build", {"items": items}))
        labels = [k.label() for k in keys]
        self.log(f"flats build queued ({self.cfg.source}): {labels} x {n}")
        msg = f"building {len(items)} master flat(s)"
        if self.cfg.source == "manual":
            msg += "; switch the panel on and confirm (`flats confirm`)"
        return {"ok": True, "msg": msg, "items": labels}

    def cmd_confirm(self):
        if self.svc.busy != "flats_build" and self.svc.jobs.empty():
            return {"ok": False, "msg": "no flats build waiting"}
        self._confirm.set()
        self.log("operator: flat panel confirmed")
        return {"ok": True, "msg": "confirmed"}

    def cmd_sim_panel(self, body):
        sim = self.svc.rig.sim
        if sim is None:
            return {"ok": False, "msg": "not a simulator"}
        sim.panel.set(bool(body.get("on", True)), body.get("brightness"))
        self.log(f"SIM: flat panel {'on' if sim.panel.on else 'off'} (brightness {sim.panel.brightness})")
        return {"ok": True}

    def http_post(self, path, body):
        """(code, body) for POST /api/flats/<cmd> and /api/sim/panel."""
        if path == "/api/sim/panel":
            r = self.cmd_sim_panel(body)
            return (200 if r["ok"] else 404), r
        cmd = path.rsplit("/", 1)[-1]
        if cmd == "build":
            pl = None
            if body.get("plan_path"):
                from . import plan as planmod
                pl = planmod.load(body["plan_path"])
            f = body.get("filters") or body.get("filter")
            if isinstance(f, str):
                f = [f]
            return 200, self.cmd_build(f, body.get("gain"), body.get("count"), pl, bool(body.get("missing")),
                                       bool(body.get("wide")))
        if cmd == "confirm":
            return 200, self.cmd_confirm()
        return 404, {"ok": False, "msg": "unknown flats command"}

    # ---- the job ----
    def wait_confirm(self, msg):
        svc = self.svc
        self.state.update(state="waiting", waiting=msg)
        svc._act("flat_wait", label=msg)
        self.log(f"flats: WAITING: {msg} (`flats confirm` or the page's button)")
        t0 = svc.clock.time()
        while not self._confirm.is_set():
            svc._check_abort()
            if svc.clock.time() - t0 > self.cfg.manual_timeout_s:
                raise FlatError(f"nobody confirmed the panel within {self.cfg.manual_timeout_s:g} s")
            svc.clock.wait(svc.abort, 0.5)
        self.state.update(state="running", waiting=None)
        svc._act(None)

    def _imager(self, key):
        return self.svc.imager if key.camera == "tele" else self.svc.rig.wide

    def _take(self, key, n, label):
        """Auto-expose, then n flats. Returns a shot dict, or raises FlatError."""
        svc, im = self.svc, self._imager(key)
        k3 = (key.camera, key.filter, key.gain)

        tries_n = [0]

        def expose(exp, i=None, what="auto-exposure"):
            if i is None:
                tries_n[0] += 1
            svc._act("flat", label=label, frame=tries_n[0] if i is None else i + 1,
                     count=self.cfg.max_iterations if i is None else n,
                     start=round(svc.clock.time(), 2), exposure=float(exp), what=what)
            return im.expose(exp, key.gain, svc.abort)

        self.state["current"] = {"key": key.label(), "phase": "auto-exposure"}
        exp, lvl, first, tries = autoexpose(lambda e: expose(e), self.cfg, self._last_exp.get(k3),
                                            lambda m: self.log(f"flats {key.label()}: {m}"), svc._check_abort)
        self._last_exp[k3] = exp
        self.state["current"] = {"key": key.label(), "phase": "flats", "exposure": exp}
        frames, temps, dropped = [], [], 0
        for i in range(n):
            svc._check_abort()
            temps.append(im.temperature())
            d = expose(exp, i, "flat")
            lv = level(d)
            if abs(lv - self.cfg.target) > 3 * self.cfg.tolerance:
                dropped += 1
                self.log(f"flat {i + 1}/{n} {key.label()}: mean {lv:.0f} far off the target, dropped")
                if dropped > n // 2:
                    raise FlatError(f"{dropped} of {i + 1} flats far off the target: is the light steady?")
                continue
            frames.append(d)
            self.log(f"flat {i + 1}/{n} {key.label()}: {exp:.3f} s mean {lv:.0f} ({100 * lv / self.cfg.full_scale:.1f} %)")
        temps.append(im.temperature())
        return {"key": key, "frames": frames, "exp": exp, "level": float(np.mean([level(f) for f in frames])),
                "focus": im.focus_pos(), "temps": temps, "tries": tries, "dropped": dropped}

    def _dark_flat(self, shot):
        """(id, array) of the dark-flat for a shot: dark library, else taken now (telephoto, wheel on Dark)."""
        svc, key = self.svc, shot["key"]
        im = self._imager(key)
        dk = DarkKey(key.camera, float(shot["exp"]), key.gain, im.offset(), key.binning)
        temps = [t for t in (read_temp(x) for x in shot["temps"]) if t is not None]
        m = svc.darklib.match(dk, float(np.mean(temps)) if temps else None)
        if m is not None:
            self.log(f"flats {key.label()}: dark-flat from the dark library ({m['id']})")
            return m["id"], svc.darklib.load_master(m)
        if key.camera != "tele":
            self.log(f"flats {key.label()}: no dark-flat in the dark library and no Dark filter: master not "
                     f"dark-subtracted (about 1 % error from the bias pedestal)")
            return None, None
        n = self.cfg.dark_count or len(shot["frames"]) or self.cfg.count
        im.set_filter("Dark")
        frames, temps = [], []
        for i in range(n):
            svc._check_abort()
            temps.append(im.temperature())
            svc._act("dark", label=f"{dk.label()} (dark-flat)", frame=i + 1, count=n,
                     start=round(svc.clock.time(), 2), exposure=float(dk.exposure))
            frames.append(im.expose(dk.exposure, dk.gain, svc.abort, light=False))
            temps.append(im.temperature())
        m = svc.darklib.add(dk, frames, temps, im.identity())
        self.log(f"flats {key.label()}: {n} dark-flats -> dark library {m['id']}")
        return m["id"], svc.darklib.load_master(m)

    def run(self, items):
        """Worker job: light on, per key auto-exposure + N flats, light off, dark-flats, masters, re-stack."""
        svc = self.svc
        im = svc.imager
        prev = im.filter_name()
        svc._set(phase="flats", state="flats")
        self.state.update(state="running", waiting=None, current=None)
        built, failed, shots = [], [], []
        src = None
        try:
            src = make_source(self.cfg, svc.rig, self.post)
            first = next((k.filter for k, _ in items if k.camera == "tele"), None)
            if first:
                im.set_filter(first)
            src.on(self, first)
            for key, n in items:
                try:
                    if key.camera == "tele":
                        im.set_filter(key.filter)
                        self.log(f"flats: filter wheel on {key.filter}")
                        src.filter_changed(self, key.filter)
                    shots.append(self._take(key, n, key.label()))
                except FlatError as e:
                    failed.append(key.label())
                    self.log(f"flats {key.label()} FAILED: {e}")
            src.off(self)
            src = None
            for sh in shots:
                if not sh["frames"]:
                    continue
                did, dark = self._dark_flat(sh)
                key = sh["key"]
                master = make_master(sh["frames"], dark)
                m = self.lib.add(key, master, sh["focus"], {
                    "frames": len(sh["frames"]), "exposure": round(sh["exp"], 4),
                    "level_frac": round(sh["level"] / self.cfg.full_scale, 3), "darkflat": did,
                    "source": self.cfg.source, "dropped": sh["dropped"], "autoexposure": sh["tries"],
                    "temp_mean": round(float(np.mean([t for t in sh["temps"] if t is not None])), 2)
                    if any(t is not None for t in sh["temps"]) else None,
                    "identity": self._imager(key).identity()})
                built.append(m["id"])
                self._gen += 1
                self.log(f"master flat {m['id']}: {m['frames']} x {m['exposure']:g} s at "
                         f"{100 * m['level_frac']:.0f} %, focus {m['focus']}, dark-flat {did or 'NONE'}")
        except (FlatError, Exception) as e:  # noqa: BLE001 - Aborted included
            from .rig import Aborted
            if isinstance(e, Aborted):
                self.log("flats build stopped")
                svc._set(state="stopped")
            elif isinstance(e, FlatError):
                self.log(f"flats build FAILED: {e}")
                svc._set(state="stopped")
            else:
                raise
        finally:
            if src is not None:
                try:
                    src.off(self)
                except Exception as e:  # noqa: BLE001
                    self.log(f"flats: light off: {e}")
            try:
                if prev in im.fw.Names and prev != "Dark" and im.filter_name() != prev:
                    im.set_filter(prev)
                    self.log(f"filter wheel back on {prev}")
            finally:
                svc._act(None)
                svc._set(phase=None)
                self.state.update(state="idle", waiting=None, current=None,
                                  last={"built": built, "failed": failed, "t": round(svc.clock.time(), 1)})
        if svc.st["state"] == "flats":
            svc._set(state="idle")
        if built:
            svc._restack()
