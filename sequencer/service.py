"""Supervised sequencer service: owns the devices, runs one job at a time (plan, park, roof close),
polls safety and watches the cable window. The HTTP API and CLI only call the command methods here.

Threads:
  worker   runs queued jobs one by one; the only thread that moves the mount
  safety   polls the safety source; on unsafe it aborts the exposure and queues a guarded park
  monitor  reads mount position / cable angle for the status page; cable watchdog backstop
"""
import copy
import datetime as dt
import json
import math
import os
import queue
import re
import threading
import time
import traceback

import numpy as np

from . import astro, imaging
from . import plan as planmod
from .darklib import DarkLibrary, Key, read_temp
from .plan import LightStep, Plan, night_failures, target_failures
from .rig import Aborted, Rig

from scope import Interrupted   # tools/ is on sys.path via sequencer.rig

HA_RATE_MIN = astro.SIDEREAL_DEG_S * 60.0     # deg of RA axis per minute of tracking


class NightOver(RuntimeError):
    pass


def _iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_name(s):
    return re.sub(r"[^A-Za-z0-9_.+-]+", "_", s).strip("_") or "target"


class Service:
    def __init__(self, rig: Rig):
        self.rig = rig
        self.s = rig.settings
        self.clock = rig.clock
        self.scope = rig.scope
        self.imager = rig.imager
        self.scope.log = self.log
        self.imager.log = self.log
        self.lock = threading.RLock()
        self.abort = threading.Event()        # interrupts the running job
        self.halt = threading.Event()         # service shutdown
        self.jobs = queue.Queue()
        self.busy = None
        self.plan: Plan | None = None
        self.plan_end_t = None
        self.progress = []
        self.out = os.path.join(self.s.data_dir, "sequencer")
        os.makedirs(self.out, exist_ok=True)
        self._logf = open(os.path.join(self.out, "log.txt"), "a")
        self._unsafe = False
        self._stacks = {}
        self._masters = {}                    # dark-library id -> loaded master array
        self.darklib = DarkLibrary(os.path.join(self.s.data_dir, "darklib"), self.s.dark_policy, self.clock)
        self.sensor_temp = None
        self.st = {
            "state": "idle", "busy": None, "sim": rig.sim is not None,
            "step": None, "phase": None, "target": None,
            "mount": {}, "cable": {}, "frames": [], "drift": None,
            "safety": {"safe": None, "reasons": [], "checked": None},
            "roof": {"configured": rig.roof is not None, "kind": getattr(rig.roof, "kind", None),
                     "state": None, "close_requested": False, "reason": None},
            "solver": {"name": getattr(rig.solver, "name", None),
                       "available": bool(getattr(rig.solver, "available", False)),
                       "error": getattr(rig.solver, "error", None)},
            "images": {}, "log": [], "plan": None,
        }
        self._threads = []

    # ---- bookkeeping ---------------------------------------------------------------------------
    def log(self, msg):
        line = time.strftime("%H:%M:%S ", time.gmtime(self.clock.time())) + str(msg)
        with self.lock:
            self.st["log"] = (self.st["log"] + [line])[-300:]
        print(line, flush=True)
        try:
            self._logf.write(line + "\n")
            self._logf.flush()
        except Exception:  # noqa: BLE001
            pass

    def _set(self, **kw):
        with self.lock:
            self.st.update(kw)

    def status(self):
        with self.lock:
            s = copy.deepcopy(self.st)
        s["busy"] = self.busy
        s["time"] = _iso(self.clock.time())
        s["queued"] = self.jobs.qsize()
        try:
            s["darklib"] = self.darklib.summary(self.dark_keys(), self.sensor_temp)
        except Exception as e:  # noqa: BLE001
            s["darklib"] = {"error": str(e)}
        if self.plan:
            s["plan"] = {"name": self.plan.name, "source": self.plan.source,
                         "end_time": _iso(self.plan_end_t) if self.plan_end_t else None,
                         "steps": copy.deepcopy(self.progress)}
        return s

    def _write_status(self):
        p = os.path.join(self.out, "status.json")
        try:
            with open(p + ".tmp", "w") as fh:
                json.dump(self.status(), fh, indent=1, default=str)
            os.replace(p + ".tmp", p)
        except Exception:  # noqa: BLE001
            pass

    # ---- lifecycle -------------------------------------------------------------------------------
    def start(self):
        for name, fn in (("worker", self._worker), ("safety", self._safety_loop), ("monitor", self._monitor_loop)):
            th = threading.Thread(target=fn, name=name, daemon=True)
            th.start()
            self._threads.append(th)
        self.log(f"service started ({'SIMULATOR' if self.rig.sim else 'real devices'}); "
                 f"cable window unwrapped HA {self.scope.ha_u_min:.0f}..{self.scope.ha_u_max:.0f}, "
                 f"home {self.scope.ha_neutral:.0f}; solver {self.st['solver']['name']} "
                 f"({'ok' if self.st['solver']['available'] else self.st['solver']['error']})")
        return self

    def shutdown(self, timeout=10):
        self.halt.set()
        self.abort.set()
        self.jobs.put(None)
        for th in self._threads:
            th.join(timeout)

    def idle(self):
        return self.busy is None and self.jobs.empty()

    def wait_idle(self, timeout=60.0, poll=0.05):
        """Real-time seconds; for tests and the CLI."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.idle():
                return True
            time.sleep(poll)
        return False

    # ---- operator commands -----------------------------------------------------------------------
    def cmd_start(self, plan: Plan):
        if self.busy == "run" or not self.jobs.empty():
            return {"ok": False, "msg": "busy: stop first"}
        if self._unsafe:
            return {"ok": False, "msg": "unsafe: " + ", ".join(self.st["safety"]["reasons"])}
        self.plan = plan
        self.plan_end_t = plan.constraints.end_after(self.clock.time())
        self.progress = [{"desc": s.describe(), "kind": s.kind, "status": "pending", "frames": 0,
                          "count": s.count,
                          "reason": None} for s in plan.steps]
        self._stacks = {}
        with self.lock:
            self.st["frames"] = []
            self.st["drift"] = None
        self.log(f"plan '{plan.name}' loaded: {len(plan.steps)} steps")
        self.jobs.put(("run", {}))
        return {"ok": True, "msg": f"plan '{plan.name}' started"}

    def cmd_stop(self):
        if self.busy in (None, "park", "roof_close"):
            return {"ok": True, "msg": f"nothing to stop ({self.busy or 'idle'})"}
        self.log("operator: stop")
        self.abort.set()
        self.imager.abort()
        self._set(state="stopping")
        return {"ok": True, "msg": "stopping"}

    def cmd_resume(self):
        if self._unsafe:
            return {"ok": False, "msg": "still unsafe: " + ", ".join(self.st["safety"]["reasons"])}
        if not self.plan:
            return {"ok": False, "msg": "no plan loaded"}
        if self.busy or not self.jobs.empty():
            return {"ok": False, "msg": f"busy ({self.busy})"}
        for p in self.progress:
            if p["status"] in ("running", "interrupted"):
                p["status"] = "pending"
        self.log("operator: resume")
        self.jobs.put(("run", {}))
        return {"ok": True, "msg": "resuming"}

    def cmd_park(self, reason="operator"):
        self.log(f"park requested ({reason})")
        if self.busy == "run":
            self.abort.set()
            self.imager.abort()
        self.jobs.put(("park", {"reason": reason}))
        return {"ok": True, "msg": "parking"}

    def cmd_roof_close(self):
        if self.rig.roof is None:
            return {"ok": False, "msg": "no roof configured"}
        try:
            parked = bool(self.scope.t.AtPark)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "msg": f"can't read AtPark: {e}"}
        if not parked or self.busy == "run":
            return {"ok": False, "msg": "refused: the scope is not parked (run `park` first)"}
        self.log("operator: roof close")
        self.jobs.put(("roof_close", {}))
        return {"ok": True, "msg": "closing roof"}

    # ---- dark library ------------------------------------------------------------------------------
    def dark_key(self, exposure, gain):
        return Key("tele", float(exposure), int(gain), self.imager.offset(), self.imager.binning())

    def dark_keys(self, plans=None):
        """Settings the active plan and the configured planned plan files need darks for."""
        pls = list(plans) if plans is not None else ([self.plan] if self.plan else [])
        if plans is None:
            for path in self.s.dark_plans:
                try:
                    pls.append(planmod.load(path))
                except Exception as e:  # noqa: BLE001
                    self.log(f"darks: can't read planned plan {path}: {e}")
        keys = []
        for pl in pls:
            for exp, gain in pl.light_settings():
                k = self.dark_key(exp, gain)
                if k not in keys:
                    keys.append(k)
        return keys

    def cmd_darks_build(self, exposure=None, gain=None, count=None, plan=None, missing=False):
        """Build masters: one (exposure, gain), or everything missing for a plan (or for the active
        and planned plans when missing=True) at the current sensor temperature."""
        if self.busy == "run" or not self.jobs.empty():
            return {"ok": False, "msg": "busy: stop the plan first"}
        n = int(count or self.s.dark_policy.default_count)
        if exposure is not None:
            items = [(self.dark_key(exposure, gain if gain is not None else 80), n)]
        elif plan is not None or missing:
            temp = self.imager.temperature()
            keys = self.dark_keys([plan] if plan is not None else None)
            items = [(Key(m["camera"], m["exposure"], m["gain"], m["offset"], m["binning"]), n)
                     for m in self.darklib.missing(keys, temp)]
            if not items:
                return {"ok": True, "msg": "nothing missing", "items": []}
        else:
            return {"ok": False, "msg": "give exposure (and gain), a plan, or missing"}
        self.jobs.put(("darks_build", {"items": items}))
        labels = [k.label() for k, _ in items]
        self.log(f"darks build queued: {labels} x {n}")
        return {"ok": True, "msg": f"building {len(items)} master dark(s)", "items": labels}

    def cloudy_hook(self):
        """Reserved for later automation (e.g. build missing darks on a cloudy night). Not wired in v1."""
        return None

    def cmd_safety(self):
        return copy.deepcopy(self.st["safety"])

    # ---- worker -----------------------------------------------------------------------------------
    def _worker(self):
        while not self.halt.is_set():
            item = self.jobs.get()
            if item is None:
                break
            name, kw = item
            self.abort.clear()
            self.busy = name
            try:
                {"run": self._job_run, "park": self._job_park, "roof_close": self._job_roof_close,
                 "darks_build": self._job_darks_build}[name](**kw)
            except Exception as e:  # noqa: BLE001
                self.log(f"ERROR in {name}: {e}")
                self.log(traceback.format_exc().strip().splitlines()[-1])
                self._set(state="error")
                try:
                    self.scope.abort()
                except Exception:  # noqa: BLE001
                    pass
            finally:
                self.busy = None
                self.scope.interrupt = None
                self._write_status()

    # ---- park / roof ------------------------------------------------------------------------------
    def _job_park(self, reason="operator"):
        self._park(reason)

    def _park(self, reason):
        self.scope.interrupt = None
        self._set(state="parking", phase="park")
        self.log(f"guarded park ({reason}): tracking off, RA to home {self.scope.ha_neutral:.0f} by axis moves, "
                 "then firmware Park under the RA guard")
        self._camsnap("park_before")
        r = self.scope.park_guarded(guard_deg=self.s.park_guard_deg)
        self._camsnap("park_after")
        if r["parked"]:
            self.log(f"parked: {r}")
            if (self.s.hooks_parked or self.rig.sim) and not r.get("already"):
                self.log(f"on_parked hook -> {self.rig.hook(self.s.hooks_parked)}")
            self._set(state="parked", phase=None)
            return True
        self.log(f"PARK FAILED: {r}. Check the scope (camera) before anything else.")
        self._set(state="error", phase=None)
        return False

    def _unpark_if_needed(self):
        t = self.scope.t
        if t.AtPark:
            self.log("unpark")
            t.Unpark()
            if self.s.hooks_unparked or self.rig.sim:
                self.log(f"on_unparked hook -> {self.rig.hook(self.s.hooks_unparked)}")

    def _job_roof_close(self):
        if not self.scope.t.AtPark:
            self.log("roof close refused: scope not parked")
            return
        self._set(phase="roof close")
        st = self.rig.roof.close(log=self.log)
        self.log(f"roof: {st}")
        with self.lock:
            self.st["roof"]["state"] = st
            if st == "closed":
                self.st["roof"]["close_requested"] = False
        self._set(phase=None)

    def _camsnap(self, tag):
        if not self.s.ipcam:
            return
        try:
            from scope import camsnap
            camsnap(os.path.join(self.out, f"cam_{tag}.jpg"), self.s.ipcam)
        except Exception as e:  # noqa: BLE001
            self.log(f"ipcam: {e}")

    # ---- safety -----------------------------------------------------------------------------------
    def _safety_loop(self):
        while not self.halt.is_set():
            try:
                safe, reasons = self.rig.safety()
            except Exception as e:  # noqa: BLE001
                safe, reasons = False, [f"safety source error: {e}"]
            with self.lock:
                self.st["safety"] = {"safe": safe, "reasons": reasons, "checked": _iso(self.clock.time())}
            if not safe and not self._unsafe:
                self._unsafe = True
                self._on_unsafe(reasons)
            elif safe and self._unsafe:
                self._unsafe = False
                self.log("safety: SAFE again. Supervised: nothing restarts by itself; `resume` to continue")
            self.clock.wait(self.halt, self.s.safety_poll_s)

    def _on_unsafe(self, reasons):
        self.log(f"safety: UNSAFE {reasons}")
        darks_parked = False
        try:
            darks_parked = (self.busy == "darks_build" and self.scope.t.AtPark)
        except Exception:  # noqa: BLE001
            pass
        if darks_parked:
            self.log("safety: scope parked and taking darks, darks continue")
        else:
            self.abort.set()
            self.imager.abort()
            self.jobs.put(("park", {"reason": "unsafe: " + ", ".join(reasons)}))
        if self.rig.roof is not None:
            try:
                rs = self.rig.roof.state()
            except Exception as e:  # noqa: BLE001
                rs = f"error: {e}"
            if rs != "closed":
                with self.lock:
                    self.st["roof"].update(close_requested=True, reason=", ".join(reasons), state=rs)
                self.log("ROOF CLOSE REQUESTED (not automatic in v1): after the park, the operator "
                         "confirms and runs `roof close`")

    # ---- monitor ----------------------------------------------------------------------------------
    def _monitor_loop(self):
        n = 0
        while not self.halt.is_set():
            try:
                self._monitor_once(roof=(n % 15 == 0))
            except Exception as e:  # noqa: BLE001
                self.log(f"monitor: {e}")
            n += 1
            self._write_status()
            self.halt.wait(self.s.monitor_s)

    def _monitor_once(self, roof=False):
        sc = self.scope
        p = sc.position()
        u = sc.ha_unwrapped()
        t = sc.t
        tracking, at_park = bool(t.Tracking), bool(t.AtPark)
        self.sensor_temp = self.imager.temperature()
        left = sc.ha_u_max - u
        mount = {k: round(v, 3) for k, v in p.items()}
        mount.update(tracking=tracking, at_park=at_park)
        cable = {"u": round(u, 2), "min": sc.ha_u_min, "max": sc.ha_u_max, "home": sc.ha_neutral,
                 "left_deg": round(left, 2), "left_min": round(left / HA_RATE_MIN, 1)}
        with self.lock:
            self.st["mount"], self.st["cable"] = mount, cable
        if tracking and u > sc.ha_u_max - 0.5 * HA_RATE_MIN:
            # backstop: the sequencer should have stopped well before this
            t.Tracking = False
            self.abort.set()
            self.imager.abort()
            self.log(f"CABLE WATCHDOG: unwrapped HA {u:.1f} at the window limit {sc.ha_u_max:.1f}: "
                     "tracking off, job aborted")
        if roof and self.rig.roof is not None:
            rs = self.rig.roof.state()
            with self.lock:
                self.st["roof"]["state"] = rs
                if rs == "closed":
                    self.st["roof"]["close_requested"] = False

    # ---- the plan ---------------------------------------------------------------------------------
    def _check_abort(self):
        if self.abort.is_set():
            raise Aborted("stopped")

    def _job_run(self):
        plan = self.plan
        self.scope.interrupt = self.abort.is_set
        self._set(state="running")
        try:
            for i, step in enumerate(plan.steps):
                pr = self.progress[i]
                if pr["status"] not in ("pending",):
                    continue
                self._check_abort()
                nf = night_failures(plan.constraints, self.clock.time(), self.s.lat, self.s.lon, self.plan_end_t)
                if nf:
                    raise NightOver("; ".join(nf))
                pr["status"] = "running"
                self._set(step=i, target=getattr(step, "name", None))
                self.log(f"step {i + 1}/{len(plan.steps)}: {step.describe()}")
                self._run_light(step, pr)
            self.log("plan finished")
            final = "done"
        except NightOver as e:
            self.log(f"night over: {e}")
            for pr in self.progress:
                if pr["status"] in ("pending", "running"):
                    pr["status"], pr["reason"] = "skipped", str(e)
            final = "done"
        except (Aborted, Interrupted) as e:
            for pr in self.progress:
                if pr["status"] == "running":
                    pr["status"] = "interrupted"
            self.log(f"plan interrupted: {e}")
            self._set(state="stopped", phase=None)
            self.scope.interrupt = None
            return
        self.scope.interrupt = None
        if plan.park_at_end and not self.scope.t.AtPark:
            self._park("end of plan")
        self._set(state=final if self.st["state"] != "error" else "error", phase=None, step=None)

    # ---- lights -----------------------------------------------------------------------------------
    def _cable_left_min(self, u):
        return (self.scope.ha_u_max - u) / HA_RATE_MIN

    def _frame_minutes(self, exp):
        return (exp + self.s.frame_overhead_s) / 60.0

    def _night_dir(self):
        """data/sessions/<UTC date of the evening>: noon-to-noon nights."""
        night = dt.datetime.fromtimestamp(self.clock.time() - 12 * 3600, dt.timezone.utc).strftime("%Y%m%d")
        return os.path.join(self.s.data_dir, "sessions", night)

    def _target_dirs(self, step):
        base = self._night_dir()
        return base, os.path.join(base, _safe_name(step.name))

    def _run_light(self, step: LightStep, pr):
        c, S = self.plan.constraints, self.s
        now = self.clock.time()
        fails = target_failures(c, step.ra, step.dec, now, S.lat, S.lon, step.min_alt)
        if fails:
            return self._finish(pr, "skipped", "; ".join(fails))
        ra_j, dec_j = astro.icrs_to_jnow(step.ra, step.dec, now)
        ha = astro.wrap180(astro.lst_deg(now, S.lon) - ra_j)
        u_t = self.scope.map_ha(ha)
        if u_t is None or not -85 <= dec_j <= 89.5:
            return self._finish(pr, "skipped", f"HA {ha:.1f} / Dec {dec_j:.1f} not reachable in the cable window")
        left = self._cable_left_min(u_t)
        need = self._frame_minutes(step.exposure)
        if left < need + S.cable_reserve_min:
            return self._finish(pr, "skipped", f"cable window: {left:.0f} min of tracking left at HA {ha:.1f}")
        todo = step.count - pr["frames"]
        fit = int((left - S.cable_reserve_min) // need)
        self.log(f"{step.name}: HA {ha:.1f} Dec {dec_j:.2f} (JNow), {left:.0f} min of tracking before the "
                 f"cable limit: {min(fit, todo)}/{todo} frames fit")

        self._unpark_if_needed()
        self._dew_heater()
        self._set(phase="slew")
        self.imager.set_filter(step.filter)
        self._goto(ra_j, dec_j)
        cen = self._centre(step, ra_j, dec_j)
        pr["centre"] = cen
        self._lights(step, pr, cen)

    def _finish(self, pr, status, reason=None):
        pr["status"], pr["reason"] = status, reason
        self.log(f"  -> {status}" + (f": {reason}" if reason else ""))

    def _goto(self, ra_j, dec_j, tol=0.3):
        """Cable-safe move to JNow (ra, dec) and tracking on. Never a firmware goto.
        tol: per-axis stop tolerance (deg); centring passes a fine one."""
        self._check_abort()
        ha = astro.wrap180(astro.lst_deg(self.clock.time(), self.s.lon) - ra_j)
        p = self.scope.move_hadec(ha, dec_j, tol=tol)
        self._check_abort()
        self.scope.t.Tracking = True
        self.log(f"at HA {p['ha']:.1f} Dec {p['dec']:.1f} alt {p['alt']:.1f} az {p['az']:.0f}; tracking on")
        return p

    def _dew_heater(self):
        sw = self.rig.switch
        if not (self.s.dew_heater and sw):
            return
        try:
            for i in range(sw.MaxSwitch):
                if "dew" in sw.GetSwitchName(i).lower():
                    sw.SetSwitch(i, True)
        except Exception as e:  # noqa: BLE001
            self.log(f"dew heater: {e}")

    def _centre(self, step: LightStep, ra_j, dec_j):
        mode, solver = step.centre, self.rig.solver
        if mode == "none":
            return {"method": "none"}
        why = None
        if mode in ("auto", "solve"):
            if getattr(solver, "available", False):
                r = self._centre_solve(step, ra_j, dec_j)
                if r.get("solved"):
                    return r
                why = r.get("why", "solve failed")
            else:
                why = f"solver unavailable ({getattr(solver, 'error', '')})"
        if mode == "blob" or step.fallback == "blob":
            self.log(f"centring: {why + '; ' if why else ''}blob centring on the brightest extended object")
            return self._centre_blob(step, ra_j, dec_j)
        self.log(f"centring skipped: {why}; using mount pointing (can be ~2 deg off before a sync)")
        return {"method": "mount", "why": why}

    def _solve_frame(self, step):
        self._set(phase="solve")
        d = self.imager.expose(step.solve_exposure, step.gain, self.abort)
        g = imaging.green(d)
        st = imaging.stars(g, n=120)
        imaging.preview(g, os.path.join(self.out, "latest.jpg"))
        self._img("latest")
        return d, st, self.rig.solver.solve(d, ra_hint=step.ra, dec_hint=step.dec, star_list=st)

    def _centre_solve(self, step, ra_j, dec_j, max_iter=4, max_sync_deg=10.0):
        """Telephoto solve -> sync (JNow) -> re-centre, until within tolerance (from tools/sync_center.py)."""
        off = None
        for it in range(max_iter):
            self._check_abort()
            _, st, sol = self._solve_frame(step)
            if sol is None:
                return {"method": "solve", "solved": False, "why": f"no solution ({len(st)} stars)"}
            off = astro.separation(sol["ra"], sol["dec"], step.ra, step.dec) * 60.0
            self.log(f"solve {it + 1}: RA {sol['ra']:.3f} Dec {sol['dec']:.3f} (J2000), {off:.1f}' from target")
            if off <= step.centre_tol_arcmin:
                return {"method": "solve", "solved": True, "offset_arcmin": round(off, 2), "iterations": it + 1}
            if off / 60.0 > max_sync_deg:
                return {"method": "solve", "solved": False, "why": f"solution {off / 60:.1f} deg off: not syncing"}
            t = self.clock.time()
            sra, sdec = astro.icrs_to_jnow(sol["ra"], sol["dec"], t)
            u0 = self.scope.ha_unwrapped()
            self.scope.t.SyncToCoordinates(sra / 15.0, sdec)
            u1 = self.scope.ha_unwrapped()
            self.log(f"synced (JNow); unwrapped HA bookkeeping {u0:.2f} -> {u1:.2f}")
            # fine move: the axis tolerance must be well inside the centring tolerance. The HA axis
            # tolerance is in HA degrees, so scale by cos(dec) to get sky degrees.
            fine = max(0.01, step.centre_tol_arcmin / 60.0 / 3.0)
            cosd = max(math.cos(math.radians(dec_j)), 0.2)
            try:
                self._goto(ra_j, dec_j, tol=min(fine, fine / cosd))
            except RuntimeError as e:
                self.log(f"fine centring move did not converge: {e}")
        return {"method": "solve", "solved": True, "converged": False,
                "offset_arcmin": round(off, 2) if off is not None else None, "iterations": max_iter}

    def _centre_blob(self, step, ra_j, dec_j, tol_arcmin=None):
        """Generalised tools/poc_m31.py centring for bright extended targets: learn the image
        direction of +Dec / +HA with two probe moves, then solve the 2x2 system (all via move_hadec)."""
        import numpy as np
        from scipy import ndimage as ndi
        tol = tol_arcmin or step.centre_tol_arcmin
        sp_arcsec = 2 * self.s.pix_arcsec

        def core():
            d = self.imager.expose(step.solve_exposure, step.gain, self.abort)
            g = imaging.green(d)
            imaging.preview(g, os.path.join(self.out, "latest.jpg"))
            sm = ndi.gaussian_filter(g, max(3, int(25 * 7.48 / sp_arcsec)))
            y, x = np.unravel_index(np.argmax(sm), sm.shape)
            return np.array([y, x], float), g.shape

        def nudge(dha, ddec):
            p0 = self.scope.position()
            self.scope.move_hadec(p0["ha"] + dha, p0["dec"] + ddec)
            self.scope.t.Tracking = True

        self._set(phase="centre (blob)")
        cosd = max(math.cos(math.radians(dec_j)), 0.2)
        c0, shp = core()
        nudge(0, 0.5)
        c1, _ = core()
        nudge(0.5 / cosd, 0)
        c2, _ = core()
        v_dec, v_ha = (c1 - c0) / 0.5, (c2 - c1) / (0.5 / cosd)
        M = np.array([v_dec, v_ha]).T
        if abs(np.linalg.det(M)) < 1e-3:
            return {"method": "blob", "solved": False, "why": "probe moves did not move the blob"}
        cur, off_arcmin = c2, None
        for it in range(3):
            self._check_abort()
            off = np.array([shp[0] / 2, shp[1] / 2]) - cur
            off_arcmin = float(np.hypot(*off) * sp_arcsec / 60)
            self.log(f"blob centring {it + 1}: offset {off_arcmin:.1f}'")
            if off_arcmin < tol:
                break
            ddec, dha = np.linalg.solve(M, off)
            nudge(dha, ddec)
            cur, _ = core()
        return {"method": "blob", "solved": True, "offset_arcmin": round(off_arcmin, 2)}

    def _stack_for(self, step, base):
        key = (step.name, step.exposure, step.gain, step.filter)
        if key not in self._stacks:
            self._stacks[key] = {"stack": imaging.RunningStack(), "ref": None, "t0": None, "drift": []}
        return self._stacks[key]

    def _master_for(self, exposure, gain, temp):
        """(entry, array) of the best usable master for these settings and temperature, or (None, None)."""
        m = self.darklib.match(self.dark_key(exposure, gain), temp)
        if m is None:
            return None, None
        if m["id"] not in self._masters or self._masters[m["id"]][0] != m["created_unix"]:
            self._masters[m["id"]] = (m["created_unix"], self.darklib.load_master(m))
        return m, self._masters[m["id"]][1]

    def _lights(self, step: LightStep, pr, cen):
        c, S = self.plan.constraints, self.s
        base, tdir = self._target_dirs(step)
        ctx = self._stack_for(step, base)
        self._set(phase="lights")
        while pr["frames"] < step.count:
            self._check_abort()
            k = pr["frames"]
            now = self.clock.time()
            t_end = now + step.exposure + S.frame_overhead_s
            nf = night_failures(c, t_end, S.lat, S.lon, self.plan_end_t)
            if nf:
                raise NightOver("; ".join(nf))
            fails = target_failures(c, step.ra, step.dec, t_end, S.lat, S.lon, step.min_alt)
            if fails:
                return self._finish(pr, "stopped", "; ".join(fails))
            u = self.scope.ha_unwrapped()
            left = self._cable_left_min(u)
            if left < self._frame_minutes(step.exposure) + S.cable_reserve_min:
                self.scope.t.Tracking = False
                return self._finish(pr, "stopped", f"cable window: {left:.1f} min of tracking left "
                                    f"(unwrapped HA {u:.1f}, limit {self.scope.ha_u_max:.1f}); tracking off")
            if step.dither and k > 0 and k % max(1, step.dither_every) == 0:
                self.log("dither: not implemented in v1 (stub), continuing")
            pos = self.scope.position()
            date_obs = _iso(self.clock.time())
            temp = self.imager.temperature()
            d = self.imager.expose(step.exposure, step.gain, self.abort)
            g = imaging.green(d)
            st = imaging.stars(g)
            q = imaging.qa(g, st)
            ok, why = imaging.qa_ok(q, step.qa)
            if ctx["ref"] is None:
                ctx["ref"], ctx["t0"] = g, now
                dy = dx = 0.0
            else:
                dy, dx = imaging.shift(ctx["ref"], g)
            sp = 2 * S.pix_arcsec
            dm, dark = self._master_for(step.exposure, step.gain, temp)
            entry = {"i": k + 1, "target": step.name, "min": round((now - ctx["t0"]) / 60, 2),
                     "dx_as": round(dx * sp, 1), "dy_as": round(dy * sp, 1), "ok": ok, "why": why,
                     "temp": read_temp(temp), "dark": dm["id"] if dm else None, **q}
            ctx["drift"].append(entry)
            rate = imaging.drift_rate(ctx["drift"])
            name = f"{_safe_name(step.name)}_{step.exposure:g}s_g{step.gain}_{step.filter}_{k + 1:03d}.fits"
            path = os.path.join(tdir, "lights", name)
            imaging.save_fits(path, d, self._header(step.exposure, step.gain, step.filter, "LIGHT", date_obs,
                                                         temp, step, pos, q, u, cen,
                                                         dark=dm["id"] if dm else "NONE"))
            if ok:
                ctx["stack"].add(d, offset=(dy, dx), dark=dark)
                ctx["stack"].save(os.path.join(tdir, "stack.fits"), os.path.join(self.out, "stack.jpg"))
                self._img("stack")
            imaging.preview(g, os.path.join(self.out, "latest.jpg"))
            self._img("latest")
            pr["frames"] += 1
            with self.lock:
                self.st["frames"] = (self.st["frames"] + [entry])[-200:]
                self.st["drift"] = rate
                self.st["stack"] = {"target": step.name, "frames": ctx["stack"].n,
                                    "uncalibrated": ctx["stack"].n_uncal}
            r = f"; drift {rate['total']}\"/min" if rate else ""
            self.log(f"light {k + 1}/{step.count}: stars {q['stars']} HFR {q['hfr_px']} ecc {q['ecc']} "
                     f"shift ({entry['dx_as']:+.0f}\", {entry['dy_as']:+.0f}\"){r}"
                     + ("" if dm else " NO DARK (uncalibrated)")
                     + ("" if ok else f" REJECTED ({', '.join(why)})"))
        self._finish(pr, "done")

    def _header(self, exp, gain, filt, kind, date_obs, temp, step=None, pos=None, q=None, u=None, cen=None,
                dark=None):
        S = self.s
        h = {"DATE-OBS": date_obs, "EXPTIME": float(exp), "GAIN": int(gain), "IMAGETYP": kind,
             "BAYERPAT": "GRBG", "CCD-TEMP": temp, "FILTER": filt,
             "INSTRUME": S.instrument, "FOCALLEN": S.focal_mm, "XPIXSZ": S.pixel_um, "YPIXSZ": S.pixel_um,
             "SITELAT": S.lat, "SITELONG": S.lon, "FOCUSPOS": self.imager.focus_pos(),
             "SWCREATE": "seestar-commander sequencer v1", "PLAN": self.plan.name if self.plan else None,
             "OFFSET": self.imager.offset(), "XBINNING": self.imager.binning(), "DARKUSED": dark}
        if kind == "LIGHT" and step is not None:
            h.update({"OBJECT": step.name, "OBJCTRA": round(step.ra, 5), "OBJCTDEC": round(step.dec, 5),
                      "RA": round(pos["ra_h"] * 15.0, 5), "DEC": round(pos["dec"], 5),
                      "MNT_RA": round(pos["ra_h"], 6), "MNT_DEC": round(pos["dec"], 5), "MNT_HA": round(pos["ha"], 4),
                      "MNT_ALT": round(pos["alt"], 3), "MNT_AZ": round(pos["az"], 3),
                      "CABLE_U": round(u, 3) if u is not None else None,
                      "CENTRED": (cen or {}).get("method")})
            if q:
                h.update({"STARS": q["stars"], "HFR": q["hfr_px"], "ECC": q["ecc"], "BGMED": q["bg"]})
        return h

    def _img(self, key):
        with self.lock:
            self.st["images"][key] = round(self.clock.time(), 1)

    # ---- dark library build -------------------------------------------------------------------
    def _job_darks_build(self, items):
        """Wheel to Dark (works with the roof closed), N frames per key, median -> library, filter back."""
        prev = self.imager.filter_name()
        self._set(phase="darks", state="darks")
        self.imager.set_filter("Dark")
        self.log("filter wheel on Dark")
        built = []
        try:
            for key, n in items:
                frames, temps = [], []
                for k in range(n):
                    self._check_abort()
                    temps.append(self.imager.temperature())
                    frames.append(self.imager.expose(key.exposure, key.gain, self.abort, light=False))
                    temps.append(self.imager.temperature())
                    self.log(f"dark {k + 1}/{n} {key.label()}: median {float(np.median(frames[-1])):.0f}, "
                             f"sensor {temps[-1]}")
                m = self.darklib.add(key, frames, temps, self.imager.identity())
                built.append(m["id"])
                self.log(f"master dark {m['id']}: {m['frames']} frames, {m['temp_mean']} C "
                         f"({m['temp_min']}..{m['temp_max']})")
        except Aborted:
            self.log("darks build stopped")
            self._set(state="stopped")
        finally:
            if prev in self.imager.fw.Names and prev != "Dark":
                self.imager.set_filter(prev)
                self.log(f"filter wheel back on {prev}")
            self._set(phase=None)
        if built:
            if self.st["state"] == "darks":
                self._set(state="idle")
            self._restack()

    def _restack(self):
        """Re-stack tonight's lights of the active plan, calibrating each frame with its matching master."""
        if not self.plan:
            return
        from astropy.io import fits
        base = self._night_dir()
        for st in self.plan.steps:
            ldir = os.path.join(base, _safe_name(st.name), "lights")
            if not os.path.isdir(ldir):
                continue
            stack, used = imaging.RunningStack(), set()
            for f in sorted(os.listdir(ldir)):
                if not f.endswith(".fits"):
                    continue
                with fits.open(os.path.join(ldir, f)) as hd:
                    h = hd[0].header
                    if h.get("FILTER") != st.filter or h.get("EXPTIME") != st.exposure or h.get("GAIN") != st.gain:
                        continue
                    if not imaging.qa_ok({"stars": h.get("STARS", 0), "hfr_px": h.get("HFR"),
                                          "ecc": h.get("ECC")}, st.qa)[0]:
                        continue
                    dm, dark = self._master_for(st.exposure, st.gain, h.get("CCD-TEMP"))
                    used.add(dm["id"] if dm else "NONE")
                    stack.add(hd[0].data, dark=dark)
            if stack.n:
                tdir = os.path.join(base, _safe_name(st.name))
                stack.save(os.path.join(tdir, "stack_darksub.fits"), os.path.join(self.out, "stack.jpg"))
                self._img("stack")
                self.log(f"{st.name}: re-stacked {stack.n} lights ({stack.n_uncal} uncalibrated), "
                         f"darks {sorted(used)}")
