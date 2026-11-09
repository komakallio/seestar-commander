"""Dark harvest (sequencer/harvest.py) on the simulator with an accelerated clock: filling bins as the
sensor temperature drifts, choice order, pre-emption by plans, locks and jobs, hold-off, on/off persistence,
the status API and its token, stall detection."""
import json
import os
import time
import urllib.error
import urllib.request

from sequencer import plan as P
from sequencer.httpapi import serve

from conftest import ra_at_ha, wait_for

from scopelock import FileLock

HV = {"enabled": True, "exposures": [10, 20], "count": 3, "bias": False, "poll_s": 10, "holdoff_min": 10,
      "sentinel": {"exposure": 20, "gain": 200, "every_days": 3}}

PLAN = f"""
[plan]
park_at_end = false
[[step]]
name = "field"
ra = {ra_at_ha(-20)}
dec = 45
exposure = 20
gain = 200
count = 2
centre = "none"
"""


def _svc(make_service, speed=600, hv=None, **kw):
    svc = make_service(speed=speed, roof=False, extra={"darks_harvest": {**HV, **(hv or {})}}, **kw)
    return svc


def _harvesting(svc):
    return svc.harvest.active and svc.imager.exposing


def _events(svc, kind):
    with open(svc.harvest.log_path) as fh:
        return [r for r in (json.loads(x) for x in fh) if r["kind"] == kind]


def test_fills_bins_as_the_sensor_cools(make_service):
    svc = _svc(make_service)
    svc.rig.sim.camera.set_thermal(ambient_c=5.0, ambient_rate_c_h=-4.0)        # idle ~11 C, cooling
    assert wait_for(lambda: len({m["temp_bin"] for m in svc.darklib.entries()}) >= 3, 60)
    ents = svc.darklib.entries()
    assert all(m["source"] == "pool" and m["frames"] >= 3 for m in ents)
    assert {m["exposure"] for m in ents} == {10.0, 20.0}
    assert svc.idle() and svc.busy is None                     # never shows as busy
    v = svc.harvest.view()
    assert v["counters"]["today"]["frames"] >= 9 and v["counters"]["today"]["masters"] >= 3
    frames = _events(svc, "frame")
    assert frames and all("temp_bin" in f and "pool_n" in f for f in frames)
    assert _events(svc, "decision") and _events(svc, "master")
    assert any("master dark tele/e" in line for line in svc.st["log"])          # main log: one line per master


def test_choice_order_and_unsteady(make_service, tmp_path):
    pf = tmp_path / "planned.toml"
    pf.write_text(PLAN.replace("exposure = 20", "exposure = 15").replace("gain = 200", "gain = 120"))
    svc = _svc(make_service, dark_plans=[str(pf)], hv={"enabled": False})
    cam = svc.rig.sim.camera
    cam.set_temp(10.6)
    H = svc.harvest
    ch = H.choose(10.6)
    assert ch["key"].exposure == 15.0 and ch["key"].gain == 120 and ch["tier"] == 1   # the plan's key first
    svc.darklib.add(svc.dark_key(15, 120), [cam_frame()], [10.5], tb=10.0)
    ch = H.choose(10.6)                                                   # top half of bin 10 (9..11)
    assert ch["tier"] == 4 and ch["key"].exposure == 10.0                 # top half: short first
    assert H.choose(9.3)["key"].exposure == 20.0                          # bottom half: long first
    # a frame that warmed > 0.5 C during the exposure is archived but not pooled
    cam.set_temp(10.0, rate_c_h=150.0)
    assert svc.cmd_harvest(True)["ok"]
    assert wait_for(lambda: any(not f["steady"] for f in _events(svc, "frame")), 30)
    bad = [f for f in _events(svc, "frame") if not f["steady"]][0]
    assert "discarded from the pool" in bad["msg"]
    assert svc.darkpool.stats()["unsteady"] >= 1


def cam_frame():
    import numpy as np
    return np.full((960, 540), 400, np.uint16)


def test_preempted_by_plan(make_service):
    svc = _svc(make_service)
    svc.rig.sim.camera.set_temp(10.5)
    assert wait_for(lambda: _harvesting(svc), 30)
    aborts = svc.rig.sim.camera.aborts
    t0 = time.time()
    r = svc.cmd_start(P.loads(PLAN))
    assert r["ok"], r
    assert wait_for(lambda: svc.busy == "run", 5)
    assert wait_for(lambda: not svc.harvest.active, 2) and time.time() - t0 < 2
    assert svc.rig.sim.camera.aborts == aborts + 1                       # the harvest frame was aborted
    assert any("pre-empted by plan" in e["msg"] for e in _events(svc, "preempt"))
    assert wait_for(lambda: svc.status()["state"] == "done", 60)
    assert len(svc.st["frames"]) == 2 and svc.imager.filter_name() == "IR"   # lights on IR, not Dark
    assert not svc.harvest.active


def test_preempted_by_api_lock_and_holdoff(make_service):
    svc = _svc(make_service)
    svc.rig.sim.camera.set_temp(10.5)
    assert wait_for(lambda: _harvesting(svc), 30)
    r = svc.cmd_lock("tester", "test", ttl_s=600)
    assert r["ok"], r                                                    # never refused because of the harvest
    assert not svc.harvest.active and not svc.imager.exposing         # free when the lock is answered
    assert svc.imager.filter_name() == "IR"
    n = svc.rig.sim.camera.exposures
    assert wait_for(lambda: svc.harvest.state == "paused", 10)
    assert "scope lock by tester" in svc.harvest.reason
    time.sleep(0.3)
    assert svc.rig.sim.camera.exposures == n                             # nothing while locked
    assert svc.cmd_unlock("tester")["ok"]
    assert wait_for(lambda: svc.harvest.state == "holdoff", 10)
    t_unlock = svc.clock.time()
    assert wait_for(lambda: _harvesting(svc), 30)
    assert svc.clock.time() - t_unlock >= 9.5 * 60                      # hold-off: 10 min after the unlock


def test_preempted_by_file_lock(make_service):
    svc = _svc(make_service, speed=60, hv={"exposures": [120], "sentinel": {"exposure": 120}})  # 2 s frames
    svc.rig.sim.camera.set_temp(10.5)
    assert wait_for(lambda: _harvesting(svc), 30)
    fl = FileLock(svc.flock.path)
    assert fl.acquire("script", "test")
    try:
        assert wait_for(lambda: not svc.harvest.active, 3)
        assert any("file lock" in e["msg"] for e in _events(svc, "preempt"))
        assert wait_for(lambda: svc.harvest.state == "paused", 10) and "file lock" in svc.harvest.reason
    finally:
        fl.release()
    assert wait_for(lambda: svc.harvest.state == "holdoff", 10)


def test_preempted_by_jobs(make_service):
    svc = _svc(make_service)
    svc.rig.sim.camera.set_temp(10.5)
    for job in (lambda: svc.cmd_darks_build(exposure=5, count=1), lambda: svc.cmd_park("test")):
        assert wait_for(lambda: _harvesting(svc), 60)
        aborts = svc.rig.sim.camera.aborts
        assert job()["ok"]
        assert wait_for(lambda: not svc.harvest.active, 3)
        assert svc.rig.sim.camera.aborts == aborts + 1
        assert wait_for(svc.idle, 30)
        assert wait_for(lambda: svc.harvest.state == "holdoff", 10)
        svc.harvest.last_block = svc.clock.time() - 3600                  # skip the hold-off
        svc.harvest.wake.set()
    # stop stops the harvest frame too (and holds off)
    assert wait_for(lambda: _harvesting(svc), 60)
    r = svc.cmd_stop()
    assert r["ok"] and "harvest" in r["msg"] and not svc.harvest.active


def test_external_busy_and_stall(make_service):
    svc = _svc(make_service, speed=3000, hv={"stall_hours": 2, "enabled": False})
    svc.rig.sim.camera.set_temp(10.5)
    svc.harvest.busy_check = lambda: "app session running"
    svc.cmd_harvest(True)
    t0 = svc.clock.time()
    assert wait_for(lambda: svc.harvest.state == "paused", 10)
    assert "app session" in svc.harvest.reason and svc.rig.sim.camera.exposures == 0
    assert not svc.harvest.view()["stall"]["stalled"]
    assert wait_for(lambda: svc.clock.time() - t0 > 2.1 * 3600, 20)
    v = svc.harvest.view()
    assert v["stall"]["stalled"] and "app session" in v["stall"]["reason"]
    assert v["headline"].startswith("Harvest stalled:")
    svc.harvest.busy_check = None
    assert wait_for(lambda: svc.rig.sim.camera.exposures > 0, 20)


def test_on_off_persists_across_restart(make_service):
    svc = _svc(make_service, hv={"enabled": True})
    assert svc.harvest.enabled and svc.harvest.enabled_source == "config"
    assert svc.cmd_harvest(False)["ok"]
    assert not svc.cmd_harvest("yes")["ok"]
    with open(os.path.join(svc.s.data_dir, "darkharvest", "state.json")) as fh:
        assert json.load(fh)["enabled"] is False
    svc.shutdown()
    again = _svc(make_service, hv={"enabled": True})          # same data dir: the saved state wins
    assert not again.harvest.enabled and again.harvest.enabled_source == "state"
    assert wait_for(lambda: again.harvest.state == "off", 10)
    assert again.rig.sim.camera.exposures == 0


def test_status_api_and_token(make_service):
    svc = _svc(make_service, api_token="sekrit")
    svc.rig.sim.camera.set_temp(10.5)
    assert wait_for(lambda: svc.darklib.entries(), 60)
    srv = serve(svc, host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/api/darks/harvest?log=5", timeout=10) as resp:   # viewing is open
            v = json.loads(resp.read())
        for k in ("enabled", "enabled_source", "state", "headline", "reason", "since", "sensor", "current",
                  "blockers", "holdoff_until", "last_frame", "counters", "coverage", "sentinel", "pool", "stall",
                  "log", "config"):
            assert k in v, k
        assert v["enabled"] and len(v["log"]) == 5
        assert set(v["sensor"]) >= {"temp", "bin", "bin_pos", "trend_c_h"} and v["sensor"]["bin"] == 10.0
        cov = v["coverage"]
        assert 10.0 in cov["bins"] and {r["exposure"] for r in cov["rows"]} == {10.0, 20.0}
        cell = cov["rows"][0]["cells"][cov["bins"].index(10.0)]
        assert set(cell) >= {"temp_bin", "state", "pool_n", "count", "reached"}
        assert cell["state"] in ("fresh", "pool", "missing")
        assert set(v["counters"]["today"]) >= {"frames", "masters", "harvest_h", "preempted_h", "bins"}
        assert svc.status()["harvest"]["enabled"] is True

        def post(body, token=None):
            req = urllib.request.Request(base + "/api/darks/harvest", data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
        assert post({"enabled": False})[0] == 401
        assert svc.harvest.enabled
        code, r = post({"enabled": False}, "sekrit")
        assert code == 200 and r["ok"] and not svc.harvest.enabled
        assert not svc.harvest.active
    finally:
        srv.shutdown()


def test_sentinel_due_in_the_common_bin(make_service):
    svc = _svc(make_service, hv={"enabled": False, "exposures": [20]})
    H = svc.harvest
    H._common = (10.0, svc.clock.time())
    sk = H.sentinel_key()
    m = svc.darklib.add(sk, [cam_frame()], [10.2], tb=10.0)
    assert not H.needs(10.0)                                           # fresh: nothing to do
    meta = os.path.join(m["dir"], "meta.json")
    with open(meta) as fh:
        d = json.load(fh)
    d["created_unix"] -= 4 * 86400
    with open(meta, "w") as fh:
        json.dump(d, fh)
    n = H.needs(10.0)
    assert n and n[0]["tier"] == 3 and "sentinel" in n[0]["why"]
    assert all(x["tier"] != 3 for x in H.needs(12.0))                 # other bins: just the standard set


def test_shutdown_aborts_the_frame_in_flight(make_service):
    svc = _svc(make_service, speed=60, hv={"exposures": [120], "sentinel": {"exposure": 120}})   # 2 s frames
    cam = svc.rig.sim.camera
    cam.set_temp(10.5)
    assert wait_for(lambda: _harvesting(svc), 30)
    assert os.path.exists(svc.harvest.inflight_path)
    aborts = cam.aborts
    t0 = time.time()
    svc.shutdown()
    assert time.time() - t0 < 4
    assert cam.aborts == aborts + 1 and cam.CameraState == 0
    assert svc.imager.filter_name() == "IR" and not svc.harvest.active
    assert not os.path.exists(svc.harvest.inflight_path)


def _leftover(svc, started_ago_s):
    """What a killed process leaves: the camera still exposing a 120 s dark, the wheel on Dark, the marker."""
    cam = svc.rig.sim.camera
    svc.imager.set_filter("Dark")
    cam.StartExposure(120, light=False)
    now = svc.clock.time()
    svc.harvest._orphan = {"started_unix": now - started_ago_s, "exposure": 120.0, "label": "tele 120s g200",
                           "prev_filter": "IR", "started": "x"}
    svc.harvest.wake.set()
    return cam


def test_restart_recovers_our_own_frame(make_service):
    svc = _svc(make_service, speed=60, hv={"enabled": False})
    svc.rig.sim.camera.set_temp(10.5)
    aborts = svc.rig.sim.camera.aborts
    cam = _leftover(svc, 10)
    assert wait_for(lambda: cam.aborts == aborts + 1, 5)            # ours: aborted, no 10-min hold-off
    assert wait_for(lambda: svc.imager.filter_name() == "IR", 5)
    assert any("previous process" in e["msg"] for e in _events(svc, "recover"))
    t0 = svc.clock.time()
    assert svc.cmd_harvest(True)["ok"]
    assert wait_for(lambda: _harvesting(svc), 10)
    assert svc.clock.time() - t0 < 5 * 60
    assert not any("camera busy" in e["msg"] for e in _events(svc, "state"))


def test_restart_stale_marker_is_someone_elses(make_service):
    svc = _svc(make_service, speed=60, hv={"enabled": True})
    cam = svc.rig.sim.camera
    cam.set_temp(10.5)
    assert wait_for(lambda: _harvesting(svc), 10)
    svc.cmd_harvest(False)
    assert wait_for(lambda: svc.harvest.state == "off", 5)
    aborts = cam.aborts
    _leftover(svc, 3600)                                             # an hour old: not ours
    svc.cmd_harvest(True)
    assert wait_for(lambda: svc.harvest.state == "paused", 10)
    assert "telephoto camera busy" in svc.harvest.reason and cam.aborts == aborts
