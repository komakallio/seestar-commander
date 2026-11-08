"""seestar-commander sequencer (v1, supervised).

  python -m sequencer serve [--sim] [--speed N] [--sim-start ISO] [--plan FILE]
  python -m sequencer check PLAN [--at ISO]        offline: parse a plan and show tonight's constraints
  python -m sequencer run PLAN                     start a plan on the running service
  python -m sequencer status [--json]
  python -m sequencer stop | resume | park | safety
  python -m sequencer roof open | close           open: refused while unsafe; close: only when parked
  python -m sequencer darks list | darks build (--exposure 60 --gain 80 | --plan FILE | --missing) [--count N]
  python -m sequencer sim-safety unsafe RAIN       (simulator only) flip the simulated safety source

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
    if s.get("drift"):
        print(f"drift  {s['drift']['total']}\"/min")
    for line in s.get("log", [])[-8:]:
        print("  " + line)


def _night_start(lat, lon, now):
    """Now if the Sun is below -15, else the next time it is."""
    from . import astro
    t = now
    for _ in range(24 * 6):
        if astro.sun_alt(t, lat, lon) < -15:
            return t
        t += 600
    return now


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
        s.safety_poll_s = min(s.safety_poll_s, 10.0)
        start = dt.datetime.fromisoformat(a.sim_start.replace("Z", "+00:00")).timestamp() if a.sim_start \
            else _night_start(s.lat, s.lon, time.time())
        clock = ScaledClock(a.speed, start)
        s.monitor_s = 1.0
        rig = build_sim(s, clock, roof=not a.sim_no_roof, solver=not a.sim_no_solver)
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
    print(f"plan '{pl.name}': {len(pl.steps)} steps; min alt {c.min_alt}, sun max {c.sun_max_alt}, "
          f"horizon {c.horizon.source if c.horizon else 'none'}, end "
          f"{dt.datetime.fromtimestamp(end, dt.timezone.utc).isoformat() if end else 'none'}")
    print(f"at {dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()}: sun alt "
          f"{astro.sun_alt(t, C.LAT, C.LON):.1f}; night: {planmod.night_failures(c, t, C.LAT, C.LON, end) or 'ok'}")
    for st in pl.steps:
        if isinstance(st, planmod.LightStep):
            alt, az = astro.radec_to_altaz(st.ra, st.dec, t, C.LAT, C.LON)
            f = planmod.target_failures(c, st.ra, st.dec, t, C.LAT, C.LON, st.min_alt)
            print(f"  {st.describe():44s} alt {alt:5.1f} az {az:5.1f}  {f or 'ok'}")
        else:
            print(f"  {st.describe():44s} sets {pl.dark_sets(st)}")


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
    sv.add_argument("--sim-no-roof", action="store_true")
    sv.add_argument("--sim-no-solver", action="store_true", help="simulate an unavailable plate solver")
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
    rf = sub.add_parser("roof")
    rf.add_argument("action", choices=["open", "close"])
    dk = sub.add_parser("darks", help="dark library")
    dk.add_argument("action", choices=["list", "build"])
    dk.add_argument("--exposure", type=float)
    dk.add_argument("--gain", type=int)
    dk.add_argument("--count", type=int)
    dk.add_argument("--plan", help="build everything this plan needs that is missing")
    dk.add_argument("--missing", action="store_true", help="build everything missing for the active/planned plans")
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
    elif a.cmd == "sim-safety":
        r = _call(a, "POST", "/api/sim/safety", {"safe": a.state == "safe", "reasons": a.reasons})
    else:
        r = _call(a, "POST", f"/api/{a.cmd}")
    print(json.dumps(r, indent=1))
    if isinstance(r, dict) and r.get("ok") is False:
        sys.exit(1)


if __name__ == "__main__":
    main()
