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
    assert abs(s["floor"] - 11.0) < 0.15 and abs(s["floor_offset"] - 6.0) < 0.15 and s["ambient"] == 5.0
    assert min(abs(s["tau_cool_s"] - 720), abs((s["tau_cool_fast_s"] or 1e9) - 720)) < 200
    rows[-1]["outside"] = 2.0                  # colder now: the floor moves with it
    s = m.refresh(force=True)
    assert s["floor"] < 10.0 and s["ambient"] == 2.0


# ---- two time scales (the production fit put the floor 0.8 C too high) ----------------------------------
T0 = 1_800_000_000.0
FLOOR_OFF = 6.0                     # true idle offset above ambient


def _two_scale(t, a_fast, a_slow, tf=360.0, ts=3600.0):
    return FLOOR_OFF + a_fast * math.exp(-t / tf) + a_slow * math.exp(-t / ts)


def _row(t, T, amb, activity="idle", exposing=0):
    import datetime as dt
    return {"time": dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sensor_c": round(round(T * 16) / 16, 4), "outside": amb, "activity": activity, "exposing": exposing}


def _production_like(long_stretch=True, short_only_offset=None):
    """~2 days of templog: a 3 h idle evening (ends at offset 6.05, 11.3 C at 5.25 outside), then a night of
    harvest series with 12-min idle gaps (die cools fast, body still warm: each gap ends ~6.9 above ambient),
    and one 26-min stretch 13.25 -> ~11.9 C at 5.75 outside. Returns (rows, now)."""
    rows, t = [], T0
    if long_stretch:
        amb = 5.25
        for i in range(181):                                   # 3 h idle evening
            rows.append(_row(t + 60 * i, amb + _two_scale(60 * i, 0.5, 1.2), amb))
        t += 181 * 60 + 3600 * 20                             # a day later (outside fit_hours)
    amb = 5.85
    for g in range(10):                                        # harvest night: 20 min series, 12 min gaps
        for i in range(20):
            rows.append(_row(t + 60 * i, amb + 7.0 + 0.5 * min(i, 10) / 10, amb, "harvest", 1))
        t += 20 * 60
        for i in range(13):
            rows.append(_row(t + 60 * i, amb + _two_scale(60 * i, 0.6, 0.95), amb))
        t += 13 * 60
    amb = 5.75
    for i in range(27):                                        # 26 min, 13.25 -> 11.9, still falling
        rows.append(_row(t + 60 * i, amb + _two_scale(60 * i, 0.6, 0.9, ts=2400.0), amb))
    t += 27 * 60
    if short_only_offset is not None:                          # one short idle dip seen earlier tonight
        for i in range(4):
            rows.append(_row(t + 60 * i, amb + short_only_offset, amb))
        t += 4 * 60
    rows.append(_row(t, amb + 7.2, 5.88, "harvest", 1))     # now: 5.88 C outside
    return rows, t


def _model(rows, now, frames=None):
    from types import SimpleNamespace as NS
    import threading
    from sequencer.thermal import ThermalModel
    tl = NS(cfg=NS(sources=[("outside", {})], interval_s=60.0), rows=lambda h, now=None: rows)
    pool = NS(_lock=threading.Lock(), frames=frames or {})
    return ThermalModel(NS(templog=tl, darkpool=pool, clock=NS(time=lambda: now)))


def test_two_time_scales_floor_from_the_long_stretches():
    rows, now = _production_like()
    idle = [r for r in rows if r["activity"] == "idle"]
    assert min(r["sensor_c"] - r["outside"] for r in idle) < 6.2
    m = _model(rows, now)
    s = m.refresh(force=True)
    f = s["floor_fit"]
    assert f["components"] == 2 and f["stretches"] >= 3
    assert abs(s["floor_offset"] - FLOOR_OFF) < 0.2, s["floor_fit"]
    assert abs(s["floor"] - (5.88 + FLOOR_OFF)) < 0.2 and s["ambient"] == 5.88
    assert s["tau_cool_fast_s"] < 15 * 60 < s["tau_cool_s"]
    # only the short gaps of the night (as in production): the single exponential stays ~0.9 C too high
    night = [r for r in rows if _unix(r["time"]) > T0 + 20 * 3600]
    from sequencer.thermal import fit_relax
    st = _idle_stretches(night)
    single = fit_relax([([_unix(r["time"]) for r in s_], [r["sensor_c"] - r["outside"] for r in s_]) for s_ in st])
    assert single["asym"] > FLOOR_OFF + 0.5


def _unix(s):
    from sequencer.thermal import _unix as u
    return u(s)


def _idle_stretches(rows):
    out, cur = [], []
    for r in rows:
        if r["activity"] == "idle":
            cur.append(r)
        elif cur:
            out.append(cur)
            cur = []
    return out + ([cur] if cur else [])


def test_floor_clamped_to_the_lowest_idle_offset_seen():
    # 40 min idle flat at 7.0 above ambient (fit: 7.0), but a 3-min dip to 6.1 was seen: never above that
    rows, t = [], T0
    for i in range(4):
        rows.append(_row(t + 60 * i, 5.5 + 6.1, 5.5))
    t += 3600
    for i in range(41):
        rows.append(_row(t + 60 * i, 5.5 + 7.0, 5.5))
    t += 41 * 60
    s = _model(rows, t).refresh(force=True)
    assert s["floor_fit"]["asym"] > 6.9 and s["floor_clamped"]
    assert abs(s["floor_offset"] - 6.125) < 0.01                 # 11.6 read as 11.625 (1/16 C steps)
    # and short stretches can't extrapolate it far below anything seen
    rows, now = _production_like(long_stretch=False)
    s = _model(rows, now).refresh(force=True)
    low = s["floor_fit"]["lowest_idle"]
    assert low - 1.0 - 1e-9 <= s["floor_offset"] <= low + 1e-9


def test_plateau_clamped_to_the_highest_exposing_offset_seen():
    rows, now = _production_like()
    frames = {("tele", f"f{i}"): {"start_unix": now - 7200 + 100 * i, "downloaded_unix": now - 7200 + 100 * i + 63,
                                  "exposure": 60.0, "temp_start": 12.0, "temp_end": 12.0} for i in range(3)}
    frames[("tele", "hot")] = {"start_unix": now - 3 * 86400, "downloaded_unix": now - 3 * 86400 + 63,
                               "exposure": 60, "temp_start": 17.5, "temp_end": 17.6}     # outside the templog span
    frames[("tele", "warm")] = {"start_unix": now - 3600, "downloaded_unix": now - 3600 + 63, "exposure": 60,
                                "temp_start": 16.4, "temp_end": 16.5}                    # 10.65 above 5.85
    s = _model(rows, now, frames).refresh(force=True)
    assert s["plateau_clamped"] and abs(s["plateau"] - (5.88 + 16.5 - 5.85)) < 0.05


def test_reach_uses_what_was_seen_at_this_ambient(make_service):
    svc = _svc(make_service)
    H = svc.harvest
    H.thermal.refresh(force=True)
    s = dict(H.thermal.s, floor=12.76, plateau=17.8, ambient=5.88, observed_low=None, observed_high=None)
    H.thermal.s, H.thermal._t = s, svc.clock.time() + 1e9         # frozen: no refresh
    H._ach = (None, {})
    assert H.reach(12.0) == "edge" and H.reach(10.0) == "unreachable_now"
    H.thermal.s = dict(s, observed_low=11.3, observed_high=13.5)    # 11.3 C seen at 5.2 outside (within 1 C)
    H._ach = (None, {})
    assert H.reach(12.0) == "reachable" and H.reach(10.0) == "unreachable_now"
    assert H.achievable(10.0, 12.0)
    H.thermal.s = dict(s, observed_low=10.8, observed_high=13.5)    # seen just inside +10's top edge
    H._ach = (None, {})
    assert H.reach(10.0) == "edge"


def test_observed_range_only_at_a_similar_ambient():
    rows, now = _production_like()
    s = _model(rows, now).refresh(force=True)
    assert s["observed_low"] is not None and s["observed_low"] < 11.4       # the evening at 5.25 (within 1 C)
    cold = [dict(r, outside=r["outside"] - 3.0) if _unix(r["time"]) < T0 + 4 * 3600 else r for r in rows]
    s2 = _model(cold, now).refresh(force=True)
    assert s2["observed_low"] > s["observed_low"] + 0.3                     # seen at 2.25: doesn't count


def test_stalled_cooling_counts_as_the_floor(make_service):
    # the fit puts the floor 0.9 C too low (11.0 vs the real 11.9): the harvester must not wait for 11.67
    # (bottom third of +12) forever once the sensor stops cooling at 11.9
    svc = _svc(make_service, hv={"sweep": False, "exposures": [10]})
    svc.rig.sim.camera.set_thermal(ambient_c=5.9, idle_offset_c=6.0, heat_c=4.9, tau_heat_min=4, tau_cool_min=12,
                                   start_c=12.3)
    _complete(svc, [14.0, 16.0])
    H = svc.harvest
    H.thermal.refresh(force=True)
    s = dict(H.thermal.s, floor=11.0, plateau=15.9, observed_low=None, observed_high=None)
    H.thermal.s, H.thermal._t = s, svc.clock.time() + 1e9
    H._ach = (None, {})
    svc.cmd_harvest(True)
    assert wait_for(lambda: any(f["temp_bin"] == 12.0 for f in _events(svc, "frame")), 60)
    first = [f for f in _events(svc, "frame") if f["temp_bin"] == 12.0][0]
    assert 11.85 < first["temp_start"] < 12.0                          # started at the real floor
