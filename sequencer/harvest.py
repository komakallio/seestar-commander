"""Dark harvest: while the observatory is idle, take dark frames at whatever temperature the uncooled
sensor happens to be, so the dark library fills across temperature bins (frames go through the frame
pool, sequencer/darkpool.py). A thread inside the service; it uses the telephoto camera and filter wheel
only, never the mount, never the scope lock, and never shows as busy: everything else wins at once.

When (all of): enabled; no plan or job running or queued; no scope lock (API lock, or a script holding
the file lock <data>/scope.lock); roof closed if a roof is configured (a roof that reports "unknown" counts
as closed: then only parked is required); scope at park; the telephoto camera idle (Alpaca CameraState,
so the vendor app or a tool that doesn't take the lock isn't disturbed); the optional external busy check
says idle; and `holdoff_min` has passed since any of these last blocked it.

What, one frame at a time. A thermal model (sequencer/thermal.py) fitted from the temperature log and the frame
archive gives the idle floor (exponential fit of idle cooling curves, following the ambient when the log has
an ambient source), the warm plateau under continuous exposure and both time constants. Before a frame is
taken its mean temperature is predicted from the current reading T and its exposure; a key is a candidate
only if that prediction lands in a bin that needs it, at least margin_c above the bin's bottom edge and at
least max(margin_c, the frame's own predicted warming) below its top, with the predicted change within
max_temp_change_c. Needs per bin, in tier order:
  1. keys the active and planned plans need (Service.dark_keys) with no master in the bin;
  2. those keys with an expired or expiring master in the bin;
  3. the sentinel key (60 s g200) in the most common bin when its master there is older than every_days
     (its masters are kept in the archive: the time series for the ageing analysis);
  4. the standard set (gain x exposures) missing or expiring in the bin;
  5. bias frames (bias_exposure, 1 ms) missing or expiring in the bin.
Within a tier: edge-only bins last, partly filled pools first (fullest first), then short exposures when T is
in the top half of its bin and long ones in the bottom half. A series starts only once T is in the bottom third
of the bin (or as low as the floor lets it), so it warms through the bin instead of out of it.

Reach (per bin, at the fitted floor F and plateau P, re-evaluated as they move): outside [F, P] the bin is
"unreachable_now" (no attempts); reachable only within edge_c of its edge it is "edge" (at most one attempt
per edge_every_min); else "reachable". A cell whose exposure can't land steadily in the bin from any start
temperature in [F, P] is "unreachable_now" too.

Nothing to take: a needed bin that idling reaches -> "waiting_cool" (until the predicted start temperature);
a needed bin only reached by warming -> a warm-up frame; else "complete": every reachable bin is complete. Then,
every sweep_every_h, a temperature sweep from the floor: back-to-back frames cycling the exposure set (short
and long interleaved) up to the plateau (from the leg's own fitted curve), then short frames at low duty
(one per sweep_down_every_s) back down, idling the last part to the floor once the frames stall the cooling.
Sweep frames go to the archive and the pools they land in (preferring keys a bin needs); each sweep's warm-up
and cool-down curves with their fitted time constants go to <data>/darkharvest/thermal.jsonl. Masters are
built only for cells that need one (a complete cell keeps its pool for when its master expires).

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
       <data>/darkharvest/thermal.jsonl  one record per sweep: floor, plateau, both legs (points, fitted tau)
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
from .thermal import ThermalModel, fit_relax

STATES = ("off", "harvesting", "sweep", "waiting_cool", "complete", "paused", "holdoff", "no_reading", "error")
REACH = ("reachable", "edge", "unreachable_now")


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
    margin_c: float = 0.15                   # a frame's predicted mean must be this far inside its bin
    edge_c: float = 0.3                      # a bin reachable only this close to its edge is "edge-only"
    edge_every_min: float = 60.0             # at most one edge-only attempt per bin this often
    fit_hours: float = 12.0                  # thermal model: recent idle stretches and frame runs
    history_days: float = 14.0               # thermal model: long idle stretches, clamps, observed range
    sweep: bool = True                       # temperature sweeps when every reachable bin is complete
    sweep_every_h: float = 6.0
    sweep_max_up_min: float = 90.0           # warm-up leg at most this long
    sweep_max_down_min: float = 180.0        # cool-down leg at most this long
    sweep_down_every_s: float = 120.0        # cool-down leg: one short frame this often (low duty)

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
        self.thermal_path = os.path.join(self.dir, "thermal.jsonl")
        self.thermal = ThermalModel(svc, fit_hours=cfg.fit_hours, history_days=cfg.history_days)
        self._ach = (None, {})                 # achievable start temperatures per (exposure, bin), per fit
        self._edge_tries = {}                  # bin -> last edge-only attempt (unix)
        self.outlook = None                    # why there is no frame to take now (see _outlook)
        self._notes = []
        self._wait_pts = []                    # (t, T) while waiting to cool (stalled cooling = at the floor)
        self.sweep = None                      # the sweep in progress (see _sweep_tick)
        self.last_sweep_unix = None
        for rec in self._read_log_tail(50, self.thermal_path):
            if rec.get("kind") == "sweep" and rec.get("end_unix"):
                self.last_sweep_unix = rec["end_unix"]
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

    # ---- thermal model: reach and prediction ----------------------------------------------------------
    def _model(self):
        s = self.thermal.refresh()
        if self._ach[0] != s["fitted_unix"]:
            self._ach = (s["fitted_unix"], {})
        return s

    def reach(self, tb):
        """reachable | edge | unreachable_now for bin tb at the fitted idle floor and warm plateau (both follow
        the ambient), widened to the readings already seen at a similar ambient. Unknown floor: reachable."""
        self._model()
        rr = self.thermal.reach_range()
        if rr is None or tb is None:
            return "reachable"
        F, P = rr
        w, e = self.svc.darklib.policy.temp_bin_c, float(self.cfg.edge_c)
        lo, hi = tb - w / 2, tb + w / 2
        if hi <= F or lo > P:
            return "unreachable_now"
        a, b = max(lo, F), min(hi, P)
        if b <= lo + e or a >= hi - e:
            return "edge"
        return "reachable"

    def _lands(self, T, exposure, tb):
        """A dark of `exposure` started at T: its predicted mean is in bin tb, at least margin_c from the
        bottom edge and max(margin_c, the frame's own predicted warming) from the top, and it stays steady."""
        mean, end = self.thermal.predict(T, exposure)
        w = self.svc.darklib.policy.temp_bin_c
        if tbin(mean, w) != tb:
            return False
        lo, hi = tb - w / 2, tb + w / 2
        warm = end - T
        return (mean - lo >= self.cfg.margin_c and hi - mean >= max(self.cfg.margin_c, warm)
                and abs(warm) <= self.cfg.max_temp_change_c + 1e-9)

    def achievable(self, exposure, tb):
        """Start temperatures between the floor and the plateau (0.05 C steps) from which a frame lands in
        tb; None when the floor isn't known yet."""
        self._model()
        rr = self.thermal.reach_range()
        if rr is None:
            return None
        k = (float(exposure), tb)
        cache = self._ach[1]
        if k not in cache:
            F, P = rr
            starts = np.arange(F, max(F, P) + 1e-9, 0.05)
            cache[k] = [round(float(T), 2) for T in starts if self._lands(float(T), exposure, tb)]
        return cache[k]

    def choose(self, T):
        """The key to take next at sensor temperature T, or None (self.outlook then says why). Every key is
        predicted from T: its frame mean (thermal model) must land well inside a bin that needs that key.
        Returns a dict with key, bin, predicted mean and reason."""
        s = self._model()
        w = self.svc.darklib.policy.temp_bin_c
        now = self.clock.time()
        entries, keys = self._entries(), self.keys()
        cur_tb = tbin(T, w)
        frac = (T - (cur_tb - w / 2)) / w
        short_first = frac >= 0.5
        need_cache, cand, notes = {}, [], []
        for k, kind in keys:
            mean, end = self.thermal.predict(T, k.exposure)
            tb = tbin(mean, w)
            if tb not in need_cache:
                need_cache[tb] = {x["key"]: x for x in self.needs(tb, entries, keys)}
            x = need_cache[tb].get(k)
            if x is None:
                continue
            if not self._lands(T, k.exposure, tb):
                notes.append(f"{k.exposure:g} s would land at {mean:+.2f} C (end {end:+.2f}): too near the edge "
                             f"of bin {_bl(tb)} or unsteady")
                continue
            r = self.reach(tb)
            edge = r == "edge"
            if edge and now - self._edge_tries.get(tb, -1e18) < self.cfg.edge_every_min * 60.0:
                notes.append(f"bin {_bl(tb)} edge-only: tried within {self.cfg.edge_every_min:g} min")
                continue
            cand.append({**x, "temp_bin": tb, "mean": mean, "end": end, "edge": edge})
        # starting a series: let the sensor cool into the lower third of the bin first (or to the floor), so
        # the series warms through the bin instead of out of it
        F = s["floor"]
        if cand and not self.active and F is not None and not self._cooling_stalled(T):
            start_max = max(cur_tb - w / 2 + w / 3, F + 0.15)
            if T > start_max and any(c["temp_bin"] == cur_tb for c in cand):
                lower = [c for c in cand if c["temp_bin"] != cur_tb]
                if not lower:
                    self.outlook = {"kind": "cool", "temp_bin": cur_tb, "start_at": round(start_max, 2),
                                    "why": f"bin {_bl(cur_tb)}: cooling to {start_max:+.2f} C before starting"}
                    return None
                cand = lower
        if not cand:
            self.outlook = None
            self._notes = notes
            return None
        best_tier = min(x["tier"] for x in cand)
        tier = [x for x in cand if x["tier"] == best_tier]
        tier.sort(key=lambda x: (x["edge"], x["pool_n"] == 0, -x["pool_n"],
                                 x["key"].exposure if short_first else -x["key"].exposure))
        x = tier[0]
        tb = x["temp_bin"]
        parts = [f"bin {_bl(tb)} ({T:+.2f} C now, frame predicted {x['mean']:+.2f} C)", x["why"]]
        if tb != cur_tb:
            parts.append(f"warms into bin {_bl(tb)}")
        if x["edge"]:
            parts.append("edge-only bin (one try an hour)")
        if x["pool_n"]:
            parts.append(f"pool {x['pool_n']}/{self.cfg.count} first")
        self.outlook = None
        return {"key": x["key"], "kind": x["kind"], "tier": x["tier"], "temp_bin": tb, "temp": T,
                "pool_n": x["pool_n"], "pred_mean": round(x["mean"], 3), "edge": x["edge"], "purpose": "fill",
                "reason": "; ".join(parts)}

    def _cooling_stalled(self, T):
        """Waiting to cool, and the reading fell < 0.05 C in the last 20 min: this is the floor now, whatever
        the fit says (it catches up once the stretch is long enough)."""
        now = self.clock.time()
        old = [p for p in self._wait_pts if p[0] <= now - 1200]
        return bool(old) and old[-1][1] - T < 0.05

    def _outlook(self, T):
        """Nothing to take now: {"kind": cool | warm | complete, ...}. cool: a needed bin is reachable by
        idling (the start temperature it waits for); warm: a needed bin is only reached by warming the sensor;
        complete: every reachable bin is complete (edge-only and unreachable ones may still miss)."""
        if self.outlook is not None:
            return self.outlook
        s = self._model()
        w = self.svc.darklib.policy.temp_bin_c
        F = s["floor"]
        P = self.thermal.plateau_for(F if F is not None else T)
        entries, keys = self._entries(), self.keys()
        lo_b = tbin(min(T, F) if F is not None else T - 6.0, w)
        hi_b = tbin(max(T, P), w)
        cool, warm, edge = [], [], set()
        tb = lo_b
        while tb <= hi_b + 1e-9:
            for x in self.needs(tb, entries, keys):
                r = self.reach(tb)
                if r == "unreachable_now":
                    continue
                if r == "edge":
                    edge.add(tb)
                    continue
                st = self.achievable(x["key"].exposure, tb)
                if st is None:                         # no floor yet: lower bins by cooling, as before
                    if tb < tbin(T, w):
                        cool.append((T - 0.5, tb, x))
                    continue
                below = [v for v in st if v < T - 0.02]
                above = [v for v in st if v > T + 0.02]
                if below:
                    cool.append((max(below), tb, x))
                if above:
                    warm.append((min(above), tb, x))
            tb = round(tb + w, 2)
        if cool and self._cooling_stalled(T):
            cool = []                                   # not getting any colder
        if cool:
            at, tb, x = max(cool, key=lambda c: c[0])
            return {"kind": "cool", "temp_bin": tb, "start_at": round(at, 2),
                    "why": f"bin {_bl(tb)} {x['key'].exposure:g} s: waiting to cool to {at:+.2f} C"}
        if warm:
            at, tb, x = min(warm, key=lambda c: c[0])
            return {"kind": "warm", "temp_bin": tb, "start_at": round(at, 2), "key": x["key"],
                    "why": f"bin {_bl(tb)} {x['key'].exposure:g} s needs the sensor at {at:+.2f} C"}
        return {"kind": "complete", "edge_bins": sorted(edge)}

    def _warmup(self, T, o):
        """A frame that warms the sensor towards a needed bin above it: the longest standard exposure whose
        predicted end doesn't overshoot that bin's start temperature by more than half a bin."""
        w = self.svc.darklib.policy.temp_bin_c
        keys = [self.svc.dark_key(e, self.cfg.gain) for e in sorted(self.cfg.exposures)]
        ok = [k for k in keys if self.thermal.predict(T, k.exposure)[1] <= o["start_at"] + w / 2]
        k = ok[-1] if ok else keys[0]
        mean = self.thermal.predict(T, k.exposure)[0]
        return {"key": k, "kind": "warmup", "tier": 0, "temp_bin": tbin(mean, w), "temp": T, "pool_n":
                self.svc.darkpool.count(k, tbin(mean, w)), "pred_mean": round(mean, 3), "edge": False,
                "purpose": "warmup", "reason": f"warming the sensor ({T:+.2f} C): {o['why']}"}

    # ---- sweep -----------------------------------------------------------------------------------------
    def sweep_due(self, now=None):
        now = self.clock.time() if now is None else now
        if not self.cfg.sweep:
            return None
        nxt = (self.last_sweep_unix or 0.0) + self.cfg.sweep_every_h * 3600.0
        return max(nxt, now)

    def _sweep_order(self):
        ex = sorted(float(e) for e in self.cfg.exposures)
        out = []
        while ex:
            out.append(ex.pop(0))
            if ex:
                out.append(ex.pop())
        return out

    def _start_sweep(self, T, now):
        s = self._model()
        self.sweep = {"phase": "up", "start_unix": now, "phase_unix": now, "i": 0, "frames": 0,
                      "floor": s["floor"], "plateau": self.thermal.plateau_for(s["floor"] or T),
                      "ambient": s["ambient"], "pts": {"up": [(now, T)], "down": []}, "last_end": None,
                      "n": {"up": 0, "down": 0}}
        self._event("sweep", f"sweep started at {T:+.2f} C (floor {s['floor']:+.2f}, plateau "
                    f"{self.sweep['plateau']:+.2f} C): warm-up leg", phase="up")

    def _sweep_frame_key(self, T, short):
        """Prefer a key whose frame lands well inside a bin that needs it; else the next in the cycle."""
        ch = self.choose(T)
        if ch is not None and (not short or ch["key"].exposure <= 15.0):
            return ch
        w = self.svc.darklib.policy.temp_bin_c
        sw = self.sweep
        if short:
            ex = [e for e in sorted(self.cfg.exposures) if e <= 15.0] or [min(self.cfg.exposures)]
        else:
            ex = self._sweep_order()
        e = ex[sw["i"] % len(ex)]
        sw["i"] += 1
        k = self.svc.dark_key(e, self.cfg.gain)
        mean = self.thermal.predict(T, e)[0]
        return {"key": k, "kind": "sweep", "tier": 0, "temp_bin": tbin(mean, w), "temp": T,
                "pool_n": self.svc.darkpool.count(k, tbin(mean, w)), "pred_mean": round(mean, 3), "edge": False}

    def _sweep_tick(self, T, now):
        sw = self.sweep
        ph = sw["phase"]
        sw["pts"][ph].append((now, T))
        if ph == "up":
            # the plateau from this leg's own warm-up curve (the model's until there is enough of it)
            up = sw["pts"]["up"]
            fit = fit_relax([([p[0] for p in up], [p[1] for p in up])]) \
                if len(up) >= 6 and up[-1][0] - up[0][0] >= 240 else None
            if fit and fit["tau_s"] and fit["asym"] >= max(p[1] for p in up) - 0.05:
                sw["plateau"] = fit["asym"]
            pts = [p for p in up if p[0] >= now - 300]
            rate = (pts[-1][1] - pts[0][1]) / (pts[-1][0] - pts[0][0]) * 60 \
                if len(pts) > 2 and pts[-1][0] > pts[0][0] + 240 else None
            done = (fit is not None and T >= sw["plateau"] - 0.15) \
                or now - sw["phase_unix"] > self.cfg.sweep_max_up_min * 60 \
                or (rate is not None and rate < 0.03 and sw["n"]["up"] >= 4)
            if done:
                self._leg_done("up", now, T)
                sw["phase"], sw["phase_unix"], sw["i"] = "down", now, 0
                sw["pts"]["down"].append((now, T))
                self._event("sweep", f"sweep: cool-down leg from {T:+.2f} C to the floor "
                            f"{(sw['floor'] or T):+.2f} C", phase="down")
                ph = "down"
            else:
                ch = self._sweep_frame_key(T, short=False)
                ch.update(purpose="sweep", reason=f"sweep warm-up leg at {T:+.2f} C (plateau {sw['plateau']:+.2f} C)")
                return ch, 0
        F = self._model()["floor"]
        F = sw["floor"] if F is None else F
        done = (F is not None and T <= F + 0.15 and sw["n"]["down"] >= 1) \
            or now - sw["phase_unix"] > self.cfg.sweep_max_down_min * 60
        if done:
            self._leg_done("down", now, T)
            self._finish_sweep(now, None)
            return None, 0
        # the short frames hold the sensor a little above the floor: once that stalls the cooling (< 0.05 C
        # in 10 min), no more frames, it idles the rest of the way down
        old = [p for p in sw["pts"]["down"] if p[0] <= now - 600]
        if not sw.get("idle_tail") and old and now - sw["phase_unix"] > 900 and old[-1][1] - T < 0.05:
            sw["idle_tail"] = True
            self._end_series("sweep: idling down to the floor")
            self._event("sweep", f"sweep: cooling stalled at {T:+.2f} C with frames, idling to the floor "
                        f"{(F or T):+.2f} C")
        if sw.get("idle_tail"):
            return None, self.cfg.poll_s
        if sw["last_end"] is not None and now - sw["last_end"] < self.cfg.sweep_down_every_s:
            return None, sw["last_end"] + self.cfg.sweep_down_every_s - now
        ch = self._sweep_frame_key(T, short=True)
        ch.update(purpose="sweep", reason=f"sweep cool-down leg at {T:+.2f} C (floor {(F or T):+.2f} C)")
        return ch, 0

    def _leg_done(self, ph, now, T):
        sw = self.sweep
        sw.setdefault("legs", {})[ph] = self._leg(ph, sw["pts"][ph], sw["n"][ph])

    @staticmethod
    def _leg(ph, pts, n):
        if not pts:
            return None
        t0 = pts[0][0]
        fit = fit_relax([([p[0] for p in pts], [p[1] for p in pts])], min_points=3) if len(pts) >= 3 else None
        step = max(1, len(pts) // 240)
        return {"start": _iso(t0), "start_unix": round(t0, 1), "duration_min": round((pts[-1][0] - t0) / 60.0, 1),
                "temp_start": pts[0][1], "temp_end": pts[-1][1], "frames": n,
                "fit": None if fit is None else {"asymptote_c": round(fit["asym"], 2),
                                                  "tau_min": round(fit["tau_s"] / 60.0, 1) if fit["tau_s"] else None,
                                                  "rms_c": round(fit["rms"], 3)},
                "points": [[round(p[0] - t0, 1), p[1]] for p in pts[::step]]}

    def _finish_sweep(self, now, aborted):
        sw, self.sweep = self.sweep, None
        if sw is None:
            return
        legs = sw.get("legs", {})
        for ph in ("up", "down"):
            if ph not in legs and sw["pts"][ph]:
                legs[ph] = self._leg(ph, sw["pts"][ph], sw["n"][ph])
        s = self.thermal.refresh(force=True)
        rec = {"kind": "sweep", "start": _iso(sw["start_unix"]), "start_unix": round(sw["start_unix"], 1),
               "end": _iso(now), "end_unix": round(now, 1), "aborted": aborted, "frames": sw["frames"],
               "floor_c": None if s["floor"] is None else round(s["floor"], 2),
               "plateau_c": None if s["plateau"] is None else round(s["plateau"], 2),
               "ambient_c": s["ambient"], "ambient_source": s["ambient_source"],
               "tau_heat_min": round(s["tau_heat_s"] / 60.0, 1), "tau_cool_min": round(s["tau_cool_s"] / 60.0, 1),
               "up": legs.get("up"), "down": legs.get("down")}
        try:
            with open(self.thermal_path, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
        except OSError:
            pass
        self.last_sweep_unix = now
        self._event("sweep", f"sweep {'aborted (' + aborted + ')' if aborted else 'done'}: {sw['frames']} frames, "
                    + ", ".join(f"{ph} {lg['temp_start']:+.2f}..{lg['temp_end']:+.2f} C"
                                + (f" tau {lg['fit']['tau_min']} min" if lg and lg.get('fit') and lg['fit']['tau_min'] else "")
                                for ph, lg in (("up", legs.get("up")), ("down", legs.get("down"))) if lg),
                    aborted=aborted, frames=sw["frames"])

    # ---- main loop -----------------------------------------------------------------------------------
    def _loop(self):
        while not self.svc.halt.is_set():
            try:
                wait = self.tick()
            except Exception as e:  # noqa: BLE001
                if self.sweep is not None:
                    self._finish_sweep(self.clock.time(), f"error: {e}")
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
            if self.sweep is not None:
                self._finish_sweep(now, "switched off")
            self._end_series("off")
            self._set_state("off", None)
            return self.cfg.poll_s
        hold, pause = self.check()
        if hold:
            self.last_block = now
        if hold or pause:
            if self.sweep is not None:
                self._finish_sweep(now, ", ".join(hold + pause))
            self._end_series(", ".join(hold + pause))
            self.blockers = hold + pause
            self._set_state("paused", ", ".join(hold + pause))
            return self.cfg.poll_s
        self.blockers = []
        ho = self.cfg.holdoff_min * 60.0
        if self.last_block is not None and now - self.last_block < ho:
            if self.sweep is not None:
                self._finish_sweep(now, "hold-off")
            self._end_series("hold-off")
            self._set_state("holdoff", f"hold-off until {_iso(self.last_block + ho)}")
            return min(self.cfg.poll_s, self.last_block + ho - now + 0.5)
        T = self.svc.imager.temperature()
        if T is None:
            self._end_series("no reading")
            self._set_state("no_reading", "no sensor temperature reading")
            return self.cfg.poll_s
        if self.sweep is not None:
            ch, wait = self._sweep_tick(T, now)
            if ch is not None:
                self.current = ch
                self._decide(ch, ch["reason"])
                self._set_state("sweep", ch["reason"])
                self._frame(ch)
                if self.sweep is not None:
                    self.sweep["frames"] += 1
                    self.sweep["n"][self.sweep["phase"]] += 1
                    self.sweep["last_end"] = self.clock.time()
                    self.sweep["pts"][self.sweep["phase"]].append((self.clock.time(), self.svc.imager.temperature()))
                return 0
            if self.sweep is not None:
                self._set_state("sweep", f"sweep cool-down leg at {T:+.2f} C: "
                                + ("idling to the floor" if self.sweep.get("idle_tail") else "idle between short frames"))
                return min(wait, self.cfg.poll_s) if wait else 0
            self._end_series("sweep done")
        ch = self.choose(T)
        if ch is None:
            o = self._outlook(T)
            if o["kind"] == "warm":
                ch = self._warmup(T, o)
            else:
                self.current = None
                w = self.svc.darklib.policy.temp_bin_c
                if o["kind"] == "cool":
                    self._wait_pts = [p for p in self._wait_pts if p[0] >= now - 3600] + [(now, T)]
                    self._decide(None, f"bin {_bl(tbin(T, w))} ({T:+.2f} C): {o['why']}", sig=("cool", o["temp_bin"]))
                    self._end_series("waiting to cool")
                    self._set_state("waiting_cool", o["why"])
                    return self.cfg.poll_s
                self._decide(None, f"every reachable bin complete at {T:+.2f} C", sig="complete")
                F = self._model()["floor"]
                due = self.sweep_due(now)
                if due is not None and due <= now and F is not None and T <= F + 0.3:
                    self._start_sweep(T, now)
                    self.wake.set()
                    return 0
                self._end_series("every reachable bin complete")
                self._set_state("complete", "every reachable bin complete"
                                + (f"; edge-only: {', '.join(_bl(b) for b in o['edge_bins'])}" if o.get("edge_bins") else ""))
                return self.cfg.poll_s
        self.current = ch
        self._wait_pts = []
        self._decide(ch, ch["reason"])
        self._set_state("harvesting", ch["reason"])
        if ch.get("edge"):
            self._edge_tries[ch["temp_bin"]] = now
        self._frame(ch)
        return 0

    def _decide(self, ch, reason, sig=None):
        sig = sig if ch is None else (ch["key"], ch["temp_bin"])
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
                    purpose=ch.get("purpose", "fill"), predicted=ch.get("pred_mean"), target_bin=ch.get("temp_bin"),
                    median=round(float(np.median(frame)), 1), start=_iso(t0), duration_s=round(t1 - t0, 2))
        if r["steady"] and r["pool_n"] >= self.cfg.count and \
                any(x["key"] == key for x in self.needs(r["temp_bin"])):     # a complete cell keeps its pool
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
        if s == "sweep" and self.sweep:
            sw = self.sweep
            leg = "warming to the plateau" if sw["phase"] == "up" else "cooling to the floor"
            T = self.svc.sensor_temp
            return (f"Temperature sweep: {leg}" + (f" ({T:+.1f} °C" if T is not None else " (")
                    + f", {sw['frames']} frames)")
        if s == "waiting_cool":
            T = self.svc.sensor_temp
            o = self.outlook or {}
            if o.get("kind") == "cool" and o.get("start_at") is not None:
                return (f"Waiting for the sensor to cool to {o['start_at']:+.1f} °C for bin {_bl(o['temp_bin'])}"
                        + (f" (now {T:+.1f} °C)" if T is not None else ""))
            nb = self.next_bin()
            return (f"Waiting for the sensor to cool ({T:+.1f} C" if T is not None else "Waiting for the sensor to cool (") \
                + (f", next useful bin {_bl(nb)})" if nb is not None else ")")
        if s == "complete":
            return self.complete_headline()
        if s == "paused":
            return f"Paused: {r}"
        if s == "holdoff":
            return f"Holding off: {r}"
        if s == "no_reading":
            return "Paused: no sensor temperature reading"
        return f"Error: {r}"

    def complete_headline(self, now=None):
        """E.g. "All reachable bins complete (12.9–17.8 °C at 5 °C outside); next sweep 14:00 UTC"."""
        now = self.clock.time() if now is None else now
        t = self.thermal.s
        F, P, A = t["floor"], t["plateau"], t["ambient"]
        h = "All reachable bins complete"
        if F is not None and P is not None:
            h += f" ({F:.1f}–{P:.1f} °C" + (f" at {A:.0f} °C {t['ambient_source'] or 'outside'}" if A is not None else "") + ")"
        due = self.sweep_due(now)
        if due is not None:
            h += "; next sweep " + ("when the sensor is back at the floor" if due <= now else
                                    dt.datetime.fromtimestamp(due, dt.timezone.utc).strftime("%H:%M UTC"))
        return h

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

    def _read_log_tail(self, n, path=None):
        path = path or self.log_path
        if not os.path.exists(path):
            return []
        try:
            with open(path, "rb") as fh:
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
        if svc.busy is not None or not svc.jobs.empty() or svc.lock_holder() is not None:
            return out
        try:
            if not bool(svc.scope.t.AtPark):
                return out
        except Exception:  # noqa: BLE001
            return out
        T = svc.sensor_temp
        if T is None or self.state == "complete" or not self.needs(tbin(T, svc.darklib.policy.temp_bin_c)):
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
        rows, done, cells, expiring, rdone, rcells = [], 0, 0, 0, 0, 0
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
                elif m:
                    # kept, never deleted; lights use it when nothing fresh matches (use_expired_fallback)
                    c.update(state="expired", expired_master=m["id"], age_days=m["age_days"], frames=m["frames"],
                             created=m["created"])
                else:
                    c.update(state="missing" if tb in reached else "never_reached", expired_master=None)
                if m and m["status"] == "expired":
                    c["fallback"] = bool(svc.darklib.policy.use_expired_fallback)
                    if c["fallback"]:
                        c["note"] = "expired, used as fallback"
                r = self.reach(tb)
                if r == "reachable" and k.exposure > 0:
                    st = self.achievable(k.exposure, tb)
                    if st is not None and not st and not (m and m["status"] != "expired"):
                        r = "unreachable_now"          # the bin is, but no steady frame of this length lands in it
                c["reach"] = r
                if r != "reachable":
                    c["reach_note"] = ("reachable only at the edge at this ambient" if r == "edge" else
                                       "not reachable now (needs colder or warmer weather)")
                if tb in reached:
                    cells += 1
                    done += c["state"] in ("fresh", "expiring")
                    if r != "unreachable_now":
                        rcells += 1
                        rdone += c["state"] in ("fresh", "expiring")
                row["cells"].append(c)
            rows.append(row)
        return {"bins": bins, "bin_width_c": w, "rows": rows, "complete": done, "cells": cells,
                "percent": round(100.0 * done / cells, 1) if cells else 0.0, "expiring_7d": expiring,
                "percent_reachable": round(100.0 * rdone / rcells, 1) if rcells else 0.0,
                "reach": {_bl(tb): self.reach(tb) for tb in bins}}

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
        out = {"enabled": self.enabled, "state": self.state, "headline": self.headline, "reason": self.reason,
               "active": self.active}
        c = self.current
        if c and self.state in ("harvesting", "sweep") and self.active:       # the main page's frame bar
            out["label"] = c["key"].label()
            out["current"] = {"exposure": c["key"].exposure, "gain": c["key"].gain, "temp": c.get("temp"),
                              "temp_bin": c.get("temp_bin"), "pool_n": c.get("pool_n"), "count": self.cfg.count}
            out["frame"] = {"start_unix": c.get("frame_start"), "exposure": float(c["key"].exposure)}
        t = self.thermal.s
        out["thermal"] = {"idle_floor": None if t["floor"] is None else round(t["floor"], 2),
                          "plateau": None if t["plateau"] is None else round(t["plateau"], 2)}
        if self.state == "holdoff" and self.last_block is not None:
            out["holdoff_until_unix"] = round(self.last_block + self.cfg.holdoff_min * 60.0, 1)
        return out

    def thermal_view(self, now=None):
        now = self.clock.time() if now is None else now
        self._model()
        v = self.thermal.view(self.svc.sensor_temp)
        due = self.sweep_due(now)
        sw = self.sweep
        v.update(last_sweep=_iso(self.last_sweep_unix) if self.last_sweep_unix else None,
                 next_sweep=_iso(due) if due is not None else None,
                 sweep=None if sw is None else {"phase": sw["phase"], "started": _iso(sw["start_unix"]),
                                                "frames": sw["frames"]},
                 outlook={k: v2 for k, v2 in (self.outlook or {}).items() if k != "key"} or None)
        return v

    def view(self, log_n=None):
        now = self.clock.time()
        c = self.current
        cur = None
        if c and self.state in ("harvesting", "sweep"):
            cur = {"label": c["key"].label(), "exposure": c["key"].exposure, "gain": c["key"].gain,
                   "offset": c["key"].offset, "binning": c["key"].binning, "kind": c["kind"], "tier": c["tier"],
                   "temp_bin": c["temp_bin"], "pool_n": self.svc.darkpool.count(c["key"], c["temp_bin"]),
                   "count": self.cfg.count, "reason": c["reason"], "frame_start": c.get("frame_start_iso"),
                   "purpose": c.get("purpose"), "predicted": c.get("pred_mean"),
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
            "thermal": self.thermal_view(now),
            "log": self._log_tail[-n:] if n > 0 else [],
            "config": {k: v for k, v in vars(self.cfg).items()},
        }
