"""Axis bench tests for satellite tracking (docs/sattrack.md, Phase 1). Roof closed; no sky needed.

  preflight            no motion: clock sync, unwrap file, mount state, read latency
  latency  (T1)        step a rate on one axis, poll its position, measure delay to start and stop
  analyse RUN_DIR      summarise a run
  park                 cable-safe park (Scope.park_guarded) + parked hook; for cleanup after a run

Every run writes data/sattrack/runs/<time>-<test>/ (calls.jsonl, status.json, result.json). Motion
runs unpark, move to a start point by the cable-safe Scope moves, and park with park_guarded at the
end (also on errors and signals). Only one process may move the mount: stop the sequencer first.
Run it detached on a host on the scope's LAN (see docs/sattrack.md, "Running on a flaky network").
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


def make_scope():
    from scope import Scope
    return Scope()


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
    sc = make_scope()
    b = Bench(sc, make_limits(), log, read_timeout=a.read_timeout, clock=sc.clock)
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
            result["park"] = park(b) if a.park else None
        finally:
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


def cmd_analyse(a):
    with open(os.path.join(a.run, "args.json")) as fh:
        args = json.load(fh)
    recs = read_log(os.path.join(a.run, "calls.jsonl"))
    test = os.path.basename(a.run.rstrip("/")).split("-", 2)[-1]
    res = {"latency": analyse_latency}[test](recs, args)
    with open(os.path.join(a.run, "analysis.json"), "w") as fh:
        json.dump(res, fh, indent=1)
    print(json.dumps(res["summary"], indent=1))


def cmd_park(a):
    log = new_run("park")
    sc = make_scope()
    b = Bench(sc, make_limits(), log, read_timeout=5.0)
    b.stop()
    r = park(b)
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
    q = sub.add_parser("analyse")
    q.add_argument("run")
    sub.add_parser("park")
    a = ap.parse_args()
    if a.cmd == "preflight":
        cmd_preflight(a)
    elif a.cmd == "latency":
        d = t1_latency(a)
        cmd_analyse(argparse.Namespace(run=d))
    elif a.cmd == "analyse":
        cmd_analyse(a)
    elif a.cmd == "park":
        cmd_park(a)


if __name__ == "__main__":
    main()
