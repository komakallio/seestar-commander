"""Service-wide night limit ([sequencer] sun_limit_deg / latest_end) and the heartbeat hook."""
import datetime as dt
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sequencer import astro
from sequencer import plan as P
from sequencer.statusview import night_window, timeline

from conftest import LAT, LON, NIGHT, ra_at_ha, wait_for


def _crossing(alt, t=NIGHT):
    """Morning time after t when the Sun rises through alt."""
    while astro.sun_alt(t, LAT, LON) < alt:
        t += 30
    return t


DAWN8 = _crossing(-8.0)


def _plan(sun=-2.0, park=False, close=False, count=50, exposure=60, open_=False, end=None, t=DAWN8):
    return P.loads(f"""
[plan]
name = "dawn"
park_at_end = {str(park).lower()}
open_roof_at_start = {str(open_).lower()}
close_roof_at_end = {str(close).lower()}
[constraints]
sun_max_alt = {sun}
{f'end_time = "{end}"' if end else ''}
[[step]]
name = "t"
ra = {ra_at_ha(-30, t)}
dec = 60
exposure = {exposure}
count = {count}
centre = "none"
""")


# ---- pure ----------------------------------------------------------------------------------------

def test_stricter_of_plan_and_limit():
    L = P.NightLimit(sun_max_alt=-8)
    assert P.sun_max_alt(P.Constraints(sun_max_alt=-12), L) == (-12, "plan")
    assert P.sun_max_alt(P.Constraints(sun_max_alt=-2), L) == (-8, "night limit")
    assert P.sun_max_alt(None, L) == (-8, "night limit")
    t = DAWN8 + 120                                         # sun just above -8
    lax, strict = P.Constraints(sun_max_alt=-2), P.Constraints(sun_max_alt=-12)
    assert P.night_failures(lax, t, LAT, LON) == []          # the plan alone would go on
    nf = P.night_failures(lax, t, LAT, LON, limit=L)
    assert P.limit_reached(nf) and "> -8.0 (night limit)" in nf[0]
    nf = P.night_failures(strict, t, LAT, LON, limit=L)
    assert nf and not P.limit_reached(nf)                    # the plan's own -12 is the reason


def test_latest_end_belongs_to_the_night():
    L = P.NightLimit(sun_max_alt=90, latest_end="03:00", timezone="UTC")     # the Sun is never the reason
    day = dt.datetime(2026, 11, 8, tzinfo=dt.timezone.utc)
    ev, morning, late = (day.replace(hour=h).timestamp() for h in (20, 2, 6))
    assert L.end_for(ev) == (day + dt.timedelta(days=1, hours=3)).timestamp()
    assert L.end_for(morning) == day.replace(hour=3).timestamp()
    assert P.night_failures(None, ev, LAT, LON, limit=L) == []
    assert P.night_failures(None, morning, LAT, LON, limit=L) == []
    nf = P.night_failures(None, late, LAT, LON, limit=L)       # 06:00, after 03:00: still over until noon
    assert P.limit_reached(nf) and "latest_end" in nf[0]
    # the earlier of the plan's end_time and latest_end wins
    c = P.Constraints(end_time="04:00")
    assert P.end_time(c.end_after(ev), L, ev) == (L.end_for(ev), "night limit")
    c = P.Constraints(end_time="01:00")
    assert P.end_time(c.end_after(ev), L, ev)[1] == "plan"
    with pytest.raises(ValueError):
        P.NightLimit(latest_end="25:00")


def test_night_window_times():
    L = P.NightLimit(sun_max_alt=-8)
    reasons, opens, ends = night_window(LAT, LON, None, None, L, NIGHT)
    assert reasons == [] and opens is None and abs(ends - DAWN8) < 60
    reasons, opens, ends = night_window(LAT, LON, None, None, L, DAWN8 + 3600)     # daytime
    assert reasons and opens > DAWN8 + 3600 and ends > opens
    assert astro.sun_alt(opens, LAT, LON) == pytest.approx(-8, abs=0.2)


# ---- service -------------------------------------------------------------------------------------

def test_plan_stops_and_parks_at_the_limit(make_service):
    svc = make_service(start=DAWN8 - 600)
    dome = svc.rig.sim.dome
    assert svc.cmd_start(_plan(sun=-2, park=False))["ok"]           # park_at_end = false: parks anyway
    assert wait_for(lambda: svc.status()["state"] == "done" and svc.idle(), 60)
    st = svc.status()
    step = st["plan"]["steps"][0]
    assert 0 < step["frames"] < 50 and "night limit" in step["reason"]
    assert svc.rig.sim.telescope.AtPark
    assert "http://hooks.invalid/parked" in svc.rig.hooks_posted
    # the last frame ended before the limit (end-of-frame look-ahead)
    assert astro.sun_alt(svc.clock.time(), LAT, LON) > -8.5
    assert dome.close_calls == 0 and st["roof"]["close_requested"] and st["roof"]["reason"] == "night limit"
    assert st["night_limit"]["reached"]
    assert any("night over" in line and "night limit" in line for line in st["log"])


@pytest.mark.parametrize("how", ["close_roof_at_end", "auto"])
def test_limit_closes_roof_per_rules(make_service, how):
    svc = make_service(start=DAWN8 - 600, auto_close_on_unsafe=(how == "auto"))
    assert svc.cmd_start(_plan(sun=-2, park=True, close=(how == "close_roof_at_end")))["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done" and svc.idle(), 60)
    assert svc.rig.sim.telescope.AtPark
    assert svc.rig.sim.dome.close_calls == 1 and svc.rig.sim.dome.ShutterStatus == 1
    assert not svc.status()["roof"]["close_requested"]


def test_plan_refused_after_the_limit(make_service):
    svc = make_service(start=DAWN8 + 300)
    r = svc.cmd_start(_plan(sun=-2))
    assert not r["ok"] and "night limit reached" in r["msg"] and "imaging allowed again from" in r["msg"]
    assert svc.rig.sim.camera.exposures == 0
    # the plan's own constraint refuses too, with its own message
    svc2 = make_service(start=_crossing(-12) + 60, sun_limit_deg=-5)
    r = svc2.cmd_start(_plan(sun=-12))
    assert not r["ok"] and "the plan's night is over" in r["msg"]


def test_resume_refused_after_the_limit(make_service):
    svc = make_service(speed=100, start=DAWN8 - 900)
    assert svc.cmd_start(_plan(sun=-2, exposure=120))["ok"]
    assert wait_for(lambda: svc.imager.exposing, 30)
    svc.cmd_stop()
    assert wait_for(lambda: svc.status()["state"] == "stopped" and svc.idle(), 30)
    assert wait_for(lambda: svc.clock.time() > DAWN8 + 30, 30)
    assert wait_for(lambda: svc.rig.sim.telescope.AtPark and svc.idle(), 60)   # the idle guard parked it
    r = svc.cmd_resume()
    assert not r["ok"] and "night limit" in r["msg"]


def test_stricter_plan_constraint_wins(make_service):
    """Plan sun_max_alt -12 is stricter than the service's -8: the plan ends at -12 by its own rule."""
    t12 = _crossing(-12)
    svc = make_service(start=t12 - 600)
    assert svc.cmd_start(_plan(sun=-12, park=True, t=t12))["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done" and svc.idle(), 60)
    st = svc.status()
    reason = st["plan"]["steps"][0]["reason"]
    assert "> -12.0" in reason and "night limit" not in reason
    assert svc.clock.time() < DAWN8                 # well before the service limit
    assert svc.rig.sim.telescope.AtPark
    tl = timeline(svc)
    assert tl["sun_max_alt"] == -12 and tl["sun_limit_by"] == "plan"


def test_service_limit_stricter_shown_in_timeline(make_service):
    svc = make_service(start=DAWN8 - 3600, sun_limit_deg=-8)
    assert svc.cmd_start(_plan(sun=-2, count=1))["ok"]
    tl = timeline(svc)
    assert tl["sun_max_alt"] == -8 and tl["sun_limit_by"] == "night limit"
    assert abs(tl["sun_limit"] - DAWN8) < 60
    p = svc.status()["plan"]
    assert abs(p["sun_limit_unix"] - DAWN8) < 60
    assert wait_for(svc.idle, 60)


def test_guard_stops_a_plan_waiting_for_the_roof(make_service):
    """The limit passes while the plan waits (here: a roof that never opens): stopped, never unparked."""
    svc = make_service(start=DAWN8 - 120, world={"roof_open": False}, roof_timeout_s=3600)
    svc.rig.sim.dome.stuck = True
    assert wait_for(lambda: svc.status()["safety"]["safe"] is True, 20)
    assert svc.cmd_start(_plan(sun=-2, open_=True))["ok"]
    assert wait_for(lambda: svc.status()["phase"] == "roof open", 20)
    assert wait_for(lambda: svc.status()["state"] in ("done", "stopped") and svc.idle(), 60)
    assert any("night guard" in line for line in svc.status()["log"])
    assert svc.rig.sim.telescope.AtPark and svc.rig.sim.camera.exposures == 0


def test_idle_scope_parked_when_the_limit_passes(make_service):
    """A plan with park_at_end = false finished before dawn and left the scope out: parked at the limit."""
    svc = make_service(start=DAWN8 - 900)
    assert svc.cmd_start(_plan(sun=-2, park=False, count=1, exposure=30))["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done" and svc.idle(), 30)
    assert not svc.rig.sim.telescope.AtPark
    assert wait_for(lambda: svc.rig.sim.telescope.AtPark and svc.idle(), 60)
    assert svc.clock.time() > DAWN8
    assert any("night limit reached with the scope out" in line for line in svc.status()["log"])


def test_status_shows_the_limit(make_service):
    svc = make_service(sun_limit_deg=-8, latest_end="05:00", timezone="UTC")
    nl = svc.status()["night_limit"]
    end = dt.datetime(2026, 11, 9, 5, 0, tzinfo=dt.timezone.utc).timestamp()
    assert nl["sun_limit_deg"] == -8 and nl["latest_end"] == "05:00" and not nl["reached"]
    assert nl["at_unix"] == pytest.approx(min(end, DAWN8), abs=60)


# ---- heartbeat -----------------------------------------------------------------------------------

class Receiver:
    """Local HTTP server that records heartbeats; code = HTTP status to answer."""

    def __init__(self, code=200):
        self.code, self.got = code, []
        rx = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self, body):
                rx.got.append((self.command, self.path, body))
                self.send_response(rx.code)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"OK")

            def do_GET(self):
                self._answer(None)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self._answer(json.loads(self.rfile.read(n)))

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/beat/check-id"


@pytest.fixture
def receiver():
    made = []

    def _make(code=200):
        r = Receiver(code)
        made.append(r)
        return r
    yield _make
    for r in made:
        r.srv.shutdown()


def test_heartbeat_payloads(make_service, receiver):
    rx = receiver()
    svc = make_service(start=DAWN8 - 600, heartbeat_url=rx.url, heartbeat_interval_s=30, heartbeat_id="test-seq")
    assert svc.cmd_start(_plan(sun=-2))["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done" and svc.idle(), 60)
    assert wait_for(lambda: any(b and b["event"] == "final" for _, _, b in rx.got), 10)
    beats = [b for m, path, b in rx.got]
    assert all(m == "POST" and path == "/beat/check-id" for m, path, _ in rx.got)
    running = [b for b in beats if b["event"] == "beat" and b["state"] == "running"]
    assert len(running) >= 5
    b = running[1]
    for k in ("service", "event", "seq", "time", "time_unix", "state", "busy", "plan", "target", "mount",
              "roof_wish", "night_limit", "expected_end_unix", "interval_s"):
        assert k in b, k
    assert b["service"] == "test-seq" and b["busy"] == "run" and b["plan"] == "dawn" and b["target"] == "t"
    assert b["roof_wish"] == "open" and b["mount"]["at_park"] is False
    assert b["expected_end_unix"] == pytest.approx(DAWN8, abs=120)
    assert b["night_limit"]["sun_limit_deg"] == -8
    gaps = [y["time_unix"] - x["time_unix"] for x, y in zip(running, running[1:])]
    assert min(gaps) >= 25
    fin = [x for x in beats if x["event"] == "final"][-1]
    assert fin["state"] == "done" and fin["busy"] is None and fin["mount"]["at_park"] is True
    assert fin["roof_wish"] == "closed" and fin["expected_end_unix"] is None
    assert [x["seq"] for x in beats] == sorted(x["seq"] for x in beats)
    hb = svc.status()["heartbeat"]
    assert hb["configured"] and hb["failed"] == 0 and hb["sent"] == len(beats) and "url" not in hb


def test_heartbeat_get(make_service, receiver):
    rx = receiver()
    svc = make_service(start=NIGHT, heartbeat_url=rx.url, heartbeat_method="GET", heartbeat_interval_s=30)
    assert svc.cmd_start(_plan(sun=-2, count=2, t=NIGHT))["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done" and svc.idle(), 60)
    assert wait_for(lambda: len(rx.got) >= 2, 10)
    assert all(m == "GET" and path == "/beat/check-id" and body is None for m, path, body in rx.got)


def test_heartbeat_idle_sends_nothing(make_service, receiver):
    rx = receiver()
    svc = make_service(heartbeat_url=rx.url, heartbeat_interval_s=10)
    assert wait_for(lambda: svc.clock.time() > NIGHT + 600, 10)
    assert rx.got == []
    svc2 = make_service(heartbeat_url=rx.url, heartbeat_interval_s=10, heartbeat_while="always")
    assert wait_for(lambda: len(rx.got) >= 3, 10)
    assert rx.got[0][2]["state"] == "idle" and rx.got[0][2]["roof_wish"] == "closed"
    svc2.shutdown()


@pytest.mark.parametrize("dead", ["http500", "refused"])
def test_heartbeat_failures_never_stop_imaging(make_service, receiver, dead):
    if dead == "http500":
        url = receiver(500).url
    else:
        import socket
        sk = socket.socket()
        sk.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{sk.getsockname()[1]}/beat"     # nothing listens: connection refused
        sk.close()
    svc = make_service(start=DAWN8 - 600, heartbeat_url=url, heartbeat_interval_s=20)
    assert svc.cmd_start(_plan(sun=-2))["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done" and svc.idle(), 60)
    st = svc.status()
    assert st["plan"]["steps"][0]["frames"] > 3 and svc.rig.sim.telescope.AtPark
    hb = st["heartbeat"]
    assert hb["failed"] >= 5 and hb["sent"] == 0 and hb["last_error"]
    assert sum("heartbeat failed" in line for line in st["log"]) == 1     # rate-limited
