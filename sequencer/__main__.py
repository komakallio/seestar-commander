"""seestar-commander sequencer (v1, supervised).

  python -m sequencer serve [--sim] [--speed N] [--sim-start ISO | --sim-before-limit MIN] [--plan FILE]
  python -m sequencer check PLAN [--at ISO]        offline: parse a plan and show tonight's constraints
  python -m sequencer run PLAN                     start a plan on the running service
  python -m sequencer status [--json]
  python -m sequencer stop | resume | park | safety
  python -m sequencer lock --owner NAME [--reason TEXT] [--ttl 600] | unlock [--owner NAME] [--force]
                                                   exclusive use of the scope for a script (see tools/scopelock.py)
  python -m sequencer roof open | close           open: refused while unsafe; close: only when parked
  python -m sequencer darks list | darks build (--exposure 60 --gain 200 | --plan FILE | --missing) [--count N]
  python -m sequencer flats list | flats build [--filter IR --filter LP --gain 200 | --plan FILE | --missing]
                      [--wide] [--count N] | flats confirm   (manual light source: the panel is on)
  python -m sequencer polar start [--dec 65] [--span 30] [--exposure 5] | polar stop | polar status | polar done
  python -m sequencer sim-safety unsafe RAIN       (simulator only) flip the simulated safety source
  python -m sequencer sim-polar DALT DAZ           (simulator only) turn the wedge knobs by DALT/DAZ arcmin

Client commands talk to --url (default http://127.0.0.1:8640, or $SEQ_URL); --token / $SEQ_TOKEN.
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _call(a, method, path, body=None):
    url = a.url.rstrip("/") + path
    data = json.dumps(body or {}).encode() if method == "POST" else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    if a.token:
        req.add_header("Authorization", f"Bearer {a.token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except ValueError:
            return {"ok": False, "msg": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        sys.exit(f"can't reach the service at {a.url}: {e.reason}")


def _print_status(s):
    m, c, sf, rf = s.get("mount", {}), s.get("cable", {}), s.get("safety", {}), s.get("roof", {})
    print(f"{s['time']}  state {s['state']}  busy {s.get('busy')}  phase {s.get('phase')}"
          f"{'  [SIMULATOR]' if s.get('sim') else ''}")
    if m:
        print(f"mount  HA {m.get('ha')}  Dec {m.get('dec')}  alt {m.get('alt')}  az {m.get('az')}  "
              f"tracking {m.get('tracking')}  parked {m.get('at_park')}")
    if c:
        print(f"cable  unwrapped HA {c.get('u')} in [{c.get('min')}, {c.get('max')}]  "
              f"{c.get('left_deg')} deg = {c.get('left_min')} min of tracking left")
    print(f"safety {'SAFE' if sf.get('safe') else 'UNSAFE' if sf.get('safe') is False else '?'} {sf.get('reasons')}")
    lk = s.get("lock") or {}
    if lk.get("locked"):
        print(f"lock   ** scope in use by {lk['owner']}" + (f": {lk['reason']}" if lk.get("reason") else "")
              + f" until {lk['until']} ({lk['remaining_s']:.0f} s left) **")
    elif lk.get("file_holder"):
        h = lk["file_holder"]
        print(f"lock   file lock held by {h.get('owner')} (pid {h.get('pid')}): {h.get('reason')}")
    if lk.get("revoked"):
        print(f"       last lock revoked: {lk['revoked']['owner']}: {lk['revoked']['why']} at {lk['revoked']['time']}")
    pt = s.get("pointing") or {}
    if pt.get("kind"):
        print(f"point  {pt['kind']}" + (f" {pt.get('terms_arcmin')}" if pt.get("terms_arcmin") else "")
              + (f" rms {pt['rms_arcmin']}'" if pt.get("rms_arcmin") is not None else "")
              + f" from {pt.get('n', 0)} solves; sync shift {pt.get('shift') or 'unknown'}")
    if rf.get("configured"):
        print(f"roof   {rf.get('state')}" + ("   ** CLOSE REQUESTED: run `roof close` **" if rf.get("close_requested") else "")
              + ("   auto-close on unsafe" if rf.get("auto_close_on_unsafe") else ""))
        la = rf.get("last_action")
        if la:
            print(f"       last: {la['action']} -> {la['result']} at {la['time']}")
    p = s.get("plan")
    if p:
        print(f"plan   {p['name']}  roof: open at start {'on' if p.get('open_roof_at_start') else 'off'}, "
              f"close at end {'on' if p.get('close_roof_at_end') else 'off'}")
        for i, st in enumerate(p["steps"]):
            mark = ">" if s.get("step") == i else " "
            print(f"  {mark} {st['desc']:42s} {st['status']:11s} {st['frames']}/{st['count']}"
                  + (f"  ({st['reason']})" if st.get("reason") else ""))
    nl = s.get("night_limit") or {}
    if nl:
        print(f"night  limit sun {nl.get('sun_limit_deg')}" + (f", latest {nl['latest_end']}" if nl.get("latest_end") else "")
              + f" (sun now {nl.get('sun_alt')}): " + ("REACHED" + (f", imaging again from {nl['opens']}" if nl.get("opens") else "")
                                                       if nl.get("reached") else f"reached at {nl.get('at')}"))
    hb = s.get("heartbeat") or {}
    if hb.get("configured"):
        print(f"beat   {hb['method']} every {hb['interval_s']:g} s while {hb['while']}: sent {hb['sent']}, "
              f"failed {hb['failed']}" + (f", last error {hb['last_error']}" if hb.get("last_error") else ""))
    if s.get("drift"):
        print(f"drift  {s['drift']['total']}\"/min")
    for line in s.get("log", [])[-8:]:
        print("  " + line)


def _print_polar(r):
    print(f"polar  {r.get('state')}  {r.get('msg') or ''}")
    for key, title in (("measured", "measured"), ("live", "live")):
        m = r.get(key)
        if m:
            w = m["words"]
            print(f"{title:8s} alt {m['alt_arcmin']:+7.1f}' {w['alt']['word']:10s} az {m['az_arcmin']:+7.1f}' "
                  f"{w['az']['word']:10s} total {m['total_arcmin']:.1f}'"
                  + (f"   fit residual {m['angle_residual_arcmin']}'" if key == "measured" else ""))
    if r.get("final"):
        f = r["final"]
        print(f"done     {f['time']}: alt {f['alt_arcmin']:+.1f}' az {f['az_arcmin']:+.1f}' total {f['total_arcmin']:.1f}'")
    if r.get("sim_truth"):
        print(f"sim truth alt {r['sim_truth']['alt_arcmin']:+.1f}' az {r['sim_truth']['az_arcmin']:+.1f}'")


def _night_start(lat, lon, now):
    """Now if the Sun is below -15, else the next time it is."""
    from . import astro
    t = now
    for _ in range(24 * 6):
        if astro.sun_alt(t, lat, lon) < -15:
            return t
        t += 600
    return now


def _before_limit(s, t, minutes):
    """MIN minutes before the night limit ([sequencer] sun_limit_deg / latest_end) of the night at t."""
    from .statusview import night_window
    _, opens, ends = night_window(s.lat, s.lon, None, None, s.night_limit, t)
    if ends is None:
        sys.exit("--sim-before-limit: no night limit within a day of the start time")
    print(f"simulator starts {minutes:g} min before the night limit at "
          f"{dt.datetime.fromtimestamp(ends, dt.timezone.utc).isoformat()}")
    return ends - minutes * 60.0


def cmd_serve(a):
    if a.sim:
        _default_config("the simulator")
    from .clock import ScaledClock
    from .httpapi import serve
    from .rig import build_real, build_sim, settings_from_config
    from .service import Service
    from . import plan as planmod
    s = settings_from_config()
    if a.host:
        s.http_host = a.host
    if a.port is not None:
        s.http_port = a.port
    if a.sim:
        s.data_dir = os.path.join(s.data_dir, "sim")     # never touch the real ra_unwrap.json
        s.extra["flats"] = {**s.extra.get("flats", {}), "source": a.sim_flats}
        s.safety_poll_s = min(s.safety_poll_s, 10.0)
        start = dt.datetime.fromisoformat(a.sim_start.replace("Z", "+00:00")).timestamp() if a.sim_start \
            else _night_start(s.lat, s.lon, time.time())
        if a.sim_before_limit is not None:
            start = _before_limit(s, start, a.sim_before_limit)
        clock = ScaledClock(a.speed, start)
        s.monitor_s = 1.0
        pe = None if a.sim_polar_error.lower() == "none" else tuple(float(x) for x in a.sim_polar_error.split(","))
        rig = build_sim(s, clock, roof=not a.sim_no_roof, solver=not a.sim_no_solver, polar_error=pe)
    else:
        rig = build_real(s)
    svc = Service(rig).start()
    srv = serve(svc)
    h, p = srv.server_address[:2]
    svc.log(f"status page and API on http://{h}:{p}/  (data in {s.data_dir})")
    if a.plan:
        print(svc.cmd_start(planmod.load(a.plan)))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        svc.log("shutting down (Ctrl-C): the mount is left as it is; park first if needed")
        svc.shutdown()


def _default_config(why):
    if not os.environ.get("SEESTAR_CONFIG") and not os.path.exists(os.path.join(REPO, "config.toml")):
        os.environ["SEESTAR_CONFIG"] = os.path.join(REPO, "config.example.toml")
        print(f"no config.toml: {why} uses config.example.toml")


def cmd_check(a):
    _default_config("check")
    from . import rig  # noqa: F401  (puts tools/ on sys.path)
    import config as C
    from . import astro
    from . import plan as planmod
    pl = planmod.load(a.plan)
    t = dt.datetime.fromisoformat(a.at.replace("Z", "+00:00")).timestamp() if a.at else time.time()
    c = pl.constraints
    end = c.end_after(t)
    from .rig import settings_from_config
    from .statusview import night_window
    L = settings_from_config().night_limit
    print(f"plan '{pl.name}': {len(pl.steps)} steps; min alt {c.min_alt}, sun max {c.sun_max_alt}, "
          f"horizon {c.horizon.source if c.horizon else 'none'}, end "
          f"{dt.datetime.fromtimestamp(end, dt.timezone.utc).isoformat() if end else 'none'}")
    print(f"at {dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()}: sun alt "
          f"{astro.sun_alt(t, C.LAT, C.LON):.1f}; night: {planmod.night_failures(c, t, C.LAT, C.LON, end, L) or 'ok'}")
    _, opens, ends = night_window(C.LAT, C.LON, c, end, L, t)
    iso = (lambda x: dt.datetime.fromtimestamp(round(x), dt.timezone.utc).isoformat() if x else "none")
    print(f"night limit ({L.describe()}; stricter of plan and service): imaging "
          + (f"from {iso(opens)} " if opens else "now ") + f"until {iso(ends)}")
    for st in pl.steps:
        if isinstance(st, planmod.LightStep):
            alt, az = astro.radec_to_altaz(st.ra, st.dec, t, C.LAT, C.LON)
            f = planmod.target_failures(c, st.ra, st.dec, t, C.LAT, C.LON, st.min_alt)
            print(f"  {st.describe():44s} alt {alt:5.1f} az {az:5.1f}  {f or 'ok'}")
        else:
            print(f"  {st.describe():44s} sets {pl.dark_sets(st)}")
    if pl.order == "best":
        from . import scheduler
        ctx = scheduler.Ctx.from_settings(settings_from_config(), pl, t)
        r = scheduler.project(pl, [{"frames": 0, "status": "pending"} for _ in pl.steps], t, ctx)
        hm = (lambda x: scheduler.hhmm(x, ctx.tz))
        print(f"order = best: projected schedule ({ctx.tz}):")
        for b in r["blocks"]:
            print(f"  {hm(b['start'])}-{hm(b['end'])}  " + (f"wait for {b['name']} ({b['why']})" if b["kind"] == "wait"
                                                           else f"{b['name']} x{b['frames']}  (score {b['score']:g})"))
        print(f"  then: {r['end_why']}" + (", not scheduled: " + ", ".join(f"{u['name']} {u['frames']}"
                                                                           for u in r["unscheduled"])
                                            if r["unscheduled"] else ""))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m sequencer", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("SEQ_URL", "http://127.0.0.1:8640"))
    ap.add_argument("--token", default=os.environ.get("SEQ_TOKEN", ""))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve", help="run the service (owns the devices)")
    sv.add_argument("--sim", action="store_true", help="simulated devices; nothing real moves")
    sv.add_argument("--speed", type=float, default=20.0, help="simulator time speed-up")
    sv.add_argument("--sim-start", help="simulator start time, ISO UTC (default: now, or tonight's dark)")
    sv.add_argument("--sim-before-limit", type=float, metavar="MIN",
                    help="start the simulator MIN minutes before tonight's night limit (sun_limit_deg / latest_end)")
    sv.add_argument("--sim-no-roof", action="store_true")
    sv.add_argument("--sim-flats", choices=["sim", "manual"], default="sim",
                    help="flat light source on the simulator: its panel switched by the job, or manual "
                         "(`sim-panel on`, then `flats confirm`)")
    sv.add_argument("--sim-no-solver", action="store_true", help="simulate an unavailable plate solver")
    sv.add_argument("--sim-polar-error", default="50,-70",
                    help="simulated polar error ALT,AZ in arcmin (+ = too high / east of the pole), or none")
    sv.add_argument("--host")
    sv.add_argument("--port", type=int)
    sv.add_argument("--plan", help="start this plan right away")
    ck = sub.add_parser("check", help="parse a plan and evaluate its constraints (offline)")
    ck.add_argument("plan")
    ck.add_argument("--at", help="ISO time (default now)")
    rn = sub.add_parser("run", help="start a plan on the service")
    rn.add_argument("plan")
    st = sub.add_parser("status")
    st.add_argument("--json", action="store_true")
    for c in ("stop", "resume", "park", "safety"):
        sub.add_parser(c)
    lk = sub.add_parser("lock", help="take the scope for a script (plans, park, darks, polar are refused)")
    lk.add_argument("--owner", default=f"{os.environ.get('USER', 'cli')}-cli")
    lk.add_argument("--reason", default="")
    lk.add_argument("--ttl", type=float, default=600.0, help="seconds until it expires unless renewed")
    ul = sub.add_parser("unlock", help="release the scope lock")
    ul.add_argument("--owner", default=f"{os.environ.get('USER', 'cli')}-cli")
    ul.add_argument("--force", action="store_true", help="release someone else's lock")
    rf = sub.add_parser("roof")
    rf.add_argument("action", choices=["open", "close"])
    dk = sub.add_parser("darks", help="dark library")
    dk.add_argument("action", choices=["list", "build"])
    dk.add_argument("--exposure", type=float)
    dk.add_argument("--gain", type=int)
    dk.add_argument("--count", type=int)
    dk.add_argument("--plan", help="build everything this plan needs that is missing")
    dk.add_argument("--missing", action="store_true", help="build everything missing for the active/planned plans")
    fl = sub.add_parser("flats", help="flat library")
    fl.add_argument("action", choices=["list", "build", "confirm"])
    fl.add_argument("--filter", action="append", help="telephoto filter (repeat; default [flats] filters)")
    fl.add_argument("--gain", type=int)
    fl.add_argument("--count", type=int)
    fl.add_argument("--plan", help="build everything this plan needs that is missing")
    fl.add_argument("--missing", action="store_true", help="build everything missing for the active/planned plans")
    fl.add_argument("--wide", action="store_true", help="also the wide camera")
    sm = sub.add_parser("sim-panel", help="simulator: switch the flat panel")
    sm.add_argument("state", choices=["on", "off"])
    sm.add_argument("--brightness", type=int)
    po = sub.add_parser("polar", help="polar alignment (three-point measure, then live adjust at the wedge)")
    po.add_argument("action", choices=["start", "stop", "status", "done"])
    po.add_argument("--dec", help="Dec of the measurement (default [polar] dec, 65) or 'current'")
    po.add_argument("--span", type=float, help="RA axis +- this many degrees around the current HA (30)")
    po.add_argument("--points", type=int, help="number of points (3)")
    po.add_argument("--exposure", type=float, help="exposure, s")
    po.add_argument("--timeout", type=float, help="adjust loop timeout, minutes")
    po.add_argument("--tolerance", type=float, help="target tolerance, arcmin")
    po.add_argument("--no-track", action="store_true", help="tracking off during the adjust loop")
    po.add_argument("--json", action="store_true")
    sp = sub.add_parser("sim-polar", help="simulator: turn the wedge knobs (arcmin)")
    sp.add_argument("dalt", type=float)
    sp.add_argument("daz", type=float)
    ss = sub.add_parser("sim-safety", help="simulator: set the safety source")
    ss.add_argument("state", choices=["safe", "unsafe"])
    ss.add_argument("reasons", nargs="*")
    a = ap.parse_args(argv)

    if a.cmd == "serve":
        return cmd_serve(a)
    if a.cmd == "check":
        return cmd_check(a)
    if a.cmd == "run":
        path = os.path.abspath(a.plan)
        with open(path) as fh:
            text = fh.read()
        fmt = "yaml" if path.endswith((".yaml", ".yml")) else "toml"
        r = _call(a, "POST", "/api/plan", {"text": text, "format": fmt, "base_dir": os.path.dirname(path),
                                           "source": path})
    elif a.cmd == "status":
        s = _call(a, "GET", "/api/status")
        return print(json.dumps(s, indent=1)) if a.json else _print_status(s)
    elif a.cmd == "safety":
        r = _call(a, "GET", "/api/safety")
    elif a.cmd == "lock":
        r = _call(a, "POST", "/api/lock", {"owner": a.owner, "reason": a.reason, "ttl_s": a.ttl})
    elif a.cmd == "unlock":
        r = _call(a, "POST", "/api/unlock", {"owner": a.owner, "force": a.force})
    elif a.cmd == "roof":
        r = _call(a, "POST", f"/api/roof/{a.action}")
    elif a.cmd == "darks" and a.action == "list":
        r = _call(a, "GET", "/api/darks")
        print(f"sensor {r.get('temp')} C")
        for e in r.get("entries", []):
            print(f"  {e['status']:9s} {e['label']:36s} age {e['age_days']:6.1f} d  {e['frames']} frames  "
                  f"{e['temp_mean']} C ({e['temp_min']}..{e['temp_max']})")
        for m in r.get("missing", []):
            print(f"  missing   {m['label']} at {m['temp_bin']} C")
        return
    elif a.cmd == "darks":
        body = {"count": a.count, "missing": a.missing}
        if a.plan:
            body["plan_path"] = os.path.abspath(a.plan)
        if a.exposure is not None:
            body.update(exposure=a.exposure, gain=a.gain)
        r = _call(a, "POST", "/api/darks/build", body)
    elif a.cmd == "flats" and a.action == "list":
        r = _call(a, "GET", "/api/flats")
        print(f"focuser {r.get('focus')}  (tolerance {r.get('policy', {}).get('focus_tolerance')} steps)")
        for e in r.get("entries", []):
            print(f"  {e['status']:9s} {e['label']:30s} age {e['age_days']:6.1f} d  {e['frames']} x "
                  f"{e['exposure']} s at {e['level_frac']}  dark-flat {e['darkflat'] or 'NONE'}")
        for m in r.get("missing", []):
            print(f"  missing   {m['label']} at focus {m['focus']}")
        return
    elif a.cmd == "flats" and a.action == "confirm":
        r = _call(a, "POST", "/api/flats/confirm")
    elif a.cmd == "flats":
        body = {"count": a.count, "missing": a.missing, "wide": a.wide, "gain": a.gain}
        if a.filter:
            body["filters"] = a.filter
        if a.plan:
            body["plan_path"] = os.path.abspath(a.plan)
        r = _call(a, "POST", "/api/flats/build", body)
    elif a.cmd == "sim-panel":
        r = _call(a, "POST", "/api/sim/panel", {"on": a.state == "on", "brightness": a.brightness})
    elif a.cmd == "polar":
        if a.action == "status":
            r = _call(a, "GET", "/api/polar")
            return print(json.dumps(r, indent=1)) if a.json else _print_polar(r)
        body = {}
        if a.action == "start":
            dec = a.dec if a.dec in (None, "current") else float(a.dec)
            body = {k: v for k, v in (("dec", dec), ("span_deg", a.span), ("points", a.points),
                                      ("exposure_s", a.exposure), ("adjust_timeout_min", a.timeout),
                                      ("tolerance_arcmin", a.tolerance)) if v is not None}
            if a.no_track:
                body["track_during_adjust"] = False
        r = _call(a, "POST", f"/api/polar/{a.action}", body)
    elif a.cmd == "sim-polar":
        r = _call(a, "POST", "/api/sim/polar", {"dalt": a.dalt, "daz": a.daz})
    elif a.cmd == "sim-safety":
        r = _call(a, "POST", "/api/sim/safety", {"safe": a.state == "safe", "reasons": a.reasons})
    else:
        r = _call(a, "POST", f"/api/{a.cmd}")
    print(json.dumps(r, indent=1))
    if isinstance(r, dict) and r.get("ok") is False:
        sys.exit(1)


if __name__ == "__main__":
    main()
