import datetime as dt
import json

import pytest

from sequencer import plan as P

BASIC = """
[plan]
name = "test"
park_at_end = false

[constraints]
min_alt = 25
sun_max_alt = -15
end_time = "05:30"
timezone = "Europe/Berlin"

[defaults]
exposure = 30
gain = 100
count = 5

[[step]]
target = "M31"
count = 12
qa = { max_hfr = 6.0, max_ecc = 0.6, min_stars = 20 }

[[step]]
target = "Custom field"
ra = "05h35m17s"
dec = "-05d23m28s"
filter = "LP"
dither = true
centre = "blob"
"""


def test_parse_basic():
    pl = P.loads(BASIC)
    assert pl.name == "test" and pl.park_at_end is False
    assert pl.constraints.min_alt == 25 and pl.constraints.sun_max_alt == -15
    a, b = pl.steps
    assert a.name == "M31" and a.count == 12 and a.exposure == 30 and a.gain == 100
    assert a.ra == pytest.approx(10.6847) and a.qa.max_hfr == 6.0 and a.qa.min_stars == 20
    assert b.ra == pytest.approx(83.82, abs=0.01) and b.dec == pytest.approx(-5.391, abs=0.01)
    assert b.filter == "LP" and b.dither and b.centre == "blob" and b.count == 5
    assert pl.light_settings() == [(30.0, 100)]


def test_numeric_coords_and_names():
    pl = P.loads('[[step]]\nra = 10.5\ndec = 41\n[[step]]\ntarget = "m 42"\n')
    assert pl.steps[0].ra == 10.5 and pl.steps[0].name.startswith("RA10.50")
    assert pl.steps[1].dec == pytest.approx(-5.39, abs=0.01)


@pytest.mark.parametrize("text, msg", [
    ('[[step]]\ntarget = "M31"\nexposre = 3\n', "unknown keys"),
    ('[[step]]\ntype = "darks"\n', "dark library"),
    ('[[step]]\ntarget = "M31"\nfilter = "Dark"\n', "Dark filter"),
    ('[[step]]\ntarget = "M31"\ncentre = "maybe"\n', "centre"),
    ('[[step]]\ntarget = "M31"\ncount = 0\n', "count"),
    ('[[step]]\nra = 10\ndec = 95\n', "out of range"),
    ('[plan]\nname = "x"\n', "no [[step]]"),
    ('[constraints]\ntimezone = "Mars/Olympus"\n[[step]]\ntarget = "M31"\n', "timezone"),
    ('[[step]\n', "TOML"),
])
def test_errors(text, msg):
    with pytest.raises(P.PlanError, match=msg.replace("[", r"\[")):
        P.loads(text)


def test_end_time_next_occurrence():
    c = P.Constraints(end_time="05:30", timezone="Europe/Berlin")
    t0 = dt.datetime(2026, 11, 8, 22, 0, tzinfo=dt.timezone(dt.timedelta(hours=1))).timestamp()
    end = dt.datetime.fromtimestamp(c.end_after(t0), dt.timezone.utc)
    assert end == dt.datetime(2026, 11, 9, 4, 30, tzinfo=dt.timezone.utc)
    c2 = P.Constraints(end_time="2026-11-09T04:00:00+00:00")
    assert c2.end_after(t0) == dt.datetime(2026, 11, 9, 4, 0, tzinfo=dt.timezone.utc).timestamp()


def test_yaml_plan():
    pytest.importorskip("yaml")
    pl = P.loads("plan: {name: y}\nsteps:\n  - {target: M31, count: 3}\n", fmt="yaml")
    assert pl.name == "y" and pl.steps[0].count == 3


def test_horizon_files(tmp_path):
    hrz = tmp_path / "site.hrz"
    hrz.write_text("# az alt\n0 10\n90 20\n180 30\n270 20\n")
    h = P.Horizon.load(str(hrz))
    assert h.alt_at(45) == pytest.approx(15)
    assert h.alt_at(315) == pytest.approx(15)       # wraps between 270 and 360/0
    assert h.alt_at(360) == pytest.approx(10)
    js = tmp_path / "horizon.json"
    js.write_text(json.dumps({"points": [[0, 5], [180, 25]]}))
    assert P.Horizon.load(str(js)).alt_at(90) == pytest.approx(15)
    js2 = tmp_path / "horizon2.json"
    js2.write_text(json.dumps({"dense_az": [0, 120, 240], "dense_alt": [3, 6, 9]}))
    assert P.Horizon.load(str(js2)).alt_at(60) == pytest.approx(4.5)
    plan = tmp_path / "p.toml"
    plan.write_text('[constraints]\nhorizon = "site.hrz"\n[[step]]\ntarget = "M31"\n')
    assert P.load(str(plan)).constraints.horizon.alt_at(180) == pytest.approx(30)


def test_missing_horizon_file(tmp_path):
    with pytest.raises(P.PlanError, match="not found"):
        P.loads('[constraints]\nhorizon = "nope.hrz"\n[[step]]\ntarget = "M31"\n', base_dir=str(tmp_path))
