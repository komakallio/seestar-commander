"""Supervised sequencer service: owns the devices, runs one job at a time (plan, park, roof close),
polls safety and watches the cable window. The HTTP API and CLI only call the command methods here.

Threads:
  worker   runs queued jobs one by one; the only thread that moves the mount
  safety   polls the safety source; on unsafe it aborts the exposure and queues a guarded park
  monitor  reads mount position / cable angle for the status page; cable watchdog backstop

Exclusive use: an API lock (cmd_lock / cmd_unlock, with a TTL) hands the scope to a script; while it is
held every job that moves the scope or uses the camera is refused. The service also holds the file lock
<data_dir>/scope.lock while it runs a job or an API lock is granted (tools/scopelock.py).
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
from . import dither as dith
from . import plan as planmod
from .darklib import DarkLibrary, Key, read_temp
from .heartbeat import Heartbeat
from .plan import LightStep, Plan, limit_reached, night_failures, target_failures
from .rig import Aborted, Rig, RoofRefused
from .polar import PolarController
from .pointing import PointingConfig, PointingMemory
from .wide import WideConfig, WideRunner

from scope import PRIMARY, SECONDARY, CableLimit, Interrupted   # tools/ is on sys.path via sequencer.rig
from scopelock import FileLock, LOCK_FILE, describe as describe_holder

HA_RATE_MIN = astro.SIDEREAL_DEG_S * 60.0     # deg of RA axis per minute of tracking


class NightOver(RuntimeError):
    """The night ended for the plan: its own Sun/end_time constraint, or the service night limit
    (limit=True: then the scope parks whatever the plan says, see _end_night)."""

    def __init__(self, reasons):
        super().__init__("; ".join(reasons))
        self.limit = limit_reached(reasons)


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
                     "state": None, "close_requested": False, "reason": None,
                     "auto_close_on_unsafe": bool(self.s.auto_close_on_unsafe and rig.roof is not None),
                     "last_action": None},
            "solver": {"name": getattr(rig.solver, "name", None),
                       "available": bool(getattr(rig.solver, "available", False)),
                       "error": getattr(rig.solver, "error", None)},
            "images": {}, "log": [], "plan": None,
            "action": None,
        }
        self._threads = []
        self.polar = PolarController(self)    # polar alignment: job "polar", see sequencer/polar.py
        # pointing memory: solve log + sync shift + model, see sequencer/pointing.py
        self.pointing = PointingMemory(os.path.join(self.s.data_dir, "pointing"),
                                       PointingConfig.from_dict(self.s.extra.get("pointing")), self.clock,
                                       self.log, since=self._polar_done_unix)
        try:
            self.pointing.load_state(bool(self.scope.t.AtPark))
        except Exception as e:  # noqa: BLE001
            self.pointing.note_unknown(f"can't read AtPark at start: {e}")
        self.dither_cfg = dith.DitherConfig.from_dict(self.s.extra.get("dither"))
        # wide camera alongside the telephoto: see sequencer/wide.py
        self.wide_cfg = WideConfig.from_dict(self.s.extra.get("wide"))
        self.wide = WideRunner(self, rig.wide, self.wide_cfg, rig.wide_solver)
        if rig.wide is not None:
            rig.wide.log = self.log
        self.plan_t0 = None
        self.changed = threading.Condition()   # notified on every state change (SSE /api/events)
        self.change_seq = 0
        self.log_n = 0                         # log lines ever written (SSE sends only new ones)
        self.hwlock = None                     # API lock: {"owner", "reason", "since", "until" (wall clock), "ttl_s"}
        self.lock_revoked = None
        self.flock = FileLock(os.path.join(self.s.data_dir, LOCK_FILE))
        self._flock_fail = None
        self.heartbeat = Heartbeat(self)       # [hooks] heartbeat_url, see sequencer/heartbeat.py
        self._night_stop = None                # NightOver the monitor found while a plan runs
        self._night_watch = False              # a plan's target loop is running (the monitor may stop it)
        self._was_night = None                 # service night limit not reached at the last monitor tick

    # ---- bookkeeping ---------------------------------------------------------------------------
    def log(self, msg):
        line = time.strftime("%H:%M:%S ", time.gmtime(self.clock.time())) + str(msg)
        with self.lock:
            self.st["log"] = (self.st["log"] + [line])[-300:]
            self.log_n += 1
        self._notify()
        print(line, flush=True)
        try:
            self._logf.write(line + "\n")
            self._logf.flush()
        except Exception:  # noqa: BLE001
            pass

    def _set(self, **kw):
        with self.lock:
            self.st.update(kw)
        self._notify()

    def _notify(self):
        with self.changed:
            self.change_seq += 1
            self.changed.notify_all()

    def _act(self, kind=None, **kw):
        """What the worker is doing right now (drives the page headline); kind=None clears it."""
        self._set(action=dict(kind=kind, t=round(self.clock.time(), 1), **kw) if kind else None)

    def status(self, log=True, darklib=True):
        with self.lock:
            st = dict(self.st)
            if not log:
                st.pop("log")
            s = copy.deepcopy(st)
            s["log_n"] = self.log_n
        s["busy"] = self.busy
        now = self.clock.time()
        s["time"] = _iso(now)
        s["time_unix"] = round(now, 2)
        s["speed"] = getattr(self.clock, "speed", 1.0)
        s["queued"] = self.jobs.qsize()
        if darklib:
            try:
                s["darklib"] = self.darklib.summary(self.dark_keys(), self.sensor_temp)
            except Exception as e:  # noqa: BLE001
                s["darklib"] = {"error": str(e)}
        s["lock"] = self.lock_view()
        s["heartbeat"] = self.heartbeat.view()
        cur = None
        if self.plan and isinstance(st.get("step"), int) and st["step"] < len(self.plan.steps):
            cur = self.plan.steps[st["step"]]
        s["wide"] = self.wide.view(cur)
        try:
            s["pointing"] = self.pointing.summary()
        except Exception as e:  # noqa: BLE001
            s["pointing"] = {"error": str(e)}
        if self.plan:
            s["plan"] = {"name": self.plan.name, "source": self.plan.source,
                         "end_time": _iso(self.plan_end_t) if self.plan_end_t else None,
                         "open_roof_at_start": self.plan.open_roof_at_start,
                         "close_roof_at_end": self.plan.close_roof_at_end,
                         "started": _iso(self.plan_t0) if self.plan_t0 else None,
                         "steps": copy.deepcopy(self.progress)}
        from . import statusview
        statusview.annotate(self, s, now)
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
        self.heartbeat.start()
        self.wide.start()
        self.log(f"service started ({'SIMULATOR' if self.rig.sim else 'real devices'}); "
                 f"cable window unwrapped HA {self.scope.ha_u_min:.0f}..{self.scope.ha_u_max:.0f}, "
                 f"home {self.scope.ha_neutral:.0f}; night limit {self.s.night_limit.describe()}; "
                 f"solver {self.st['solver']['name']} "
                 f"({'ok' if self.st['solver']['available'] else self.st['solver']['error']})")
        if self.wide.available:
            ws = self.wide.solver
            self.log(f"wide camera: on (default every {self.wide_cfg.every or 'off'}); wide solver "
                     f"{ws.name} ({'ok' if ws.available else ws.error})")
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
    # ---- exclusive use (scope lock) -------------------------------------------------------------------
    def _lock_active(self):
        """The API lock if held and not expired (an expired one is dropped here)."""
        with self.lock:
            L = self.hwlock
            if L is None or time.time() <= L["until"]:
                return L
            self.hwlock = None
        self.log(f"scope lock by {L['owner']} expired (not renewed within {L['ttl_s']:.0f} s)")
        self._flock_sync()
        self._notify()
        return None

    def _locked_msg(self):
        L = self._lock_active()
        if L is None:
            return None
        until = time.strftime("%H:%M:%S", time.gmtime(L["until"]))
        return (f"refused: scope in use by {L['owner']}" + (f": {L['reason']}" if L["reason"] else "")
                + f" (lock until {until} UTC unless renewed; `unlock --force` to take it back)")

    def _flock_sync(self):
        """Hold the file lock exactly while a job runs or an API lock is granted. False if it's wanted
        but someone else (a script that couldn't reach the API) holds it."""
        with self.lock:
            L = self.hwlock
            want = self.busy is not None or L is not None
            if want and not self.flock.held:
                why = f"job {self.busy}" if self.busy else f"API lock for {L['owner']}"
                if not self.flock.acquire("sequencer", why, via="service"):
                    self._flock_fail = self.flock.holder() or {"owner": "?"}
                    return False
                self._flock_fail = None
            elif not want and self.flock.held:
                self.flock.release()
            return True

    def lock_view(self):
        L = self._lock_active()
        now = time.time()
        v = {"locked": L is not None, "revoked": copy.deepcopy(self.lock_revoked)}
        if L is not None:
            v.update(owner=L["owner"], reason=L["reason"], since=_iso(L["since"]), until=_iso(L["until"]),
                     until_unix=round(L["until"], 1), remaining_s=round(L["until"] - now, 1), ttl_s=L["ttl_s"])
        if not self.flock.held:
            h = self.flock.holder()
            if h:
                v["file_holder"] = h          # a script holding the file lock without the API
        elif self._flock_fail:
            v["file_holder"] = self._flock_fail
        return v

    def cmd_lock(self, owner, reason="", ttl_s=300.0, renew=False):
        """Hand the scope to `owner` for ttl_s (wall-clock) seconds; locking again with the same owner
        renews. renew=True only renews: it fails if the lock is gone (released, revoked, expired), so a
        forced unlock or an unsafe revocation isn't undone by the holder's renewal thread."""
        owner = str(owner or "").strip()
        reason = str(reason or "").strip()
        if not owner:
            return {"ok": False, "msg": "owner required"}
        try:
            ttl = max(1.0, min(float(ttl_s if ttl_s is not None else 300.0), 12 * 3600.0))
        except (TypeError, ValueError):
            return {"ok": False, "msg": f"bad ttl_s {ttl_s!r}"}
        with self.lock:
            L = self._lock_active()
            if L is not None and L["owner"] != owner:
                return {"ok": False, "msg": self._locked_msg(), "lock": self.lock_view()}
            if renew and L is None:
                rv = self.lock_revoked
                why = f"revoked: {rv['why']}" if rv and rv["owner"] == owner else "released or expired"
                return {"ok": False, "msg": f"no lock held by {owner} ({why})"}
            if L is None:
                if self.busy is not None or not self.jobs.empty():
                    return {"ok": False, "msg": f"busy ({self.busy or 'queued jobs'}): stop it first"}
                if self._unsafe:
                    return {"ok": False, "msg": "unsafe: " + ", ".join(self.st["safety"]["reasons"])}
                now = time.time()
                self.hwlock = {"owner": owner, "reason": reason, "since": now, "until": now + ttl, "ttl_s": ttl}
                if not self._flock_sync():
                    self.hwlock = None
                    return {"ok": False, "msg": f"scope in use by {describe_holder(self._flock_fail)} (file lock)"}
                self.lock_revoked = None
                renewed = False
                self.wide.cancel("scope lock granted")
            else:
                L.update(until=time.time() + ttl, ttl_s=ttl, reason=reason or L["reason"])
                renewed = True
        if not renewed:
            self.log(f"scope LOCKED by {owner}" + (f": {reason}" if reason else "") + f" (ttl {ttl:.0f} s); "
                     "plans, park, roof open, darks and polar are refused until it's released")
        self._notify()
        return {"ok": True, "msg": ("renewed" if renewed else "locked") + f" for {owner}, {ttl:.0f} s",
                "lock": self.lock_view()}

    def cmd_unlock(self, owner=None, force=False):
        with self.lock:
            L = self._lock_active()
            if L is None:
                return {"ok": True, "msg": "not locked"}
            if not force and (owner or "") != L["owner"]:
                return {"ok": False, "msg": f"locked by {L['owner']}, not {owner or '(no owner given)'}; "
                                           "use force to take it back"}
            self.hwlock = None
        self._flock_sync()
        self.log(f"scope lock by {L['owner']} released" + (" (forced)" if force and owner != L["owner"] else ""))
        self._notify()
        return {"ok": True, "msg": f"unlocked ({L['owner']})"}

    def _revoke_lock(self, why):
        with self.lock:
            L, self.hwlock = self.hwlock, None
            if L is None:
                return None
            self.lock_revoked = {"owner": L["owner"], "why": why, "time": _iso(time.time())}
        self.log(f"scope lock by {L['owner']} REVOKED: {why}")
        self._notify()
        return L

    # ---- operator commands -----------------------------------------------------------------------
    def cmd_start(self, plan: Plan):
        if self.busy in ("run", "polar") or not self.jobs.empty():
            return {"ok": False, "msg": "busy: stop first"}
        if self._locked_msg():
            return {"ok": False, "msg": self._locked_msg()}
        if self._unsafe:
            return {"ok": False, "msg": "unsafe: " + ", ".join(self.st["safety"]["reasons"])}
        why = self._night_why(plan, plan.constraints.end_after(self.clock.time()))
        if why:
            return {"ok": False, "msg": why}
        if (plan.open_roof_at_start or plan.close_roof_at_end) and self.rig.roof is None:
            return {"ok": False, "msg": "plan wants roof handling (open_roof_at_start / close_roof_at_end) "
                                        "but no [roof] is configured"}
        self.plan = plan
        self.plan_end_t = plan.constraints.end_after(self.clock.time())
        self.plan_t0 = self.clock.time()
        self.progress = [{"desc": s.describe(), "kind": s.kind, "status": "pending", "frames": 0,
                          "count": s.count,
                          "reason": None} for s in plan.steps]
        self._stacks = {}
        self.wide.reset()
        with self.lock:
            self.st["frames"] = []
            self.st["drift"] = None
            self.st["dither"] = None
        roof = [k for k, on in (("auto-open at start", plan.open_roof_at_start),
                                ("auto-close at end", plan.close_roof_at_end)) if on]
        self.log(f"plan '{plan.name}' loaded: {len(plan.steps)} steps"
                 + (f"; roof: {', '.join(roof)}" if roof else ""))
        self.jobs.put(("run", {}))
        return {"ok": True, "msg": f"plan '{plan.name}' started"}

    def cmd_stop(self):
        if self.busy in (None, "park", "roof_close", "roof_open"):
            return {"ok": True, "msg": f"nothing to stop ({self.busy or 'idle'})"}
        self.log("operator: stop")
        self.abort.set()
        self.imager.abort()
        self.wide.cancel("stop")
        self._set(state="stopping")
        return {"ok": True, "msg": "stopping"}

    def cmd_resume(self):
        if self._unsafe:
            return {"ok": False, "msg": "still unsafe: " + ", ".join(self.st["safety"]["reasons"])}
        if not self.plan:
            return {"ok": False, "msg": "no plan loaded"}
        if self._locked_msg():
            return {"ok": False, "msg": self._locked_msg()}
        if self.busy or not self.jobs.empty():
            return {"ok": False, "msg": f"busy ({self.busy})"}
        why = self._night_why(self.plan, self.plan_end_t)
        if why:
            return {"ok": False, "msg": why}
        for p in self.progress:
            if p["status"] in ("running", "interrupted"):
                p["status"] = "pending"
        self.log("operator: resume")
        self.jobs.put(("run", {}))
        return {"ok": True, "msg": "resuming"}

    def cmd_park(self, reason="operator"):
        if self._locked_msg():
            return {"ok": False, "msg": self._locked_msg()}
        self.log(f"park requested ({reason})")
        if self.busy in ("run", "polar"):
            self.abort.set()
            self.imager.abort()
        self.wide.cancel("park")
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

    def _unsafe_why(self):
        """None when safety reports safe right now, else the reason opening must be refused."""
        sf = self.st["safety"]
        if self._unsafe or sf.get("safe") is False:
            return "unsafe: " + (", ".join(sf.get("reasons") or []) or "no reason given")
        if sf.get("safe") is not True:
            return "safety not known yet"
        return None

    def cmd_roof_open(self):
        if self.rig.roof is None:
            return {"ok": False, "msg": "no roof configured"}
        why = self._unsafe_why() or self._locked_msg()
        if why:
            return {"ok": False, "msg": why if why.startswith("refused") else f"refused: {why}"}
        if self.busy == "run" or not self.jobs.empty():
            return {"ok": False, "msg": f"busy ({self.busy or 'queued jobs'})"}
        self.log("operator: roof open")
        self.jobs.put(("roof_open", {}))
        return {"ok": True, "msg": "opening roof"}

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
        if self.busy in ("run", "polar") or not self.jobs.empty():
            return {"ok": False, "msg": "busy: stop the plan first"}
        if self._locked_msg():
            return {"ok": False, "msg": self._locked_msg()}
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
            locked = self._locked_msg() if name != "roof_close" else None
            if locked:                         # queued just before a lock was granted
                self.log(f"{name}: {locked}")
                continue
            self.busy = name
            if not self._flock_sync():
                self.log(f"{name} REFUSED: scope in use by {describe_holder(self._flock_fail)} (file lock "
                         f"{self.flock.path}), a script that isn't going through the service's lock API")
                self.busy = None
                self._notify()
                continue
            self._notify()
            self.heartbeat.kick()
            try:
                {"run": self._job_run, "park": self._job_park, "roof_close": self._job_roof_close,
                 "roof_open": self._job_roof_open, "darks_build": self._job_darks_build,
                 "polar": self.polar.run}[name](**kw)
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
                if name in ("run", "park"):
                    self.heartbeat.final()
                self._flock_sync()
                self.scope.interrupt = None
                self._act(None)
                self._write_status()

    # ---- park / roof ------------------------------------------------------------------------------
    def _job_park(self, reason="operator", close_roof=False, grace_s=0.0):
        if grace_s:
            # a script had the scope: its lock renewal now fails, which stops tools/scope.Scope moves
            self.log(f"park: waiting {grace_s:.0f} s for the former lock holder to stop")
            self._set(state="parking", phase="park")
            self.halt.wait(grace_s)
        ok = self._park(reason)
        if close_roof:
            self._close_after_park(ok, "auto_close_on_unsafe")

    def _park(self, reason):
        self.scope.interrupt = None
        self._set(state="parking", phase="park")
        self._act("park", reason=reason)
        self.log(f"guarded park ({reason}): tracking off, RA to home {self.scope.ha_neutral:.0f} by axis moves, "
                 "then firmware Park under the RA guard")
        self._camsnap("park_before")
        r = self.scope.park_guarded(guard_deg=self.s.park_guard_deg)
        self._camsnap("park_after")
        if r["parked"]:
            self.pointing.note_parked("guarded park")
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
            self.pointing.note_parked("parked before unpark")    # the firmware has no sync now
            # Report "unparked" BEFORE the first motion (Unpark itself unfolds the arm), so a roof
            # controller never treats the pier as parked while it is moving.
            if self.s.hooks_unparked or self.rig.sim:
                self.log(f"on_unparked hook -> {self.rig.hook(self.s.hooks_unparked)}")
            self.log("unpark")
            t.Unpark()

    def _roof_action(self, action, result, ok):
        with self.lock:
            self.st["roof"]["last_action"] = {"action": action, "result": result, "ok": ok,
                                              "time": _iso(self.clock.time())}

    def _job_roof_close(self):
        if not self.scope.t.AtPark:
            self.log("roof close refused: scope not parked")
            self._roof_action("close", "refused: scope not parked", False)
            return
        self._close_roof("operator")

    def _close_roof(self, why):
        """Close and wait for closed. The caller has checked AtPark. Returns True when it reads closed."""
        self._set(phase="roof close")
        self._act("roof_close")
        self.log(f"roof: closing ({why})")
        try:
            st = self.rig.roof.close(timeout=self.s.roof_timeout_s, log=self.log)
        except Exception as e:  # noqa: BLE001 - refusals and controller errors
            st = f"error: {e}"
        ok = st in ("closed", "unknown")     # "unknown": an HTTP roof without a state URL
        self.log(f"roof: {st}" + ("" if ok else " -- ROOF NOT CONFIRMED CLOSED, check it"))
        with self.lock:
            self.st["roof"]["state"] = st
            if st == "closed":
                self.st["roof"]["close_requested"] = False
        self._roof_action(f"close ({why})", st, ok)
        self._set(phase=None)
        return ok

    def _close_after_park(self, park_ok, why):
        """Close the roof only after a verified park that the mount confirms with AtPark."""
        try:
            at_park = bool(self.scope.t.AtPark)
        except Exception as e:  # noqa: BLE001
            at_park, park_ok = False, False
            self.log(f"can't read AtPark: {e}")
        if not (park_ok and at_park):
            msg = "park not verified" if not park_ok else "mount doesn't report AtPark"
            self.log(f"roof NOT closed ({why}): {msg}. ROOF CLOSE REQUESTED: check the scope, then `roof close`")
            with self.lock:
                self.st["roof"].update(close_requested=True, reason=f"{why}: {msg}")
            self._roof_action(f"close ({why})", f"skipped: {msg}", False)
            return False
        if self.rig.roof.state() == "closed":
            self.log(f"roof already closed ({why})")
            with self.lock:
                self.st["roof"].update(state="closed", close_requested=False)
            return True
        return self._close_roof(why)

    def _job_roof_open(self):
        try:
            self._open_roof("operator")
        except (Aborted, RoofRefused, RuntimeError) as e:
            self.log(f"roof open failed: {e}")

    def _open_roof(self, why):
        """Open only while safety reports safe, then wait for open. Raises on refusal, unsafe or timeout."""
        why_not = self._unsafe_why()
        if why_not:
            self._roof_action(f"open ({why})", f"refused: {why_not}", False)
            raise RoofRefused(f"roof open refused: {why_not}")
        st = self.rig.roof.state()
        if st == "open":
            self.log(f"roof already open ({why})")
            with self.lock:
                self.st["roof"]["state"] = st
            return
        self._set(phase="roof open")
        self._act("roof_open")
        self.log(f"roof: opening ({why}), waiting up to {self.s.roof_timeout_s:.0f} s for open")
        try:
            st = self.rig.roof.open(timeout=self.s.roof_timeout_s, log=self.log, stop=self.abort.is_set)
        except RoofRefused as e:
            self._roof_action(f"open ({why})", str(e), False)
            self._set(phase=None)
            raise
        finally:
            with self.lock:
                self.st["roof"]["state"] = self.rig.roof.state()
        self.log(f"roof: {st}")
        self._set(phase=None)
        if self.abort.is_set():
            self._roof_action(f"open ({why})", f"interrupted ({st})", False)
            raise Aborted(f"roof open interrupted ({st})")
        if st != "open":
            self._roof_action(f"open ({why})", f"not open after {self.s.roof_timeout_s:.0f} s: {st}", False)
            raise RuntimeError(f"roof didn't report open within {self.s.roof_timeout_s:.0f} s (state {st})")
        self._roof_action(f"open ({why})", "open", True)

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
            self._notify()
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
        auto = bool(self.s.auto_close_on_unsafe and self.rig.roof is not None)
        if darks_parked and not auto:
            self.log("safety: scope parked and taking darks, darks continue")
        else:
            self.abort.set()
            self.imager.abort()
            self.wide.cancel("unsafe")
            held = self._revoke_lock("unsafe: " + ", ".join(reasons))
            self.jobs.put(("park", {"reason": "unsafe: " + ", ".join(reasons), "close_roof": auto,
                                    "grace_s": self.s.lock_unsafe_grace_s if held else 0.0}))
        if self.rig.roof is not None:
            try:
                rs = self.rig.roof.state()
            except Exception as e:  # noqa: BLE001
                rs = f"error: {e}"
            if rs != "closed":
                with self.lock:
                    self.st["roof"].update(close_requested=True, reason=", ".join(reasons), state=rs)
                if auto:
                    self.log("ROOF CLOSE REQUESTED: auto_close_on_unsafe is on, the roof closes after a verified park")
                else:
                    self.log("ROOF CLOSE REQUESTED (not automatic): after the park, the operator "
                             "confirms and runs `roof close`")

    # ---- monitor ----------------------------------------------------------------------------------
    def _monitor_loop(self):
        n = 0
        while not self.halt.is_set():
            try:
                self._lock_active()             # expires a lock that wasn't renewed
                self._monitor_once(roof=(n % 15 == 0))
            except Exception as e:  # noqa: BLE001
                self.log(f"monitor: {e}")
            try:
                self._night_guard()
            except Exception as e:  # noqa: BLE001
                self.log(f"night guard: {e}")
            n += 1
            self._write_status()
            self.halt.wait(self.s.monitor_s)

    def _monitor_once(self, roof=False):
        sc = self.scope
        L = self.hwlock
        if L is not None:
            # Scope lent out: don't touch the SeeStar at all. Polling it slows the holder's MoveAxis
            # calls (one Alpaca bridge), and the holder owns the cable bookkeeping (ra_unwrap.json) and
            # its own guards while it moves. Mount/cable fields are marked stale; roof and safety go on.
            with self.lock:
                for k in ("mount", "cable"):
                    if isinstance(self.st.get(k), dict):
                        self.st[k]["stale"] = f"not polled: scope locked by {L['owner']}"
        else:
            self._monitor_mount(sc)
        if roof and self.rig.roof is not None:
            rs = self.rig.roof.state()
            with self.lock:
                self.st["roof"]["state"] = rs
                if rs == "closed":
                    self.st["roof"]["close_requested"] = False

    def _monitor_mount(self, sc):
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
        self._notify()
        if tracking and u > sc.ha_u_max - 0.5 * HA_RATE_MIN:
            # backstop: the sequencer should have stopped well before this (not armed while the scope
            # is lent out: the lock holder's own guards apply then)
            t.Tracking = False
            self.abort.set()
            self.imager.abort()
            self.wide.cancel("cable watchdog")
            self.log(f"CABLE WATCHDOG: unwrapped HA {u:.1f} at the window limit {sc.ha_u_max:.1f}: "
                     "tracking off, job aborted")

    # ---- the plan ---------------------------------------------------------------------------------
    def _check_abort(self):
        if self.abort.is_set():
            raise Aborted("stopped")

    def _job_run(self):
        plan = self.plan
        self.scope.interrupt = self.abort.is_set
        self._set(state="running")
        self._night_stop, self._night_watch = None, True
        try:
            if plan.open_roof_at_start and any(p["status"] == "pending" for p in self.progress):
                try:
                    self._open_roof("plan start")
                except (RoofRefused, RuntimeError) as e:
                    self.log(f"plan stopped before the first target: {e}")
                    self._set(state="stopped", phase=None)
                    self.scope.interrupt = None
                    return
            for i, step in enumerate(plan.steps):
                pr = self.progress[i]
                if pr["status"] not in ("pending",):
                    continue
                self._check_abort()
                nf = self._night_failures(self.clock.time())
                if nf:
                    raise NightOver(nf)
                pr["status"] = "running"
                self._set(step=i, target=getattr(step, "name", None))
                self.log(f"step {i + 1}/{len(plan.steps)}: {step.describe()}")
                self._run_light(step, pr)
            self.log("plan finished")
            final = "done"
        except (NightOver, Aborted, Interrupted) as e:
            night = e if isinstance(e, NightOver) else self._night_stop   # the monitor stopped it
            if night is not None:
                self._night_watch = False
                return self._end_night(plan, night)
            self._night_watch = False
            for pr in self.progress:
                if pr["status"] == "running":
                    pr["status"] = "interrupted"
            self.log(f"plan interrupted: {e}")
            self._set(state="stopped", phase=None)
            self.scope.interrupt = None
            return
        self._night_watch = False
        self.scope.interrupt = None
        if plan.close_roof_at_end:
            # always through the guarded park, so "verified" means park_guarded said so (already-parked counts)
            self._close_after_park(self._park("end of plan"), "close_roof_at_end")
        elif plan.park_at_end and not self.scope.t.AtPark:
            self._park("end of plan")
        self._set(state=final if self.st["state"] != "error" else "error", phase=None, step=None)

    # ---- end of night -----------------------------------------------------------------------------
    def _night_failures(self, t, plan=None, end_t=None):
        """The plan's Sun / end_time constraints and the service night limit at t (stricter wins).
        Without a plan argument: the active plan and its end time."""
        if plan is None:
            plan, end_t = self.plan, self.plan_end_t
        return night_failures(plan.constraints if plan else None, t, self.s.lat, self.s.lon, end_t,
                              self.s.night_limit)

    def _night_why(self, plan, end_t):
        """Refusal message when a plan can't start (or resume) because its night is over, else None."""
        nf = self._night_failures(self.clock.time(), plan, end_t)
        if not nf:
            return None
        from .statusview import night_window
        _, opens, _ = night_window(self.s.lat, self.s.lon, plan.constraints, end_t, self.s.night_limit,
                                   self.clock.time())
        return ("refused: " + ("night limit reached" if limit_reached(nf) else "the plan's night is over")
                + f" ({'; '.join(nf)})" + (f"; imaging allowed again from {_iso(opens)}" if opens else ""))

    def _end_night(self, plan, e):
        """End of the night inside a plan. The plan's own constraint ends it like a finished plan; the
        service night limit parks whatever park_at_end says, then the roof: close_roof_at_end or
        auto_close_on_unsafe close it after a verified park, otherwise roof close requested."""
        self.log(f"night over: {e}" + (" -> plan stopped, parking (service night limit)" if e.limit else ""))
        for pr in self.progress:
            if pr["status"] in ("pending", "running", "interrupted"):
                pr["status"], pr["reason"] = "skipped", str(e)
        self.scope.interrupt = None
        if not e.limit:
            if plan.close_roof_at_end:
                self._close_after_park(self._park("end of plan"), "close_roof_at_end")
            elif plan.park_at_end and not self.scope.t.AtPark:
                self._park("end of plan")
        else:
            ok = self._park("night limit")
            self._night_roof(ok, plan.close_roof_at_end)
        self._set(state="done" if self.st["state"] != "error" else "error", phase=None, step=None)

    def _night_roof(self, park_ok, close_at_end=False):
        if self.rig.roof is None:
            return
        if close_at_end or self.s.auto_close_on_unsafe:
            self._close_after_park(park_ok, "close_roof_at_end" if close_at_end else "night limit")
            return
        rs = self.rig.roof.state()
        if rs != "closed":
            self.log("ROOF CLOSE REQUESTED (night limit): the scope is parked; `roof close` or the roof controller")
            with self.lock:
                self.st["roof"].update(close_requested=True, reason="night limit", state=rs)

    def _night_guard(self):
        """Monitor thread: the night limit (or the plan's own end) while a plan is between frames
        (slewing, centring, opening the roof...), and once at the moment the service limit is reached
        while idle with the scope out (a plan with park_at_end = false, a stopped plan, polar)."""
        now = self.clock.time()
        if self.busy == "run" and self._night_watch and self._night_stop is None:
            nf = self._night_failures(now)
            if nf:
                self._night_stop = NightOver(nf)
                self.log(f"night guard: {'; '.join(nf)}: stopping the plan")
                self.abort.set()
                self.imager.abort()
                self.wide.cancel("night limit")
        night = not night_failures(None, now, self.s.lat, self.s.lon, None, self.s.night_limit)
        was, self._was_night = self._was_night, night
        if not (was and not night) or self.busy not in (None, "polar") or not self.jobs.empty():
            return
        if self._lock_active() is not None:
            self.log("night limit reached while a script holds the scope lock: not parking; the holder must park")
            return
        if self.scope.t.AtPark:
            return
        self.log("night limit reached with the scope out: guarded park")
        if self.busy == "polar":
            self.abort.set()
            self.imager.abort()
        if self.rig.roof is not None and not self.s.auto_close_on_unsafe and self.rig.roof.state() != "closed":
            with self.lock:
                self.st["roof"].update(close_requested=True, reason="night limit")
        self.jobs.put(("park", {"reason": "night limit",
                                "close_roof": bool(self.s.auto_close_on_unsafe and self.rig.roof is not None)}))

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
        self._act("slew", target=step.name)
        self.imager.set_filter(step.filter)
        self._goto(ra_j, dec_j, correct=True, target=step.name)
        cen = self._centre(step, ra_j, dec_j)
        pr["centre"] = cen
        self._lights(step, pr, cen)

    def _finish(self, pr, status, reason=None):
        pr["status"], pr["reason"] = status, reason
        self.log(f"  -> {status}" + (f": {reason}" if reason else ""))

    def _polar_done_unix(self):
        """Time of the last stored polar alignment (pointing solves before it are ignored), or 0."""
        try:
            with open(os.path.join(self.polar.out, "results.jsonl")) as fh:
                lines = [ln for ln in fh if ln.strip()]
            return float(json.loads(lines[-1]).get("unix", 0.0)) if lines else 0.0
        except (OSError, ValueError, IndexError):
            return 0.0

    def _hadec_now(self, ra_deg, t=None):
        t = self.clock.time() if t is None else t
        return astro.wrap180(astro.lst_deg(t, self.s.lon) - ra_deg)

    def _goto(self, ra_j, dec_j, tol=0.3, correct=False, target=None):
        """Cable-safe move to JNow (ra, dec) and tracking on. Never a firmware goto.
        tol: per-axis stop tolerance (deg); centring passes a fine one.
        correct: offset the commanded position by the pointing memory (model at the target minus the
        current sync shift). Only for a target's first slew; centring's fine moves follow its own sync."""
        self._check_abort()
        ra_c, dec_c = ra_j, dec_j
        if correct:
            ha = self._hadec_now(ra_j)
            dh, dd, why = self.pointing.correction(ha, dec_j)
            if dh is None:
                self.pointing.last_correction = {"target": target, "applied": False, "why": why,
                                                 "time": _iso(self.clock.time())}
                self.log(f"pointing memory: no correction ({why})")
            else:
                # true = raw + model, reported = raw + shift  ->  command reported HA = HA_t - (model - shift)
                ra_c, dec_c = (ra_j + dh) % 360.0, dec_j - dd
                tol = min(tol, 0.05)      # worth landing close: the coarse 0.3 deg would leave 10-15'
                size = math.hypot(dh * math.cos(math.radians(dec_j)), dd) * 60.0
                self.pointing.last_correction = {"target": target, "applied": True, "model": why,
                                                 "dha_arcmin": round(dh * 60, 1), "ddec_arcmin": round(dd * 60, 1),
                                                 "arcmin": round(size, 1), "time": _iso(self.clock.time())}
                self.log(f"pointing memory ({why}): commanding HA {-dh * 60:+.1f}' Dec {-dd * 60:+.1f}' "
                         f"off the target ({size:.0f}')")
        p = self.scope.move_radec(ra_c, dec_c, tol=tol, track=True)   # moving target, Dec first, tracking on arrival
        self._check_abort()
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

    def _record_solve(self, step, sol, it):
        """Store a centring solve in the pointing memory: the mount's reported RA/Dec (read right after
        the frame; with tracking on it doesn't change) and the solved frame centre, both JNow, as HA/Dec
        at the same instant. Returns the solved JNow (ra, dec)."""
        t = self.clock.time()
        sra, sdec = astro.icrs_to_jnow(sol["ra"], sol["dec"], t)
        try:
            mra, mdec = float(self.scope.t.RightAscension) * 15.0, float(self.scope.t.Declination)
            self.pointing.add_solve(t, step.name, (self._hadec_now(mra, t), mdec), (self._hadec_now(sra, t), sdec),
                                    {"iteration": it + 1})
        except Exception as e:  # noqa: BLE001 - never let the log break centring
            self.log(f"pointing memory: solve not recorded: {e}")
        return sra, sdec

    def _sync(self, sra, sdec):
        """SyncToCoordinates in JNow, keeping the pointing memory's sync shift and the unwrapped-HA
        bookkeeping consistent: the sync moves the reported HA by (synced - reported); the next
        ha_unwrapped() read takes that jump into the bookkeeping (< 10 deg, see max_sync_deg)."""
        t = self.clock.time()
        mra, mdec = float(self.scope.t.RightAscension) * 15.0, float(self.scope.t.Declination)
        u0 = self.scope.ha_unwrapped()
        self.scope.t.SyncToCoordinates(sra / 15.0, sdec)
        self.pointing.note_sync((self._hadec_now(mra, t), mdec), (self._hadec_now(sra, t), sdec))
        u1 = self.scope.ha_unwrapped()
        self.log(f"synced (JNow); unwrapped HA bookkeeping {u0:.2f} -> {u1:.2f}")

    def _centre_solve(self, step, ra_j, dec_j, max_iter=4, max_sync_deg=10.0, tol_arcmin=None):
        """Telephoto solve -> sync (JNow) -> re-centre, until within tolerance (from tools/sync_center.py).
        The result carries the last solve's orientation ("cd"), which dithering uses to predict moves."""
        tol = step.centre_tol_arcmin if tol_arcmin is None else tol_arcmin
        off = first = cd = None
        for it in range(max_iter):
            self._check_abort()
            self._act("centre", target=step.name, solve=it + 1, of=max_iter,
                      off_arcmin=round(off, 1) if off is not None else None)
            _, st, sol = self._solve_frame(step)
            if sol is None:
                return {"method": "solve", "solved": False, "why": f"no solution ({len(st)} stars)",
                        "first_offset_arcmin": first}
            off = astro.separation(sol["ra"], sol["dec"], step.ra, step.dec) * 60.0
            first = round(off, 2) if first is None else first
            cd = sol.get("cd", cd)
            self.log(f"solve {it + 1}: RA {sol['ra']:.3f} Dec {sol['dec']:.3f} (J2000), {off:.1f}' from target")
            sra, sdec = self._record_solve(step, sol, it)
            if off <= tol:
                return {"method": "solve", "solved": True, "offset_arcmin": round(off, 2), "iterations": it + 1,
                        "first_offset_arcmin": first, "cd": cd}
            if off / 60.0 > max_sync_deg:
                return {"method": "solve", "solved": False, "why": f"solution {off / 60:.1f} deg off: not syncing",
                        "first_offset_arcmin": first}
            self._sync(sra, sdec)
            # fine move: the axis tolerance must be well inside the centring tolerance. The HA axis
            # tolerance is in HA degrees, so scale by cos(dec) to get sky degrees.
            fine = max(0.01, tol / 60.0 / 3.0)
            cosd = max(math.cos(math.radians(dec_j)), 0.2)
            try:
                self._goto(ra_j, dec_j, tol=min(fine, fine / cosd))
            except RuntimeError as e:
                self.log(f"fine centring move did not converge: {e}")
        return {"method": "solve", "solved": True, "converged": False,
                "offset_arcmin": round(off, 2) if off is not None else None, "iterations": max_iter,
                "first_offset_arcmin": first, "cd": cd}

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
        self._act("centre", target=step.name, method="blob")
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
        """The target's lights, with the wide camera alongside (sequencer/wide.py): whatever way the loop
        ends, a wide exposure still running is aborted; a finished one is downloaded on a normal end."""
        try:
            r = self._lights_loop(step, pr, cen)
        except BaseException as e:
            self.wide.end_target(normal=False, why=f"{type(e).__name__}: {e}" if str(e) else type(e).__name__)
            raise
        self.wide.end_target(normal=True)
        return r

    def _lights_loop(self, step: LightStep, pr, cen):
        c, S = self.plan.constraints, self.s
        base, tdir = self._target_dirs(step)
        ctx = self._stack_for(step, base)
        D = self._dither_state(step, ctx, cen)
        self._set(phase="lights")
        while pr["frames"] < step.count:
            self._check_abort()
            k = pr["frames"]
            now = self.clock.time()
            t_end = now + step.exposure + S.frame_overhead_s
            nf = self._night_failures(t_end)
            if nf:
                raise NightOver(nf)
            fails = target_failures(c, step.ra, step.dec, t_end, S.lat, S.lon, step.min_alt)
            if fails:
                return self._finish(pr, "stopped", "; ".join(fails))
            if D is not None and k > 0 and k % step.dither_every == 0:
                self._dither(step, ctx, k)
                self._set(phase="lights")
            u = self.scope.ha_unwrapped()             # after the dither: the cable check sees its move
            left = self._cable_left_min(u)
            if left < self._frame_minutes(step.exposure) + S.cable_reserve_min:
                self.wide.end_target(normal=True, why="cable window")    # before tracking goes off
                self.scope.t.Tracking = False
                return self._finish(pr, "stopped", f"cable window: {left:.1f} min of tracking left "
                                    f"(unwrapped HA {u:.1f}, limit {self.scope.ha_u_max:.1f}); tracking off")
            pos = self.scope.position()
            t_obs = self.clock.time()
            date_obs = _iso(t_obs)
            temp = self.imager.temperature()
            self._act("expose", target=step.name, frame=k + 1, count=step.count,
                      start=round(self.clock.time(), 2), exposure=float(step.exposure))
            wmeta = {"pos": pos, "u": u, "cen": cen, "tdir": tdir, "dither": self._dither_cards(ctx)}

            def ready(k=k):
                self.wide.tele_done()
                self._act("download", target=step.name, frame=k + 1, count=step.count)
            d = self.imager.expose(step.exposure, step.gain, self.abort, on_ready=ready,
                                   on_started=lambda k=k: self.wide.tele_started(step, ctx, k, wmeta))
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
            dh = None
            if D is not None:
                dh = self._dither_measured(step, ctx, entry, k, now, dy, dx)
                rate = imaging.drift_rate(ctx["drift"], keys=("ddx_as", "ddy_as"))
            else:
                rate = imaging.drift_rate(ctx["drift"])
            name = f"{_safe_name(step.name)}_{step.exposure:g}s_g{step.gain}_{step.filter}_{k + 1:03d}.fits"
            path = os.path.join(tdir, "lights", name)
            imaging.save_fits(path, d, self._header(step.exposure, step.gain, step.filter, "LIGHT", date_obs,
                                                         temp, step, pos, q, u, cen,
                                                         dark=dm["id"] if dm else "NONE", dither=dh, t_start=t_obs))
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

    # ---- dithering (see sequencer/dither.py) ------------------------------------------------------
    def _dither_state(self, step, ctx, cen):
        """Per-target dither state, kept with the stack so a resumed target continues its pattern. Every
        call follows a slew + centring, so the commanded offset starts at the centre again."""
        if not step.dither:
            return None
        a0 = dith.shift_matrix((cen or {}).get("cd"))
        D = ctx.get("dither")
        if D is None:
            seed = int.from_bytes(step.name.encode()[:8].ljust(8, b"\0"), "little") & 0x7FFFFFFF
            D = ctx["dither"] = {"pattern": dith.Pattern(step.dither_px, seed=seed), "offset": (0.0, 0.0),
                                 "n": 0, "seg": 0, "dec_sign": 0, "last": None,
                                 "resp": dith.Response(a0)}
        else:
            D["offset"], D["seg"] = (0.0, 0.0), D["seg"] + 1
            if a0 is not None:
                D["resp"].a0 = a0
        return D

    def _dither(self, step, ctx, k):
        """Move to the next pattern position (offset from the centred position) by timed MoveAxis pulses
        with tracking on, then settle. A move the cable window or Dec range doesn't allow is skipped.
        The wide camera is kept off the mount's moves (and the Alpaca bridge) until the settle is over."""
        self.wide.quiet("dither")
        try:
            self._dither_move(step, ctx, k)
        finally:
            self.wide.moved()

    def _dither_cards(self, ctx):
        """Commanded dither offset for a wide frame's header (the telephoto has the measured one too)."""
        D = ctx.get("dither")
        if not D:
            return None
        return {"DITHN": (D["n"], "dithers so far on this target"),
                "DITHRA": (round(D["offset"][0], 1), "[arcsec] commanded offset E of centre"),
                "DITHDEC": (round(D["offset"][1], 1), "[arcsec] commanded offset N of centre")}

    def _dither_move(self, step, ctx, k):
        D, S, cfg = ctx["dither"], self.s, self.dither_cfg
        D["n"] += 1
        n, resp = D["n"], D["resp"]
        why = None
        drift = resp.segment_drift_px()
        if step.dither_recentre_every and n % step.dither_recentre_every == 0:
            why = f"every {step.dither_recentre_every} dithers"
        elif step.dither_recentre_px and drift is not None and drift > step.dither_recentre_px:
            why = f"drift {drift:.0f} px since the last centring"
        if why:
            self._recentre(step, D, why)
        px = S.pix_arcsec
        cur = (D["offset"][0] / px, D["offset"][1] / px)
        tgt = D["pattern"].next(cur)
        de, dn = (tgt[0] - cur[0]) * px, (tgt[1] - cur[1]) * px
        size = math.hypot(de, dn) / px
        self._set(phase="dither")
        self._act("dither", target=step.name, n=n, px=round(size, 1))
        p = self.scope.position()
        du, ddec = dith.axis_moves(de, dn, p["dec"])
        t = self.scope.t
        tracking = bool(t.Tracking)
        done_e = done_n = 0.0
        try:
            for axis, rate, secs, lead, base in dith.pulses(cfg, du, ddec, tracking, D["dec_sign"]):
                self.scope.pulse_axis(PRIMARY if axis == 0 else SECONDARY, rate, secs, lead_s=lead,
                                      runaway_s=cfg.runaway_s, base_rate=base, max_s=cfg.max_pulse_s)
                if axis == 0:
                    done_e = de
                else:
                    done_n, D["dec_sign"] = dn, (1 if rate > 0 else -1)
        except (CableLimit, ValueError) as e:
            self.log(f"dither {n} skipped: {e}")
        finally:
            D["offset"] = (D["offset"][0] + done_e, D["offset"][1] + done_n)
        if tracking and not t.Tracking:
            self.log("dither: tracking was off after the pulse, back on")
            t.Tracking = True
        D["last"] = {"n": n, "frame": k + 1, "cmd_px": round(math.hypot(done_e, done_n) / px, 1),
                     "move_as": (done_e, done_n)}
        if done_e or done_n:
            self.log(f"dither {n}: {size:.1f} px to ({D['offset'][0] / px:+.1f}, {D['offset'][1] / px:+.1f}) px "
                     f"E/N of centre (axes: HA {du * 3600:+.0f}\", Dec {ddec * 3600:+.0f}\")")
        t_end = self.clock.time() + step.dither_settle_s
        while self.clock.time() < t_end:
            self._check_abort()
            self.clock.sleep(min(0.5, max(0.0, t_end - self.clock.time())))
        self._check_abort()

    def _recentre(self, step, D, why):
        """Plate-solve re-centre on the target (sync + closed-loop fine move, like the first centring)."""
        if step.centre in ("none", "blob") or not getattr(self.rig.solver, "available", False):
            self.log(f"re-centre ({why}) skipped: needs centre = auto/solve and a plate solver")
            return False
        self.log(f"re-centre ({why})")
        ra_j, dec_j = astro.icrs_to_jnow(step.ra, step.dec, self.clock.time())
        r = self._centre_solve(step, ra_j, dec_j, tol_arcmin=min(step.centre_tol_arcmin, 1.0))
        a0 = dith.shift_matrix(r.get("cd"))
        if a0 is not None:
            D["resp"].a0 = a0
        if r.get("solved") and r.get("iterations", 1) > 1:     # it moved: back at the centre
            D["offset"], D["seg"] = (0.0, 0.0), D["seg"] + 1
            return True
        return False

    def _dither_measured(self, step, ctx, entry, k, now, dy, dx):
        """Book the frame's registration offset against the commanded dither offset: frame-log fields,
        the dither response (achieved vs commanded), the drift path without the dithers, FITS headers."""
        D, px = ctx["dither"], self.s.pix_arcsec
        resp = D["resp"]
        mx, my = 2.0 * dx, 2.0 * dy                       # green superpixels -> full-resolution px
        resp.add((now - ctx["t0"]) / 60.0, D["offset"], (mx, my), D["seg"])
        entry.update(dither_n=D["n"], dither_e_px=round(D["offset"][0] / px, 1),
                     dither_n_px=round(D["offset"][1] / px, 1), dither_x_px=round(mx, 1), dither_y_px=round(my, 1))
        path = resp.drift_path()
        with self.lock:                                   # the entries are also in st["frames"]
            for e, pt in zip(ctx["drift"], path or []):
                e["ddx_as"], e["ddy_as"] = round(float(pt[0]) * px, 1), round(float(pt[1]) * px, 1)
            if path is None:
                entry["ddx_as"] = entry["ddy_as"] = None
        last = D.get("last")
        if last and last["frame"] == k + 1 and len(resp.frames) >= 2 and resp.frames[-2][3] == D["seg"] \
                and last["cmd_px"] > 0:
            f0, f1 = resp.frames[-2], resp.frames[-1]
            meas = f1[2] - f0[2]
            pred = resp.predicted(last["move_as"])
            if resp.fit is not None:
                meas = meas - resp.fit["v_px_min"] * (f1[0] - f0[0])
            last["meas_px"] = round(float(np.hypot(*meas)), 1)
            msg = f"dither {last['n']} measured: {last['meas_px']:.1f} px (commanded {last['cmd_px']:.1f} px"
            if pred is not None:
                last["err_px"] = round(float(np.hypot(*(meas - pred))), 1)
                msg += f", off the prediction by {last['err_px']:.1f} px"
            self.log(msg + ")")
        sm = resp.summary()
        with self.lock:
            self.st["dither"] = {"target": step.name, "n": D["n"], "offset_px": [round(D["offset"][0] / px, 1),
                                                                                 round(D["offset"][1] / px, 1)],
                                 "last": {k2: v for k2, v in (last or {}).items() if k2 != "move_as"} or None,
                                 **sm}
        return {"DITHN": (D["n"], "dithers so far on this target"),
                "DITHRA": (round(D["offset"][0], 1), "[arcsec] commanded offset E of centre"),
                "DITHDEC": (round(D["offset"][1], 1), "[arcsec] commanded offset N of centre"),
                "DITHERX": (round(mx, 2), "[px] measured x shift vs reference"),
                "DITHERY": (round(my, 2), "[px] measured y shift vs reference")}

    def _header(self, exp, gain, filt, kind, date_obs, temp, step=None, pos=None, q=None, u=None, cen=None,
                dark=None, dither=None, t_start=None, cam="tele"):
        """FITS header of a telephoto (cam="tele") or wide-camera (cam="wide") frame. t_start: exposure
        start (unix) for DATE-AVG (mid-exposure) and DATE-END."""
        S = self.s
        if cam == "wide":
            im, inst, focal, pix = self.rig.wide, "SeeStar S30 Pro wide", self.wide_cfg.focal_mm, self.wide_cfg.pixel_um
            camnum = self.wide_cfg.camera
        else:
            im, inst, focal, pix, camnum = self.imager, S.instrument, S.focal_mm, S.pixel_um, 0
        h = {"DATE-OBS": date_obs, "EXPTIME": float(exp), "GAIN": int(gain), "IMAGETYP": kind,
             "BAYERPAT": "GRBG", "CCD-TEMP": temp, "FILTER": filt,
             "INSTRUME": inst, "FOCALLEN": focal, "XPIXSZ": pix, "YPIXSZ": pix,
             "CAMERA": (cam, "tele or wide"), "CAMNUM": (camnum, "Alpaca camera number"),
             "SITELAT": S.lat, "SITELONG": S.lon, "FOCUSPOS": im.focus_pos(),
             "SWCREATE": "seestar-commander sequencer v1", "PLAN": self.plan.name if self.plan else None,
             "OFFSET": im.offset(), "XBINNING": im.binning(), "DARKUSED": dark}
        if t_start is not None:
            h["DATE-AVG"] = (_iso(t_start + float(exp) / 2.0), "mid-exposure (UTC)")
            h["DATE-END"] = (_iso(t_start + float(exp)), "exposure end (UTC)")
        if kind == "LIGHT" and step is not None:
            # target (J2000): OBJCTRA/OBJCTDEC sexagesimal (RA in hours), RA/DEC numeric in degrees.
            # The mount's own pointing (JNow) is in MNT_*.
            h.update({"OBJECT": step.name,
                      "OBJCTRA": (astro.ra_sexagesimal(step.ra), "target RA (J2000), hours"),
                      "OBJCTDEC": (astro.dec_sexagesimal(step.dec), "target Dec (J2000), degrees"),
                      "RA": (round(step.ra, 6), "target RA (J2000), degrees"),
                      "DEC": (round(step.dec, 6), "target Dec (J2000), degrees"), "EQUINOX": 2000.0,
                      "MNT_RA": round(pos["ra_h"], 6), "MNT_DEC": round(pos["dec"], 5), "MNT_HA": round(pos["ha"], 4),
                      "MNT_ALT": round(pos["alt"], 3), "MNT_AZ": round(pos["az"], 3),
                      "CABLE_U": round(u, 3) if u is not None else None,
                      "CENTRED": (cen or {}).get("method")})
            if q:
                h.update({"STARS": q["stars"], "HFR": q["hfr_px"], "ECC": q["ecc"], "BGMED": q["bg"]})
            if dither:
                h.update(dither)
        return h

    def _wide_header(self, j, temp):
        """Header of a wide frame (wide thread, right after its download): the telephoto frame's mount,
        target and cable data, the wide exposure's own times."""
        m, step = j["meta"], j["step"]
        h = self._header(j["exp"], j["gain"], None, "LIGHT", _iso(j["t0"]), temp, step, m["pos"], None, m["u"],
                         m["cen"], dither=m.get("dither"), t_start=j["t0"], cam="wide")
        h["TELEFRM"] = (j["k"] + 1, "telephoto light taken alongside")
        return h

    def _img(self, key):
        with self.lock:
            self.st["images"][key] = round(self.clock.time(), 1)
        self._notify()

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
                    self._act("dark", label=key.label(), frame=k + 1, count=n,
                              start=round(self.clock.time(), 2), exposure=float(key.exposure))
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
