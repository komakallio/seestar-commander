"""Target scheduler for plans with `order = "best"`.

The plan loop (service._job_run) normally runs the steps in order. With order = "best" it asks
`blocks()` here for the next (step, block) instead: at the start and after each block the scheduler
scores every step that still needs frames and is observable now, and the best one gets a block of
`block_frames` / `block_minutes` frames (the whole rest without either). A step with frames left is
revisited later; it is done when its count is reached. When nothing is observable but something will be
before the night ends, the scope parks and waits.

Everything except `blocks()` is a pure function of the plan, the progress table and a time, so the
status page can project the rest of the night (`project()`) with the same rules the worker uses.

Score (higher wins; only candidates with at least one frame that fits are scored):
  priority   25 x the step's `priority` (default 0)
  urgency    40 x (1 - spare / 3 h), clipped to 0..1, where spare = time until it sets or the night
             ends minus the time its remaining frames need: a target that would otherwise be lost
             goes first
  soon       10 / (1 + hours until it sets or the night ends)
  altitude   20 x (alt - limit) / (culmination alt - limit), clipped to 0..1: high / transiting first
  left       10 x frames left / count
  cable     -30 x the part of the block the RA cable window would cut off
Ties go to the earlier step. Equal targets alternate block by block (the one just imaged has fewer
frames left); there is deliberately no bonus for staying on the same target.
"""
import datetime as dt
import math
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from . import astro, unwind
from .plan import target_failures
from .statusview import DITHER_MOVE_S, SAMPLE_S, TARGET_OVERHEAD_S, night_window

HA_RATE_MIN = astro.SIDEREAL_DEG_S * 60.0    # deg of RA axis per minute of tracking
CABLE_MARGIN_DEG = 0.5                       # J2000 vs JNow and dither moves, on top of cable_reserve_min
W = {"priority": 25.0, "urgency": 40.0, "soon": 10.0, "altitude": 20.0, "left": 10.0, "cable": 30.0}
SLACK_S = 3 * 3600.0                         # urgency starts when a step has less than this to spare
ACTIVE = ("pending", "running", "interrupted")


@dataclass
class Ctx:
    """What the scheduler needs besides the plan: site, night, cable window, per-frame overhead."""
    lat: float
    lon: float
    cons: object                  # plan.Constraints
    end_t: float | None           # the plan's absolute end_time
    limit: object                 # plan.NightLimit (service-wide)
    overhead_s: float = 15.0      # download + save per frame
    reserve_min: float = 2.0      # cable_reserve_min
    u_min: float | None = None    # cable window, unwrapped HA (None: no cable check)
    u_max: float | None = None
    tz: str = "UTC"

    @classmethod
    def from_service(cls, svc):
        S = svc.s
        return cls(S.lat, S.lon, svc.plan.constraints, svc.plan_end_t, S.night_limit, S.frame_overhead_s,
                   S.cable_reserve_min, svc.scope.ha_u_min, svc.scope.ha_u_max, S.night_limit.timezone)

    @classmethod
    def from_settings(cls, S, plan, t):
        _, u_min, u_max = S.window
        return cls(S.lat, S.lon, plan.constraints, plan.constraints.end_after(t), S.night_limit,
                   S.frame_overhead_s, S.cable_reserve_min, u_min, u_max, S.night_limit.timezone)


@dataclass
class Candidate:
    step: int
    name: str
    frames: int                   # this block
    left: int                     # frames left before the block
    duration_s: float             # slew + centring + frames
    until: float                  # when it sets (or the night ends)
    score: float
    parts: dict

    def describe(self):
        return f"{self.name} x{self.frames} (score {self.score:.0f}: " + \
            ", ".join(f"{k} {v:+.0f}" for k, v in self.parts.items() if abs(v) >= 0.5) + ")"


# ---- pure pieces ---------------------------------------------------------------------------------

def frame_s(step, ctx):
    """Seconds per light including download/save and the step's share of dither moves."""
    s = step.exposure + ctx.overhead_s
    if getattr(step, "dither_every", 0):
        s += (step.dither_settle_s + DITHER_MOVE_S) / step.dither_every
    return s


def block_size(step, ctx):
    """Frames per visit: block_frames, or what fits in block_minutes (the smaller of both); 0 = no limit."""
    n = step.block_frames or math.inf
    if step.block_minutes:
        n = min(n, max(1, int(step.block_minutes * 60 // frame_s(step, ctx))))
    return n


def limit_alt(step, ctx, t):
    """The step's altitude limit at t: max(min alt, horizon at its azimuth)."""
    alt, az = astro.radec_to_altaz(step.ra, step.dec, t, ctx.lat, ctx.lon)
    lim = ctx.cons.min_alt if step.min_alt is None else step.min_alt
    if ctx.cons.horizon is not None:
        lim = max(lim, ctx.cons.horizon.alt_at(az))
    return alt, lim


def visible(step, ctx, t):
    return not target_failures(ctx.cons, step.ra, step.dec, t, ctx.lat, ctx.lon, step.min_alt)


def sets_at(step, ctx, t, until):
    """First time in (t, until] the step drops below its limit (refined to ~10 s), else `until`."""
    x = t
    while x < until:
        nx = min(x + SAMPLE_S, until)
        if not visible(step, ctx, nx):
            lo, hi = x, nx
            while hi - lo > 10:
                mid = (lo + hi) / 2
                lo, hi = (mid, hi) if visible(step, ctx, mid) else (lo, mid)
            return lo
        x = nx
    return until


def night_end(ctx, t):
    """(opens, ends): when imaging is allowed from t (t itself if it is now) and when that night ends."""
    reasons, opens, ends = night_window(ctx.lat, ctx.lon, ctx.cons, ctx.end_t, ctx.limit, t)
    return (t if not reasons else opens), ends


def cable_frames(step, ctx, t):
    """Frames whose tracking fits in the RA cable window if the step is slewed to at t."""
    if ctx.u_max is None:
        return math.inf
    ha = astro.wrap180(astro.lst_deg(t, ctx.lon) - step.ra)
    u = ha
    while u > ctx.u_max:
        u -= 360.0
    while u < ctx.u_min:
        u += 360.0
    if u > ctx.u_max:
        return 0
    left_min = (ctx.u_max - CABLE_MARGIN_DEG - u) / HA_RATE_MIN - ctx.reserve_min
    if getattr(step, "unwind", False) and ctx.u_max - 360.0 >= ctx.u_min + unwind.SLACK_DEG:
        # window wider than a turn: the lights loop unwinds inline without a wait (one more turn counted)
        left_min += 360.0 / HA_RATE_MIN - (360.0 / unwind.MOVE_RATE_DEG_S + unwind.MOVE_EXTRA_S
                                           + unwind.CENTRE_S) / 60.0
    return max(0, int(left_min * 60 // (step.exposure + ctx.overhead_s)))


def candidate(i, step, frames_done, t, ctx, ends):
    """Score step i for a block starting at t (None if it can't take a frame now). frames_done: so far."""
    left = step.count - frames_done
    if left <= 0 or not hasattr(step, "ra") or ends is None or t >= ends or not visible(step, ctx, t):
        return None
    fs = frame_s(step, ctx)
    until = sets_at(step, ctx, t, ends)
    n_win = int((until - t - TARGET_OVERHEAD_S) // fs)
    want = min(left, block_size(step, ctx))
    n_cab = cable_frames(step, ctx, t)
    n = int(min(want, n_win, n_cab))
    if n < 1:
        return None
    window = max(until - t, 1.0)
    need = left * fs + TARGET_OVERHEAD_S
    alt, lim = limit_alt(step, ctx, t)
    culm = 90.0 - abs(ctx.lat - step.dec)
    parts = {
        "priority": W["priority"] * step.priority,
        "urgency": W["urgency"] * min(1.0, max(0.0, 1.0 - (window - need) / SLACK_S)),
        "soon": W["soon"] / (1.0 + window / 3600.0),
        "altitude": W["altitude"] * (1.0 if culm <= lim else min(1.0, max(0.0, (alt - lim) / (culm - lim)))),
        "left": W["left"] * left / step.count,
        "cable": -W["cable"] * (1.0 - n_cab / want) if n_cab < want else 0.0,
    }
    return Candidate(i, step.name, n, left, TARGET_OVERHEAD_S + n * fs, until, sum(parts.values()),
                     {k: round(v, 1) for k, v in parts.items()})


def ranked(plan, frames, active, t, ctx, ends):
    """Candidates for a block at t, best first. frames[i]: done so far; active[i]: still wanted."""
    cs = [candidate(i, st, frames[i], t, ctx, ends)
          for i, st in enumerate(plan.steps) if active[i]]
    return sorted((c for c in cs if c), key=lambda c: (-c.score, c.step))


def next_start(plan, frames, active, t, ctx, ends):
    """(time, candidate) when the first block becomes possible after t, before the night ends; else None."""
    x = t
    while ends is not None and x < ends:
        nx = min(x + SAMPLE_S, ends)
        if ranked(plan, frames, active, nx, ctx, ends):
            lo, hi = x, nx
            while hi - lo > 30:
                mid = (lo + hi) / 2
                lo, hi = (lo, mid) if ranked(plan, frames, active, mid, ctx, ends) else (mid, hi)
            return hi, ranked(plan, frames, active, hi, ctx, ends)[0]
        x = nx
    return None


def wait_why(plan, c, t_from, t_at, ctx):
    """Why the scheduler waits for candidate c: "rises above 25°" or a generic reason."""
    st = plan.steps[c.step]
    if not visible(st, ctx, t_from):
        _, lim = limit_alt(st, ctx, t_at)
        return f"rises above {lim:.0f}°"
    return "becomes observable"


def no_more_why(plan, active, t, ctx):
    """Why nothing more gets scheduled tonight from t."""
    if any(a and visible(plan.steps[i], ctx, t) for i, a in enumerate(active)):
        return "night ends"                  # still up, but no block fits before the end
    return "nothing else observable before the night ends"


def hhmm(t, tz):
    return dt.datetime.fromtimestamp(t, ZoneInfo(tz)).strftime("%H:%M")


def project(plan, progress, t, ctx, max_blocks=300):
    """The rest of the night as the scheduler would run it from t: a list of blocks and waits, plus what
    won't fit. Steps whose status isn't pending/running/interrupted are left out."""
    frames = [p["frames"] for p in progress]
    active = [p["status"] in ACTIVE for p in progress]
    opens, ends = night_end(ctx, t)
    out, end_why = [], "all done"
    t = opens if opens is not None else t
    while len(out) < max_blocks:
        if not any(a and frames[i] < plan.steps[i].count for i, a in enumerate(active)):
            break
        if ends is None or t >= ends:
            end_why = "night ends"
            break
        cs = ranked(plan, frames, active, t, ctx, ends)
        if cs:
            c = cs[0]
            out.append({"kind": "block", "step": c.step, "name": c.name, "start": round(t, 1),
                        "end": round(t + c.duration_s, 1), "frames": c.frames, "score": round(c.score, 1)})
            frames[c.step] += c.frames
            t += c.duration_s
            continue
        w = next_start(plan, frames, active, t, ctx, ends)
        if w is None:
            end_why = no_more_why(plan, [a and frames[i] < plan.steps[i].count for i, a in enumerate(active)],
                                  t, ctx)
            break
        wt, c = w
        out.append({"kind": "wait", "step": c.step, "name": c.name, "start": round(t, 1), "end": round(wt, 1),
                    "why": wait_why(plan, c, t, wt, ctx)})
        t = wt
    left =[{"step": i, "name": st.name, "frames": st.count - frames[i]}
            for i, st in enumerate(plan.steps) if active[i] and frames[i] < st.count]
    return {"blocks": out, "unscheduled": left, "end": round(t, 1), "end_why": end_why,
            "night_end": round(ends, 1) if ends else None}


# ---- the service side --------------------------------------------------------------------------------

def blocks(svc, plan):
    """Generator for service._job_run with order = "best": yields (i, step) for each block after setting
    progress[i]["block_to"], parks and waits when nothing is observable yet, and returns when every step
    is done or nothing more can be observed before the night ends."""
    from .service import NightOver
    while True:
        svc._check_abort()
        P = svc.progress
        frames = [p["frames"] for p in P]
        active = [p["status"] == "pending" and p["frames"] < st.count for p, st in zip(P, plan.steps)]
        if not any(active):
            return
        now = svc.clock.time()
        ctx = Ctx.from_service(svc)
        _, ends = night_end(ctx, now)
        cs = ranked(plan, frames, active, now, ctx, ends)
        if cs:
            c, pr = cs[0], P[cs[0].step]
            others = "; ".join(f"{x.name} {x.score:.0f}" for x in cs[1:4])
            svc.log(f"scheduler: {c.describe()}" + (f"; next best {others}" if others else ""))
            before = pr["frames"]
            pr["block_to"], pr["block_t0"] = before + c.frames, round(now, 1)
            yield c.step, plan.steps[c.step]
            pr.pop("block_to", None)
            pr.pop("block_t0", None)
            if pr["status"] in ("stopped", "skipped") and before < pr["frames"] < plan.steps[c.step].count:
                pr["status"] = "pending"          # set or lost cable early, but made progress: may come back
            continue
        w = next_start(plan, frames, active, now, ctx, ends)
        if w is None:
            why = no_more_why(plan, active, now, ctx)
            why = "night ends before another block fits" if why == "night ends" else \
                "not observable again before the night ends"
            names = ", ".join(plan.steps[i].name for i, a in enumerate(active) if a)
            svc.log(f"scheduler: {why} ({names})")
            for i, a in enumerate(active):
                if a:
                    P[i]["status"], P[i]["reason"] = "skipped", why
            return
        wt, c = w
        _wait(svc, wt, c.name, wait_why(plan, c, now, wt, ctx), ctx.tz, NightOver)


def _wait(svc, until, name, why, tz, NightOver):
    """Park (tracking off, RA home, firmware Park: safest while nothing is imaged) and wait until `until`,
    interruptible by stop/unsafe, ending the plan if the night ends first."""
    at = hhmm(until, tz)
    svc.log(f"scheduler: nothing observable now; waiting for {name} ({why} at {at} {tz})")
    if not svc.scope.t.AtPark:
        if not svc._park(f"waiting for {name}"):
            raise RuntimeError("park failed before a scheduler wait")
        svc.scope.interrupt = svc.abort.is_set           # _park clears it
    svc._set(state="running", phase="wait", step=None, target=None)
    svc._act("wait", target=name, until=round(until, 1), why=why, at=at)
    while (now := svc.clock.time()) < until:
        svc._check_abort()
        nf = svc._night_failures(now)
        if nf:
            raise NightOver(nf)
        svc.clock.wait(svc.abort, min(60.0, until - now))
    svc._act(None)
