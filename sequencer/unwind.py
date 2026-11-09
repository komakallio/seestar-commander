"""Unwind and re-acquire: the window maths (pure functions; the service does the moves).

Tracking turns the RA axis clockwise (+HA) at the sidereal rate, so a target uses up the cable window.
When the window can't fit the next frame plus the reserve, the same sky position is also at the
unwrapped hour angle u - 360 (one turn back, counter-clockwise, through home): the axis angle differs
by a full turn, so the field, the camera orientation and the pointing are the same, but the cable has
the whole window to wind up again.

That turn is only inside the window if the window is wider than 360 deg plus the tracking room the
target needs. A window narrower than that (e.g. -175..+175, 350 deg) leaves a gap behind the CCW end:
u - 360 lies below the window, and the target only comes back in after the sky has turned it across
the gap (tracking off, waiting at the CCW end). `options` lists the turns (k = 1, 2, ...) whose wait
fits `max_wait_min`; for each it estimates when the scope is back on target, centred, and how many of
the remaining frames the cable window and the sky (altitude, horizon, Sun, night limit) then allow.
"""
from dataclasses import dataclass

MOVE_RATE_DEG_S = 5.0     # Scope._axis_to fast stage: a full turn takes ~72 s plus the approach
MOVE_EXTRA_S = 20.0       # slower approach stages and settle
CENTRE_S = 120.0          # re-centring with plate solves after the move
SLACK_DEG = 2.0           # the target must be this far inside the CCW end before the scope goes to it
                          # (room for the pointing-memory offset of the first move and the centring)


@dataclass
class Option:
    turns: int            # full turns back (counter-clockwise)
    u_now: float          # the target's unwrapped HA on that turn, now
    wait_s: float         # tracking off at the CCW end until the target is SLACK_DEG inside the window
    move_s: float         # estimated axis move time
    arrive_t: float       # estimated time the scope is back on target and centred
    room_min: float       # tracking left from arrival to the CW limit, less the reserve
    frames: int           # frames that fit after arrival (cable room, sky, at most `todo`)


def options(u, u_min, u_max, rate_deg_min, need_min, reserve_min, todo, now, max_wait_min, sky_frames,
            centre_s=CENTRE_S):
    """Candidate unwinds from the current unwrapped HA u (the target's, tracking).

    rate_deg_min: tracking rate (deg of RA axis per minute); need_min: one frame plus overhead;
    sky_frames(t, n): how many of n consecutive frames starting at unix time t the sky allows."""
    out = []
    k = 1
    while k < 10:
        uk = u - 360.0 * k
        gap = max(0.0, u_min + SLACK_DEG - uk)          # deg the sky must turn before the target is inside
        wait_s = gap / rate_deg_min * 60.0
        if wait_s > max_wait_min * 60.0:
            break
        dist = abs(u - (uk + gap))                      # to the target, or to where it enters the window
        move_s = dist / MOVE_RATE_DEG_S + MOVE_EXTRA_S
        arrive = max(now + move_s, now + wait_s) + centre_s
        u_arr = uk + rate_deg_min * (arrive - now) / 60.0
        room = (u_max - u_arr) / rate_deg_min - reserve_min
        fit = min(todo, max(0, int(room // need_min))) if room > 0 else 0
        out.append(Option(k, uk, wait_s, move_s, arrive, room, sky_frames(arrive, fit) if fit else 0))
        k += 1
    return out


def choose(opts, todo):
    """The fewest turns (then the shortest wait) with room for all `todo` frames, else the most frames."""
    full = [o for o in opts if o.frames >= todo]
    if full:
        return min(full, key=lambda o: (o.wait_s, o.turns))
    useful = [o for o in opts if o.frames > 0]
    return max(useful, key=lambda o: (o.frames, -o.wait_s)) if useful else None
