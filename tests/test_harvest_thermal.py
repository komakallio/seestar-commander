"""Dark harvest thermal model (sequencer/thermal.py, sequencer/harvest.py): the idle floor fitted from cooling
curves, predicting each frame's temperature before it is taken (no predictably wasted frames at a bin edge),
edge-only and unreachable bins, the temperature sweep and thermal.jsonl, the "complete" headline."""
import json
import math
import os

import numpy as np

from sequencer.thermal import fit_relax

from conftest import wait_for

HV = {"enabled": False, "exposures": [10, 15], "count": 3, "bias": False, "poll_s": 10, "holdoff_min": 0,
      "sentinel": {"exposure": 15, "gain": 200, "every_days": 3}}


def _svc(make_service, hv=None, speed=600):
    return make_service(speed=speed, roof=False, extra={"darks_harvest": {**HV, **(hv or {})}})


def _events(svc, kind):
    with open(svc.harvest.log_path) as fh:
        return [r for r in (json.loads(x) for x in fh) if r["kind"] == kind]


def _complete(svc, bins):
    """Fresh masters for every key the harvester knows in these bins."""
    frame = np.full((960, 540), 400, np.uint16)
    for k, _ in svc.harvest.keys():
        for tb in bins:
            svc.darklib.add(k, [frame], [tb], tb=tb)


def test_fit_relax_finds_the_floor_of_a_curve_still_falling():
    # idle cooling 13.25 -> ~11.9 C in 26 min and still falling (1/16 C readings): the floor is lower
    F, tau = 11.0, 15 * 60.0
    t = np.arange(0, 26 * 60 + 1, 60.0)
    y = np.round((F + (13.25 - F) * np.exp(-t / tau)) * 16) / 16
    assert y[-1] < 12.0 and y[-1] - y[-2] < 0                 # where a naive "lowest reading" would stop
    f = fit_relax([(t, y)])
    assert abs(f["asym"] - F) < 0.3 and 8 * 60 < f["tau_s"] < 25 * 60
    flat = fit_relax([(t, np.full(len(t), 12.5))])
    assert flat["asym"] == 12.5 and flat["tau_s"] is None


def test_floor_fitted_from_cooling_not_from_frames(make_service):
    svc = _svc(make_service, speed=1200)
    th = svc.rig.sim.camera.set_thermal(ambient_c=5.0, idle_offset_c=6.0, start_c=14.0, tau_cool_min=15)
    assert wait_for(lambda: svc.clock.time() - th.t_amb0 > 30 * 60, 30)    # 30 min idle, still ~0.4 C above
    s = svc.harvest.thermal.refresh(force=True)
    assert svc.rig.sim.camera.CCDTemperature > 11.3
    assert abs(s["floor"] - 11.0) < 0.3 and s["tau_cool_s"] > 5 * 60
    v = svc.harvest.thermal_view()
    assert set(v) >= {"idle_floor", "plateau", "warming_rate", "ambient", "tau_cool_min", "tau_heat_min",
                      "next_sweep", "last_sweep"}


def test_no_predictably_wasted_frames_at_a_bin_edge(make_service):
    # floor 12.75 C, plateau ~17.65 C: bins +14..+18 complete, +12 [11, 13) reachable only at its top edge.
    # Before: every cooled-down frame warmed the sensor past 13.0 and landed in the complete +14.
    svc = _svc(make_service, hv={"sweep": False})
    svc.rig.sim.camera.set_thermal(ambient_c=6.75, idle_offset_c=6.0, heat_c=4.9, tau_heat_min=4, tau_cool_min=12)
    _complete(svc, [14.0, 16.0, 18.0])
    t0 = svc.clock.time()
    assert wait_for(lambda: svc.clock.time() - t0 > 40 * 60, 30)            # idle log for the floor fit
    H = svc.harvest
    svc.cmd_harvest(True)
    t1 = svc.clock.time()
    assert wait_for(lambda: svc.clock.time() - t1 > 4 * 3600, 120)
    frames = [f for f in _events(svc, "frame") if not f.get("aborted")]
    assert all(f["temp_bin"] == 12.0 for f in frames), [(f["msg"]) for f in frames]
    assert 1 <= len(frames) <= 6                                            # edge-only: about one an hour
    assert H.reach(12.0) == "edge" and H.reach(10.0) == "unreachable_now" and H.reach(14.0) == "reachable"
    assert H.reach(22.0) == "unreachable_now"
    cov = H.coverage()
    cell = cov["rows"][0]["cells"][cov["bins"].index(12.0)]
    assert cell["reach"] == "edge" and "edge" in cell["reach_note"]
    assert wait_for(lambda: H.state == "complete", 30)
    v = H.view()
    assert v["headline"].startswith("All reachable bins complete (12.") and "outside" not in v["headline"]
    assert "next sweep" not in v["headline"]
    assert 12.5 < v["thermal"]["idle_floor"] < 13.0 and v["thermal"]["plateau"] > 15.0
    assert not v["stall"]["stalled"]


def test_series_starts_low_in_the_bin(make_service):
    # cooling from 14.4 towards the floor 11.0: bin +12 isn't started at its top (12.9), only in its lower third
    svc = _svc(make_service, hv={"sweep": False, "exposures": [10]}, speed=600)
    svc.rig.sim.camera.set_thermal(ambient_c=5.0, idle_offset_c=6.0, heat_c=4.9, tau_heat_min=4, tau_cool_min=12)
    _complete(svc, [14.0, 16.0])
    t0 = svc.clock.time()
    assert wait_for(lambda: svc.clock.time() - t0 > 40 * 60, 30)
    svc.rig.sim.camera.thermal.temp = 14.4                                   # just after a warm session
    svc.harvest.thermal.refresh(force=True)
    svc.cmd_harvest(True)
    assert wait_for(lambda: any(f["temp_bin"] == 12.0 for f in _events(svc, "frame")), 60)
    first = [f for f in _events(svc, "frame") if f["temp_bin"] == 12.0][0]
    assert first["temp_start"] < 11.0 + 2 / 3 + 0.2                     # lower third of [11, 13), or the floor
    assert any("cool to" in e["msg"] for e in _events(svc, "decision") + _events(svc, "state"))


def test_sweep_spans_floor_to_plateau_and_back(make_service):
    svc = _svc(make_service, hv={"exposures": [10, 30, 60], "sentinel": {"exposure": 60}, "sweep": True,
                                 "sweep_every_h": 6})
    svc.rig.sim.camera.set_thermal(ambient_c=5.0, idle_offset_c=6.0, heat_c=6.5, tau_heat_min=4, tau_cool_min=12)
    _complete(svc, [12.0, 14.0, 16.0, 18.0])
    t0 = svc.clock.time()
    assert wait_for(lambda: svc.clock.time() - t0 > 40 * 60, 30)
    H = svc.harvest
    svc.cmd_harvest(True)
    assert wait_for(lambda: H.state == "sweep", 30)
    assert wait_for(lambda: os.path.exists(H.thermal_path), 120)
    with open(H.thermal_path) as fh:
        rec = [json.loads(x) for x in fh][-1]
    assert rec["kind"] == "sweep" and rec["aborted"] is None and rec["frames"] >= 10
    up, down = rec["up"], rec["down"]
    assert up["temp_start"] < 11.4 and up["temp_end"] > 16.0                # floor -> plateau
    assert down["temp_end"] < 11.4                                          # back down to the floor
    assert up["fit"]["tau_min"] and down["fit"]["tau_min"] and len(up["points"]) >= 5
    assert 3 < up["fit"]["tau_min"] < 8 and 8 < down["fit"]["tau_min"] < 25
    sweep_frames = [f for f in _events(svc, "frame") if f.get("purpose") == "sweep"]
    assert {12.0, 14.0, 16.0} <= {f["temp_bin"] for f in sweep_frames}
    assert {f["exposure"] for f in sweep_frames} >= {10.0, 30.0, 60.0}
    assert wait_for(lambda: H.state == "complete", 30)
    v = H.view()
    assert "next sweep" in v["headline"] and "UTC" in v["headline"]
    assert v["thermal"]["last_sweep"] and v["thermal"]["next_sweep"]
    n = svc.rig.sim.camera.exposures
    svc.harvest.wake.set()
    t2 = svc.clock.time()
    assert wait_for(lambda: svc.clock.time() - t2 > 2 * 3600, 60)
    assert svc.rig.sim.camera.exposures == n                               # no new sweep within sweep_every_h
    assert math.isfinite(v["thermal"]["tau_heat_min"])


def test_floor_follows_the_ambient():
    import datetime as dt
    from types import SimpleNamespace as NS
    from sequencer.thermal import ThermalModel
    t0 = 1_800_000_000.0
    rows = []
    for i in range(40):                       # idle cooling towards ambient + 6, ambient 5.0
        t = t0 + 60 * i
        rows.append({"time": dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "sensor_c": round(11.0 + 2.5 * math.exp(-60 * i / 720.0), 2), "outside": 5.0,
                     "activity": "idle", "exposing": 0})
    tl = NS(cfg=NS(sources=[("outside", {})], interval_s=60.0), rows=lambda h, now=None: rows)
    clock = NS(time=lambda: t0 + 40 * 60)
    m = ThermalModel(NS(templog=tl, clock=clock))
    s = m.refresh(force=True)
    assert abs(s["floor"] - 11.0) < 0.1 and abs(s["floor_offset"] - 6.0) < 0.1 and s["ambient"] == 5.0
    assert abs(s["tau_cool_s"] - 720) < 150
    rows[-1]["outside"] = 2.0                  # colder now: the floor moves with it
    s = m.refresh(force=True)
    assert s["floor"] < 10.0 and s["ambient"] == 2.0
