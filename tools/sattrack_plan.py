"""T0 pass planner: which satellite passes can the mount follow?

For each NORAD id, finds passes over a time span, samples them at 0.1 s in the mount frame (unwrapped
RA-axis hour angle, Dec), applies the cable window, Dec range, rate limit and runaway rule, and prints
a table. Writes a catalogue (summaries) and, with --out, one JSON per trackable pass with its samples
for the pass replay test. HA/Dec paths do not depend on when they are flown, so any pass, past or
future, can be replayed with the roof closed.

usage: sattrack_plan.py 25544 48274 [--fetch] [--start 2026-11-09T12:00Z] [--hours 48] [--out DIR]
Elements come from the cache (data/sattrack/elements); --fetch refreshes it first. See [sattrack]
in config.example.toml.
"""
import argparse
import datetime as dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
from sattrack import elements as el  # noqa: E402
from sattrack import geometry as geo  # noqa: E402
from sattrack import passes as ps  # noqa: E402
from sattrack.guard import Limits  # noqa: E402

CACHE = os.path.join(config.DATA_DIR, "sattrack", "elements")
UNWRAP = os.path.join(config.DATA_DIR, "ra_unwrap.json")


def parse_time(s):
    if s in (None, "now"):
        return time.time()
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).replace(
        tzinfo=dt.datetime.fromisoformat(s.replace("Z", "+00:00")).tzinfo or dt.timezone.utc).timestamp()


def iso(t, fmt="%Y-%m-%d %H:%M:%S"):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime(fmt)


def load_horizon(path):
    if not path:
        return None
    from sequencer.plan import Horizon
    return Horizon.load(path if os.path.isabs(path) else os.path.join(config.REPO, path))


def window():
    return (config.HOME_HA - config.CCW_LIMIT + config.LIMIT_MARGIN,
            config.HOME_HA + config.CW_LIMIT - config.LIMIT_MARGIN)


def main():
    g = lambda k, d=None: config.get("sattrack", k, d)  # noqa: E731
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("ids", nargs="+", type=int)
    ap.add_argument("--fetch", action="store_true", help="refresh cached elements first")
    ap.add_argument("--source", default=g("elements_source", ""), help="source name passed to the URL template")
    ap.add_argument("--start", default="now", help="ISO UTC start (default now)")
    ap.add_argument("--hours", type=float, default=48.0)
    ap.add_argument("--min-alt", type=float, default=float(g("min_alt", 10.0)))
    ap.add_argument("--min-track", type=float, default=float(g("min_track_s", 60.0)), help="s")
    ap.add_argument("--step", type=float, default=float(g("step_s", 0.1)))
    ap.add_argument("--horizon", default=g("horizon", ""))
    ap.add_argument("--max-age", type=float, default=None, help="max element age in hours")
    ap.add_argument("--out", default="", help="write catalogue.json and per-pass sample files here")
    ap.add_argument("--json", action="store_true", help="print the catalogue as JSON")
    a = ap.parse_args()

    site = geo.Site(config.LAT, config.LON, config.ELEVATION)
    u_min, u_max = window()
    lim = Limits.from_config(config.get, u_min, u_max)
    hz = load_horizon(a.horizon)
    u_now = None
    if os.path.exists(UNWRAP):
        with open(UNWRAP) as fh:
            u_now = json.load(fh).get("ha_unwrapped")
    t0 = parse_time(a.start)
    t1 = t0 + a.hours * 3600

    cat = {"created": time.time(), "start": t0, "hours": a.hours, "site": site.__dict__,
           "limits": lim.__dict__, "horizon": a.horizon, "min_alt": a.min_alt, "u_now": u_now, "passes": []}
    if a.out:
        os.makedirs(a.out, exist_ok=True)
    for norad in a.ids:
        if a.fetch:
            el.fetch(norad, CACHE, url=g("elements_url", el.DEFAULT_URL), source=a.source,
                     key_env=g("elements_key_env", ""), user_agent=g("user_agent", el.DEFAULT_UA))
        omm = el.load(norad, CACHE, a.source, max_age_h=a.max_age)
        sat = el.satellite(omm, geo.timescale())
        name = omm.get("OBJECT_NAME", str(norad))
        for rise, set_ in ps.find_passes(sat, site, t0, t1):
            p = ps.analyse(sat, site, rise, set_, lim, horizon=hz, min_alt=a.min_alt, step=a.step,
                           u_now=u_now, min_track_s=a.min_track, norad=norad, name=name,
                           keep_samples=bool(a.out))
            row = {"norad": norad, "name": name, "rise": rise, "set": set_, "verdict": p.verdict,
                   "reasons": p.reasons, "epoch": omm.get("EPOCH"), **p.summary}
            if a.out and p.samples:
                fn = f"pass-{norad}-{iso(rise, '%Y%m%dT%H%M%S')}.json"
                with open(os.path.join(a.out, fn), "w") as fh:
                    json.dump({"pass": row, "samples": p.samples}, fh)
                row["file"] = fn
            cat["passes"].append(row)
    cat["passes"].sort(key=lambda r: r["rise"])
    if a.out:
        with open(os.path.join(a.out, "catalogue.json"), "w") as fh:
            json.dump(cat, fh, indent=1)
    if a.json:
        print(json.dumps(cat, indent=1))
        return
    hdr = f"{'rise UTC':19} {'object':18} {'verdict':7} {'alt':>4} {'vis s':>6} {'trk s':>6} " \
          f"{'rRA':>5} {'rDec':>5} {'aRA':>6} {'decmax':>6} {'cable':>5}  reasons"
    print(hdr)
    for r in cat["passes"]:
        rs = ", ".join(f"{k} {v:.0f}s" for k, v in r["reasons"].items())
        print(f"{iso(r['rise']):19} {r['name'][:18]:18} {r['verdict']:7} {r.get('peak_alt', 0):4.0f} "
              f"{r.get('visible_s', 0):6.0f} {r.get('track_s', 0):6.0f} "
              f"{r.get('peak_rate_ra', r.get('visible_peak_rate_ra', 0)):5.2f} "
              f"{r.get('peak_rate_dec', r.get('visible_peak_rate_dec', 0)):5.2f} "
              f"{r.get('peak_accel_ra', 0):6.3f} {r.get('visible_max_dec', 0):6.1f} "
              f"{r.get('cable_margin', 0):5.0f}  {rs}")


if __name__ == "__main__":
    main()
