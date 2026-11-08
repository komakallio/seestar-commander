"""Test fixtures. Everything runs against the simulator; no network, no hardware."""
import datetime as dt
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
os.environ["SEESTAR_CONFIG"] = os.path.join(HERE, "config.test.toml")
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import pytest  # noqa: E402

from sequencer import astro  # noqa: E402
from sequencer.clock import ScaledClock  # noqa: E402
from sequencer.rig import Settings, build_sim  # noqa: E402

LAT, LON = 60.0, 25.0
NIGHT = dt.datetime(2026, 11, 8, 20, 0, tzinfo=dt.timezone.utc).timestamp()   # sun about -40
SMALL = dict(width=540, height=960, pix_arcsec=14.96)                        # faster frames


def wait_for(pred, timeout=30.0, poll=0.02):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(poll)
    return False


@pytest.fixture
def make_service(tmp_path):
    from sequencer.service import Service
    made = []

    def _make(speed=200.0, start=NIGHT, roof=True, solver=True, world=None, **settings):
        kw = dict(lat=LAT, lon=LON, data_dir=str(tmp_path / "data"), safety_poll_s=2.0, monitor_s=0.05,
                  hooks_parked="http://hooks.invalid/parked", hooks_unparked="http://hooks.invalid/unparked",
                  dew_heater=True)
        kw.update(settings)
        s = Settings(**kw)
        clock = ScaledClock(speed, start)
        rig = build_sim(s, clock, roof=roof, solver=solver, **{**SMALL, **(world or {})})
        svc = Service(rig).start()
        made.append(svc)
        return svc

    yield _make
    for svc in made:
        svc.shutdown(timeout=5)


def ra_at_ha(ha, t=NIGHT):
    """J2000-ish RA that has hour angle `ha` at time t (good to the precession, ~0.3 deg)."""
    return (astro.lst_deg(t, LON) - ha) % 360.0
