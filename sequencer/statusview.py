"""Derived views for the status page: the headline ("M45 exposure 1/3 (37 / 60 s)"), the
estimated finish of the plan and the night timeline (altitude curves). Pure functions of the
service state, so they are cheap to test without a running service."""
import math

from . import astro

TARGET_OVERHEAD_S = 180.0      # slew + centring per target, for the finish estimate
SAMPLE_S = 300.0               # timeline resolution

_STATE_TEXT = {"idle": "Idle", "done": "Done", "parked": "Parked", "stopped": "Stopped", "error": "Error",
               "stopping": "Stopping", "running": "Running", "darks": "Dark frames", "parking": "Parking"}


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
    if k in ("expose", "dark"):
        exp, el = float(a.get("exposure") or 0), max(0.0, (now or 0) - float(a.get("start") or 0))
        what = f"{t} exposure" if k == "expose" else f"Dark {a.get('label')}"
        if el >= exp:
            return f"Downloading {t if k == 'expose' else 'dark'} {a.get('frame')}/{a.get('count')}"
        return f"{what} {a.get('frame')}/{a.get('count')} ({_fmt_s(el)} / {exp:g} s)"
    if k == "download":
        return f"Downloading {t} {a.get('frame')}/{a.get('count')}"
    sf = s.get("safety") or {}
    if sf.get("safe") is False and not busy:
        return "Waiting: unsafe (" + (", ".join(sf.get("reasons") or []) or "?") + ")"
    return _STATE_TEXT.get(state, str(state).capitalize())


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


_cache = {}


def _night_end(svc, now):
    """(sun-limit time, plan end time) for the active plan; sun limit cached per 5 minutes."""
    c = svc.plan.constraints
    key = (id(svc.plan), int(now // SAMPLE_S))
    if _cache.get("key") != key:
        t = now     # in daylight, the limit we care about is the morning one after dusk
        while t < now + 86400 and astro.sun_alt(t, svc.s.lat, svc.s.lon) > c.sun_max_alt:
            t += SAMPLE_S
        _cache.update(key=key, sun=sun_limit_after(t, svc.s.lat, svc.s.lon, c.sun_max_alt))
    return _cache["sun"], svc.plan_end_t


def annotate(svc, s, now):
    """Add headline and finish estimate to a status() dict, in place."""
    s["headline"] = headline(s, now)
    p = s.get("plan")
    if not p or not svc.plan:
        return s
    sun_t, end_t = _night_end(svc, now)
    rem = _remaining_s(svc, now, s.get("action"))
    p["sun_limit_unix"] = round(sun_t, 1) if sun_t else None
    p["end_unix"] = round(end_t, 1) if end_t else None
    if rem is not None and (svc.busy == "run" or any(x["status"] in ("pending", "interrupted")
                                                     for x in svc.progress)):
        eta = now + rem
        limit = min([x for x in (sun_t, end_t) if x] or [math.inf])
        p["eta_unix"] = round(min(eta, limit), 1)
        p["eta_cut"] = eta > limit          # the plan won't finish before the night ends
    return s


def timeline(svc, now=None):
    """Altitude curves of the plan's targets for the rest of the night, for the page's SVG."""
    now = svc.clock.time() if now is None else now
    S = svc.s
    if not svc.plan:
        return {"targets": [], "now": now}
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
    return {"t": [round(t, 1) for t in ts], "sun": sun, "sun_max_alt": c.sun_max_alt, "min_alt": c.min_alt,
            "horizon": c.horizon is not None, "now": round(now, 1), "start": round(t0, 1), "end": round(t1, 1),
            "sun_limit": round(sun_t, 1) if sun_t else None, "plan_end": round(end_t, 1) if end_t else None,
            "eta": (s.get("plan") or {}).get("eta_unix"), "targets": targets}
