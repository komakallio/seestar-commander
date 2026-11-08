import datetime as dt

import pytest

from sequencer import astro
from sequencer import plan as P

from conftest import LAT, LON, NIGHT, ra_at_ha, wait_for

NOON = dt.datetime(2026, 11, 8, 10, 0, tzinfo=dt.timezone.utc).timestamp()


def test_fast_astro_matches_astropy():
    from astropy.coordinates import AltAz, EarthLocation, get_sun
    from astropy.time import Time
    import astropy.units as u
    t = Time(NIGHT, format="unix")
    lst = t.sidereal_time("apparent", longitude=LON * u.deg).deg
    assert abs(astro.wrap180(astro.lst_deg(NIGHT, LON) - lst)) < 0.01
    loc = EarthLocation(lat=LAT * u.deg, lon=LON * u.deg)
    for tt in (NIGHT, NOON):
        ref = get_sun(Time(tt, format="unix")).transform_to(AltAz(obstime=Time(tt, format="unix"), location=loc))
        assert astro.sun_alt(tt, LAT, LON) == pytest.approx(ref.alt.deg, abs=0.1)


def test_min_alt_and_horizon():
    c = P.Constraints(min_alt=20)
    ra = ra_at_ha(0)
    assert P.target_failures(c, ra, 41, NIGHT, LAT, LON) == []          # alt ~71
    assert P.target_failures(c, ra, -40, NIGHT, LAT, LON)                 # alt ~-10
    assert P.target_failures(c, ra, 41, NIGHT, LAT, LON, min_alt=75)      # per-step override
    c.horizon = P.Horizon([(0, 80), (359, 80)])                           # a wall all round
    assert any("horizon" in f for f in P.target_failures(c, ra, 41, NIGHT, LAT, LON))


def test_sun_and_end_time():
    c = P.Constraints(sun_max_alt=-12)
    assert P.night_failures(c, NIGHT, LAT, LON) == []
    assert any("sun" in f for f in P.night_failures(c, NOON, LAT, LON))
    assert any("end_time" in f for f in P.night_failures(c, NIGHT, LAT, LON, end_t=NIGHT - 60))


def test_service_skips_target_below_limits(make_service):
    svc = make_service()
    pl = P.loads(f"""
[plan]
park_at_end = false
[[step]]
name = "low"
ra = {ra_at_ha(0)}
dec = -45
count = 2
[[step]]
name = "walled"
ra = {ra_at_ha(0)}
dec = 41
min_alt = 85
count = 2
""")
    assert svc.cmd_start(pl)["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done", 30)
    steps = svc.status()["plan"]["steps"]
    assert [s["status"] for s in steps] == ["skipped", "skipped"]
    assert "altitude" in steps[0]["reason"]
    assert svc.rig.sim.camera.exposures == 0


def test_service_stops_at_daylight(make_service):
    # dawn: start just before the Sun reaches the limit, so the plan ends on the sun constraint
    t = NIGHT
    while astro.sun_alt(t, LAT, LON) < -12.5:
        t += 60
    svc = make_service(start=t - 300)
    pl = P.loads(f'[plan]\npark_at_end = true\n[[step]]\nra = {ra_at_ha(-60, t)}\ndec = 60\n'
                 'exposure = 60\ncount = 50\ncentre = "none"\n')
    assert svc.cmd_start(pl)["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done", 60)
    st = svc.status()
    assert "sun" in st["plan"]["steps"][0]["reason"]
    assert 0 < st["plan"]["steps"][0]["frames"] < 50
    assert svc.rig.sim.telescope.AtPark                 # parked at the end
