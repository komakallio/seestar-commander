"""Derived views for the status page: the headline ("M45 exposure 1/3 (37 / 60 s)"), the
estimated finish of the plan and the night timeline (altitude curves). Pure functions of the
service state, so they are cheap to test without a running service."""
import datetime as dt
import math

from . import astro
from .plan import end_time, night_failures, sun_max_alt

TARGET_OVERHEAD_S = 180.0      # slew + centring per target, for the finish estimate
SAMPLE_S = 300.0               # timeline resolution
DITHER_MOVE_S = 8.0            # dither pulses, for the finish estimate (plus the step's settle time)

_STATE_TEXT = {"idle": "Idle", "done": "Done", "parked": "Parked", "stopped": "Stopped", "error": "Error",
               "stopping": "Stopping", "running": "Running", "darks": "Dark frames", "flats": "Flat frames",
               "parking": "Parking"}


def _fmt_s(x):
    return f"{x:.0f}"


def headline(s, now=None):
    """One line saying what the service is doing right now. `s` is a status() dict."""
    now = s.get("time_unix") if now is None else now
    a = s.get("action") or {}
    k, t = a.get("kind"), a.get("target")
    busy, state = s.get("busy"), s.get("state")
    if k == "park" or busy == "park":
        return "Parking"
    if k == "roof_close" or busy == "roof_close":
        return "Closing roof"
    if k == "roof_open" or busy == "roof_open":
        return "Opening roof"
    if busy == "polar":
        return "Polar alignment"
    if state == "stopping":
        return "Stopping"
    if k == "slew":
        return f"Slewing to {t}"
    if k == "centre":
        if a.get("method") == "blob":
            return f"Centring {t} (blob)"
        off = a.get("off_arcmin")
        return f"Centring {t} (solve {a.get('solve')}/{a.get('of')}" + \
            (f", {off:.1f}′ off)" if off is not None else ")")
    if k == "wait":
        return f"Waiting for {t} ({a.get('why')} at {a.get('at')})"
    if k == "unwind":
        until = a.get("until")
        if until and now is not None and until > now:
            return f"Unwinding cable ({t}): waiting {(until - now) / 60:.0f} min for it to clear the cable stop"
        return f"Unwinding cable ({t})"
    if k == "dither":
        return f"Dithering {t} (#{a.get('n')}, {a.get('px', 0):.0f} px)"
    if k == "flat_wait":
        return "Flats: waiting for the panel (confirm)"
    if k in ("expose", "dark", "flat"):
        exp, el = float(a.get("exposure") or 0), max(0.0, (now or 0) - float(a.get("start") or 0))
        what = {"expose": f"{t} exposure", "dark": f"Dark {a.get('label')}"}.get(k) or \
            (f"Flat {a.get('label')}" + (" auto-exposure" if a.get("what") == "auto-exposure" else ""))
        if el >= exp:
            return f"Downloading {t if k == 'expose' else k} {a.get('frame')}/{a.get('count')}"
        return f"{what} {a.get('frame')}/{a.get('count')} ({_fmt_s(el)} / {exp:g} s)"
    if k == "download":
        return f"Downloading {t} {a.get('frame')}/{a.get('count')}"
    lk = s.get("lock") or {}
    if lk.get("locked") and not busy:
        return f"Scope in use by {lk.get('owner')}"
    sf = s.get("safety") or {}
    if sf.get("safe") is False and not busy:
        return "Waiting: unsafe (" + (", ".join(sf.get("reasons") or []) or "?") + ")"
    if not busy and state in ("idle", "done", "parked", "stopped"):
        h = harvest_headline(s.get("harvest"), now)
        if h:
            return h
    return _STATE_TEXT.get(state, str(state).capitalize())


def harvest_headline(hv, now=None):
    """Short text for the dark harvest while the service is otherwise idle; None when it's off."""
    if not hv or not hv.get("enabled"):
        return None
    st, hl, r = hv.get("state"), hv.get("headline") or "", hv.get("reason") or ""
    if st == "harvesting":
        c = hv.get("current") or {}
        if c.get("exposure") is not None and c.get("temp") is not None and c.get("temp_bin") is not None:
            return (f"Harvesting darks · {c['exposure']:g} s g{c['gain']} at {c['temp']:+.1f} °C "
                    f"(bin {c['temp_bin']:+.0f}, {c.get('pool_n', 0)}/{c.get('count', 15)})")
        return "Harvesting darks · " + (hl[len("Harvesting "):] if hl.startswith("Harvesting ") else hl)
    if st == "waiting_cool":
        return "Darks: " + (hl[:1].lower() + hl[1:] if hl else "waiting for the sensor to cool")
    if st == "holdoff":
        until = hv.get("holdoff_until_unix")
        return "Darks: holding off" + (f" ({max(1, round((until - now) / 60))} min)" if until and now else "")
    if st == "paused":
        return "Darks paused" + (f": {r}" if r else "")
    if st == "no_reading":
        return "Darks paused: no sensor reading"
    if st == "error":
        return "Darks: error" + (f" ({r})" if r else "")
    return None


def _remaining_s(svc, now, action):
    """Seconds of work left in the plan, from the progress table."""
    if not svc.plan:
        return None
    tot = 0.0
    for st, pr in zip(svc.plan.steps, svc.progress):
        if pr["status"] not in ("pending", "running", "interrupted") or not hasattr(st, "exposure"):
            continue
        left = max(0, st.count - pr["frames"])
        tot += left * (st.exposure + svc.s.frame_overhead_s)
        if getattr(st, "dither_every", 0):          # pulses + settle per dither
            tot += left / st.dither_every * (st.dither_settle_s + DITHER_MOVE_S)
        if pr["status"] != "running":
            tot += TARGET_OVERHEAD_S
        elif action and action.get("kind") == "expose":
            tot -= min(st.exposure, max(0.0, now - action.get("start", now)))
    return tot


def sun_limit_after(t0, lat, lon, sun_max_alt, horizon_s=86400.0, step=SAMPLE_S):
    """First time >= t0 the Sun rises above sun_max_alt (None if not within horizon_s).
    If the Sun is already above, returns t0."""
    t = t0
    while t <= t0 + horizon_s:
        if astro.sun_alt(t, lat, lon) > sun_max_alt:
            if t == t0:
                return t0
            lo, hi = t - step, t           # refine to ~10 s
            while hi - lo > 10:
                mid = (lo + hi) / 2
                lo, hi = (lo, mid) if astro.sun_alt(mid, lat, lon) > sun_max_alt else (mid, hi)
            return hi
        t += step
    return None


def _first(pred, t0, horizon_s=86400.0, step=SAMPLE_S):
    """First t >= t0 where pred(t) holds, refined to ~10 s; None if not within horizon_s."""
    if pred(t0):
        return t0
    t = t0
    while t < t0 + horizon_s:
        t += step
        if pred(t):
            lo, hi = t - step, t
            while hi - lo > 10:
                mid = (lo + hi) / 2
                lo, hi = (lo, mid) if pred(mid) else (mid, hi)
            return hi
    return None


_cache = {}


def night_window(lat, lon, cons, end_t, limit, now):
    """(reasons, opens, ends) for imaging under a plan's constraints (cons may be None) and the
    service night limit: why it isn't allowed now (empty = it is), when it next becomes allowed (None if
    allowed now) and when that night ends. Cached per 5 minutes."""
    lim, _ = sun_max_alt(cons, limit)
    key = ("win", lat, lon, lim, end_t, limit and (limit.latest_end, limit.timezone), int(now // SAMPLE_S))
    if key not in _cache:
        if len(_cache) > 64:
            _cache.clear()

        def stop(t):
            return bool(night_failures(cons, t, lat, lon, end_t, limit))
        if stop(now):
            opens = _first(lambda t: not stop(t), now)
            ends = _first(stop, opens) if opens is not None else None
        else:
            opens, ends = None, _first(stop, now)
        _cache[key] = (opens, ends)
    opens, ends = _cache[key]
    return night_failures(cons, now, lat, lon, end_t, limit), opens, ends


def _night_end(svc, now):
    """(sun-limit time, end time) for the active plan, both the stricter of the plan's constraints and
    the service night limit: the Sun crossing and the earlier of end_time / latest_end."""
    c, L = svc.plan.constraints, svc.s.night_limit
    _, opens, _ = night_window(svc.s.lat, svc.s.lon, c, svc.plan_end_t, L, now)
    t = opens if opens is not None else now     # in daylight, the limit we care about is the morning one after dusk
    key = ("sun", svc.s.lat, svc.s.lon, sun_max_alt(c, L)[0], int(t // SAMPLE_S))
    if key not in _cache:
        _cache[key] = sun_limit_after(t, svc.s.lat, svc.s.lon, sun_max_alt(c, L)[0])
    return _cache[key], end_time(svc.plan_end_t, L, t)[0]


def night_limit_view(svc, now):
    """The service night limit for status(): settings, reached now, and when it is reached."""
    L = svc.s.night_limit
    reasons, opens, ends = night_window(svc.s.lat, svc.s.lon, None, None, L, now)

    def iso(t):
        return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None
    return {"sun_limit_deg": L.sun_max_alt, "latest_end": L.latest_end, "timezone": L.timezone,
            "sun_alt": round(astro.sun_alt(now, svc.s.lat, svc.s.lon), 1),
            "reached": bool(reasons), "reasons": reasons,
            "at_unix": round(ends, 1) if ends else None, "at": iso(ends),
            "opens_unix": round(opens, 1) if opens else None, "opens": iso(opens)}


def annotate(svc, s, now):
    """Add headline and finish estimate to a status() dict, in place."""
    s["headline"] = headline(s, now)
    s["night_limit"] = night_limit_view(svc, now)
    p = s.get("plan")
    if not p or not svc.plan:
        return s
    sun_t, end_t = _night_end(svc, now)
    rem = _remaining_s(svc, now, s.get("action"))
    p["sun_limit_unix"] = round(sun_t, 1) if sun_t else None
    p["end_unix"] = round(end_t, 1) if end_t else None
    if svc.plan.order == "best":
        p["order"] = "best"
        sch = schedule(svc, now, s.get("action"))
        p["schedule"] = sch
        if sch["blocks"] and (svc.busy == "run" or any(x["status"] in ("pending", "interrupted")
                                                       for x in svc.progress)):
            p["eta_unix"] = sch["blocks"][-1]["end"]
            p["eta_cut"] = bool(sch["unscheduled"])     # frames the night won't hold
        return s
    if rem is not None and (svc.busy == "run" or any(x["status"] in ("pending", "interrupted")
                                                     for x in svc.progress)):
        eta = now + rem
        limit = min([x for x in (sun_t, end_t) if x] or [math.inf])
        p["eta_unix"] = round(min(eta, limit), 1)
        p["eta_cut"] = eta > limit          # the plan won't finish before the night ends
    return s


def schedule(svc, now, action=None):
    """order = "best": the scheduler's projected order for the rest of the night (sequencer/scheduler.py),
    starting with the block that is running. Cached until the progress changes or 5 minutes pass."""
    from . import scheduler
    P = svc.progress
    sig = tuple((p["status"], p["frames"], p.get("block_to")) for p in P)
    key = ("sched", id(svc.plan), svc.plan_t0, sig, (action or {}).get("kind") == "wait", int(now // SAMPLE_S))
    if key in _cache:
        return _cache[key]
    ctx = scheduler.Ctx.from_service(svc)
    prog, head, t = [dict(p) for p in P], [], now
    cur = svc.st.get("step")
    if svc.busy == "run" and isinstance(cur, int) and cur < len(P) and P[cur]["status"] == "running":
        st, pr = svc.plan.steps[cur], P[cur]
        to = pr.get("block_to", st.count)
        t = now + max(0, to - pr["frames"]) * scheduler.frame_s(st, ctx)
        head = [{"kind": "block", "step": cur, "name": st.name, "start": pr.get("block_t0", round(now, 1)),
                 "end": round(t, 1), "frames": to - pr["frames"], "running": True}]
        prog[cur].update(frames=to, status="pending")
    for p in prog:
        if p["status"] == "running":
            p["status"] = "pending"
    out = scheduler.project(svc.plan, prog, t, ctx)
    out["blocks"] = head + out["blocks"]
    if len(_cache) > 64:
        _cache.clear()
    _cache[key] = out
    return out


def timeline(svc, now=None):
    """Altitude curves of the plan's targets for the rest of the night, for the page's SVG."""
    now = svc.clock.time() if now is None else now
    S = svc.s
    if not svc.plan:
        return idle_timeline(svc, now)
    c = svc.plan.constraints
    sun_t, end_t = _night_end(svc, now)
    t0 = max(min(svc.plan_t0 or now, now), now - 3 * 3600)
    t1 = min([x for x in (sun_t, end_t) if x and x > now] or [now + 12 * 3600])
    t1 = max(t1, now + 3600) + 1200
    n = int((t1 - t0) // SAMPLE_S) + 2
    ts = [t0 + i * (t1 - t0) / (n - 1) for i in range(n)]
    sun = [round(astro.sun_alt(t, S.lat, S.lon), 1) for t in ts]
    targets = []
    for i, (st, pr) in enumerate(zip(svc.plan.steps, svc.progress)):
        if not hasattr(st, "ra"):
            continue
        lim0 = c.min_alt if getattr(st, "min_alt", None) is None else st.min_alt
        alt, lim = [], []
        for t in ts:
            a, az = astro.radec_to_altaz(st.ra, st.dec, t, S.lat, S.lon)
            alt.append(round(a, 1))
            lim.append(round(max(lim0, c.horizon.alt_at(az)) if c.horizon is not None else lim0, 1))
        targets.append({"step": i, "name": st.name, "status": pr["status"], "alt": alt, "lim": lim})
    s = svc.status(log=False, darklib=False)
    sun_lim, sun_by = sun_max_alt(c, S.night_limit)     # the page shades daylight above the effective limit
    return {"t": [round(t, 1) for t in ts], "sun": sun, "sun_max_alt": sun_lim, "sun_limit_by": sun_by,
            "moon": [round(astro.moon_alt(t, S.lat, S.lon), 1) for t in ts],
            "moon_illum": round(astro.moon_illumination(now), 2),
            "min_alt": c.min_alt,
            "horizon": c.horizon is not None, "now": round(now, 1), "start": round(t0, 1), "end": round(t1, 1),
            "sun_limit": round(sun_t, 1) if sun_t else None, "plan_end": round(end_t, 1) if end_t else None,
            "eta": (s.get("plan") or {}).get("eta_unix"), "targets": targets,
            "schedule": ((s.get("plan") or {}).get("schedule") or {}).get("blocks")}


def _crossing(t, lat, lon, lim, below, horizon_s=86400.0, step=SAMPLE_S):
    """First time >= t the Sun is below (below=True) / above lim, refined to ~10 s; None if not within
    horizon_s. t itself if it already is."""
    side = (lambda a: a < lim) if below else (lambda a: a > lim)
    if side(astro.sun_alt(t, lat, lon)):
        return t
    x = t
    while x <= t + horizon_s:
        x += step
        if side(astro.sun_alt(x, lat, lon)):
            lo, hi = x - step, x
            while hi - lo > 10:
                mid = (lo + hi) / 2
                lo, hi = (lo, mid) if side(astro.sun_alt(mid, lat, lon)) else (mid, hi)
            return hi
    return None


def idle_timeline(svc, now):
    """The night timeline without a plan: Sun and Moon altitude through tonight (the service night
    limit marks dusk and dawn), and now. Same shape as timeline() with no targets."""
    S, L = svc.s, svc.s.night_limit
    lim = L.sun_max_alt
    key = ("idle", S.lat, S.lon, lim, int(now // SAMPLE_S))
    if key not in _cache:
        if astro.sun_alt(now, S.lat, S.lon) < lim:          # night now: from dusk (at most 6 h back)
            dusk, x = None, now
            while x > now - 6 * 3600:
                x -= SAMPLE_S
                if astro.sun_alt(x, S.lat, S.lon) >= lim:
                    dusk = _crossing(x, S.lat, S.lon, lim, True, SAMPLE_S)
                    break
            dawn = _crossing(now, S.lat, S.lon, lim, False)
        else:
            dusk = _crossing(now, S.lat, S.lon, lim, True)
            dawn = _crossing(dusk, S.lat, S.lon, lim, False) if dusk else None
        t0 = min(now - 3600, (dusk - 1800) if dusk else now - 3600)
        t0 = max(t0, now - 6 * 3600)
        t1 = (dawn + 1800) if dawn else now + 12 * 3600
        t1 = min(max(t1, now + 3600), t0 + 24 * 3600)
        _cache[key] = (dusk, dawn, t0, t1)
    dusk, dawn, t0, t1 = _cache[key]
    n = int((t1 - t0) // SAMPLE_S) + 2
    ts = [t0 + i * (t1 - t0) / (n - 1) for i in range(n)]
    return {"t": [round(t, 1) for t in ts], "sun": [round(astro.sun_alt(t, S.lat, S.lon), 1) for t in ts],
            "moon": [round(astro.moon_alt(t, S.lat, S.lon), 1) for t in ts],
            "moon_illum": round(astro.moon_illumination(now), 2),
            "sun_max_alt": lim, "sun_limit_by": "night limit", "min_alt": None, "horizon": False,
            "now": round(now, 1), "start": round(t0, 1), "end": round(t1, 1),
            "dusk": round(dusk, 1) if dusk else None, "dawn": round(dawn, 1) if dawn else None,
            "sun_limit": None, "plan_end": None, "eta": None, "targets": [],
            "schedule": None, "idle": True}
