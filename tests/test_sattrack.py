"""Satellite pass planner: geometry, passes, guard, element cache. Offline (a 2014 ISS TLE)."""
import json

import numpy as np
import pytest

skyfield = pytest.importorskip("skyfield")
from skyfield.api import EarthSatellite  # noqa: E402

from sattrack import elements as el  # noqa: E402
from sattrack import geometry as geo  # noqa: E402
from sattrack import passes as ps  # noqa: E402
from sattrack.guard import GuardError, Limits  # noqa: E402

L1 = "1 25544U 98067A   14020.93268519  .00009878  00000-0  18200-3 0  5082"
L2 = "2 25544  51.6498 109.4756 0003572  55.9686 274.8005 15.49815350868473"
EPOCH = 1390256584.0          # 2014-01-20 22:23 UTC
SITE = geo.Site(45.0, 25.0, 0.0)   # mid latitude: ISS goes overhead


@pytest.fixture(scope="module")
def iss():
    return EarthSatellite(L1, L2, "ISS (ZARYA)", geo.timescale())


@pytest.fixture(scope="module")
def day(iss):
    return ps.find_passes(iss, SITE, EPOCH, EPOCH + 86400)


def wide():
    return Limits(u_min=-175.0, u_max=175.0)


def test_track_matches_skyfield_hadec(iss):
    t = EPOCH + np.arange(0, 3600, 120.0)
    tr = geo.track(iss, SITE, t)
    ha, dec, _ = (iss - SITE.topos()).at(geo.times(t)).hadec()
    assert np.abs(geo.wrap180(tr["ha"] - ha.hours * 15)).max() < 1e-6
    assert np.abs(tr["dec"] - dec.degrees).max() < 1e-6


def test_find_passes(day):
    assert 3 <= len(day) <= 8
    for rise, set_ in day:
        assert 60 < set_ - rise < 900


def test_analyse_ok_pass_is_continuous_and_inside_window(iss, day):
    lim = wide()
    res = [ps.analyse(iss, SITE, r, s, lim, min_alt=10, norad=25544) for r, s in day]
    good = [p for p in res if p.verdict == "ok"]
    assert good
    for p in good:
        u = np.array(p.samples["u"])
        assert np.abs(np.diff(u)).max() < 1.0            # no 360 jumps at 0.1 s
        assert lim.u_min <= u.min() and u.max() <= lim.u_max
        assert p.summary["peak_rate_ra"] < 6
        assert len(u) * 0.1 == pytest.approx(p.summary["track_s"], abs=0.2)


def test_rate_limit_rejects(iss, day):
    hi = max(day, key=lambda rs: ps.analyse(iss, SITE, *rs, wide(), keep_samples=False).summary.get("peak_alt", 0))
    p = ps.analyse(iss, SITE, *hi, Limits(u_min=-175, u_max=175, max_rate=0.05), min_alt=10)
    assert p.verdict in ("trim", "reject")
    assert "rate" in p.reasons


def test_narrow_window_trims_with_cable_reason(iss, day):
    rs = day[0]
    p = ps.analyse(iss, SITE, *rs, wide(), min_alt=0)
    u = np.array(p.samples["u"])
    mid = float(np.median(u))
    lim = Limits(u_min=mid - 3, u_max=mid + 3, runaway_ra_s=0)    # window much smaller than the pass
    q = ps.analyse(iss, SITE, *rs, lim, min_alt=0, min_track_s=1)
    assert q.verdict in ("trim", "reject")
    assert "cable" in q.reasons


def test_guard_runaway():
    lim = Limits(u_min=-175, u_max=175, runaway_ra_s=30)
    lim.check(100, 0, 0.5, 0)
    with pytest.raises(GuardError, match="runaway_ra"):
        lim.check(170, 0, 0.5, 0)                  # 170 + 15 > 175
    with pytest.raises(GuardError, match="rate"):
        lim.check(0, 0, 7.0, 0)
    lim.check(0, -79, 0, -1.0)                      # Dec low side: firmware stop, allowed
    with pytest.raises(GuardError, match="runaway_dec"):
        lim.check(0, 80, 0, 1.0)                    # 80 + 10 > 85


def test_elements_parse_and_cache(tmp_path):
    omm = {"OBJECT_NAME": "X", "NORAD_CAT_ID": 1, "EPOCH": "2026-11-08T21:14:20.484384", "MEAN_MOTION": 15.49}
    assert el.parse_omm(json.dumps([omm]))["OBJECT_NAME"] == "X"
    assert el.parse_omm(json.dumps(omm))["MEAN_MOTION"] == 15.49
    with pytest.raises(el.ElementsError):
        el.parse_omm("[]")
    p = el.cache_path(str(tmp_path), 1, "gp")
    import os
    os.makedirs(os.path.dirname(p))
    with open(p, "w") as fh:
        json.dump({"fetched": 0, "source": "gp", "omm": omm}, fh)
    t_epoch = el.epoch_unix(omm)
    assert el.load(1, str(tmp_path), "gp", max_age_h=24, now=t_epoch + 3600)["NORAD_CAT_ID"] == 1
    with pytest.raises(el.ElementsError, match="old"):
        el.load(1, str(tmp_path), "gp", max_age_h=24, now=t_epoch + 48 * 3600)
    with pytest.raises(el.ElementsError, match="fetch first"):
        el.load(2, str(tmp_path), "gp")
