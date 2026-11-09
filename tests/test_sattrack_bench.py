"""Bench harness against the simulator: guard on every rate, stop on a bad link, T1 run + analysis."""
import argparse
import json
import os

import pytest

from sattrack.bench import Bench, LinkError, PRIMARY
from sattrack.guard import GuardError, Limits
from sattrack.runlog import RunLog, read


def lim():
    return Limits(u_min=-175, u_max=175)


def test_guard_blocks_unsafe_rates(make_service, tmp_path):
    svc = make_service(speed=50)
    sc, tel = svc.scope, svc.rig.sim.telescope
    tel.Unpark()
    sc.move_hadec(-90, -60)
    log = RunLog(str(tmp_path / "r"))
    with Bench(sc, lim(), log, clock=sc.clock) as b:
        b.read()
        b.rate(PRIMARY, 0.5)                        # -90 + 15 after 30 s: fine
        with pytest.raises(GuardError, match="rate"):
            b.rate(PRIMARY, 6.0)                    # above max_rate
    with Bench(sc, Limits(u_min=-175, u_max=-80), log, clock=sc.clock) as b:
        b.read()
        with pytest.raises(GuardError, match="runaway_ra"):
            b.rate(PRIMARY, 0.5)                    # -90 + 15 > -80
    assert tel._rate == [0.0, 0.0]


def test_slow_read_stops_axes(make_service, tmp_path):
    svc = make_service(speed=50)
    sc, tel = svc.scope, svc.rig.sim.telescope
    tel.Unpark()
    log = RunLog(str(tmp_path / "r"))
    with pytest.raises(LinkError):
        with Bench(sc, lim(), log, read_timeout=-1, clock=sc.clock) as b:   # every read is "slow"
            b.last = {"u": -90.0, "dec": -60.0, "t": 0}
            b.rate(PRIMARY, 0.5)
            b.read_ra()
    assert tel._rate == [0.0, 0.0]
    recs = read(os.path.join(log.dir, "calls.jsonl"))
    assert any(r["c"] == "bad_link" for r in recs)
    assert any(r["c"] == "stop" and r["ok"] for r in recs)


def test_latency_run_and_analysis(make_service, tmp_path, monkeypatch):
    import axis_bench as ab
    svc = make_service(speed=20)
    sc = svc.scope
    monkeypatch.setattr(ab, "make_scope", lambda reason=None: sc)
    monkeypatch.setattr(ab, "RUNS", str(tmp_path / "runs"))
    a = argparse.Namespace(axis="ra", rate=1.0, hold=0.6, settle=0.4, reps=2, ha_offset=0.0, dec_start=-70.0,
                           read_timeout=1.0, park=True, require_sync=False)
    d = ab.t1_latency(a)
    res = json.load(open(os.path.join(d, "result.json")))
    assert res["ok"] and res["park"]["parked"]
    ab.cmd_analyse(argparse.Namespace(run=d))
    an = json.load(open(os.path.join(d, "analysis.json")))
    good = [r for r in an["reps"] if "error" not in r]
    assert len(good) == 2
    for r in good:
        assert r["rate_meas"] / r["rate_cmd"] == pytest.approx(1.0, rel=0.1)
        assert abs(r["start_delay"]) < 2.0          # sim time; the sim has no command delay
    assert svc.rig.sim.telescope.firmware_gotos == 0


def test_lost_lock_stops_axes(make_service, tmp_path):
    from sattrack.bench import Stopped
    svc = make_service(speed=50)
    sc, tel = svc.scope, svc.rig.sim.telescope
    tel.Unpark()
    sc.move_hadec(-90, -60)
    lost = [False]
    log = RunLog(str(tmp_path / "r"))
    with pytest.raises(Stopped, match="lock"):
        with Bench(sc, lim(), log, clock=sc.clock, lost=lambda: lost[0]) as b:
            b.read()
            b.rate(PRIMARY, 0.5)
            lost[0] = True
            b.read_ra()
    assert tel._rate == [0.0, 0.0]


def test_profile_run_and_analysis(make_service, tmp_path, monkeypatch):
    import axis_bench as ab
    svc = make_service(speed=20)
    sc = svc.scope
    monkeypatch.setattr(ab, "make_scope", lambda reason=None: sc)
    monkeypatch.setattr(ab, "RUNS", str(tmp_path / "runs"))
    a = argparse.Namespace(axis="ra", profile="0.5:1.5,1.0:1.5,0:1", settle=0.5, no_poll=False, max_rate=None, tracking=False,
                           runaway_s=None, ha_offset=0.0, dec_start=-70.0, read_timeout=1.0, park=True,
                           require_sync=False)
    d = ab.t2_profile(a)
    ab.cmd_analyse(argparse.Namespace(run=d))
    an = json.load(open(os.path.join(d, "analysis.json")))
    s0, s1 = an["segments"][:2]
    assert s0["rate_meas"] == pytest.approx(0.5, rel=0.1)
    assert s1["rate_meas"] == pytest.approx(1.0, rel=0.1)
    assert an["summary"]["transitions"]


def test_profile_both_axes(make_service, tmp_path, monkeypatch):
    import axis_bench as ab
    svc = make_service(speed=20)
    sc = svc.scope
    monkeypatch.setattr(ab, "make_scope", lambda reason=None: sc)
    monkeypatch.setattr(ab, "RUNS", str(tmp_path / "runs"))
    a = argparse.Namespace(axis="both", profile="1/0.5:2,0/0:1", settle=0.5, no_poll=False, max_rate=None,
                           tracking=False, runaway_s=None, ha_offset=0.0, dec_start=-60.0, read_timeout=1.0,
                           park=True, require_sync=False)
    d = ab.t2_profile(a)
    ab.cmd_analyse(argparse.Namespace(run=d))
    an = json.load(open(os.path.join(d, "analysis.json")))
    ra0 = next(s for s in an["segments"] if s["axis"] == "ra" and s["seg"] == 0)
    de0 = next(s for s in an["segments"] if s["axis"] == "dec" and s["seg"] == 0)
    assert ra0["rate_meas"] == pytest.approx(1.0, rel=0.15)
    assert de0["rate_meas"] == pytest.approx(0.5, rel=0.15)
