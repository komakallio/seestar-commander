"""Target scheduler (plan order = "best", sequencer/scheduler.py): scoring, blocks and revisits, waiting
for a target to rise, the end of the night, and that order = "sequence" is unchanged."""
import pytest

from sequencer import astro
from sequencer import plan as P
from sequencer import scheduler as S

from conftest import LAT, LON, NIGHT, ra_at_ha, wait_for

LIMIT = P.NightLimit(sun_max_alt=-8)


def _crossing(alt, t=NIGHT):
    while astro.sun_alt(t, LAT, LON) < alt:
        t += 30
    return t


DAWN8 = _crossing(-8.0)


def _plan(steps, order="best", min_alt=20, park=False, extra=""):
    body = "\n".join("[[step]]\n" + "\n".join(f"{k} = {v!r}" if isinstance(v, str) else f"{k} = {v}"
                                              for k, v in st.items()) for st in steps)
    return P.loads(f"""
[plan]
name = "sched"
order = "{order}"
park_at_end = {str(park).lower()}
[constraints]
min_alt = {min_alt}
{extra}
[defaults]
exposure = 60
centre = "none"
{body}
""")


def _ctx(pl, u_max=175.0, t=NIGHT):
    return S.Ctx(LAT, LON, pl.constraints, pl.constraints.end_after(t), LIMIT, 15.0, 2.0, -175.0, u_max, "UTC")


def _fresh(pl):
    return [{"frames": 0, "status": "pending"} for _ in pl.steps]


def _picks(r):
    return [b["name"] for b in r["blocks"] if b["kind"] == "block"]


# ---- plan options --------------------------------------------------------------------------------

def test_plan_options_parse():
    pl = _plan([dict(name="a", ra=10, dec=60, block_frames=5, priority=2),
                dict(name="b", ra=20, dec=60, block_minutes=30)])
    assert pl.order == "best"
    a, b = pl.steps
    assert (a.block_frames, a.priority, b.block_minutes) == (5, 2.0, 30.0)
    assert "blocks of 5" in a.describe() and "priority 2" in a.describe()
    assert S.block_size(b, _ctx(pl)) == 24          # 30 min / (60 s + 15 s)
    assert P.loads('[[step]]\nra = 1\ndec = 2\n').order == "sequence"     # default: today's behaviour
    with pytest.raises(P.PlanError, match="order"):
        _plan([dict(name="a", ra=1, dec=2)], order="random")
    with pytest.raises(P.PlanError, match="block_frames"):
        _plan([dict(name="a", ra=1, dec=2, block_frames=-1)])


# ---- pure projection -----------------------------------------------------------------------------

def test_setting_target_first_then_high_one():
    pl = _plan([dict(name="high", ra=ra_at_ha(-10), dec=70, count=40, block_frames=10),
                dict(name="setting", ra=ra_at_ha(55), dec=10, count=20, block_frames=10)])
    r = S.project(pl, _fresh(pl), NIGHT, _ctx(pl))
    assert _picks(r)[:2] == ["setting", "setting"]       # it would otherwise be lost
    assert _picks(r).count("high") == 4 and r["unscheduled"] == [] and r["end_why"] == "all done"
    blocks = [b for b in r["blocks"] if b["name"] == "setting"]
    st = pl.steps[1]
    assert all(S.visible(st, _ctx(pl), b["end"]) for b in blocks)      # every block fits before it sets


def test_equal_targets_interleave_and_revisit():
    ra = ra_at_ha(-20)
    pl = _plan([dict(name="a", ra=ra, dec=60, count=20, block_frames=5),
                dict(name="b", ra=ra, dec=60, count=20, block_frames=5)])
    r = S.project(pl, _fresh(pl), NIGHT, _ctx(pl))
    assert _picks(r) == ["a", "b"] * 4
    assert all(b["frames"] == 5 for b in r["blocks"])
    # the times chain: each block starts where the last one ended
    assert all(x["end"] == y["start"] for x, y in zip(r["blocks"], r["blocks"][1:]))


def test_priority_wins():
    pl = _plan([dict(name="setting", ra=ra_at_ha(55), dec=10, count=10),
                dict(name="prio", ra=ra_at_ha(-10), dec=70, count=10, priority=2)])
    assert _picks(S.project(pl, _fresh(pl), NIGHT, _ctx(pl)))[0] == "prio"


def test_waits_for_a_rising_target():
    pl = _plan([dict(name="rising", ra=ra_at_ha(-110), dec=5, count=10)])
    r = S.project(pl, _fresh(pl), NIGHT, _ctx(pl))
    w, b = r["blocks"]
    assert w["kind"] == "wait" and w["name"] == "rising" and w["why"] == "rises above 20°"
    assert w["start"] == NIGHT and b["start"] == w["end"] > NIGHT + 3600
    alt, _ = astro.radec_to_altaz(pl.steps[0].ra, pl.steps[0].dec, b["start"], LAT, LON)
    assert 20 <= alt < 21


def test_more_targets_than_the_night_holds():
    pl = _plan([dict(name=f"t{i}", ra=ra_at_ha(-30 + 15 * i), dec=60, count=120, block_minutes=60)
                for i in range(5)])
    r = S.project(pl, _fresh(pl), NIGHT, _ctx(pl))
    assert r["unscheduled"] and r["end_why"] == "night ends"
    assert r["blocks"][-1]["end"] <= r["night_end"]
    assert len(set(_picks(r))) == 5                      # everyone gets time
    assert sum(u["frames"] for u in r["unscheduled"]) + sum(b["frames"] for b in r["blocks"]) == 600


def test_never_rising_target_is_not_scheduled():
    pl = _plan([dict(name="south", ra=ra_at_ha(0), dec=-60, count=5)])
    r = S.project(pl, _fresh(pl), NIGHT, _ctx(pl))
    assert r["blocks"] == [] and r["end_why"].startswith("nothing else observable")
    assert r["unscheduled"] == [{"step": 0, "name": "south", "frames": 5}]


def test_cable_room_is_preferred():
    # window ends 30 min of tracking past HA 0: the target at HA -10 has room for ~60 min, the one at
    # HA +5 for ~6 frames only
    pl = _plan([dict(name="short", ra=ra_at_ha(5), dec=60, count=20),
                dict(name="roomy", ra=ra_at_ha(-10), dec=60, count=20)])
    ctx = _ctx(pl, u_max=7.5)
    c = {x.name: x for x in S.ranked(pl, [0, 0], [True, True], NIGHT, ctx, S.night_end(ctx, NIGHT)[1])}
    assert c["short"].frames < 20 and c["short"].parts["cable"] < 0
    assert c["roomy"].frames == 20 and c["roomy"].parts["cable"] == 0
    assert c["roomy"].score > c["short"].score
    assert S.cable_frames(pl.steps[0], ctx, NIGHT) == c["short"].frames


def test_blocks_shrink_to_the_night_end():
    pl = _plan([dict(name="a", ra=ra_at_ha(-30), dec=60, count=100)], extra="sun_max_alt = -2")
    t = DAWN8 - 1200                                     # 20 min to the night limit (sun -8)
    r = S.project(pl, _fresh(pl), t, _ctx(pl, t=t))
    (b,) = r["blocks"]
    assert b["frames"] == int((1200 - S.TARGET_OVERHEAD_S) // 75) and b["end"] <= DAWN8 + 10


# ---- on the simulator ----------------------------------------------------------------------------

def _sched_lines(svc):
    return [ln.split("scheduler: ", 1)[1] for ln in svc.status()["log"] if "scheduler: " in ln]


def test_service_interleaves_blocks(make_service):
    svc = make_service(speed=300)
    ra = ra_at_ha(-20)
    pl = _plan([dict(name="a", ra=ra, dec=60, count=4, block_frames=2, exposure=20),
                dict(name="b", ra=ra, dec=60, count=4, block_frames=2, exposure=20)])
    assert svc.cmd_start(pl)["ok"]
    assert wait_for(lambda: (svc.status()["plan"] or {}).get("schedule", {}).get("blocks"), 20)
    sch = svc.status()["plan"]["schedule"]
    assert [b["name"] for b in sch["blocks"]] == ["a", "b", "a", "b"]
    assert wait_for(lambda: svc.status()["state"] == "done" and svc.idle(), 90)
    picks = [ln.split(" x")[0] for ln in _sched_lines(svc) if " x" in ln]
    assert picks == ["a", "b", "a", "b"]                 # revisited after the other target's block
    st = svc.status()["plan"]["steps"]
    assert [(x["frames"], x["status"]) for x in st] == [(4, "done"), (4, "done")]
    assert svc.rig.sim.telescope.firmware_gotos == 0


def test_service_parks_and_waits_for_a_rising_target(make_service):
    svc = make_service(speed=300)
    # dec 60 at lat 60 crosses 40 deg at HA -115: about 20 minutes after the start
    pl = _plan([dict(name="riser", ra=ra_at_ha(-120), dec=60, count=2, exposure=20)], min_alt=40)
    svc.rig.sim.telescope.Unpark()                        # out, so the wait has to park it
    assert svc.cmd_start(pl)["ok"]
    assert wait_for(lambda: svc.status()["headline"].startswith("Waiting for riser"), 30)
    st = svc.status()
    assert st["headline"].startswith("Waiting for riser (rises above 40° at ")
    assert st["state"] == "running" and st["busy"] == "run"
    assert svc.rig.sim.telescope.AtPark and not svc.rig.sim.telescope.Tracking
    assert st["plan"]["schedule"]["blocks"][0]["kind"] == "wait"
    assert wait_for(lambda: svc.status()["state"] == "done" and svc.idle(), 90)
    step = svc.status()["plan"]["steps"][0]
    assert step["frames"] == 2 and step["status"] == "done"
    alt, _ = astro.radec_to_altaz(pl.steps[0].ra, pl.steps[0].dec, svc.clock.time(), LAT, LON)
    assert alt > 40


def test_service_stop_during_a_wait(make_service):
    svc = make_service(speed=50)
    pl = _plan([dict(name="riser", ra=ra_at_ha(-120), dec=60, count=2, exposure=20)], min_alt=40)
    assert svc.cmd_start(pl)["ok"]
    assert wait_for(lambda: (svc.status()["action"] or {}).get("kind") == "wait", 30)
    svc.cmd_stop()
    assert wait_for(lambda: svc.idle() and svc.status()["state"] == "stopped", 10)


def test_service_skips_what_never_rises(make_service):
    svc = make_service(speed=300)
    pl = _plan([dict(name="south", ra=ra_at_ha(0), dec=-60, count=2, exposure=20),
                dict(name="north", ra=ra_at_ha(-20), dec=60, count=2, exposure=20)])
    assert svc.cmd_start(pl)["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done" and svc.idle(), 60)
    south, north = svc.status()["plan"]["steps"]
    assert north["status"] == "done" and south["status"] == "skipped" and south["frames"] == 0
    assert "not observable again" in south["reason"]


def test_service_ends_at_the_night_limit(make_service):
    svc = make_service(speed=300, start=DAWN8 - 900)
    pl = _plan([dict(name="a", ra=ra_at_ha(-30, DAWN8), dec=60, count=50, exposure=30, block_frames=5),
                dict(name="b", ra=ra_at_ha(-10, DAWN8), dec=60, count=50, exposure=30, block_frames=5)],
               extra="sun_max_alt = -2")
    assert svc.cmd_start(pl)["ok"]
    assert wait_for(lambda: svc.status()["state"] in ("done", "parked") and svc.idle(), 90)
    # park_at_end = false, but the service night limit parks the scope anyway (monitor or plan end)
    assert wait_for(lambda: svc.rig.sim.telescope.AtPark, 60)
    steps = svc.status()["plan"]["steps"]
    assert sum(x["frames"] for x in steps) > 0
    assert all(x["frames"] < 50 and x["status"] == "skipped" and "night" in x["reason"] for x in steps)
    assert astro.sun_alt(svc.clock.time(), LAT, LON) > -8.5
    assert svc.cmd_start(pl)["ok"] is False             # the night is over


def test_sequence_order_is_unchanged(make_service):
    svc = make_service(speed=300)
    # block_frames and priority are ignored in sequence order: step 1 runs to its count, then step 2
    pl = _plan([dict(name="first", ra=ra_at_ha(-20), dec=60, count=3, exposure=20, block_frames=1),
                dict(name="second", ra=ra_at_ha(55), dec=10, count=2, exposure=20, priority=5)],
               order="sequence")
    assert svc.cmd_start(pl)["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done" and svc.idle(), 60)
    st = svc.status()
    assert [(x["frames"], x["status"]) for x in st["plan"]["steps"]] == [(3, "done"), (2, "done")]
    assert not _sched_lines(svc) and "schedule" not in st["plan"] and "order" not in st["plan"]
    names = [f["target"] for f in st["frames"]]
    assert names == ["first"] * 3 + ["second"] * 2


def test_timeline_carries_the_schedule(make_service):
    from sequencer.statusview import timeline
    svc = make_service(speed=50)
    pl = _plan([dict(name="a", ra=ra_at_ha(-20), dec=60, count=6, block_frames=3, exposure=20),
                dict(name="b", ra=ra_at_ha(-20), dec=60, count=6, block_frames=3, exposure=20)])
    assert svc.cmd_start(pl)["ok"]
    assert wait_for(lambda: (svc.status()["action"] or {}).get("kind") == "expose", 30)
    st = svc.status()
    p = st["plan"]
    assert p["order"] == "best" and p["schedule"]["blocks"][0].get("running")
    assert p["eta_unix"] == p["schedule"]["blocks"][-1]["end"] and not p["eta_cut"]
    tl = timeline(svc)
    assert tl["schedule"] and {b["name"] for b in tl["schedule"]} == {"a", "b"}
    svc.cmd_stop()
    assert wait_for(svc.idle, 20)
