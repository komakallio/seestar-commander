"""The device set the service owns: mount (tools/scope.Scope), telephoto camera, filter wheel,
focuser, dew heater, optional roof, safety source and plate solver. Built either from config.toml
(real Alpaca devices) or from the simulator."""
import os
import sys
import threading
import urllib.error
import urllib.request
import json
from dataclasses import dataclass, field

import numpy as np

from . import astro
from .clock import RealClock
from .darklib import DarkPolicy

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)


class Aborted(RuntimeError):
    """An exposure or step was stopped on request."""


class Imager:
    """Telephoto camera + filter wheel + focuser, with abortable exposures."""

    def __init__(self, camera, wheel, focuser, clock, log=print, timeout_extra=120.0):
        self.c, self.fw, self.f, self.clock, self.log = camera, wheel, focuser, clock, log
        self.timeout_extra = timeout_extra
        self._busy = threading.Event()
        for d in (camera, wheel, focuser):
            if d is not None:
                d.Connected = True

    def expose(self, exp, gain, abort: threading.Event = None, light=True):
        c = self.c
        c.Gain = int(gain)
        self.last_start = self.clock.time()       # exposure start (polar alignment needs the mid time)
        c.StartExposure(float(exp), bool(light))
        self._busy.set()
        t0 = self.clock.time()
        try:
            while not c.ImageReady:
                if abort is not None and abort.is_set():
                    self.abort()
                    raise Aborted("exposure aborted")
                if self.clock.time() - t0 > exp + self.timeout_extra:
                    self.abort()
                    raise RuntimeError("exposure timeout")
                if abort is not None:
                    self.clock.wait(abort, 0.5)
                else:
                    self.clock.sleep(0.5)
            return np.array(c.ImageArray).T.astype(np.uint16)
        finally:
            self._busy.clear()

    def abort(self):
        try:
            self.c.AbortExposure()
        except Exception as e:  # noqa: BLE001
            self.log(f"AbortExposure: {e}")

    @property
    def exposing(self):
        return self._busy.is_set()

    def filter_name(self):
        try:
            p = self.fw.Position
            return self.fw.Names[p] if p >= 0 else "moving"
        except Exception:  # noqa: BLE001
            return "?"

    def set_filter(self, name, timeout=30.0):
        names = list(self.fw.Names)
        if name not in names:
            raise ValueError(f"filter {name!r} not in wheel {names}")
        i = names.index(name)
        if self.fw.Position == i:
            return
        self.fw.Position = i
        t0 = self.clock.time()
        while self.fw.Position != i:
            if self.clock.time() - t0 > timeout:
                raise RuntimeError("filter wheel timeout")
            self.clock.sleep(0.5)

    def temperature(self):
        """Sensor temperature, or None (missing, or 0 = no reading)."""
        from .darklib import read_temp
        try:
            return read_temp(self.c.CCDTemperature)
        except Exception:  # noqa: BLE001
            return None

    def offset(self):
        try:
            return int(self.c.Offset)
        except Exception:  # noqa: BLE001 - not every camera has Offset
            return 0

    def binning(self):
        try:
            return int(self.c.BinX)
        except Exception:  # noqa: BLE001
            return 1

    def identity(self):
        out = {}
        for k in ("Name", "SensorName", "DriverVersion", "DriverInfo"):
            try:
                out[k] = str(getattr(self.c, k))
            except Exception:  # noqa: BLE001
                pass
        return out

    def focus_pos(self):
        try:
            return int(self.f.Position) if self.f is not None else None
        except Exception:  # noqa: BLE001
            return None


# ---- roof ----------------------------------------------------------------------------------------

class RoofRefused(RuntimeError):
    """The roof controller refused a command (the reason is in the message)."""


def _poll_roof(roof, want, timeout, clock, stop=None, poll_s=5.0):
    """Poll roof.state() until it reads one of `want`, the timeout passes, it reports an error, or stop()."""
    want = (want,) if isinstance(want, str) else tuple(want)
    t0 = clock.time()
    while True:
        st = roof.state()
        if st in want or st.startswith("error") or clock.time() - t0 >= timeout or (stop and stop()):
            return st
        clock.sleep(poll_s)


class AlpacaRoof:
    """ASCOM Dome shutter: 0 open, 1 closed, 2 opening, 3 closing, 4 error."""
    kind = "alpaca"

    def __init__(self, dome, clock):
        self.d, self.clock = dome, clock
        self.d.Connected = True

    def state(self):
        try:
            return {0: "open", 1: "closed", 2: "opening", 3: "closing"}.get(int(self.d.ShutterStatus), "error")
        except Exception as e:  # noqa: BLE001
            return f"error: {e}"

    def _command(self, name):
        try:
            getattr(self.d, name)()
        except Exception as e:  # noqa: BLE001 - an Alpaca error is the driver refusing
            raise RoofRefused(f"roof refused {name}: {e}") from e

    def open(self, timeout=300.0, log=print, stop=None):
        self._command("OpenShutter")
        log("roof: OpenShutter sent")
        return _poll_roof(self, "open", timeout, self.clock, stop)

    def close(self, timeout=300.0, log=print, stop=None):
        self._command("CloseShutter")
        log("roof: CloseShutter sent")
        return _poll_roof(self, "closed", timeout, self.clock, stop)


def _http(url, method="GET", timeout=10):
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
    except urllib.error.HTTPError as e:   # error bodies are data, e.g. {"message": ..., "error": true}
        body = e.read().decode()
    try:
        return json.loads(body)
    except ValueError:
        return body


class HttpRoof:
    """POST open_url / close_url, GET state_url JSON ("open": bool) for the state. An answer like
    {"message": REASON, "error": true} is a refusal and its reason is surfaced."""
    kind = "http"

    def __init__(self, close_url, state_url, clock, open_url=""):
        self.open_url, self.close_url, self.state_url, self.clock = open_url, close_url, state_url, clock

    def state(self):
        if not self.state_url:
            return "unknown"
        try:
            d = _http(self.state_url)
        except Exception as e:  # noqa: BLE001
            return f"error: {e}"
        if isinstance(d, dict):
            if d.get("error"):
                return f"error: {d.get('message')}"
            if "open" in d:
                return "open" if d["open"] else "closed"
        return "unknown"

    def _post(self, url, what, log):
        if not url:
            raise RuntimeError(f"no [roof] {what}_url configured")
        try:
            r = _http(url, "POST")
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"roof {what}: {e}") from e
        log(f"roof: {what} posted -> {r}")
        if isinstance(r, dict) and r.get("error"):
            raise RoofRefused(f"roof refused {what}: {r.get('message')}")

    def open(self, timeout=300.0, log=print, stop=None):
        self._post(self.open_url, "open", log)
        if not self.state_url:
            return "unknown"           # can't be verified; the service won't treat this as open
        return _poll_roof(self, "open", timeout, self.clock, stop)

    def close(self, timeout=300.0, log=print, stop=None):
        self._post(self.close_url, "close", log)
        return _poll_roof(self, ("closed", "unknown"), timeout, self.clock, stop)


# ---- solver --------------------------------------------------------------------------------------

class AstrometrySolver:
    """Telephoto plate solve with the `astrometry` package (astrometry.net 4100 index series).
    Index files (~170 MB for scales 10-12) download into cache_dir on first use."""
    name = "astrometry"

    def __init__(self, cache_dir, pix_arcsec, scales=(10, 11, 12), log=print):
        self.cache_dir, self.pix_arcsec, self.scales, self.log = cache_dir, pix_arcsec, set(scales), log
        self._solver = None
        self.error = None
        try:
            import astrometry  # noqa: F401  (availability check)
            self.available = True
        except ImportError:
            self.available = False
            self.error = "astrometry package not installed (pip install astrometry)"

    def _get(self):
        if self._solver is None:
            import astrometry
            os.makedirs(self.cache_dir, exist_ok=True)
            self._solver = astrometry.Solver(
                astrometry.series_4100.index_files(cache_directory=self.cache_dir, scales=self.scales))
        return self._solver

    def solve(self, image, ra_hint=None, dec_hint=None, star_list=None):
        """image: raw frame. star_list: [(y, x, flux, ...)] in green superpixels. Returns ICRS dict."""
        if not self.available:
            return None
        import astrometry
        from . import imaging
        try:
            solver = self._get()
        except Exception as e:  # noqa: BLE001
            self.available, self.error = False, f"index files: {e}"
            self.log(f"solver unavailable: {self.error}")
            return None
        st = star_list if star_list is not None else imaging.stars(imaging.green(image), n=120)
        if len(st) < 8:
            return None
        xy = [[float(s[1]), float(s[0])] for s in st]
        sp = 2 * self.pix_arcsec          # superpixels
        pos = astrometry.PositionHint(ra_deg=ra_hint, dec_deg=dec_hint, radius_deg=6.0) \
            if ra_hint is not None else None
        sol = solver.solve(stars=xy, size_hint=astrometry.SizeHint(sp * 0.85, sp * 1.15),
                           position_hint=pos, solution_parameters=astrometry.SolutionParameters())
        if not sol.has_match():
            return None
        m = sol.best_match()
        h, w = image.shape[0] // 2, image.shape[1] // 2
        try:
            wcs = m.astropy_wcs()
            c = wcs.pixel_to_world(w / 2, h / 2)
            out = {"ra": float(c.ra.deg), "dec": float(c.dec.deg), "scale": m.scale_arcsec_per_pixel}
            try:
                out["cd"] = _local_cd(wcs, w / 2, h / 2, float(c.ra.deg), float(c.dec.deg))
            except Exception:  # noqa: BLE001 - orientation is optional (polar alignment's reticle needs it)
                pass
            return out
        except Exception:  # noqa: BLE001
            return {"ra": m.center_ra_deg, "dec": m.center_dec_deg, "scale": m.scale_arcsec_per_pixel}


def _local_cd(wcs, x, y, ra0, dec0, step=10.0):
    """2x2 matrix: full-resolution pixel offset at the frame centre -> tangent plane (xi east, eta
    north) in degrees, from a WCS in green-superpixel coordinates."""
    from .polar import tangent
    cols = []
    for dx, dy in ((step, 0.0), (0.0, step)):
        c = wcs.pixel_to_world(x + dx, y + dy)
        xi, eta = tangent(float(c.ra.deg), float(c.dec.deg), ra0, dec0)
        cols.append((float(xi) / (2 * step), float(eta) / (2 * step)))     # superpixel = 2 px
    return [[cols[0][0], cols[1][0]], [cols[0][1], cols[1][1]]]


class NoSolver:
    name = "none"
    available = False
    error = "no solver configured"

    def solve(self, *a, **k):
        return None


# ---- the rig -------------------------------------------------------------------------------------

@dataclass
class Settings:
    """Everything the service needs from config.toml (kept explicit so tests can build one)."""
    lat: float
    lon: float
    home_ha: float = -90.0
    ccw_limit: float = 90.0
    cw_limit: float = 270.0
    limit_margin: float = 5.0
    data_dir: str = "data"
    pix_arcsec: float = 3.74          # telephoto, full-resolution pixel
    focal_mm: float = 160.0
    pixel_um: float = 2.9
    instrument: str = "SeeStar S30 Pro tele"
    safety_poll_s: float = 30.0
    monitor_s: float = 2.0
    frame_overhead_s: float = 15.0    # download + save per frame, for the cable/constraint look-ahead
    cable_reserve_min: float = 2.0    # stop this long before the tracking limit
    park_guard_deg: float = 10.0
    hooks_parked: str = ""
    hooks_unparked: str = ""
    ipcam: str = ""
    http_host: str = "127.0.0.1"
    http_port: int = 8640
    api_token: str = ""
    dew_heater: bool = True
    auto_close_on_unsafe: bool = False   # close the roof after a verified unsafe park, no operator
    roof_timeout_s: float = 300.0        # open/close must report done within this
    dark_policy: DarkPolicy = field(default_factory=DarkPolicy)
    dark_plans: list = field(default_factory=list)   # planned plan files whose darks count as "missing"
    extra: dict = field(default_factory=dict)

    @property
    def window(self):
        return (self.home_ha, self.home_ha - self.ccw_limit + self.limit_margin,
                self.home_ha + self.cw_limit - self.limit_margin)


@dataclass
class Rig:
    settings: Settings
    clock: object
    scope: object
    imager: Imager
    switch: object = None
    roof: object = None
    safety: object = None          # callable -> (safe, reasons)
    solver: object = None
    hook: object = None            # callable(url) -> response; POSTs hooks
    sim: object = None             # SimWorld when simulated


def settings_from_config():
    import config as C
    g = C.get
    return Settings(
        lat=C.LAT, lon=C.LON, home_ha=C.HOME_HA, ccw_limit=C.CCW_LIMIT, cw_limit=C.CW_LIMIT,
        limit_margin=C.LIMIT_MARGIN, data_dir=C.DATA_DIR,
        pix_arcsec=float(g("telephoto", "pixel_arcsec", 3.74)),
        focal_mm=float(g("telephoto", "focal_mm", 160.0)), pixel_um=float(g("telephoto", "pixel_um", 2.9)),
        safety_poll_s=C.SAFETY_POLL_S, monitor_s=float(g("sequencer", "monitor_s", 2.0)),
        frame_overhead_s=float(g("sequencer", "frame_overhead_s", 15.0)),
        cable_reserve_min=float(g("sequencer", "cable_reserve_min", 2.0)),
        park_guard_deg=float(g("sequencer", "park_guard_deg", 10.0)),
        hooks_parked=C.HOOK_PARKED, hooks_unparked=C.HOOK_UNPARKED, ipcam=C.IPCAM_RTSP,
        http_host=g("sequencer", "host", "127.0.0.1"), http_port=int(g("sequencer", "port", 8640)),
        api_token=g("sequencer", "api_token", ""), dew_heater=bool(g("sequencer", "dew_heater", True)),
        auto_close_on_unsafe=bool(g("sequencer", "auto_close_on_unsafe", False)),
        roof_timeout_s=float(g("roof", "timeout_s", 300.0)),
        dark_policy=DarkPolicy(temp_bin_c=float(g("darks", "temp_bin_c", 2.0)),
                               temp_tolerance_c=float(g("darks", "temp_tolerance_c", 1.0)),
                               max_age_days=float(g("darks", "max_age_days", 60)),
                               expiring_days=float(g("darks", "expiring_days", 7)),
                               default_count=int(g("darks", "count", 15))),
        dark_plans=[p if os.path.isabs(p) else os.path.join(C.REPO, p) for p in g("darks", "plans", [])],
        extra={"polar": dict(C.CFG.get("polar", {}))},
    )


def _hook(url):
    if not url:
        return None
    try:
        return _http(url, "POST")
    except Exception as e:  # noqa: BLE001
        return f"hook failed: {e}"


def build_real(settings=None, log=print):
    """Real Alpaca devices from config.toml."""
    import config as C
    import safety
    from scope import Scope
    from alpaca.camera import Camera
    from alpaca.filterwheel import FilterWheel
    from alpaca.focuser import Focuser
    from alpaca.switch import Switch
    s = settings or settings_from_config()
    clock = RealClock()
    host = C.SEESTAR
    g = C.get
    scope = Scope(host, log=log, clock=clock, window=s.window,
                  unwrap_path=os.path.join(s.data_dir, "ra_unwrap.json"))
    imager = Imager(Camera(host, int(g("telephoto", "camera", 0))),
                    FilterWheel(host, int(g("telephoto", "filterwheel", 0))),
                    Focuser(host, int(g("telephoto", "focuser", 0))), clock, log)
    sw = None
    try:
        sw = Switch(host, 0)
        sw.Connected = True
    except Exception as e:  # noqa: BLE001
        log(f"switch: {e}")
    roof = None
    if C.ROOF_ALPACA:
        from alpaca.dome import Dome
        roof = AlpacaRoof(Dome(C.ROOF_ALPACA, C.ROOF_DOME), clock)
    elif C.ROOF_OPEN_URL or C.ROOF_CLOSE_URL or C.ROOF_STATE_URL:
        roof = HttpRoof(C.ROOF_CLOSE_URL, C.ROOF_STATE_URL, clock, open_url=C.ROOF_OPEN_URL)
    kind = g("solver", "kind", "astrometry")
    solver = AstrometrySolver(os.path.join(s.data_dir, g("solver", "cache_dir", "astrometry_cache")),
                              s.pix_arcsec, tuple(g("solver", "scales", [10, 11, 12])), log) \
        if kind == "astrometry" else NoSolver()
    return Rig(s, clock, scope, imager, sw, roof, safety.safety_state, solver, _hook)


def build_sim(settings: Settings, clock, roof=True, solver=True, log=print, **world_kw):
    """Simulated devices. The mount still moves through tools/scope.Scope."""
    from scope import Scope
    from .sim import SimWorld
    w = SimWorld(clock, settings.lat, settings.lon, settings.home_ha, settings.ccw_limit, settings.cw_limit,
                 roof=roof, **world_kw)
    os.makedirs(settings.data_dir, exist_ok=True)
    up = os.path.join(settings.data_dir, "ra_unwrap.json")
    with open(up, "w") as fh:
        json.dump({"ha_unwrapped": settings.home_ha, "note": "sim start at home"}, fh)
    scope = Scope(log=log, telescope=w.telescope, camera=None, focuser=None, clock=clock,
                  lst=lambda: astro.lst_deg(clock.time(), settings.lon), unwrap_path=up, window=settings.window)
    imager = Imager(w.camera, w.wheel, w.focuser, clock, log)
    settings.pix_arcsec = w.camera.pix_arcsec
    hooks = []

    def hook(url):
        hooks.append(url)
        return {"sim": True, "url": url}
    rig = Rig(settings, clock, scope, imager, w.switch, AlpacaRoof(w.dome, clock) if roof else None,
              w.safety.state, w.solver if solver else NoSolver(), hook, w)
    rig.hooks_posted = hooks
    return rig
