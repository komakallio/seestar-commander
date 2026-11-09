"""Dark harvest: while the observatory is idle, take dark frames at whatever temperature the uncooled
sensor happens to be, so the dark library fills across temperature bins (frames go through the frame
pool, sequencer/darkpool.py). A thread inside the service; it uses the telephoto camera and filter wheel
only, never the mount, never the scope lock, and never shows as busy: everything else wins at once.

When (all of): enabled; no plan or job running or queued; no scope lock (API lock, or a script holding
the file lock <data>/scope.lock); roof closed if a roof is configured (a roof that reports "unknown" counts
as closed: then only parked is required); scope at park; the telephoto camera idle (Alpaca CameraState,
so the vendor app or a tool that doesn't take the lock isn't disturbed); the optional external busy check
says idle; and `holdoff_min` has passed since any of these last blocked it.

What, one frame at a time: read the sensor temperature T -> bin b, then the first of
  1. keys the active and planned plans need (Service.dark_keys) with no master in b;
  2. those keys with an expired or expiring master in b;
  3. the sentinel key (60 s g200) in the most common bin when its master there is older than every_days
     (its masters are kept in the archive: the time series for the ageing analysis);
  4. the standard set (gain x exposures) missing or expiring in b;
  5. bias frames (bias_exposure, 1 ms) missing or expiring in b.
Within a tier: partly filled pools first (fullest first), then short exposures when T is in the top half of
its bin and long ones in the bottom half (less heat where the bin edge is near); an exposure whose predicted
warm-up (the last frame's rate x exposure) would exceed max_temp_change_c is skipped while a shorter one
exists. T is re-read after every frame. Nothing to do in b -> the camera idles and cools (lower bins).

Pre-emption: the worker calls preempt() for every job it starts (plan, flats, darks build, polar, park, roof
open, ...) and cmd_lock for every lock it grants: the exposure is aborted (AbortExposure, the partial frame
is discarded, finished ones stay in their pools), the wheel goes back, and preempt() returns when the camera
and wheel are free (in ~1 s; it never refuses). Hold-off: holdoff_min after the last blocker.

Stall: enabled, service idle, scope parked, the current bin has work and no frame for stall_hours -> the
status says "stalled" with the reason (what is blocking it).

Restarts: on shutdown the frame in flight is aborted and the wheel put back (bounded to ~2 s). While a frame
exposes, <data>/darkharvest/inflight.json names it (start, exposure, the wheel's previous filter); if the
process died without cleaning up and the camera is still exposing at the next start, a marker whose frame
started within exposure + 60 s means the exposure is our own: it is aborted and the wheel restored, without
the "camera busy" hold-off (an older marker, or none, is treated as someone else's exposure).

Files: <data>/darkharvest/state.json   {"enabled": ..., "set": ISO, "by": ...}: overrides the config
       <data>/darkharvest/inflight.json  the frame being exposed (deleted when it ends)
       <data>/darkharvest/log.jsonl    every decision with its reason, every frame, master, pre-emption
       <data>/darkharvest/counters.json  per UTC day: frames, masters, seconds harvesting / pre-empted
Config [darks.harvest]: see HarvestConfig.
"""
import datetime as dt
import json
import os
import threading
import time
import urllib.request
from dataclasses import dataclass, field

import numpy as np

from .darklib import Key, tbin
from .rig import Aborted

STATES = ("off", "harvesting", "waiting_cool", "paused", "holdoff", "no_reading", "error")


def _iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d")


def _bl(tb):
    return "none" if tb is None else f"{tb:+.0f}"


@dataclass
class HarvestConfig:
    enabled: bool = False
    gain: int = 200
    exposures: list = field(default_factory=lambda: [10.0, 15.0, 20.0, 30.0, 60.0, 120.0])
    count: int = 15
    bias: bool = True
    bias_exposure: float = 0.001
    sentinel: dict = field(default_factory=lambda: {"exposure": 60.0, "gain": 200, "every_days": 3.0})
    holdoff_min: float = 10.0
    stall_hours: float = 2.0
    pool_max_age_days: float = 14.0          # read by the frame pool
    max_temp_change_c: float = 0.5           # read by the frame pool
    poll_s: float = 30.0                     # re-check this often while waiting or blocked
    busy_url: str = ""                       # optional external busy check: GET -> JSON
    busy_path: str = "busy"                  # dot path to a value that is truthy while busy
    log_lines: int = 50                      # log entries in the status

    @classmethod
    def from_dict(cls, d):
        d = dict(d or {})
        c = cls()
        for k in list(vars(c)):
            if k in d:
                setattr(c, k, d[k])
        c.exposures = [float(x) for x in c.exposures]
        c.sentinel = {"exposure": 60.0, "gain": int(c.gain), "every_days": 3.0, **dict(c.sentinel or {})}
        c.gain, c.count = int(c.gain), int(c.count)
        return c


class Harvester:
    def __init__(self, svc, cfg: HarvestConfig):
        self.svc, self.cfg = svc, cfg
        self.clock = svc.clock
        self.dir = os.path.join(svc.s.data_dir, "darkharvest")
        os.makedirs(self.dir, exist_ok=True)
        self.state_path = os.path.join(self.dir, "state.json")
        self.log_path = os.path.join(self.dir, "log.jsonl")
        self.counters_path = os.path.join(self.dir, "counters.json")
        self.inflight_path = os.path.join(self.dir, "inflight.json")
        self._orphan = self._load_json(self.inflight_path, None)   # a frame a previous process left behind
        self.busy_check = None                 # optional callable -> reason str (busy) or None (idle)
        self.gate = threading.Lock()           # frame start vs. preempt
        self.stop = threading.Event()          # abort the frame in progress
        self.free = threading.Event()          # camera and wheel not in use by the harvester
        self.free.set()
        self.wake = threading.Event()
        self.active = False                    # a series is on (wheel on Dark)
        self._prev_filter = None
        self._pending = set()                  # (key, bin) whose master is being built
        self._build_q = []
        self._build_cv = threading.Condition()
        self.state, self.reason, self.headline = "off", None, "Off"
        self.state_since = self.clock.time()
        self.current = None                    # chosen key and why
        self.blockers = []
        self.last_block = None                 # last time a blocker was seen (hold-off)
        self.last_frame_unix = None
        self.last_rate = None                  # C/s during the last frame, and when it ended
        self.started_unix = self.clock.time()
        self.enabled_since = self.clock.time()
        self._decision = None
        self._tick_t = None
        self._counters = self._load_json(self.counters_path, {}) or {}
        self._counters_saved = 0.0
        self._summary_day = _day(self.clock.time())
        self._common = (None, 0.0)             # (most common bin, computed at)
        self._log_tail = []
        self._threads = []
        st = self._load_json(self.state_path, None)
        self.enabled_source = "state" if isinstance(st, dict) and "enabled" in st else "config"
        self.enabled = bool(st["enabled"]) if self.enabled_source == "state" else bool(cfg.enabled)
        for rec in self._read_log_tail(max(cfg.log_lines, 200)):
            self._log_tail.append(rec)
            if rec.get("kind") == "frame" and rec.get("unix"):
                self.last_frame_unix = rec["unix"]

    # ---- lifecycle -------------------------------------------------------------------------------
    def start(self):
        for name, fn in (("harvest", self._loop), ("harvest-build", self._builder)):
            th = threading.Thread(target=fn, name=name, daemon=True)
            th.start()
            self._threads.append(th)
        self.svc.log(f"dark harvest: {'ON' if self.enabled else 'off'} ({self.enabled_source}); "
                     f"{self.cfg.gain} gain x {[f'{e:g}' for e in self.cfg.exposures]} s"
                     + (", bias" if self.cfg.bias else "") + f", {self.cfg.count} frames a master")
        return self

    def shutdown(self, timeout_s=2.0):
        """Service stopping: abort the frame in flight and put the wheel back, waiting at most timeout_s."""
        self.stop.set()
        self.wake.set()
        with self._build_cv:
            self._build_cv.notify_all()
        if self.active or not self.free.is_set():
            if self.svc.imager.exposing:
                self.svc.imager.abort()
            if not self.free.wait(timeout_s):
                self.svc.log("dark harvest: camera/wheel not released within "
                             f"{timeout_s:.0f} s of shutdown")

    # ---- restart recovery ------------------------------------------------------------------------
    def _write_inflight(self, key, t0):
        try:
            with open(self.inflight_path + ".tmp", "w") as fh:
                json.dump({"started_unix": round(t0, 3), "started": _iso(t0), "exposure": key.exposure,
                           "gain": key.gain, "label": key.label(), "prev_filter": self._prev_filter,
                           "pid": os.getpid()}, fh)
            os.replace(self.inflight_path + ".tmp", self.inflight_path)
        except OSError:
            pass

    def _clear_inflight(self):
        try:
            os.remove(self.inflight_path)
        except OSError:
            pass

    def _orphan_ours(self, o, now=None):
        """The marker a previous process left names a frame that could still be exposing."""
        if not isinstance(o, dict):
            return False
        now = self.clock.time() if now is None else now
        try:
            return float(o["started_unix"]) <= now <= float(o["started_unix"]) + float(o["exposure"]) + 60.0
        except (KeyError, TypeError, ValueError):
            return False

    def _recover(self):
        """After a restart: abort our own frame if the camera still exposes it, put the wheel back."""
        o, self._orphan = self._orphan, None
        self._clear_inflight()
        im = self.svc.imager
        try:
            cs = int(im.c.CameraState)
        except Exception:  # noqa: BLE001
            cs = None
        ours = self._orphan_ours(o)
        if cs not in (None, 0) and ours:
            im.abort()
            self._event("recover", f"aborted our own {o.get('label')} frame left exposing by the previous "
                        f"process (started {o.get('started')})", aborted=True)
            self.svc.log(f"dark harvest: aborted the {o.get('label')} frame the previous process left exposing")
        prev = o.get("prev_filter")
        try:
            if ours and prev and prev != "Dark" and prev in im.fw.Names and im.filter_name() == "Dark":
                im.set_filter(prev)
                self._event("recover", f"filter wheel back on {prev} (left on Dark by the previous process)")
        except Exception as e:  # noqa: BLE001
            self._event("recover", f"filter wheel back: {e}")

    def set_enabled(self, on, by="api"):
        on = bool(on)
        now = self.clock.time()
        try:
            with open(self.state_path + ".tmp", "w") as fh:
                json.dump({"enabled": on, "set": _iso(now), "set_unix": round(now, 1), "by": by}, fh)
            os.replace(self.state_path + ".tmp", self.state_path)
        except OSError as e:
            return {"ok": False, "msg": f"can't save {self.state_path}: {e}"}
        was, self.enabled, self.enabled_source = self.enabled, on, "state"
        if on and not was:
            self.enabled_since = now
        self._event("state", f"switched {'on' if on else 'off'} by {by}", enabled=on)
        self.svc.log(f"dark harvest switched {'ON' if on else 'off'} ({by})")
        if not on:
            self.preempt("switched off", holdoff=False)
        self.wake.set()
        return {"ok": True, "msg": f"dark harvest {'on' if on else 'off'}", "enabled": on}

    # ---- pre-emption -----------------------------------------------------------------------------
    def preempt(self, why, holdoff=True, timeout_s=10.0):
        """Stop the harvest now: abort the exposure, wheel back. Returns when the camera and wheel are free
        (or after timeout_s of real time). Never raises, never refuses."""
        with self.gate:
            if holdoff:
                self.last_block = self.clock.time()
            was = self.active or not self.free.is_set()
            if was:
                self.stop.set()
                if self.svc.imager.exposing:
                    self.svc.imager.abort()
        if was:
            self._event("preempt", f"pre-empted by {why}")
            self.free.wait(timeout_s)
        self.wake.set()
        return was

    def note_block(self):
        """A plan, job or lock just ended (or is going on): the hold-off counts from now."""
        self.last_block = self.clock.time()
        self.wake.set()

    # ---- conditions --------------------------------------------------------------------------------
    def _service_blocker(self):
        """Cheap checks (no device I/O): a reason string, or None."""
        svc = self.svc
        if svc.halt.is_set():
            return "service stopping"
        if svc.busy is not None:
            return f"plan {svc.plan.name}" if svc.busy == "run" and svc.plan else f"job {svc.busy}"
        if not svc.jobs.empty():
            return "job queued"
        L = svc.hwlock
        if L is not None:
            return f"scope lock by {L['owner']}"
        if not svc.flock.held:
            h = svc.flock.holder()
            if h:
                return f"scope lock by {h.get('owner', '?')} (file lock)"
        return None

    def _external_blocker(self):
        if self.busy_check is not None:
            try:
                r = self.busy_check()
            except Exception as e:  # noqa: BLE001
                return f"busy check failed: {e}"
            if r:
                return str(r) if not isinstance(r, bool) else "external busy check"
        if self.cfg.busy_url:
            try:
                req = urllib.request.Request(self.cfg.busy_url, headers={"User-Agent": "seestar-commander"})
                with urllib.request.urlopen(req, timeout=3) as resp:
                    doc = json.loads(resp.read())
                from .templog import dig
                v = dig(doc, self.cfg.busy_path)
            except Exception as e:  # noqa: BLE001
                return f"busy_url unreachable: {e}"
            if v:
                return f"external busy ({self.cfg.busy_path} = {v})"
        return None

    def _device_blockers(self):
        """Roof, park, camera: (reasons that hold off, reasons that only pause)."""
        hold, pause = [], []
        rig = self.svc.rig
        if rig.roof is not None:
            try:
                rs = rig.roof.state()
            except Exception as e:  # noqa: BLE001
                rs = f"error: {e}"
            if rs not in ("closed", "unknown"):
                pause.append(f"roof {rs}")
        try:
            if not bool(self.svc.scope.t.AtPark):
                pause.append("scope not parked")
        except Exception as e:  # noqa: BLE001
            pause.append(f"can't read AtPark: {e}")
        if not self.active:
            try:
                cs = int(self.svc.imager.c.CameraState)
            except Exception as e:  # noqa: BLE001
                cs = None
                pause.append(f"can't read CameraState: {e}")
            if cs not in (None, 0) or self.svc.imager.exposing:
                hold.append(f"telephoto camera busy (CameraState {cs})")
        return hold, pause

    def check(self):
        """(hold-off reasons, pause reasons): everything that stops harvesting now."""
        hold, pause = [], []
        b = self._service_blocker()
        if b:
            hold.append(b)
            return hold, pause                 # don't touch the SeeStar while it's someone else's
        e = self._external_blocker()
        if e:
            hold.append(e)
        h, p = self._device_blockers()
        return hold + h, pause + p

    # ---- library state per bin ----------------------------------------------------------------------
    def keys(self):
        """[(key, tier-kind)] the harvester knows: plan keys, the standard set, the sentinel, bias."""
        svc, c = self.svc, self.cfg
        out = []
        try:
            plan_keys = svc.dark_keys()
        except Exception:  # noqa: BLE001
            plan_keys = []
        for k in plan_keys:
            out.append((k, "plan"))
        for e in c.exposures:
            k = svc.dark_key(e, c.gain)
            if k not in [x for x, _ in out]:
                out.append((k, "standard"))
        sk = self.sentinel_key()
        if sk not in [x for x, _ in out]:
            out.append((sk, "sentinel"))
        if c.bias:
            out.append((svc.dark_key(c.bias_exposure, c.gain), "bias"))
        return out

    def sentinel_key(self):
        s = self.cfg.sentinel
        return self.svc.dark_key(float(s["exposure"]), int(s["gain"]))

    def _entries(self):
        idx = {}
        for m in self.svc.darklib.entries():
            idx[(self.svc.darklib.key_of(m), m.get("temp_bin"))] = m
        return idx

    def common_bin(self):
        """The bin the sensor sits in most (temperature log, last 7 days; else the archive's frames)."""
        b, t = self._common
        now = self.clock.time()
        if t and now - t < 3600:
            return b
        counts = {}
        try:
            for r in self.svc.templog.rows(24 * 7):
                if r.get("sensor_c") is not None:
                    tb = tbin(r["sensor_c"], self.svc.darklib.policy.temp_bin_c)
                    counts[tb] = counts.get(tb, 0) + 1
        except Exception:  # noqa: BLE001
            pass
        if not counts:
            P = self.svc.darkpool
            with P._lock:
                recs = list(P.frames.values())
            for r in recs:
                tb = P.frame_tbin(r)
                if tb is not None:
                    counts[tb] = counts.get(tb, 0) + 1
        b = max(counts, key=lambda k: (counts[k], -abs(k))) if counts else None
        self._common = (b, now)
        return b

    def needs(self, tb, entries=None, keys=None):
        """Work in bin tb: [{key, kind, tier, why, pool_n}] in tier order (1 best)."""
        entries = self._entries() if entries is None else entries
        keys = self.keys() if keys is None else keys
        P, c = self.svc.darkpool, self.cfg
        out = []
        common = self.common_bin()
        for k, kind in keys:
            if (k, tb) in self._pending:
                continue
            m = entries.get((k, tb))
            st = m["status"] if m else "missing"
            tier, why = None, None
            if kind == "plan":
                if st == "missing":
                    tier, why = 1, "a plan needs it, missing"
                elif st in ("expired", "expiring"):
                    tier, why = 2, f"a plan needs it, {st}"
            if tier is None and k == self.sentinel_key() and tb is not None and tb == common:
                if m is None or m["age_days"] > float(c.sentinel["every_days"]):
                    tier, why = 3, ("sentinel, none yet" if m is None else
                                    f"sentinel, last {m['age_days']:.1f} d ago (every {c.sentinel['every_days']:g} d)")
            if tier is None and kind in ("standard", "sentinel") and st in ("missing", "expired", "expiring"):
                tier, why = 4, f"standard set, {st}"
            if tier is None and kind == "bias" and st in ("missing", "expired", "expiring"):
                tier, why = 5, f"bias, {st}"
            if tier is not None:
                out.append({"key": k, "kind": kind, "tier": tier, "why": why, "pool_n": P.count(k, tb)})
        return out

    def choose(self, T):
        """The key to take next at sensor temperature T, or None. Returns a dict with key, bin and reason."""
        w = self.svc.darklib.policy.temp_bin_c
        tb = tbin(T, w)
        cand = self.needs(tb)
        if not cand:
            return None
        frac = (T - (tb - w / 2)) / w                       # 0 bottom .. 1 top of the bin
        short_first = frac >= 0.5
        best_tier = min(x["tier"] for x in cand)
        tier = [x for x in cand if x["tier"] == best_tier]
        skipped = []
        rate = self.last_rate[0] if self.last_rate and self.clock.time() - self.last_rate[1] < 600 else None
        if rate is not None and rate > 0:
            ok = [x for x in tier if rate * x["key"].exposure <= self.cfg.max_temp_change_c]
            if ok and len(ok) < len(tier):
                skipped = [x["key"].exposure for x in tier if x not in ok]
                tier = ok
        tier.sort(key=lambda x: (x["pool_n"] == 0, -x["pool_n"],
                                 x["key"].exposure if short_first else -x["key"].exposure))
        x = tier[0]
        parts = [f"bin {_bl(tb)} ({T:+.2f} C, {'top' if short_first else 'bottom'} half)", x["why"]]
        if x["pool_n"]:
            parts.append(f"pool {x['pool_n']}/{self.cfg.count} first")
        if skipped:
            parts.append(f"skipping {[f'{e:g}' for e in skipped]} s: warming {rate * 60:.2f} C/min")
        return {"key": x["key"], "kind": x["kind"], "tier": x["tier"], "temp_bin": tb, "temp": T,
                "pool_n": x["pool_n"], "reason": "; ".join(parts)}

    # ---- main loop -----------------------------------------------------------------------------------
    def _loop(self):
        while not self.svc.halt.is_set():
            try:
                wait = self.tick()
            except Exception as e:  # noqa: BLE001
                self._end_series("error")
                self._set_state("error", f"{type(e).__name__}: {e}")
                self.svc.log(f"dark harvest error: {e}")
                wait = self.cfg.poll_s
            if wait:
                self.wake.clear()
                self.clock.wait(self.wake, wait)

    def tick(self):
        """One decision (and at most one frame). Returns how long to wait (sim/real seconds) before the next."""
        now = self.clock.time()
        self._account(now)
        self._daily_summary(now)
        if self._orphan is not None and self._service_blocker() is None:
            self._recover()                    # a frame the previous process left (also when off)
        if not self.enabled:
            self._end_series("off")
            self._set_state("off", None)
            return self.cfg.poll_s
        hold, pause = self.check()
        if hold:
            self.last_block = now
        if hold or pause:
            self._end_series(", ".join(hold + pause))
            self.blockers = hold + pause
            self._set_state("paused", ", ".join(hold + pause))
            return self.cfg.poll_s
        self.blockers = []
        ho = self.cfg.holdoff_min * 60.0
        if self.last_block is not None and now - self.last_block < ho:
            self._end_series("hold-off")
            self._set_state("holdoff", f"hold-off until {_iso(self.last_block + ho)}")
            return min(self.cfg.poll_s, self.last_block + ho - now + 0.5)
        T = self.svc.imager.temperature()
        if T is None:
            self._end_series("no reading")
            self._set_state("no_reading", "no sensor temperature reading")
            return self.cfg.poll_s
        ch = self.choose(T)
        if ch is None:
            self.current = None
            self._decide(None, f"bin {_bl(tbin(T, self.svc.darklib.policy.temp_bin_c))} ({T:+.2f} C) complete")
            self._end_series("nothing to do in this bin")
            self._set_state("waiting_cool", f"nothing to do at {T:+.1f} C")
            return self.cfg.poll_s
        self.current = ch
        self._decide(ch, ch["reason"])
        self._set_state("harvesting", ch["reason"])
        self._frame(ch)
        return 0

    def _decide(self, ch, reason):
        sig = None if ch is None else (ch["key"], ch["temp_bin"])
        if sig != self._decision:
            self._decision = sig
            if ch is None:
                self._event("decision", reason)
            else:
                self._event("decision", f"{ch['key'].label()}: {reason}", key=ch["key"].label(),
                            temp_bin=ch["temp_bin"], tier=ch["tier"], temp=ch["temp"])

    def _begin_series(self):
        im = self.svc.imager
        if self.active:
            return
        self._prev_filter = im.filter_name()
        self.active = True
        if self._prev_filter != "Dark":
            im.set_filter("Dark")
            self._event("series", f"filter wheel on Dark (was {self._prev_filter})")

    def _end_series(self, why):
        if not self.active:
            self.free.set()
            return
        prev, self._prev_filter = self._prev_filter, None
        try:
            if prev in self.svc.imager.fw.Names and prev != "Dark":
                self.svc.imager.set_filter(prev)
                self._event("series", f"filter wheel back on {prev} ({why})")
        except Exception as e:  # noqa: BLE001
            self._event("series", f"filter wheel back: {e}")
        finally:
            self.active = False
            self.free.set()

    def _frame(self, ch):
        svc, im, key = self.svc, self.svc.imager, ch["key"]
        with self.gate:
            b = self._service_blocker()
            if b or self.stop.is_set() or not self.enabled:
                self.stop.clear()
                self.last_block = self.clock.time() if b else self.last_block
                return
            self.free.clear()
        try:
            if self.stop.is_set():                   # pre-empted right after the gate: don't move the wheel
                raise Aborted("pre-empted")
            self._begin_series()
            if self.stop.is_set():
                raise Aborted("pre-empted")
            temp0 = im.temperature()
            t0 = self.clock.time()
            self.current = {**ch, "frame_start": round(t0, 2), "frame_start_iso": _iso(t0)}
            svc._notify()
            frame = self._expose(key)
            t1 = self.clock.time()
            temp1 = im.temperature()
        except Aborted as e:
            self._event("frame", f"{key.label()}: aborted ({e}), partial frame discarded", key=key.label(),
                        aborted=True)
            self._end_series("pre-empted")
            self.stop.clear()
            return
        except BaseException:
            self._end_series("error")
            self.stop.clear()
            raise
        finally:
            if not self.active:
                self.free.set()
        self.last_frame_unix = t1
        r = svc._pool_frame(key, frame, temp0, temp1, t0, t1, im.identity(), "harvest")
        dt_s = max(1.0, t1 - t0)
        if temp0 is not None and temp1 is not None:
            self.last_rate = ((temp1 - temp0) / dt_s, t1)
        day = self._day_counters(t1)
        day["frames"] = day.get("frames", 0) + 1
        bins = day.setdefault("bins", [])
        if r["temp_bin"] is not None and r["temp_bin"] not in bins:
            bins.append(r["temp_bin"])
        if not r["steady"]:
            day["unsteady"] = day.get("unsteady", 0) + 1
        self._event("frame", f"{key.label()} at {temp0}..{temp1} C -> bin {_bl(r['temp_bin'])}"
                    + (f", pool {r['pool_n']}/{self.cfg.count}" if r["steady"] else
                       f": discarded from the pool, T moved {r['temp_change']:+.2f} C during the exposure"),
                    key=key.label(), exposure=key.exposure, gain=key.gain, temp_start=temp0, temp_end=temp1,
                    temp_bin=r["temp_bin"], steady=r["steady"], pool_n=r["pool_n"],
                    median=round(float(np.median(frame)), 1), start=_iso(t0), duration_s=round(t1 - t0, 2))
        if r["steady"] and r["pool_n"] >= self.cfg.count:
            self._queue_build(key, r["temp_bin"], key == self.sentinel_key() and r["temp_bin"] == self.common_bin())
        with self.gate:
            if self.stop.is_set():
                self.stop.clear()
                self._end_series("pre-empted")
        if not self.active:
            self.free.set()

    def _expose(self, key):
        """One dark, abortable by preempt() (stop), a file lock or the external check."""
        im = self.svc.imager
        self._write_inflight(key, self.clock.time())
        im.start(key.exposure, key.gain, light=False)
        im._busy.set()
        t0 = self.clock.time()
        last_chk = time.monotonic()
        try:
            while not im.ready():
                if self.stop.is_set():
                    im.abort()
                    raise Aborted("pre-empted")
                if time.monotonic() - last_chk > 0.5:
                    last_chk = time.monotonic()
                    b = self._service_blocker() or self._external_blocker()
                    if b:
                        im.abort()
                        self.last_block = self.clock.time()
                        self._event("preempt", f"pre-empted by {b}")
                        raise Aborted(b)
                if self.clock.time() - t0 > key.exposure + im.timeout_extra:
                    im.abort()
                    raise RuntimeError("exposure timeout")
                self.clock.wait(self.stop, 0.5)
            return im.download()
        finally:
            im._busy.clear()
            self._clear_inflight()

    # ---- masters -------------------------------------------------------------------------------------
    def _queue_build(self, key, tb, keep):
        with self._build_cv:
            if (key, tb) in self._pending:
                return
            self._pending.add((key, tb))
            self._build_q.append((key, tb, keep))
            self._build_cv.notify_all()

    def _builder(self):
        while not self.svc.halt.is_set():
            with self._build_cv:
                while not self._build_q and not self.svc.halt.is_set():
                    self._build_cv.wait(1.0)
                if self.svc.halt.is_set():
                    return
                key, tb, keep = self._build_q.pop(0)
            try:
                m = self.svc._build_pool_master(key, tb, self.cfg.count, None)
                if m:
                    if keep:
                        self._keep_master(m)
                    day = self._day_counters(self.clock.time())
                    day["masters"] = day.get("masters", 0) + 1
                    self._event("master", f"built {m['id']}: {m['frames']} frames, {m['temp_min']}..{m['temp_max']} C, "
                                f"{m.get('date_first')}..{m.get('date_last')}", master=m["id"], frames=m["frames"],
                                temp_bin=tb, sentinel=keep)
                    if self.svc.busy is None:
                        self.svc._restack()
            except Exception as e:  # noqa: BLE001
                self._event("master", f"building {key.label()} bin {_bl(tb)} failed: {e}", error=str(e))
                self.svc.log(f"dark harvest: master {key.label()} bin {_bl(tb)} failed: {e}")
            finally:
                with self._build_cv:
                    self._pending.discard((key, tb))
                self.wake.set()

    def _keep_master(self, m):
        """Sentinel masters are kept (the library replaces a bin's master): the ageing time series."""
        import shutil
        d = os.path.join(self.svc.darkpool.root, m["camera"], "masters",
                         time.strftime("%Y%m%dT%H%M%S", time.gmtime(m["created_unix"])) + "_" + os.path.basename(m["dir"]))
        os.makedirs(d, exist_ok=True)
        for f in ("master.fits", "meta.json"):
            shutil.copy2(os.path.join(m["dir"], f), os.path.join(d, f))

    # ---- state, counters, logs ---------------------------------------------------------------------------
    def _set_state(self, state, reason):
        if state != self.state or reason != self.reason:
            if state != self.state:
                self.state_since = self.clock.time()
                if state in ("paused", "holdoff", "waiting_cool", "error", "no_reading"):
                    self._event("state", f"{state}: {reason}" if reason else state, state=state)
            self.state, self.reason = state, reason
            self.headline = self._headline()
            self.svc._notify()

    def _headline(self):
        s, r = self.state, self.reason
        if s == "off":
            return "Off"
        if s == "harvesting" and self.current:
            c = self.current
            return (f"Harvesting {c['key'].exposure:g} s g{c['key'].gain} at {c['temp']:+.1f} C "
                    f"(bin {_bl(c['temp_bin'])}, {c['pool_n']}/{self.cfg.count})")
        if s == "waiting_cool":
            T = self.svc.sensor_temp
            nb = self.next_bin()
            return (f"Waiting for the sensor to cool ({T:+.1f} C" if T is not None else "Waiting for the sensor to cool (") \
                + (f", next useful bin {_bl(nb)})" if nb is not None else ")")
        if s == "paused":
            return f"Paused: {r}"
        if s == "holdoff":
            return f"Holding off: {r}"
        if s == "no_reading":
            return "Paused: no sensor temperature reading"
        return f"Error: {r}"

    def next_bin(self):
        """The nearest lower bin with work (the sensor cools towards it)."""
        T = self.svc.sensor_temp
        if T is None:
            return None
        w = self.svc.darklib.policy.temp_bin_c
        b = tbin(T, w)
        entries, keys = self._entries(), self.keys()
        for i in range(1, 8):
            if self.needs(b - i * w, entries, keys):
                return b - i * w
        return None

    def _day_counters(self, t):
        return self._counters.setdefault(_day(t), {})

    def _account(self, now):
        """Seconds harvesting / pre-empted per day, from the state the previous tick left."""
        last, self._tick_t = self._tick_t, now
        if last is None or now <= last:
            return
        d = self._day_counters(now)
        if self.state == "harvesting":
            d["harvest_s"] = round(d.get("harvest_s", 0.0) + (now - last), 1)
        elif self.state in ("paused", "holdoff") and self.enabled:
            d["preempted_s"] = round(d.get("preempted_s", 0.0) + (now - last), 1)
        if now - self._counters_saved > 300:
            self._save_counters()

    def _save_counters(self):
        keep = sorted(self._counters)[-40:]
        self._counters = {k: self._counters[k] for k in keep}
        try:
            with open(self.counters_path + ".tmp", "w") as fh:
                json.dump(self._counters, fh)
            os.replace(self.counters_path + ".tmp", self.counters_path)
            self._counters_saved = self.clock.time()
        except OSError:
            pass

    def counters(self, now=None):
        now = self.clock.time() if now is None else now
        today = _day(now)
        week = [_day(now - 86400 * i) for i in range(7)]

        def agg(days):
            out = {"frames": 0, "masters": 0, "unsteady": 0, "harvest_h": 0.0, "preempted_h": 0.0, "bins": []}
            for d in days:
                c = self._counters.get(d, {})
                out["frames"] += c.get("frames", 0)
                out["masters"] += c.get("masters", 0)
                out["unsteady"] += c.get("unsteady", 0)
                out["harvest_h"] += c.get("harvest_s", 0.0) / 3600.0
                out["preempted_h"] += c.get("preempted_s", 0.0) / 3600.0
                out["bins"] = sorted(set(out["bins"]) | set(c.get("bins", [])))
            out["harvest_h"], out["preempted_h"] = round(out["harvest_h"], 2), round(out["preempted_h"], 2)
            return out
        return {"today": {"date": today, **agg([today])}, "week": {"from": week[-1], **agg(week)}}

    def _daily_summary(self, now):
        d = _day(now)
        if d == self._summary_day:
            return
        prev, self._summary_day = self._summary_day, d
        c = self._counters.get(prev, {})
        if not self.enabled and not c.get("frames"):
            return
        bins = sorted(c.get("bins", []))
        cov = self.coverage()
        stall = self.stall()
        msg = (f"darks {prev}: {c.get('frames', 0)} frames, {c.get('masters', 0)} masters"
               + (f", bins {_bl(bins[0])}..{_bl(bins[-1])}" if bins else "")
               + f", {cov['percent']:.0f} % coverage, {cov['expiring_7d']} expiring within 7 d"
               + (f"; STALLED: {stall['reason']}" if stall["stalled"] else ""))
        self.svc.log(msg)
        self._event("summary", msg, day=prev, **{k: v for k, v in c.items() if k != "bins"})
        self._save_counters()

    def _event(self, kind, msg, **kw):
        now = self.clock.time()
        rec = {"time": _iso(now), "unix": round(now, 2), "kind": kind, "msg": msg, **kw}
        self._log_tail = (self._log_tail + [rec])[-max(self.cfg.log_lines, 200):]
        try:
            with open(self.log_path, "a") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
        except OSError:
            pass

    def _read_log_tail(self, n):
        if not os.path.exists(self.log_path):
            return []
        try:
            with open(self.log_path, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 400 * n))
                lines = fh.read().decode(errors="replace").splitlines()[-n:]
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    @staticmethod
    def _load_json(path, default):
        try:
            with open(path) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return default

    # ---- views ----------------------------------------------------------------------------------------
    def stall(self, now=None):
        """Stalled: enabled, service idle, parked, work in the current bin and no frame for stall_hours."""
        now = self.clock.time() if now is None else now
        out = {"stalled": False, "reason": None, "hours_since_frame": None}
        ref = max(x for x in (self.last_frame_unix, self.enabled_since, self.started_unix) if x is not None)
        if self.last_frame_unix is not None:
            out["hours_since_frame"] = round((now - self.last_frame_unix) / 3600.0, 2)
        if not self.enabled or now - ref < self.cfg.stall_hours * 3600.0:
            return out
        svc = self.svc
        if svc.busy is not None or not svc.jobs.empty() or svc.hwlock is not None:
            return out
        try:
            if not bool(svc.scope.t.AtPark):
                return out
        except Exception:  # noqa: BLE001
            return out
        T = svc.sensor_temp
        if T is None or not self.needs(tbin(T, svc.darklib.policy.temp_bin_c)):
            return out
        h = (now - ref) / 3600.0
        out.update(stalled=True, reason=f"no frame for {h:.1f} h with bin {_bl(tbin(T, svc.darklib.policy.temp_bin_c))} "
                   f"missing: {self.reason or self.state}")
        return out

    def coverage(self):
        """Grid: rows = keys (standard set, sentinel, bias, plan keys), columns = reached temperature bins."""
        svc = self.svc
        w = svc.darklib.policy.temp_bin_c
        entries = self._entries()
        keys = self.keys()
        P = svc.darkpool
        reached = {tb for (_, tb) in entries if tb is not None}
        with P._lock:
            recs = list(P.frames.values())
        for r in recs:
            tb = P.frame_tbin(r)
            if tb is not None:
                reached.add(tb)
        if svc.sensor_temp is not None:
            reached.add(tbin(svc.sensor_temp, w))
        bins = []
        if reached:
            lo, hi = min(reached), max(reached)
            n = int(round((hi - lo) / w))
            bins = [round(lo + i * w, 2) for i in range(n + 1)]
        pools = {(Key(p["camera"], p["exposure"], p["gain"], p["offset"], p["binning"]), p["temp_bin"]): p
                 for p in P.pools()}
        rows, done, cells, expiring = [], 0, 0, 0
        for k, kind in keys:
            row = {"label": k.label(), "kind": kind, "exposure": k.exposure, "gain": k.gain, "offset": k.offset,
                   "binning": k.binning, "cells": []}
            for tb in bins:
                m, p = entries.get((k, tb)), pools.get((k, tb))
                c = {"temp_bin": tb, "reached": tb in reached, "pool_n": p["n"] if p else 0, "count": self.cfg.count,
                     "pool_oldest_age_days": p["oldest_age_days"] if p else None,
                     "building": (k, tb) in self._pending}
                if m and m["status"] != "expired":
                    c.update(state=m["status"], master=m["id"], age_days=m["age_days"], frames=m["frames"],
                             created=m["created"])
                    expiring += m["status"] == "expiring"
                elif p:
                    c.update(state="pool", expired_master=m["id"] if m else None)
                else:
                    c.update(state="missing" if tb in reached else "never_reached",
                             expired_master=m["id"] if m else None)
                if tb in reached:
                    cells += 1
                    done += c["state"] in ("fresh", "expiring")
                row["cells"].append(c)
            rows.append(row)
        return {"bins": bins, "bin_width_c": w, "rows": rows, "complete": done, "cells": cells,
                "percent": round(100.0 * done / cells, 1) if cells else 0.0, "expiring_7d": expiring}

    def sensor(self):
        svc = self.svc
        T = svc.sensor_temp
        w = svc.darklib.policy.temp_bin_c
        out = {"temp": T, "bin": tbin(T, w), "bin_pos": None, "trend_c_h": None, "ambient": {}}
        if T is not None:
            tb = tbin(T, w)
            out["bin_pos"] = round((T - (tb - w / 2)) / w, 3)
        try:
            rows = [r for r in svc.templog.rows(1.0) if r.get("sensor_c") is not None]
            if len(rows) >= 3:
                ts = np.array([dt.datetime.strptime(r["time"], "%Y-%m-%dT%H:%M:%SZ").timestamp() for r in rows])
                ys = np.array([r["sensor_c"] for r in rows], float)
                if ts.ptp() > 0:
                    out["trend_c_h"] = round(float(np.polyfit((ts - ts[0]) / 3600.0, ys, 1)[0]), 2)
            last = svc.templog.rows(0.2)
            if last:
                out["ambient"] = {n: last[-1].get(n) for n, _ in svc.templog.cfg.sources}
        except Exception:  # noqa: BLE001
            pass
        return out

    def brief(self):
        """Small summary for /api/status."""
        return {"enabled": self.enabled, "state": self.state, "headline": self.headline, "reason": self.reason,
                "active": self.active}

    def view(self, log_n=None):
        now = self.clock.time()
        c = self.current
        cur = None
        if c and self.state == "harvesting":
            cur = {"label": c["key"].label(), "exposure": c["key"].exposure, "gain": c["key"].gain,
                   "offset": c["key"].offset, "binning": c["key"].binning, "kind": c["kind"], "tier": c["tier"],
                   "temp_bin": c["temp_bin"], "pool_n": self.svc.darkpool.count(c["key"], c["temp_bin"]),
                   "count": self.cfg.count, "reason": c["reason"], "frame_start": c.get("frame_start_iso"),
                   "frame_start_unix": c.get("frame_start")}
        ho = None
        if self.last_block is not None and now - self.last_block < self.cfg.holdoff_min * 60.0:
            ho = _iso(self.last_block + self.cfg.holdoff_min * 60.0)
        sk = self.sentinel_key()
        cb = self.common_bin()
        sm = self._entries().get((sk, cb)) if cb is not None else None
        n = self.cfg.log_lines if log_n is None else int(log_n)
        stall = self.stall(now)
        return {
            "enabled": self.enabled, "enabled_source": self.enabled_source, "state": self.state,
            "headline": ("Harvest stalled: " + stall["reason"]) if stall["stalled"] else self.headline,
            "reason": self.reason, "since": _iso(self.state_since), "active": self.active,
            "time": _iso(now), "time_unix": round(now, 2), "speed": getattr(self.clock, "speed", 1.0),
            "sensor": self.sensor(), "current": cur, "blockers": list(self.blockers), "holdoff_until": ho,
            "last_frame": _iso(self.last_frame_unix) if self.last_frame_unix else None,
            "counters": self.counters(now), "coverage": self.coverage(),
            "sentinel": {"label": sk.label(), "exposure": sk.exposure, "gain": sk.gain,
                         "every_days": float(self.cfg.sentinel["every_days"]), "common_bin": cb,
                         "last_master": sm["id"] if sm else None, "last_age_days": sm["age_days"] if sm else None},
            "pool": self.svc.darkpool.summary(now),
            "stall": stall,
            "log": self._log_tail[-n:] if n > 0 else [],
            "config": {k: v for k, v in vars(self.cfg).items()},
        }
