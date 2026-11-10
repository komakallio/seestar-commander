"""The device set the service owns: mount (tools/scope.Scope), telephoto camera, filter wheel,
focuser, dew heater, optional roof, safety source and plate solver. Built either from config.toml
(real Alpaca devices) or from the simulator."""
import math
import os
import shutil
import subprocess
import sys
import tempfile
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
    """A SeeStar camera with its focuser (and filter wheel, telephoto only), with abortable exposures.
    The telephoto and the wide camera share one Alpaca bridge: `bus` (a lock shared by both imagers)
    keeps their ImageArray downloads from overlapping."""

    def __init__(self, camera, wheel, focuser, clock, log=print, timeout_extra=120.0, bus=None):
        self.c, self.fw, self.f, self.clock, self.log = camera, wheel, focuser, clock, log
        self.timeout_extra = timeout_extra
        self.bus = bus
        self.bus_waits = 0                 # downloads that had to wait for the other camera's
        self._busy = threading.Event()
        for d in (camera, wheel, focuser):
            if d is not None:
                d.Connected = True

    def start(self, exp, gain, light=True):
        """Start an exposure and return at once (the wide camera runs alongside the telephoto)."""
        self.c.Gain = int(gain)
        self.last_start = self.clock.time()
        self.c.StartExposure(float(exp), bool(light))

    def ready(self):
        return bool(self.c.ImageReady)

    def download(self):
        """ImageArray as [y][x] uint16, holding the shared download lock."""
        if self.bus is None:
            return np.array(self.c.ImageArray).T.astype(np.uint16)
        if not self.bus.acquire(blocking=False):
            self.bus_waits += 1
            self.bus.acquire()
        try:
            return np.array(self.c.ImageArray).T.astype(np.uint16)
        finally:
            self.bus.release()

    def expose(self, exp, gain, abort: threading.Event = None, light=True, on_ready=None, on_started=None):
        c = self.c
        self.start(exp, gain, light)       # last_start: exposure start (polar alignment needs the mid time)
        self._busy.set()
        t0 = self.clock.time()
        try:
            if on_started is not None:
                on_started()
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
            if on_ready is not None:
                on_ready()          # ImageArray is the slow part (~16 MB over Alpaca)
            return self.download()
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

class AstapSolver:
    """Telephoto plate solve with the ASTAP command-line solver (separate executable, MPL-2.0) and a
    star database such as D05. The raw frame goes to a temporary FITS, `astap_cli` solves it (its own
    star detection, 2x2 downsample) and writes `<frame>.ini`, which is parsed into a WCS. See
    docs/astap.md for installing the binary and the database."""
    name = "astap"

    def __init__(self, astap_path, db_dir, pix_arcsec, database="d05", radius_deg=6.0, downsample=2,
                 timeout_s=30.0, log=print):
        self.pix_arcsec, self.database, self.radius = pix_arcsec, database, float(radius_deg)
        self.downsample, self.timeout, self.log = int(downsample), float(timeout_s), log
        self.db_dir = os.path.expanduser(db_dir) if db_dir else ""
        self.error = None
        p = os.path.expanduser(astap_path or "astap_cli")
        self.path = p if os.path.sep in p else shutil.which(p)
        if not self.path or not (os.path.isfile(self.path) and os.access(self.path, os.X_OK)):
            self.available = False
            self.error = (f"ASTAP CLI not found ({astap_path!r}; install it and set [solver] astap_path, "
                          f"see docs/astap.md)")
        elif self.db_dir and not os.path.isdir(self.db_dir):
            self.available = False
            self.error = f"ASTAP database dir {self.db_dir!r} not found (set [solver] db_dir, see docs/astap.md)"
        else:
            self.available = True

    def command(self, fits_path, height_px, ra_hint=None, dec_hint=None):
        """The astap_cli command line. -fov is the image height in degrees, -ra in hours, -spd = Dec + 90."""
        cmd = [self.path, "-f", fits_path, "-fov", f"{height_px * self.pix_arcsec / 3600.0:.3f}",
               "-z", str(self.downsample)]
        if ra_hint is not None and dec_hint is not None:
            cmd += ["-ra", f"{(ra_hint % 360.0) / 15.0:.6f}", "-spd", f"{dec_hint + 90.0:.6f}",
                    "-r", f"{self.radius:g}"]
        else:
            cmd += ["-r", "180"]                      # blind
        if self.db_dir:
            cmd += ["-d", self.db_dir]
        if self.database:
            cmd += ["-D", self.database]
        return cmd

    def solve(self, image, ra_hint=None, dec_hint=None, star_list=None):
        """image: raw frame (rows x columns). star_list is ignored (ASTAP detects its own stars).
        Returns the ICRS position of the frame centre, the scale and the local orientation, or None."""
        if not self.available:
            return None
        from astropy.io import fits
        h, w = image.shape[:2]
        with tempfile.TemporaryDirectory(prefix="astap-") as tmp:
            fp = os.path.join(tmp, "frame.fits")
            fits.PrimaryHDU(np.asarray(image, dtype=np.uint16)).writeto(fp)
            try:
                r = subprocess.run(self.command(fp, h, ra_hint, dec_hint), capture_output=True, text=True,
                                   timeout=self.timeout)
            except subprocess.TimeoutExpired:
                self.log(f"astap: no answer within {self.timeout:g} s")
                return None
            except OSError as e:
                self.log(f"astap: {e}")
                return None
            ini = os.path.join(tmp, "frame.ini")
            if not os.path.exists(ini):
                self.log(f"astap: exit {r.returncode}, no .ini: {(r.stdout + r.stderr).strip()[-200:]}")
                return None
            with open(ini, errors="replace") as fh:
                res = parse_astap_ini(fh.read())
        if res.get("PLTSOLVD", "").strip().upper() != "T":
            why = res.get("ERROR") or res.get("WARNING") or "no solution"
            self.log(f"astap: not solved ({why})")
            return None
        return wcs_solution(res, w, h)


def parse_astap_ini(text):
    """ASTAP's `<frame>.ini`: KEY=value lines (PLTSOLVD=T, CRPIX1, CRVAL1, CD1_1, ..., ERROR, WARNING)."""
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip().upper()] = v.strip()
    return out


def wcs_solution(cards, width, height):
    """Solution dict from TAN WCS cards (CRPIX/CRVAL/CD, FITS 1-based pixels) for a width x height frame.
    ra/dec is the sky position of the frame centre pixel, evaluated from the WCS rather than taken
    from CRVAL, so it holds whatever reference pixel the solver chose."""
    from astropy.wcs import WCS
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crpix = [float(cards["CRPIX1"]), float(cards["CRPIX2"])]
    w.wcs.crval = [float(cards["CRVAL1"]), float(cards["CRVAL2"])]
    w.wcs.cd = [[float(cards["CD1_1"]), float(cards["CD1_2"])], [float(cards["CD2_1"]), float(cards["CD2_2"])]]
    # 0-based pixel centres: the geometric centre of the frame is ((width - 1) / 2, (height - 1) / 2)
    x0, y0 = (width - 1) / 2.0, (height - 1) / 2.0
    ra, dec = (float(v) for v in w.wcs_pix2world([[x0, y0]], 0)[0])
    cd = np.asarray(w.wcs.cd, float)
    scale = math.sqrt(abs(np.linalg.det(cd))) * 3600.0
    return {"ra": ra % 360.0, "dec": dec, "scale": scale, "cd": _local_cd(w, x0, y0, ra, dec)}


def _local_cd(wcs, x, y, ra0, dec0, step=10.0):
    """2x2 matrix: full-resolution pixel offset at (x, y) -> tangent plane at (ra0, dec0) (xi east,
    eta north) in degrees, from a WCS in full-resolution pixel coordinates."""
    from .polar import tangent
    cols = []
    for dx, dy in ((step, 0.0), (0.0, step)):
        ra, dec = wcs.wcs_pix2world([[x + dx, y + dy]], 0)[0]
        xi, eta = tangent(float(ra), float(dec), ra0, dec0)
        cols.append((float(xi) / step, float(eta) / step))
    return [[cols[0][0], cols[1][0]], [cols[0][1], cols[1][1]]]


class NoSolver:
    name = "none"
    available = False

    def __init__(self, error="no solver configured"):
        self.error = error

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
    lock_unsafe_grace_s: float = 10.0    # on unsafe with the scope locked: revoke, wait this long, park
    sun_limit_deg: float = -8.0          # night limit for every plan: park when the Sun rises above this
    latest_end: str = ""                 # optional night limit "HH:MM" (local, [site] timezone)
    timezone: str = "UTC"
    heartbeat_url: str = ""              # optional: pinged while a plan runs (see sequencer/heartbeat.py)
    heartbeat_interval_s: float = 60.0
    heartbeat_method: str = "POST"       # POST (JSON payload) or GET (URL only, healthchecks.io style)
    heartbeat_while: str = "plan"        # plan | job (any job) | always
    heartbeat_id: str = "seestar-commander"
    dark_policy: DarkPolicy = field(default_factory=DarkPolicy)
    dark_plans: list = field(default_factory=list)   # planned plan files whose darks count as "missing"
    dark_archive: bool = True        # darks build keeps every frame + metadata in data/darkarchive/
    extra: dict = field(default_factory=dict)

    @property
    def night_limit(self):
        from .plan import NightLimit
        return NightLimit(float(self.sun_limit_deg), self.latest_end or None, self.timezone or "UTC")

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
    wide: object = None            # Imager for the wide camera (camera 1, focuser 1, no wheel), or None
    wide_solver: object = None     # wide-field solver (sequencer.wide), or None


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
        lock_unsafe_grace_s=float(g("sequencer", "lock_unsafe_grace_s", 10.0)),
        sun_limit_deg=float(g("sequencer", "sun_limit_deg", -8.0)), latest_end=str(g("sequencer", "latest_end", "")),
        timezone=str(g("site", "timezone", "UTC")),
        heartbeat_url=str(g("hooks", "heartbeat_url", "")),
        heartbeat_interval_s=float(g("hooks", "heartbeat_interval_s", 60.0)),
        heartbeat_method=str(g("hooks", "heartbeat_method", "POST")).upper(),
        heartbeat_while=str(g("hooks", "heartbeat_while", "plan")),
        heartbeat_id=str(g("hooks", "heartbeat_id", "seestar-commander")),
        dark_policy=DarkPolicy(temp_bin_c=float(g("darks", "temp_bin_c", 2.0)),
                               temp_tolerance_c=float(g("darks", "temp_tolerance_c", 1.0)),
                               max_age_days=float(g("darks", "max_age_days", 60)),
                               expiring_days=float(g("darks", "expiring_days", 7)),
                               default_count=int(g("darks", "count", 15)),
                               use_expired_fallback=bool(g("darks", "use_expired_fallback", True)),
                               match_tolerance_c=(None if g("darks", "match_tolerance_c", None) is None
                                                  else float(g("darks", "match_tolerance_c", None)))),
        dark_plans=[p if os.path.isabs(p) else os.path.join(C.REPO, p) for p in g("darks", "plans", [])],
        dark_archive=bool(g("darks", "archive", True)),
        extra={"polar": dict(C.CFG.get("polar", {})), "pointing": dict(C.CFG.get("pointing", {})),
               "dither": dict(C.CFG.get("dither", {})), "flats": dict(C.CFG.get("flats", {})),
               "templog": dict(C.CFG.get("templog", {})),
               "darks_harvest": dict(C.CFG.get("darks", {}).get("harvest", {})),
               "wide": {"projections_dir": C.PROJECTIONS_DIR, **dict(C.CFG.get("wide", {}))}},
    )


def _hook(url):
    if not url:
        return None
    try:
        return _http(url, "POST")
    except Exception as e:  # noqa: BLE001
        return f"hook failed: {e}"


def make_solver(cfg, pix_arcsec, log=print):
    """The telephoto solver from the [solver] config table."""
    kind = cfg.get("kind", "astap")
    if kind == "astap":
        return AstapSolver(cfg.get("astap_path", "astap_cli"), cfg.get("db_dir", ""), pix_arcsec,
                           database=cfg.get("database", "d05"), radius_deg=cfg.get("radius_deg", 6.0),
                           downsample=cfg.get("downsample", 2), timeout_s=cfg.get("timeout_s", 30.0), log=log)
    if kind == "none":
        return NoSolver()
    return NoSolver(f"unknown [solver] kind {kind!r} (use \"astap\" or \"none\"; astrometry was removed)")


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
    bus = threading.Lock()
    imager = Imager(Camera(host, int(g("telephoto", "camera", 0))),
                    FilterWheel(host, int(g("telephoto", "filterwheel", 0))),
                    Focuser(host, int(g("telephoto", "focuser", 0))), clock, log, bus=bus)
    from .wide import WideConfig, make_wide_solver
    wcfg = WideConfig.from_dict(s.extra.get("wide"))
    wide = None
    try:     # camera 1 and its own focuser 1 (never moved: the factory focus is at stars)
        wide = Imager(Camera(host, wcfg.camera), None, Focuser(host, wcfg.focuser), clock, log, bus=bus)
    except Exception as e:  # noqa: BLE001 - imaging works without it
        log(f"wide camera: {e}")
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
    solver = make_solver(dict(C.CFG.get("solver", {})), s.pix_arcsec, log)
    return Rig(s, clock, scope, imager, sw, roof, safety.safety_state, solver, _hook,
               wide=wide, wide_solver=make_wide_solver(wcfg, log) if wide is not None else None)


def build_sim(settings: Settings, clock, roof=True, solver=True, log=print, wide=True, wide_solver=True,
              **world_kw):
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
    bus = threading.Lock()
    imager = Imager(w.camera, w.wheel, w.focuser, clock, log, bus=bus)
    wimager = Imager(w.wide_camera, None, w.wide_focuser, clock, log, bus=bus) if wide else None
    settings.pix_arcsec = w.camera.pix_arcsec
    hooks = []

    def hook(url):
        hooks.append(url)
        return {"sim": True, "url": url}
    rig = Rig(settings, clock, scope, imager, w.switch, AlpacaRoof(w.dome, clock) if roof else None,
              w.safety.state, w.solver if solver else NoSolver(), hook, w, wide=wimager,
              wide_solver=w.wide_solver if wide_solver else None)
    rig.hooks_posted = hooks
    return rig
