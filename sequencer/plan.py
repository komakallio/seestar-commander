"""Plan files (TOML, or YAML if PyYAML is installed), horizon files and observing constraints.

See plan.example.toml for the format. Coordinates are J2000 (ICRS) degrees internally.
"""
import datetime as dt
import json
import math
import os
import tomllib
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from . import astro


class PlanError(ValueError):
    pass


# A few bright targets so plans work offline; anything else needs ra/dec or a network name lookup.
CATALOG = {
    "M31": (10.6847, 41.2690), "M33": (23.4621, 30.6599), "M42": (83.8221, -5.3911),
    "M45": (56.6010, 24.1140), "M13": (250.4235, 36.4613), "M27": (299.9016, 22.7211),
    "M51": (202.4696, 47.1952), "M57": (283.3963, 33.0292), "M81": (148.8882, 69.0653),
    "M101": (210.8024, 54.3488), "M1": (83.6331, 22.0145), "M35": (92.2250, 24.3333),
    "NGC7000": (314.6800, 44.3333), "NGC869": (34.7500, 57.1333), "NGC884": (35.5833, 57.1333),
    "IC1396": (324.7417, 57.5000), "NGC7635": (350.2000, 61.2000), "M76": (25.5817, 51.5754),
}


def _norm(name):
    return name.upper().replace(" ", "")


def resolve_name(name):
    key = _norm(name)
    if key in CATALOG:
        return CATALOG[key]
    try:
        from astropy.coordinates import SkyCoord
        c = SkyCoord.from_name(name)
        return float(c.ra.deg), float(c.dec.deg)
    except Exception as e:  # noqa: BLE001
        raise PlanError(f"target {name!r}: not in the built-in list and name lookup failed ({e}); give ra/dec")


def parse_ra(v):
    if isinstance(v, (int, float)):
        return float(v) % 360.0
    from astropy.coordinates import Angle
    import astropy.units as u
    s = str(v).strip()
    try:
        if "d" in s.lower() and "h" not in s.lower():
            return Angle(s, unit=u.deg).deg % 360.0
        return Angle(s, unit=u.hourangle).deg % 360.0
    except Exception as e:  # noqa: BLE001
        raise PlanError(f"bad ra {v!r}: {e}")


def parse_dec(v):
    if isinstance(v, (int, float)):
        d = float(v)
    else:
        from astropy.coordinates import Angle
        import astropy.units as u
        try:
            d = Angle(str(v).strip(), unit=u.deg).deg
        except Exception as e:  # noqa: BLE001
            raise PlanError(f"bad dec {v!r}: {e}")
    if not -90.0 <= d <= 90.0:
        raise PlanError(f"dec {d} out of range")
    return d


# ---- horizon -----------------------------------------------------------------------------------

class Horizon:
    """Local horizon as (az, alt) points, linearly interpolated around 360 deg."""

    def __init__(self, points, source=""):
        pts = sorted(((float(a) % 360.0, float(h)) for a, h in points), key=lambda p: p[0])
        if len(pts) < 2:
            raise PlanError(f"horizon {source}: need at least 2 points")
        self.az = [p[0] for p in pts]
        self.alt = [p[1] for p in pts]
        self.source = source

    def alt_at(self, az):
        import numpy as np
        return float(np.interp(az % 360.0, self.az, self.alt, period=360.0))

    @classmethod
    def load(cls, path):
        if not os.path.exists(path):
            raise PlanError(f"horizon file not found: {path}")
        if path.lower().endswith(".json"):
            with open(path) as fh:
                d = json.load(fh)
            if isinstance(d, dict) and "points" in d:
                pts = d["points"]
            elif isinstance(d, dict) and "dense_az" in d:
                pts = list(zip(d["dense_az"], d["dense_alt"]))
            elif isinstance(d, list):
                pts = d
            else:
                raise PlanError(f"horizon {path}: expected 'points', 'dense_az'/'dense_alt' or a list")
            return cls(pts, path)
        pts = []
        with open(path) as fh:
            for line in fh:
                line = line.split("#")[0].replace(",", " ").split()
                if len(line) >= 2:
                    try:
                        pts.append((float(line[0]), float(line[1])))
                    except ValueError:
                        continue
        return cls(pts, path)


# ---- plan ----------------------------------------------------------------------------------------

@dataclass
class QA:
    min_stars: int = 0
    max_hfr: float = math.inf       # px (full resolution)
    max_ecc: float = 1.0


@dataclass
class Constraints:
    min_alt: float = 15.0
    horizon: Horizon | None = None
    sun_max_alt: float = -12.0
    end_time: str | None = None     # "HH:MM" (next occurrence, in timezone) or ISO datetime
    timezone: str = "UTC"

    def end_after(self, t0):
        """Absolute end-of-night (unix) for a run starting at t0, or None."""
        if not self.end_time:
            return None
        tz = ZoneInfo(self.timezone)
        s = str(self.end_time)
        if len(s) <= 5 and ":" in s:
            return _next_hhmm(s, dt.datetime.fromtimestamp(t0, tz)).timestamp()
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=tz)
        return d.timestamp()


def _next_hhmm(hhmm, start):
    """First "HH:MM" strictly after the aware datetime `start`, in its timezone."""
    hh, mm = (int(x) for x in str(hhmm).split(":"))
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError(f"bad time {hhmm!r}")
    end = start.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if end <= start:
        end += dt.timedelta(days=1)
    return end


@dataclass
class NightLimit:
    """Service-wide end of the night ([sequencer] sun_limit_deg / latest_end): applies to every plan on
    top of the plan's own constraints, and the stricter of the two wins. latest_end is a local "HH:MM"
    that belongs to the night: the first HH:MM after the local noon before t, so "05:30" means
    tomorrow morning in the evening and is already past at 06:00 (until the next noon)."""
    sun_max_alt: float = -8.0
    latest_end: str | None = None
    timezone: str = "UTC"

    def __post_init__(self):
        ZoneInfo(self.timezone)
        if self.latest_end:
            _next_hhmm(self.latest_end, dt.datetime.now(dt.timezone.utc))

    def end_for(self, t):
        """latest_end of the night that contains t (unix), or None."""
        if not self.latest_end:
            return None
        local = dt.datetime.fromtimestamp(t, ZoneInfo(self.timezone))
        noon = local.replace(hour=12, minute=0, second=0, microsecond=0)
        if noon > local:
            noon -= dt.timedelta(days=1)
        return _next_hhmm(self.latest_end, noon - dt.timedelta(microseconds=1)).timestamp()

    def describe(self):
        return f"sun {self.sun_max_alt:g}" + (f", latest {self.latest_end} {self.timezone}" if self.latest_end else "")


@dataclass
class LightStep:
    name: str
    ra: float
    dec: float
    exposure: float = 60.0
    gain: int = 200                 # 200: the S30 Pro telephoto's low-noise (HCG) mode starts here
    count: int = 10
    filter: str = "IR"
    dither_every: int = 0           # dither before every Nth light (0 = off)
    dither_px: float = 15.0         # max offset from the centred position, telephoto px (radius of a disc)
    dither_settle_s: float = 5.0    # wait after the move before the next exposure
    dither_recentre_every: int = 0  # plate-solve re-centre instead of every Nth dither (0 = never)
    dither_recentre_px: float = 0.0  # ... or once drift since the last centring exceeds this (0 = off)
    centre: str = "auto"            # auto (solve, else fallback) | solve | blob | none
    fallback: str = "none"          # when solving is unavailable/fails: none | blob
    centre_tol_arcmin: float = 3.0
    solve_exposure: float = 2.0     # centring solves (ASTAP needs ~100 stars: 2 s at gain 200 is plenty)
    min_alt: float | None = None    # overrides the plan's min_alt
    qa: QA = field(default_factory=QA)
    kind: str = "light"
    wide_every: int | None = None   # one wide-camera frame per N lights (0 = off; None = [wide] every)
    wide_exposure: float | None = None   # s (None = [wide] exposure)
    wide_gain: int | None = None    # (None = [wide] gain)
    priority: float = 0.0           # order = "best": higher is chosen first, other things equal
    block_frames: int = 0           # order = "best": frames per visit (0 = no limit; see block_minutes)
    block_minutes: float = 0.0      # order = "best": or minutes per visit (0 = no limit); the smaller wins
    unwind: bool = True             # at the cable limit: turn RA back a full turn and re-centre (unwind.py)
    unwind_min_frames: int = 3      # ... only if at least this many frames then fit
    unwind_min_minutes: float = 10.0   # ... and at least this much imaging time (frames x (exposure + overhead))
    unwind_max_wait_min: float = 0.0   # window < ~363 deg: wait at most this long, tracking off, for the
                                       # target to clear the gap behind the CCW end (0 = never wait)

    @property
    def dither(self):
        return self.dither_every > 0

    def describe(self):
        d = f", dither {self.dither_px:g} px every {self.dither_every}" if self.dither else ""
        if self.wide_every:
            d += f", wide every {self.wide_every}"
        blk = ([f"{self.block_frames}"] if self.block_frames else []) \
            + ([f"{self.block_minutes:g} min"] if self.block_minutes else [])
        if blk:
            d += ", blocks of " + " / ".join(blk)
        if self.priority:
            d += f", priority {self.priority:g}"
        return f"{self.name}: {self.count} x {self.exposure:g}s gain {self.gain} {self.filter}{d}"


@dataclass
class Plan:
    name: str
    constraints: Constraints
    steps: list
    park_at_end: bool = True
    source: str = ""
    open_roof_at_start: bool = False    # open the roof (only when safe) before the first target
    close_roof_at_end: bool = False     # close the roof after a verified end-of-plan park
    order: str = "sequence"             # sequence: steps in order | best: sequencer/scheduler.py picks

    def light_settings(self):
        """Unique (exposure, gain) pairs of the lights: what the dark library must cover."""
        seen = []
        for s in self.steps:
            if (s.exposure, s.gain) not in seen:
                seen.append((s.exposure, s.gain))
        return seen


_LIGHT_KEYS = {"type", "target", "name", "ra", "dec", "exposure", "gain", "count", "filter", "dither",
               "dither_every", "dither_px", "dither_settle_s", "dither_recentre_every", "dither_recentre_px",
               "centre", "center", "fallback", "centre_tol_arcmin", "solve_exposure",
               "min_alt", "qa", "wide_every", "wide_exposure", "wide_gain",
               "priority", "block_frames", "block_minutes",
               "unwind", "unwind_min_frames", "unwind_min_minutes", "unwind_max_wait_min"}

ORDERS = ("sequence", "best")


def _qa(d):
    d = d or {}
    unknown = set(d) - {"min_stars", "max_hfr", "max_ecc"}
    if unknown:
        raise PlanError(f"unknown qa keys {sorted(unknown)}")
    return QA(int(d.get("min_stars", 0)), float(d.get("max_hfr", math.inf)), float(d.get("max_ecc", 1.0)))


def parse(data, base_dir=".", source=""):
    """Build a Plan from a parsed TOML/YAML dict. Relative paths resolve against base_dir."""
    if not isinstance(data, dict):
        raise PlanError("plan must be a table/mapping")
    p = data.get("plan", {})
    c = data.get("constraints", {})
    defaults = data.get("defaults", {})
    hz = None
    if c.get("horizon"):
        hp = c["horizon"]
        hz = Horizon.load(hp if os.path.isabs(hp) else os.path.join(base_dir, hp))
    cons = Constraints(min_alt=float(c.get("min_alt", 15.0)), horizon=hz,
                       sun_max_alt=float(c.get("sun_max_alt", -12.0)),
                       end_time=c.get("end_time"), timezone=c.get("timezone", "UTC"))
    try:
        ZoneInfo(cons.timezone)
    except Exception as e:  # noqa: BLE001
        raise PlanError(f"bad timezone {cons.timezone!r}: {e}")
    if cons.end_time:
        try:
            cons.end_after(0)
        except Exception as e:  # noqa: BLE001
            raise PlanError(f"bad end_time {cons.end_time!r}: {e}")
    steps_in = data.get("step", data.get("steps"))
    if not steps_in:
        raise PlanError("plan has no [[step]] entries")
    steps = []
    for i, s in enumerate(steps_in):
        kind = s.get("type", "light")
        where = f"step {i + 1}"
        if kind == "light":
            unknown = set(s) - _LIGHT_KEYS
            if unknown:
                raise PlanError(f"{where}: unknown keys {sorted(unknown)}")
            m = {**defaults, **s}
            name = m.get("target") or m.get("name")
            if "ra" in m and "dec" in m:
                ra, dec = parse_ra(m["ra"]), parse_dec(m["dec"])
                name = name or f"RA{ra:.2f}_Dec{dec:+.2f}"
            elif name:
                ra, dec = resolve_name(name)
            else:
                raise PlanError(f"{where}: needs target (name) or ra and dec")
            centre = m.get("centre", m.get("center", "auto"))
            if centre not in ("auto", "solve", "blob", "none"):
                raise PlanError(f"{where}: centre must be auto, solve, blob or none")
            fb = m.get("fallback", "none")
            if fb not in ("none", "blob"):
                raise PlanError(f"{where}: fallback must be none or blob")
            # dither_every = N turns it on; `dither = true` alone means every frame, `dither = false` off
            if m.get("dither") is False:
                every = 0
            elif "dither_every" in m:
                every = int(m["dither_every"])
            else:
                every = 1 if m.get("dither") else 0
            st = LightStep(name=str(name), ra=ra, dec=dec,
                           exposure=float(m.get("exposure", 60)), gain=int(m.get("gain", 200)),
                           count=int(m.get("count", 10)), filter=str(m.get("filter", "IR")),
                           dither_every=every, dither_px=float(m.get("dither_px", 15.0)),
                           dither_settle_s=float(m.get("dither_settle_s", 5.0)),
                           dither_recentre_every=int(m.get("dither_recentre_every", 0)),
                           dither_recentre_px=float(m.get("dither_recentre_px", 0.0)),
                           centre=centre, fallback=fb,
                           centre_tol_arcmin=float(m.get("centre_tol_arcmin", 3.0)),
                           solve_exposure=float(m.get("solve_exposure", 2.0)),
                           min_alt=float(m["min_alt"]) if "min_alt" in m else None,
                           qa=_qa(m.get("qa")),
                           wide_every=int(m["wide_every"]) if "wide_every" in m else None,
                           wide_exposure=float(m["wide_exposure"]) if "wide_exposure" in m else None,
                           wide_gain=int(m["wide_gain"]) if "wide_gain" in m else None,
                           priority=float(m.get("priority", 0.0)),
                           block_frames=int(m.get("block_frames", 0)),
                           block_minutes=float(m.get("block_minutes", 0.0)),
                           unwind=bool(m.get("unwind", True)),
                           unwind_min_frames=int(m.get("unwind_min_frames", 3)),
                           unwind_min_minutes=float(m.get("unwind_min_minutes", 10.0)),
                           unwind_max_wait_min=float(m.get("unwind_max_wait_min", 0.0)))
            if st.unwind_min_frames < 1 or st.unwind_min_minutes < 0 or st.unwind_max_wait_min < 0:
                raise PlanError(f"{where}: unwind_min_frames must be >= 1, unwind_min_minutes and "
                                "unwind_max_wait_min >= 0")
            if st.exposure <= 0 or st.count < 1:
                raise PlanError(f"{where}: exposure must be > 0 and count >= 1")
            if st.dither_every < 0 or st.dither_px <= 0 or st.dither_settle_s < 0 \
                    or st.dither_recentre_every < 0 or st.dither_recentre_px < 0:
                raise PlanError(f"{where}: dither_every, dither_recentre_* and dither_settle_s must be >= 0, "
                                "dither_px > 0")
            if (st.wide_every is not None and st.wide_every < 0) \
                    or (st.wide_exposure is not None and not 3e-5 <= st.wide_exposure <= 60.0) \
                    or (st.wide_gain is not None and st.wide_gain < 0):
                raise PlanError(f"{where}: wide_every must be >= 0, wide_exposure 30 us .. 60 s, wide_gain >= 0")
            if st.block_frames < 0 or st.block_minutes < 0:
                raise PlanError(f"{where}: block_frames and block_minutes must be >= 0")
            if st.filter == "Dark":
                raise PlanError(f"{where}: lights can't use the Dark filter; darks come from the dark library")
            steps.append(st)
        elif kind == "darks":
            raise PlanError(f"{where}: darks are not plan steps; they come from the dark library "
                            "(`python -m sequencer darks build --plan FILE`)")
        else:
            raise PlanError(f"{where}: unknown type {kind!r} (only light)")
    pl = Plan(name=str(p.get("name", "plan")), constraints=cons, steps=steps,
              park_at_end=bool(p.get("park_at_end", True)), source=source,
              open_roof_at_start=bool(p.get("open_roof_at_start", False)),
              close_roof_at_end=bool(p.get("close_roof_at_end", False)),
              order=str(p.get("order", "sequence")))
    if pl.order not in ORDERS:
        raise PlanError(f"order must be one of {', '.join(ORDERS)} (got {pl.order!r})")
    if pl.close_roof_at_end and not pl.park_at_end:
        raise PlanError("close_roof_at_end needs park_at_end = true (the roof only closes after a verified park)")
    return pl


def loads(text, fmt="toml", base_dir=".", source=""):
    if fmt in ("yaml", "yml"):
        try:
            import yaml
        except ImportError:
            raise PlanError("YAML plans need PyYAML (pip install pyyaml), or use TOML")
        data = yaml.safe_load(text)
    else:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise PlanError(f"TOML: {e}")
    return parse(data, base_dir=base_dir, source=source)


def load(path):
    fmt = "yaml" if path.lower().endswith((".yaml", ".yml")) else "toml"
    with open(path) as fh:
        return loads(fh.read(), fmt, base_dir=os.path.dirname(os.path.abspath(path)), source=path)


# ---- constraint checks ---------------------------------------------------------------------------

def target_failures(cons: Constraints, ra, dec, t, lat, lon, min_alt=None):
    """Reasons why a J2000-ish (ra, dec) is not observable at unix time t (empty list = fine)."""
    alt, az = astro.radec_to_altaz(ra, dec, t, lat, lon)
    lim = cons.min_alt if min_alt is None else min_alt
    out = []
    if alt < lim:
        out.append(f"altitude {alt:.1f} < min {lim:.1f}")
    if cons.horizon is not None:
        h = cons.horizon.alt_at(az)
        if alt < h:
            out.append(f"below horizon: alt {alt:.1f} < {h:.1f} at az {az:.0f}")
    return out


def sun_max_alt(cons, limit=None):
    """(highest allowed Sun altitude, who set it): the stricter of the plan's sun_max_alt and the
    service's night limit. cons may be None (the service limit alone)."""
    if limit is None or (cons is not None and cons.sun_max_alt <= limit.sun_max_alt):
        return (cons.sun_max_alt if cons is not None else math.inf), "plan"
    return limit.sun_max_alt, "night limit"


def end_time(cons_end_t, limit, t):
    """(earliest end, who set it) for time t: the plan's absolute end_t or the night limit's latest_end."""
    le = limit.end_for(t) if limit is not None else None
    if le is not None and (cons_end_t is None or le < cons_end_t):
        return le, "night limit"
    return cons_end_t, "plan"


def night_failures(cons: Constraints | None, t, lat, lon, end_t=None, limit: NightLimit | None = None):
    """Plan-wide reasons to stop at time t: the Sun above the stricter of the plan's sun_max_alt and the
    service night limit, or past the earlier of the plan's end_t and the limit's latest_end. Reasons
    that come from the service limit say "(night limit)"."""
    out = []
    s = astro.sun_alt(t, lat, lon)
    lim, who = sun_max_alt(cons, limit)
    if s > lim:
        out.append(f"sun altitude {s:.1f} > {lim:.1f}" + (" (night limit)" if who == "night limit" else ""))
    e, who = end_time(end_t, limit, t)
    if e is not None and t > e:
        tz = limit.timezone if who == "night limit" else (cons.timezone if cons is not None else "UTC")
        out.append(("past latest_end " if who == "night limit" else "past end_time ")
                   + dt.datetime.fromtimestamp(e, ZoneInfo(tz)).isoformat()
                   + (" (night limit)" if who == "night limit" else ""))
    return out


def limit_reached(reasons):
    """True if any of night_failures' reasons comes from the service night limit."""
    return any(r.endswith("(night limit)") for r in reasons)
