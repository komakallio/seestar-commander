"""Wide camera alongside the telephoto: one wide frame every N telephoto lights, exposed while the
telephoto exposes and the mount tracks, downloaded while a later telephoto exposure runs, saved with the
same headers, and plate-solved in the background with the wide-field (projections) solver.

Timing (see docs/design.md, "Wide camera alongside the telephoto"):
  - the worker calls `tele_started()` right after the telephoto's StartExposure. If a wide frame is due
    and the whole wide exposure fits into the time until the mount next moves (the telephoto lights up to
    the next dither, re-centre or the end of the target), the wide exposure starts at once; otherwise
    it waits for a later light where it fits. It never runs across a dither pulse, a slew or a park.
  - the wide thread waits for the exposure to end and for ImageReady, then downloads only while a
    telephoto exposure is running with enough time left for the download (the two downloads share one
    Alpaca bridge, and a shared lock keeps them from overlapping), or when the worker flushes it at the
    end of a target. Nothing touches the SeeStar while the mount moves.
  - solving runs in its own thread; a failure or a missing solver never touches imaging.
"""
import glob
import json
import math
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass

import numpy as np

from . import imaging

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class WideConfig:
    """[wide] in config.toml: the service defaults (plans override every/exposure/gain per step)."""
    every: int = 0                 # one wide frame per N telephoto lights (0 = off)
    exposure: float = 10.0         # s (camera 1: 30 us .. 60 s)
    gain: int = 100
    camera: int = 1                # Alpaca device numbers on the SeeStar
    focuser: int = 1               # read for FOCUSPOS only; the wide camera is never refocused
    focal_mm: float = 6.0
    pixel_um: float = 1.6
    solve: bool = True             # plate-solve wide frames in the background (if a solver is available)
    solver: str = "projections"    # "projections" or "none"
    projections_dir: str = ""      # a projections clone with node_modules ([paths] projections_dir)
    solve_timeout_s: float = 300.0
    solve_backlog: int = 4         # more frames than this waiting for the solver: the newest is not solved
    download_s: float = 3.0        # expected ImageArray download (about 2.5 s on the LAN); for the timing
    margin_s: float = 2.0          # the download must end this long before the telephoto exposure does
    grid_step: int = 60            # sidecar grid of pixel -> sky samples (green px), for the mosaic

    @classmethod
    def from_dict(cls, d):
        d = dict(d or {})
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        c = cls(**known)
        c.every, c.gain, c.camera, c.focuser = int(c.every), int(c.gain), int(c.camera), int(c.focuser)
        c.exposure = float(c.exposure)
        return c


# ---- the wide-field solver ----------------------------------------------------------------------

class NoWideSolver:
    name = "none"
    available = False

    def __init__(self, error="no wide-field solver configured"):
        self.error = error

    def solve(self, job):
        return None


def _vec_radec(v):
    x, y, z = (float(c) for c in v)
    n = math.sqrt(x * x + y * y + z * z)
    return math.degrees(math.atan2(y, x)) % 360.0, math.degrees(math.asin(max(-1.0, min(1.0, z / n))))


def tangent(ra, dec, ra0, dec0):
    """Gnomonic projection (deg) about (ra0, dec0): xi towards increasing RA (east), eta north."""
    a, d = np.radians(ra), np.radians(dec)
    a0, d0 = math.radians(ra0), math.radians(dec0)
    cosc = math.sin(d0) * np.sin(d) + math.cos(d0) * np.cos(d) * np.cos(a - a0)
    xi = np.cos(d) * np.sin(a - a0) / cosc
    eta = (math.cos(d0) * np.sin(d) - math.sin(d0) * np.cos(d) * np.cos(a - a0)) / cosc
    return np.degrees(xi), np.degrees(eta)


def tan_from_grid(pts, xc, yc, radius):
    """Local TAN approximation from (x, y, ra, dec) samples: CD (deg/px) fitted by least squares to the
    samples within `radius` px of (xc, yc), about the sky position at (xc, yc). Returns (ra0, dec0, cd)."""
    a = np.asarray(pts, float)
    near = a[np.hypot(a[:, 0] - xc, a[:, 1] - yc) <= radius]
    if len(near) < 3:
        raise ValueError("too few grid samples near the centre")
    c = near[np.argmin(np.hypot(near[:, 0] - xc, near[:, 1] - yc))]
    ra0, dec0 = float(c[2]), float(c[3])
    xi, eta = tangent(near[:, 2], near[:, 3], ra0, dec0)
    A = np.column_stack([near[:, 0] - c[0], near[:, 1] - c[1]])
    cd = np.linalg.lstsq(A, np.column_stack([xi, eta]), rcond=None)[0].T
    return ra0, dec0, cd.tolist(), (float(c[0]), float(c[1]))


class ProjectionsSolver:
    """Blind solve with the projections solver (a separate node/TypeScript project, see the README):
    the raw frame -> green-superpixel 16-bit PNG -> `node scripts/solve-blind.ts` (its own star
    detection, catalogue and lens model) -> solution JSON, which tools/solution_grid.ts samples on a
    pixel grid. Pixel units of the solution and the grid are green superpixels (2x2 of the raw frame)."""
    name = "projections"

    def __init__(self, projections_dir, timeout_s=300.0, grid_step=60, log=print):
        self.dir = os.path.expanduser(projections_dir or "")
        self.timeout, self.grid_step, self.log = float(timeout_s), int(grid_step), log
        self.node = shutil.which("node")
        self.error = None
        if not self.dir:
            self.error = "no [paths] projections_dir (or [wide] projections_dir) set"
        elif not os.path.isfile(os.path.join(self.dir, "scripts", "solve-blind.ts")):
            self.error = f"projections_dir {self.dir!r} has no scripts/solve-blind.ts"
        elif not self.node:
            self.error = "node not found on PATH (the projections solver runs under node)"
        self.available = self.error is None

    def solve(self, job):
        """job: {"image": raw frame [y][x] or None, "path": FITS path, "solution_path": where to keep the
        solution JSON}. Returns a solution dict or None (not solved); raises on solver errors."""
        if not self.available:
            return None
        from PIL import Image
        img = job.get("image")
        if img is None:
            from astropy.io import fits
            img = fits.getdata(job["path"])
        g = imaging.green(img)
        hg, wg = g.shape
        with tempfile.TemporaryDirectory(prefix="wide-") as tmp:
            png, sol = os.path.join(tmp, "frame.png"), os.path.join(tmp, "solution.json")
            Image.fromarray(np.clip(g, 0, 65535).astype(np.uint16)).save(png)
            t0 = time.time()
            p = subprocess.run([self.node, "scripts/solve-blind.ts", png, "--solution", sol], cwd=self.dir,
                               capture_output=True, text=True, timeout=self.timeout)
            out = p.stdout + p.stderr
            solved = re.search(r"^\s*Solved\.", out, re.M) is not None
            if not solved or not os.path.exists(sol):
                tail = " | ".join(out.strip().splitlines()[-2:])[-200:]
                self.log(f"wide solve: not solved (exit {p.returncode}): {tail}")
                return None
            m = re.search(r"Winner: (\d+) stars, RMS ([\d.]+) px", out)
            g_out = subprocess.run([self.node, os.path.join(REPO, "tools", "solution_grid.ts"), sol,
                                    str(self.grid_step)], capture_output=True, text=True, timeout=120,
                                   env={**os.environ, "PROJECTIONS_DIR": self.dir})
            grid = json.loads(g_out.stdout)
            if job.get("solution_path"):
                shutil.copyfile(sol, job["solution_path"])
        pts = [(x, y, *_vec_radec((vx, vy, vz))) for x, y, vx, vy, vz in (q for q in grid["pts"] if q)]
        xc, yc = grid["width"] / 2.0, grid["height"] / 2.0
        ra0, dec0, cd, (x0, y0) = tan_from_grid(pts, xc, yc, 0.3 * min(grid["width"], grid["height"]))
        cra, cdec = _vec_radec(grid["center"])
        return {"solver": self.name, "ra": cra, "dec": cdec, "units": "green", "width": wg, "height": hg,
                "tan": {"crpix_green": [x0, y0], "crval": [ra0, dec0], "cd_green": cd},
                "stars": int(m.group(1)) if m else None, "rms_px": float(m.group(2)) if m else None,
                "grid": {"step": grid.get("step"), "pts": [[round(a, 2), round(b, 2), round(r, 6), round(d, 6)]
                                                           for a, b, r, d in pts]},
                "seconds": round(time.time() - t0, 1)}


def make_wide_solver(cfg: WideConfig, log=print):
    if not cfg.solve or cfg.solver == "none":
        return NoWideSolver("wide solving off ([wide] solve = false)")
    if cfg.solver == "projections":
        return ProjectionsSolver(cfg.projections_dir, cfg.solve_timeout_s, cfg.grid_step, log)
    return NoWideSolver(f"unknown [wide] solver {cfg.solver!r} (use \"projections\" or \"none\")")


def header_wcs(sol, width, height):
    """FITS cards for a solution: a local TAN approximation at the frame centre, in full-resolution
    pixels (FITS 1-based). Over a 30 x 50 deg field a TAN is only approximate away from the centre; the
    sidecar keeps the solver's full model and a pixel -> sky grid."""
    t = sol.get("tan") or {}
    if sol.get("units") == "green":
        # green superpixel i covers raw pixels 2i, 2i+1: its centre is raw 2i + 0.5 (0-based)
        cx, cy = (2.0 * v + 0.5 + 1.0 for v in t["crpix_green"])
        cd = (np.asarray(t["cd_green"], float) / 2.0).tolist()
        crval = t["crval"]
    else:
        cx, cy = t.get("crpix", ((width + 1) / 2.0, (height + 1) / 2.0))
        cd, crval = t["cd"], t["crval"]
    return {"CTYPE1": "RA---TAN", "CTYPE2": "DEC--TAN", "CUNIT1": "deg", "CUNIT2": "deg",
            "CRPIX1": round(float(cx), 3), "CRPIX2": round(float(cy), 3),
            "CRVAL1": round(float(crval[0]), 6), "CRVAL2": round(float(crval[1]), 6),
            "CD1_1": float(cd[0][0]), "CD1_2": float(cd[0][1]), "CD2_1": float(cd[1][0]), "CD2_2": float(cd[1][1]),
            "RADESYS": "ICRS", "WCSAXES": 2}


# ---- the runner -----------------------------------------------------------------------------------

class WideRunner:
    """Owns camera 1 while a plan runs. The service's worker calls tele_started / tele_done / quiet /
    end_target; stop, unsafe and the scope lock call cancel. One wide frame at a time."""

    def __init__(self, svc, imager, cfg: WideConfig, solver):
        self.svc, self.im, self.cfg, self.solver = svc, imager, cfg, solver or NoWideSolver()
        self.clock = svc.clock
        self.cv = threading.Condition()
        self.wake = threading.Event()
        self.motion = threading.Event()        # set while the mount moves: no Alpaca calls from here
        self.job = None                        # the frame on the camera (exposing .. downloaded)
        self.tele = None                       # (start, end) of the running telephoto exposure
        self.flush_req = False
        self.solves = queue.Queue()
        self.stats = {}
        self.worker_block_s = 0.0              # sim seconds the worker spent waiting on the wide camera
        self.reset()
        self._threads = []

    @property
    def available(self):
        return self.im is not None

    def reset(self):
        self.stats = {"count": 0, "solved": 0, "failed": 0, "aborted": 0, "skipped": 0, "last": None}

    def start(self):
        if self.im is None:
            return self
        for name, fn in (("wide", self._camera_loop), ("wide-solve", self._solve_loop)):
            th = threading.Thread(target=fn, name=name, daemon=True)
            th.start()
            self._threads.append(th)
        return self

    def log(self, msg):
        self.svc.log(f"wide: {msg}")

    # ---- per-step settings ---------------------------------------------------------------------
    def settings(self, step):
        """(every, exposure, gain) for a step: the plan's values, else the [wide] defaults."""
        every = step.wide_every if getattr(step, "wide_every", None) is not None else self.cfg.every
        exp = step.wide_exposure if getattr(step, "wide_exposure", None) is not None else self.cfg.exposure
        gain = step.wide_gain if getattr(step, "wide_gain", None) is not None else self.cfg.gain
        return int(every), float(exp), int(gain)

    def enabled(self, step):
        return self.available and self.settings(step)[0] > 0

    # ---- called by the worker -----------------------------------------------------------------
    def tele_started(self, step, ctx, k, meta):
        """Right after the telephoto light k's StartExposure (worker thread). Starts a due wide frame if it
        fits before the mount next moves. Never raises, never waits on the camera beyond StartExposure."""
        t0 = self.clock.time()
        self.tele = (t0, t0 + float(step.exposure))
        self.wake.set()
        try:
            every, exp, gain = self.settings(step)
            if not self.available or every <= 0:
                return
            W = ctx.setdefault("wide", {"last_k": None, "logged": set(), "n": 0})
            if W["last_k"] is not None and k - W["last_k"] < every:
                return
            room = self._steady_lights(step, k) * float(step.exposure)
            if exp > room:
                if step.dither and exp > step.dither_every * float(step.exposure) and "nofit" not in W["logged"]:
                    W["logged"].add("nofit")
                    self.log(f"{step.name}: {exp:g} s doesn't fit between dithers "
                             f"({step.dither_every} x {step.exposure:g} s): no wide frames on this target")
                    self.stats["skipped"] += 1
                return                                   # wait for a light with more room before the next move
            why = self._why_not()
            if why:
                if why not in W["logged"]:
                    W["logged"].add(why)
                    self.log(f"{step.name}: frame skipped ({why})")
                self.stats["skipped"] += 1
                return
            with self.cv:
                if self.job is not None:                 # the previous frame isn't downloaded yet
                    return
                try:
                    if not bool(self.svc.scope.t.Tracking):
                        self.log(f"{step.name}: mount not tracking, frame skipped")
                        self.stats["skipped"] += 1
                        return
                    self.im.start(exp, gain)
                except Exception as e:  # noqa: BLE001 - the wide camera never breaks the telephoto
                    self.log(f"StartExposure failed: {e}")
                    self.stats["skipped"] += 1
                    return
                ts = self.clock.time()
                W["last_k"] = k
                self.job = {"t0": ts, "t_end": ts + exp, "exp": exp, "gain": gain, "state": "exposing",
                            "date_obs": ts, "step": step, "k": k, "meta": meta, "tdir": meta.get("tdir")}
                self.cv.notify_all()
            self.svc._notify()
        except Exception as e:  # noqa: BLE001
            self.log(f"error starting a frame: {e}")

    def _steady_lights(self, step, k):
        """Telephoto lights from k on before the mount next moves (a dither or re-centre, or the target's end)."""
        m = 1
        while k + m < step.count and not (step.dither and (k + m) % step.dither_every == 0):
            m += 1
        return m

    def _why_not(self):
        s = self.svc
        if s.abort.is_set():
            return "stopping"
        if s._unsafe:
            return "unsafe"
        if s.hwlock is not None:
            return "scope locked"
        return None

    def tele_done(self):
        """The telephoto exposure ended (ImageReady): its download comes next."""
        self.tele = None

    def quiet(self, why):
        """Before the mount moves (dither, re-centre): abort a wide exposure that is still running (only
        happens if the timing assumption failed) and keep the wide thread off the bridge until moved()."""
        self.motion.set()
        j = self.job
        if j is not None and self.clock.time() < j["t_end"]:
            self.cancel(f"the mount moves ({why}) before the exposure ended")

    def moved(self):
        self.motion.clear()
        self.wake.set()

    def end_target(self, normal=True, why="target ended"):
        """At every exit of a target's lights (worker thread). A still-running wide exposure is aborted
        (the mount moves or stops next). normal: a finished exposure is downloaded now (the mount waits,
        a few seconds once per target); otherwise it is dropped."""
        self.tele = None
        j = self.job
        if j is None:
            return
        if self.clock.time() < j["t_end"] or not normal:
            self.cancel(why if not normal else f"{why} before the exposure did")
            return
        t0 = self.clock.time()
        with self.cv:
            self.flush_req = True
        self.wake.set()
        deadline = t0 + self.cfg.download_s * 3 + 120.0
        try:
            while self.job is j and self.clock.time() < deadline and not self.svc.halt.is_set():
                with self.cv:
                    self.cv.wait(0.05)
            if self.job is j:
                self.cancel("not downloaded in time at the end of the target")
        finally:
            with self.cv:
                self.flush_req = False
            self.worker_block_s += self.clock.time() - t0

    def cancel(self, why):
        """Abort the wide exposure / drop a frame not yet downloaded. Any thread; safe when idle."""
        with self.cv:
            j, self.job = self.job, None
            self.cv.notify_all()
        if j is None:
            return False
        if j["state"] in ("exposing", "reading"):
            try:
                self.im.abort()
            except Exception as e:  # noqa: BLE001
                self.svc.log(f"wide: AbortExposure: {e}")
        self.stats["aborted"] += 1
        self.log(f"frame aborted ({why})")
        self.wake.set()
        self.svc._notify()
        return True

    # ---- the camera thread ----------------------------------------------------------------------
    def _alive(self, j):
        if self.job is not j or self.svc.halt.is_set():
            return False
        if self.svc.hwlock is not None:            # a script has the scope: leave the SeeStar alone
            self.cancel("scope locked")
            return False
        return True

    def _wait(self, s):
        self.clock.wait(self.wake, max(0.0, s))
        self.wake.clear()

    def _slot(self, j):
        """True when the download may start now: a telephoto exposure runs with room for the download,
        or the worker is waiting for it at the end of a target."""
        if self.motion.is_set():
            return False
        if self.flush_req:
            return True
        tw = self.tele
        return tw is not None and self.clock.time() + self.cfg.download_s + self.cfg.margin_s <= tw[1]

    def _camera_loop(self):
        while not self.svc.halt.is_set():
            with self.cv:
                while self.job is None and not self.svc.halt.is_set():
                    self.cv.wait(0.5)
                j = self.job
            if j is None:
                continue
            try:
                self._run_job(j)
            except Exception as e:  # noqa: BLE001
                self.log(f"error: {e}")
                with self.cv:
                    if self.job is j:
                        self.job = None
                        self.cv.notify_all()

    def _run_job(self, j):
        while self._alive(j) and self.clock.time() < j["t_end"]:
            self._wait(min(0.5, j["t_end"] - self.clock.time()))
        if not self._alive(j):
            return
        j["state"] = "reading"
        limit = j["t_end"] + 60.0 + self.cfg.download_s * 4
        while self._alive(j):
            if not self.motion.is_set() and self.im.ready():
                break
            if self.clock.time() > limit:
                self.cancel("no ImageReady within 60 s of the exposure end")
                return
            self._wait(0.5)
        j["state"] = "waiting"
        self.svc._notify()
        while self._alive(j) and not self._slot(j):
            self._wait(0.5)
        if not self._alive(j):
            return
        j["state"] = "downloading"
        self.svc._notify()
        data = self.im.download()
        truth = getattr(self.im.c, "last_truth", None)       # simulator only
        try:      # the remaining Alpaca reads (temperature, focuser...) before the worker may move on
            h = self.svc._wide_header(j, self.im.temperature())
        except Exception as e:  # noqa: BLE001
            h = None
            self.log(f"header: {e}")
        with self.cv:
            if self.job is not j:                             # cancelled during the download
                return
            self.job = None
            self.cv.notify_all()
        if h is not None:
            self._save(j, data, dict(truth) if isinstance(truth, dict) else None, h)

    def _save(self, j, data, truth, h):
        """File work only (no Alpaca): FITS, thumbnail, status, then queue the solve."""
        svc, step = self.svc, j["step"]
        try:
            wdir = os.path.join(j["tdir"], "wide")
            os.makedirs(wdir, exist_ok=True)
            n = len(glob.glob(os.path.join(wdir, "*.fits"))) + 1
            base = f"{_safe(step.name)}_wide_{j['exp']:g}s_g{j['gain']}_{n:03d}"
            path = os.path.join(wdir, base + ".fits")
            imaging.save_fits(path, data, h)
            g = imaging.green(data)
            imaging.preview(g, os.path.join(svc.out, "wide.jpg"), size=480)
            svc._img("wide")
            self.stats["count"] += 1
            last = {"file": os.path.relpath(path, svc.s.data_dir), "target": step.name,
                    "time": h["DATE-OBS"], "exposure": j["exp"], "gain": j["gain"], "light": j["k"] + 1,
                    "solved": None}
            self.stats["last"] = last
            self.log(f"{step.name}: frame {n} saved ({j['exp']:g} s, with light {j['k'] + 1})")
            svc._notify()
        except Exception as e:  # noqa: BLE001
            self.log(f"save failed: {e}")
            return
        if not getattr(self.solver, "available", False):
            return
        if self.solves.qsize() >= self.cfg.solve_backlog:
            self.log(f"solver backlog ({self.solves.qsize()} frames): {base} saved unsolved")
            last["solved"] = False
            last["why"] = "solver backlog"
            return
        self.solves.put({"path": path, "image": None, "truth": truth, "last": last,
                         "width": data.shape[1], "height": data.shape[0],
                         "solution_path": os.path.join(wdir, base + ".solution.json")})

    # ---- the solve thread -----------------------------------------------------------------------
    def _solve_loop(self):
        while not self.svc.halt.is_set():
            try:
                job = self.solves.get(timeout=0.5)
            except queue.Empty:
                continue
            self._solve(job)

    def _solve(self, job):
        """Solve one saved frame; WCS into the header and a sidecar <frame>.wcs.json. Never raises."""
        path, last = job["path"], job["last"]
        side = path[:-5] + ".wcs.json"
        try:
            sol = self.solver.solve(job)
            why = None if sol else "no solution"
        except Exception as e:  # noqa: BLE001
            sol, why = None, f"{type(e).__name__}: {e}"
        try:
            from astropy.io import fits
            with fits.open(path, mode="update") as hd:
                h = hd[0].header
                h["WCSSOLVD"] = (bool(sol), "wide-field plate solve")
                h["WCSSOLVR"] = (getattr(self.solver, "name", "?"), "solver")
                if sol:
                    for k, v in header_wcs(sol, job["width"], job["height"]).items():
                        h[k] = v
                    h["WCSAPPRX"] = (True, "local TAN at centre; full model in .wcs.json")
            out = {"version": 1, "solved": bool(sol), "solver": getattr(self.solver, "name", "?"),
                   "frame": os.path.basename(path), "why": why}
            if sol:
                out.update({k: v for k, v in sol.items() if k != "solver"})
                out["header_wcs"] = header_wcs(sol, job["width"], job["height"])
                if os.path.exists(job.get("solution_path") or ""):
                    out["solution"] = os.path.basename(job["solution_path"])
            with open(side + ".tmp", "w") as fh:
                json.dump(out, fh, indent=1)
            os.replace(side + ".tmp", side)
        except Exception as e:  # noqa: BLE001
            sol, why = None, f"writing the WCS: {e}"
        if sol:
            self.stats["solved"] += 1
            last.update(solved=True, ra=round(float(sol["ra"]), 3), dec=round(float(sol["dec"]), 3))
            self.log(f"{os.path.basename(path)} solved: RA {sol['ra']:.2f} Dec {sol['dec']:.2f} (J2000)")
        else:
            self.stats["failed"] += 1
            last.update(solved=False, why=why)
            self.log(f"{os.path.basename(path)} not solved ({why}); saved without WCS")
        self.svc._notify()

    # ---- status -----------------------------------------------------------------------------------
    def view(self, step=None):
        j = self.job
        st = j["state"] if j else "idle"
        if j and st == "exposing" and self.clock.time() >= j["t_end"]:
            st = "reading"
        v = {"available": self.available, "state": st, **{k: v for k, v in self.stats.items() if k != "last"},
             "last": dict(self.stats["last"]) if self.stats["last"] else None,
             "solver": {"name": getattr(self.solver, "name", None),
                        "available": bool(getattr(self.solver, "available", False)),
                        "error": getattr(self.solver, "error", None)},
             "solve_queue": self.solves.qsize()}
        if j:
            v["exposure"] = {"start": round(j["t0"], 2), "exposure": j["exp"], "target": j["step"].name}
        if step is not None and hasattr(step, "exposure"):
            every, exp, gain = self.settings(step)
            v["step"] = {"every": every, "exposure": exp, "gain": gain}
        return v


def _safe(s):
    return re.sub(r"[^A-Za-z0-9_.+-]+", "_", s).strip("_") or "target"
