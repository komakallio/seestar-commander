"""Axis bench tests for satellite tracking (docs/sattrack.md, Phase 1). Roof closed; no sky needed.

  preflight            no motion: clock sync, unwrap file, mount state, read latency
  latency  (T1)        step a rate on one axis, poll its position, measure delay to start and stop
  profile  (T2-T4)     a sequence of rates without stopping, e.g. "0.5:3,1:3,0.2:3,0:2", or on both
                       axes "1/0.5:4,0/0:2" (--axis both, T3); --tracking for the T4 interaction
                       (rate deg/s : seconds); --no-poll sends nothing between rate changes (does a
                       MoveAxis time out by itself?)
  analyse RUN_DIR      summarise a run
  park                 cable-safe park (Scope.park_guarded) + parked hook; for cleanup after a run

Every run writes data/sattrack/runs/<time>-<test>/ (calls.jsonl, status.json, result.json). Motion
runs unpark, move to a start point by the cable-safe Scope moves, and park with park_guarded at the
end (also on errors and signals). Only one process may move the mount: stop the sequencer first.
Motion runs and park hold the scope lock (tools/scopelock.py, owner "sattrack"), so the sequencer
service can stay up; if the lock is lost (e.g. revoked as unsafe) both axes stop and the run ends
without parking, because the service then parks. Run it detached on a host on the scope's LAN (see
docs/sattrack.md, "Running on a flaky network").
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np  # noqa: E402

import config  # noqa: E402
from sattrack.bench import PRIMARY, SECONDARY, Bench  # noqa: E402
from sattrack.guard import Limits  # noqa: E402
from sattrack.runlog import RunLog, read as read_log  # noqa: E402

RUNS = os.path.join(config.DATA_DIR, "sattrack", "runs")


def make_limits():
    return Limits.from_config(config.get, config.HOME_HA - config.CCW_LIMIT + config.LIMIT_MARGIN,
                              config.HOME_HA + config.CW_LIMIT - config.LIMIT_MARGIN)


def clock_synced():
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return out == "yes"
    except Exception:  # noqa: BLE001 - not systemd
        return None


def new_run(test):
    d = os.path.join(RUNS, time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + test)
    return RunLog(d)


def make_scope(reason=None):
    """Scope; with a reason, it holds the scope lock (owner "sattrack") until close()."""
    from scope import Scope
    if reason is None:
        return Scope()
    return Scope(lock_owner="sattrack", lock_reason=reason, lock_ttl_s=60.0)


def lock_lost(sc):
    lk = getattr(sc, "scopelock", None)
    return bool(lk is not None and lk.lost.is_set())


def stats(x):
    x = np.asarray(x, float)
    if not len(x):
        return None
    return {"n": int(len(x)), "median": round(float(np.median(x)), 4), "p05": round(float(np.percentile(x, 5)), 4),
            "p95": round(float(np.percentile(x, 95)), 4), "min": round(float(x.min()), 4),
            "max": round(float(x.max()), 4), "std": round(float(x.std()), 4)}


# ---- preflight ------------------------------------------------------------------------------------

def cmd_preflight(a):
    log = new_run("preflight")
    sc = make_scope()
    b = Bench(sc, make_limits(), log, read_timeout=5.0)
    res = {"clock_synced": clock_synced(), "unwrap": sc._load_u()}
    for prop in ("AtPark", "Tracking", "Slewing"):
        res[prop] = b.get(prop)[0]
    lat = {"RightAscension": [], "Declination": []}
    for _ in range(a.n):
        for prop in lat:
            lat[prop].append(b.get(prop)[2])
    res["read_s"] = {k: stats(v) for k, v in lat.items()}
    res["position"] = b.read()
    log.save("result.json", res)
    log.status(state="done", test="preflight")
    print(json.dumps(res, indent=1))


# ---- motion runs ----------------------------------------------------------------------------------

def unpark(b):
    import safety
    t = b.t
    if b.get("AtPark")[0]:
        b._call("Unpark", t.Unpark)
        b.log.write(c="hook_unparked", r=str(safety.unparked()))
    if b.get("Tracking")[0]:
        b._call("Tracking=False", setattr, t, "Tracking", False)


def park(b):
    import safety
    r = b.s.park_guarded()
    b.log.write(c="park", r=r)
    if r.get("parked") and not r.get("already"):
        b.log.write(c="hook_parked", r=str(safety.parked()))
    return r


def run_motion(test, a, body):
    """Common frame: run dir, guard, unpark, start point, body(b, log), park. Returns the run dir."""
    log = new_run(test)
    log.save("args.json", vars(a))
    if a.require_sync and clock_synced() is False:
        raise SystemExit("clock not NTP-synchronised")
    sc = make_scope(f"bench {test}")
    b = Bench(sc, make_limits(), log, read_timeout=a.read_timeout, clock=sc.clock,
              lost=lambda: lock_lost(sc))
    log.status(state="starting", test=test)
    result = {"test": test, "ok": False}
    try:
        with b:
            unpark(b)
            log.status(state="moving to start", test=test)
            sc.move_hadec(config.HOME_HA + a.ha_offset, a.dec_start, tol=0.3)
            b.read()
            log.status(state="running", test=test)
            result.update(body(b, log))
            result["ok"] = True
    except BaseException as e:  # noqa: BLE001 - always park
        result["error"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        log.save("result.json", result)
        log.status(state="parking", test=test)
        try:
            if lock_lost(sc):
                result["park"] = "skipped: scope lock lost (the service parks)"
            else:
                result["park"] = park(b) if a.park else None
        finally:
            sc.close()
            log.save("result.json", result)
            log.status(state="done" if result["ok"] else "failed", test=test, error=result.get("error"))
            print(log.dir)
    return log.dir


def t1_latency(a):
    axis = PRIMARY if a.axis == "ra" else SECONDARY

    def poll(b, until):
        while time.monotonic() < until:
            b.read_ra() if axis == PRIMARY else b.read_dec()

    def body(b, log):
        for i in range(a.reps):
            sign = 1 if i % 2 == 0 else -1          # alternate so the axis stays near its start
            log.status(state="running", test="latency", rep=i + 1, of=a.reps)
            poll(b, time.monotonic() + a.settle)
            log.write(c="mark", rep=i, ev="go", rate=sign * a.rate)
            b.rate(axis, sign * a.rate)
            poll(b, time.monotonic() + a.hold)
            log.write(c="mark", rep=i, ev="stop")
            b.rate(axis, 0.0)
            poll(b, time.monotonic() + a.settle)
        return {"axis": a.axis, "rate": a.rate, "reps": a.reps}
    return run_motion("latency", a, body)


def parse_profile(txt):
    """ "r:d,..." -> [(r, d)]; "r1/r2:d,..." (both axes, RA/Dec) -> [((r1, r2), d)]."""
    out = []
    for part in txt.split(","):
        r, d = part.split(":")
        if "/" in r:
            ra, de = r.split("/")
            out.append(((float(ra), float(de)), float(d)))
        else:
            out.append((float(r), float(d)))
    return out


def t2_profile(a):
    both = a.axis == "both"
    axis = PRIMARY if a.axis == "ra" else SECONDARY
    prof = parse_profile(a.profile)

    def read1(b):
        if both:
            b.read()
        elif axis == PRIMARY:
            b.read_ra()
        else:
            b.read_dec()

    def poll(b, until):
        if a.no_poll:
            time.sleep(max(0.0, until - time.monotonic()))
            return
        while time.monotonic() < until:
            read1(b)

    def command(b, r):
        if both:
            b.rate(PRIMARY, r[0])
            b.rate(SECONDARY, r[1])
        else:
            b.rate(axis, r)

    def body(b, log):
        extra = {}
        if a.tracking:
            b._call("Tracking=True", setattr, b.t, "Tracking", True)
            extra["tracking_before"] = b.get("Tracking")[0]
        poll(b, time.monotonic() + a.settle) if not a.no_poll else None
        for _ in range(4 if a.no_poll else 0):
            read1(b)
        for i, (r, d) in enumerate(prof):
            log.status(state="running", test="profile", seg=i + 1, of=len(prof), rate=r)
            log.write(c="mark", seg=i, ev="seg", rate=r, dur=d)
            command(b, r)
            poll(b, time.monotonic() + d)
        log.write(c="mark", seg=len(prof), ev="end")
        command(b, (0.0, 0.0) if both else 0.0)
        for _ in range(int(a.settle / 0.25) + 4):
            read1(b)
        if a.tracking:
            extra["tracking_after"] = b.get("Tracking")[0]
            log.write(c="mark", seg=len(prof) + 1, ev="tracking_probe", rate=0.0, dur=a.settle)
            poll(b, time.monotonic() + max(10.0, a.settle))     # HA rate with tracking (if still on)
            b._call("Tracking=False", setattr, b.t, "Tracking", False)
        return {"axis": a.axis, "profile": prof, **extra}
    if a.max_rate is not None or a.runaway_s is not None:
        global make_limits
        base = make_limits

        def make_limits():
            lim = base()
            if a.max_rate is not None:
                lim.max_rate = a.max_rate
            if a.runaway_s is not None:
                lim.runaway_ra_s = lim.runaway_dec_s = a.runaway_s
            return lim
    return run_motion("profile", a, body)


# ---- analysis -------------------------------------------------------------------------------------

def analyse_latency(recs, args):
    """Per repetition: delay from the MoveAxis send to the effective start and stop of motion
    (steady-rate line extrapolated to the rest positions), first visible motion, steady rate."""
    key = "u" if args["axis"] == "ra" else "d"
    pos = [(r["t"], r[key]) for r in recs if r["c"] == key]
    moves = [r for r in recs if r["c"].startswith("MoveAxis")]
    marks = [r for r in recs if r["c"] == "mark"]
    T = np.array([p[0] for p in pos])
    X = np.array([p[1] for p in pos])
    out = []
    for i in range(args["reps"]):
        go = next((m for m in marks if m["rep"] == i and m["ev"] == "go"), None)
        st = next((m for m in marks if m["rep"] == i and m["ev"] == "stop"), None)
        if not go or not st:
            break
        mg = next(m for m in moves if m["utc"] >= go["utc"])
        ms = next(m for m in moves if m["utc"] >= st["utc"])
        tg, ts = mg["t0"], ms["t0"]                       # send times
        rate = go["rate"]
        pre = (T > tg - args["settle"] + 0.2) & (T < tg)
        post = (T > ts + args["settle"] * 0.6) & (T < ts + args["settle"])
        steady = (T > tg + 0.5 * args["hold"]) & (T < ts)
        if pre.sum() < 2 or post.sum() < 2 or steady.sum() < 3:
            out.append({"rep": i, "error": "too few samples"})
            continue
        x0, x1 = float(np.median(X[pre])), float(np.median(X[post]))
        k, c = np.polyfit(T[steady], X[steady], 1)
        t_start, t_stop = (x0 - c) / k, (x1 - c) / k
        moved = (T > tg) & (np.abs(X - x0) > args.get("first_thresh", 0.005))
        first = float(T[moved][0] - tg) if moved.any() else None
        out.append({"rep": i, "rate_cmd": rate, "rate_meas": round(float(k), 5),
                    "start_delay": round(float(t_start - tg), 4), "stop_delay": round(float(t_stop - ts), 4),
                    "first_motion": round(first, 4) if first is not None else None,
                    "send_dt_go": mg["dt"], "send_dt_stop": ms["dt"], "travel": round(x1 - x0, 4),
                    "rest_noise": round(float(np.std(X[pre]) * 3600), 2)})
    good = [o for o in out if "error" not in o]
    reads = [r["dt"] for r in recs if r["c"] in ("RightAscension", "Declination")]
    summ = {"start_delay": stats([o["start_delay"] for o in good]),
            "stop_delay": stats([o["stop_delay"] for o in good]),
            "first_motion": stats([o["first_motion"] for o in good if o["first_motion"] is not None]),
            "rate_ratio": stats([o["rate_meas"] / o["rate_cmd"] for o in good]),
            "read_dt": stats(reads), "read_hz": round(len(reads) / max(1e-9, T.max() - T.min()), 1) if len(T) else None,
            "movecall_dt": stats([m["dt"] for m in moves])}
    return {"summary": summ, "reps": out}


def _profile_axis(recs, key, rates, marks, send):
    pos = [(r["t"], r[key]) for r in recs if r["c"] == key]
    T = np.array([p[0] for p in pos])
    X = np.array([p[1] for p in pos])
    segs = []
    bounds = send + [T.max() + 1 if len(T) else 0]
    for i in range(len(bounds) - 1):
        t0, t1 = bounds[i], bounds[i + 1]
        sel = (T > t0 + 0.6) & (T < t1 + 0.2)
        fit = np.polyfit(T[sel], X[sel], 1) if sel.sum() >= 3 else None
        segs.append({"seg": i, "rate_cmd": rates[i], "dur": marks[i].get("dur"), "t_send": t0,
                     "rate_meas": round(float(fit[0]), 6) if fit is not None else None,
                     "n": int(sel.sum()), "_fit": fit})
    trans = []
    for i in range(1, len(segs)):
        f0, f1 = segs[i - 1]["_fit"], segs[i]["_fit"]
        if f0 is None or f1 is None or abs(f0[0] - f1[0]) < 1e-4:
            continue
        tx = (f1[1] - f0[1]) / (f0[0] - f1[0])
        trans.append({"from": segs[i - 1]["rate_cmd"], "to": segs[i]["rate_cmd"],
                      "switch_delay": round(float(tx - segs[i]["t_send"]), 4)})
    pre, post = T < send[0], (T > send[-1] + 1.5)
    travel = float(np.median(X[post]) - np.median(X[pre])) if pre.any() and post.any() else None
    for sg in segs:
        sg.pop("_fit")
    return segs, trans, travel


def analyse_profile(recs, args):
    """Per segment: fitted steady rate (middle part). Per rate change: the effective switch time
    (where the fitted lines of the two segments cross) minus the send time; a firmware that stops
    between rates would show up as a much larger value than the T1 start delay, and lost travel.
    With no polling, only the end-to-end travel is known: compare with the commanded travel.
    With both axes, each axis is analysed on its own. A tracking_probe segment (after the profile,
    rate 0, tracking left as the firmware has it) shows the HA rate with tracking."""
    marks = [r for r in recs if r["c"] == "mark"]
    moves = [r for r in recs if r["c"].startswith("MoveAxis")]
    send = []
    for m in marks:
        mv = next((x for x in moves if x["utc"] >= m["utc"]), None)
        send.append(mv["t0"] if (mv and m["ev"] != "tracking_probe") else m["utc"])
    use = [i for i, m in enumerate(marks) if m["ev"] in ("seg", "end", "tracking_probe")]
    marks = [marks[i] for i in use]
    send = [send[i] for i in use]
    if marks and marks[-1]["ev"] == "end":
        marks, send = marks[:-1] + [dict(marks[-1], rate=0.0, dur=None)], send
    axes = ["ra", "dec"] if args["axis"] == "both" else [args["axis"]]
    out = {"summary": {}, "segments": []}
    for j, ax in enumerate(axes):
        rates = [(m["rate"][j] if isinstance(m["rate"], list) else m["rate"]) for m in marks]
        segs, trans, travel = _profile_axis(recs, "u" if ax == "ra" else "d", rates, marks, send)
        nominal = sum((r[j] if isinstance(r, (list, tuple)) else r) * d for r, d in parse_profile(args["profile"]))
        out["summary"][ax] = {"travel_meas": round(travel, 4) if travel is not None else None,
                              "travel_cmd_nominal": round(nominal, 4), "transitions": trans}
        for sg in segs:
            sg["axis"] = ax
        out["segments"] += segs
    if len(axes) == 1:
        out["summary"].update(out["summary"].pop(axes[0]))
    return out


def cmd_analyse(a):
    with open(os.path.join(a.run, "args.json")) as fh:
        args = json.load(fh)
    recs = read_log(os.path.join(a.run, "calls.jsonl"))
    test = os.path.basename(a.run.rstrip("/")).split("-", 2)[-1]
    res = {"latency": analyse_latency, "profile": analyse_profile}[test](recs, args)
    with open(os.path.join(a.run, "analysis.json"), "w") as fh:
        json.dump(res, fh, indent=1)
    print(json.dumps(res["summary"], indent=1))


def cmd_park(a):
    log = new_run("park")
    sc = make_scope("bench cleanup park")
    b = Bench(sc, make_limits(), log, read_timeout=5.0)
    b.stop()
    try:
        r = park(b)
    finally:
        sc.close()
    log.save("result.json", r)
    print(json.dumps(r))
    if not r.get("parked"):
        raise SystemExit("PARK FAILED: check the scope on the camera")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("preflight")
    p.add_argument("-n", type=int, default=30)

    def motion(name):
        q = sub.add_parser(name)
        q.add_argument("--ha-offset", type=float, default=0.0, help="start HA relative to home (deg)")
        q.add_argument("--dec-start", type=float, default=-60.0)
        q.add_argument("--read-timeout", type=float, default=1.0, help="s; slower reads stop the run")
        q.add_argument("--no-park", dest="park", action="store_false")
        q.add_argument("--no-require-sync", dest="require_sync", action="store_false")
        return q
    q = motion("latency")
    q.add_argument("--axis", choices=("ra", "dec"), default="ra")
    q.add_argument("--rate", type=float, default=1.0)
    q.add_argument("--hold", type=float, default=3.0)
    q.add_argument("--settle", type=float, default=2.0)
    q.add_argument("--reps", type=int, default=20)
    q = motion("profile")
    q.add_argument("--axis", choices=("ra", "dec", "both"), default="ra")
    q.add_argument("--profile", required=True, help='"rate:seconds,..." in deg/s; both axes: "ra/dec:seconds,..."')
    q.add_argument("--tracking", action="store_true",
                   help="turn sidereal tracking on first; afterwards probe the HA rate with tracking left as is")
    q.add_argument("--settle", type=float, default=2.0)
    q.add_argument("--no-poll", action="store_true", help="no calls between rate changes")
    q.add_argument("--max-rate", type=float, default=None, help="override the guard rate limit")
    q.add_argument("--runaway-s", type=float, default=None, help="override the guard runaway horizon")
    q = sub.add_parser("analyse")
    q.add_argument("run")
    sub.add_parser("park")
    a = ap.parse_args()
    if a.cmd == "preflight":
        cmd_preflight(a)
    elif a.cmd == "latency":
        d = t1_latency(a)
        cmd_analyse(argparse.Namespace(run=d))
    elif a.cmd == "profile":
        d = t2_profile(a)
        cmd_analyse(argparse.Namespace(run=d))
    elif a.cmd == "analyse":
        cmd_analyse(a)
    elif a.cmd == "park":
        cmd_park(a)


if __name__ == "__main__":
    main()
