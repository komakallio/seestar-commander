"""Optional roof handling: open at plan start (only when safe), close at plan end (only after a verified
park), auto_close_on_unsafe, the `roof open` command, and refusals from Alpaca and HTTP roofs."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sequencer import plan as P
from sequencer.clock import ScaledClock
from sequencer.rig import HttpRoof, RoofRefused

from conftest import NIGHT, ra_at_ha, wait_for


def _plan(open_=False, close=False, park=True, count=1, exposure=30, ha=-20, dec=45):
    return P.loads(f"""
[plan]
park_at_end = {str(park).lower()}
open_roof_at_start = {str(open_).lower()}
close_roof_at_end = {str(close).lower()}
[[step]]
name = "t"
ra = {ra_at_ha(ha)}
dec = {dec}
exposure = {exposure}
count = {count}
centre = "none"
""")


def _safe_known(svc):
    assert wait_for(lambda: svc.status()["safety"]["safe"] is True, 20)


def _log_index(svc, text):
    return next(i for i, line in enumerate(svc.status()["log"]) if text in line)


# ---- plan parsing ------------------------------------------------------------------------------

def test_plan_options_parse():
    pl = _plan(open_=True, close=True)
    assert pl.open_roof_at_start and pl.close_roof_at_end
    pl = P.loads("[[step]]\ntarget = 'M31'\n")
    assert not pl.open_roof_at_start and not pl.close_roof_at_end
    with pytest.raises(P.PlanError, match="park_at_end"):
        _plan(close=True, park=False)


def test_plan_with_roof_options_needs_a_roof(make_service):
    svc = make_service(roof=False)
    r = svc.cmd_start(_plan(open_=True))
    assert not r["ok"] and "no [roof]" in r["msg"]


# ---- open at start -----------------------------------------------------------------------------

def test_opens_roof_at_start_when_safe(make_service):
    svc = make_service(world={"roof_open": False})
    dome = svc.rig.sim.dome
    _safe_known(svc)
    assert dome.ShutterStatus == 1
    assert svc.cmd_start(_plan(open_=True, park=False, count=1))["ok"]
    assert wait_for(lambda: svc.status()["phase"] == "lights", 30)
    assert dome.open_calls == 1 and dome.ShutterStatus == 0
    st = svc.status()
    assert st["plan"]["open_roof_at_start"] is True and st["plan"]["close_roof_at_end"] is False
    assert st["roof"]["last_action"]["ok"] and st["roof"]["last_action"]["result"] == "open"
    assert _log_index(svc, "roof: opening (plan start)") < _log_index(svc, "step 1/1")
    assert wait_for(svc.idle, 60)


def test_open_skipped_when_already_open(make_service):
    svc = make_service()
    _safe_known(svc)
    assert svc.cmd_start(_plan(open_=True, park=False))["ok"]
    assert wait_for(lambda: svc.status()["phase"] == "lights", 30)
    assert svc.rig.sim.dome.open_calls == 0
    svc.cmd_stop()
    assert wait_for(svc.idle, 20)


def test_open_refused_when_unsafe(make_service):
    svc = make_service(world={"roof_open": False})
    svc.rig.sim.safety.set(False, ["RAIN"])
    assert wait_for(lambda: svc.status()["safety"]["safe"] is False and svc.idle(), 20)
    r = svc.cmd_roof_open()
    assert not r["ok"] and "unsafe" in r["msg"] and "RAIN" in r["msg"]
    assert not svc.cmd_start(_plan(open_=True))["ok"]
    assert svc.rig.sim.dome.open_calls == 0


def test_unsafe_while_opening_stops_the_plan(make_service):
    svc = make_service(world={"roof_open": False})
    dome = svc.rig.sim.dome
    dome.stuck = True                                # accepts OpenShutter, never finishes
    _safe_known(svc)
    assert svc.cmd_start(_plan(open_=True, close=True))["ok"]
    assert wait_for(lambda: svc.status()["phase"] == "roof open", 20)
    svc.rig.sim.safety.set(False, ["WIND"])
    assert wait_for(lambda: svc.status()["state"] == "parked" and svc.idle(), 60)
    st = svc.status()
    assert st["plan"]["steps"][0]["status"] == "pending"          # never started imaging
    assert st["roof"]["close_requested"] and "WIND" in st["roof"]["reason"]
    assert "interrupted" in st["roof"]["last_action"]["result"]
    assert dome.close_calls == 0


def test_open_timeout_stops_the_plan(make_service):
    svc = make_service(world={"roof_open": False}, roof_timeout_s=60)
    svc.rig.sim.dome.stuck = True
    _safe_known(svc)
    assert svc.cmd_start(_plan(open_=True))["ok"]
    assert wait_for(lambda: svc.status()["state"] == "stopped" and svc.idle(), 30)
    st = svc.status()
    assert st["plan"]["steps"][0]["status"] == "pending"
    assert "not open after 60 s" in st["roof"]["last_action"]["result"]
    assert svc.rig.sim.telescope.AtPark                              # never unparked


def test_alpaca_refusal_stops_the_plan(make_service):
    svc = make_service(world={"roof_open": False})
    svc.rig.sim.dome.refuse = "shutter interlock engaged"
    _safe_known(svc)
    assert svc.cmd_start(_plan(open_=True))["ok"]
    assert wait_for(lambda: svc.status()["state"] == "stopped" and svc.idle(), 30)
    la = svc.status()["roof"]["last_action"]
    assert not la["ok"] and "shutter interlock engaged" in la["result"]
    assert svc.rig.sim.telescope.AtPark


def test_roof_open_command(make_service):
    svc = make_service(world={"roof_open": False})
    _safe_known(svc)
    assert svc.cmd_roof_open()["ok"]
    assert wait_for(lambda: svc.idle() and svc.rig.sim.dome.ShutterStatus == 0, 30)
    assert svc.status()["roof"]["last_action"]["action"] == "open (operator)"
    assert not make_service(roof=False).cmd_roof_open()["ok"]


# ---- close at end ------------------------------------------------------------------------------

def test_closes_at_end_after_verified_park(make_service):
    svc = make_service()
    dome = svc.rig.sim.dome
    _safe_known(svc)
    assert svc.cmd_start(_plan(close=True, count=2))["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done" and svc.idle(), 90)
    assert svc.rig.sim.telescope.AtPark and svc.rig.sim.telescope.firmware_gotos == 0
    assert dome.close_calls == 1 and dome.ShutterStatus == 1
    assert _log_index(svc, "parked:") < _log_index(svc, "roof: closing (close_roof_at_end)")
    st = svc.status()
    assert st["roof"]["last_action"]["ok"] and st["roof"]["state"] == "closed"
    assert st["plan"]["steps"][0]["frames"] == 2


def test_no_close_when_park_failed(make_service):
    svc = make_service()
    svc.scope.park_guarded = lambda **k: {"parked": False, "aborted": True, "max_dev": 12.0}
    _safe_known(svc)
    assert svc.cmd_start(_plan(close=True))["ok"]
    assert wait_for(lambda: svc.idle() and svc.status()["roof"]["last_action"] is not None, 90)
    st = svc.status()
    assert svc.rig.sim.dome.close_calls == 0
    assert st["state"] == "error"
    assert st["roof"]["close_requested"] and "park not verified" in st["roof"]["reason"]
    assert "skipped" in st["roof"]["last_action"]["result"]


def test_no_close_when_mount_not_at_park(make_service):
    svc = make_service()
    svc.rig.sim.telescope.Unpark()
    svc.scope.park_guarded = lambda **k: {"parked": True, "aborted": False, "max_dev": 0.0}  # lies
    _safe_known(svc)
    assert svc.cmd_start(_plan(close=True, ha=150, dec=-60))["ok"]     # skipped: not observable
    assert wait_for(lambda: svc.idle() and svc.status()["roof"]["last_action"] is not None, 30)
    assert svc.rig.sim.dome.close_calls == 0
    assert "AtPark" in svc.status()["roof"]["reason"]


def test_no_close_when_plan_aborted(make_service):
    svc = make_service(speed=100)
    _safe_known(svc)
    assert svc.cmd_start(_plan(close=True, count=30, exposure=120))["ok"]
    assert wait_for(lambda: svc.status()["phase"] == "lights", 30)
    svc.cmd_stop()
    assert wait_for(lambda: svc.status()["state"] == "stopped" and svc.idle(), 20)
    assert svc.rig.sim.dome.close_calls == 0
    assert not svc.rig.sim.telescope.AtPark


# ---- unsafe mid-plan ---------------------------------------------------------------------------

@pytest.mark.parametrize("auto", [False, True])
def test_auto_close_on_unsafe(make_service, auto):
    svc = make_service(speed=100, auto_close_on_unsafe=auto)
    dome = svc.rig.sim.dome
    _safe_known(svc)
    assert svc.status()["roof"]["auto_close_on_unsafe"] is auto
    assert svc.cmd_start(_plan(park=False, count=30, exposure=120))["ok"]
    assert wait_for(lambda: svc.status()["phase"] == "lights" and svc.imager.exposing, 30)
    svc.rig.sim.safety.set(False, ["RAIN"])
    assert wait_for(lambda: svc.status()["state"] == "parked" and svc.idle(), 60)
    assert svc.rig.sim.telescope.AtPark
    st = svc.status()
    if auto:
        assert dome.close_calls == 1 and dome.ShutterStatus == 1
        assert not st["roof"]["close_requested"]
        assert _log_index(svc, "parked:") < _log_index(svc, "roof: closing (auto_close_on_unsafe)")
    else:
        assert dome.close_calls == 0 and dome.ShutterStatus == 0
        assert st["roof"]["close_requested"]


def test_auto_close_skipped_when_unsafe_park_fails(make_service):
    svc = make_service(speed=100, auto_close_on_unsafe=True)
    svc.rig.sim.telescope.Unpark()
    svc.scope.park_guarded = lambda **k: {"parked": False, "aborted": True, "max_dev": 15.0}
    svc.rig.sim.safety.set(False, ["RAIN"])
    assert wait_for(lambda: svc.idle() and svc.status()["roof"]["last_action"] is not None, 30)
    assert svc.rig.sim.dome.close_calls == 0
    assert svc.status()["roof"]["close_requested"]


# ---- HTTP roof ---------------------------------------------------------------------------------

class FakeHttpRoof:
    """open_url / close_url / state_url on a local server. refuse = REASON answers like a controller
    that says no: HTTP 409 with {"message": REASON, "error": true}."""

    def __init__(self, open_=False):
        self.open, self.refuse, self.state_error, self.posts = open_, None, None, []
        roof = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, body):
                b = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if roof.state_error:
                    return self._json(500, {"message": roof.state_error, "error": True})
                self._json(200, {"open": roof.open})

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                roof.posts.append(self.path)
                if roof.refuse:
                    return self._json(409, {"message": roof.refuse, "error": True})
                roof.open = self.path == "/open"
                self._json(200, {"ok": True})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def client(self, clock):
        return HttpRoof(self.base + "/close", self.base + "/state", clock, open_url=self.base + "/open")


@pytest.fixture
def http_roof():
    r = FakeHttpRoof()
    yield r
    r.srv.shutdown()


def test_http_roof_open_close(http_roof):
    roof = http_roof.client(ScaledClock(200, NIGHT))
    assert roof.state() == "closed"
    assert roof.open(timeout=60, log=lambda m: None) == "open"
    assert roof.close(timeout=60, log=lambda m: None) == "closed"
    assert http_roof.posts == ["/open", "/close"]


def test_http_roof_refusal_reason(http_roof):
    roof = http_roof.client(ScaledClock(200, NIGHT))
    http_roof.refuse = "manual mode, remote control disabled"
    with pytest.raises(RoofRefused, match="manual mode, remote control disabled"):
        roof.open(log=lambda m: None)
    with pytest.raises(RoofRefused, match="manual mode"):
        roof.close(log=lambda m: None)
    http_roof.refuse, http_roof.state_error = None, "sensor fault"
    assert roof.state() == "error: sensor fault"


def test_http_roof_refusal_surfaced_by_service(make_service, http_roof):
    svc = make_service()
    svc.rig.roof = http_roof.client(svc.clock)
    _safe_known(svc)
    http_roof.refuse = "maintenance lock"
    assert svc.cmd_roof_open()["ok"]
    assert wait_for(lambda: svc.idle() and svc.status()["roof"]["last_action"] is not None, 20)
    la = svc.status()["roof"]["last_action"]
    assert not la["ok"] and "maintenance lock" in la["result"]
    assert any("maintenance lock" in line for line in svc.status()["log"])

    # as part of a plan: the plan stops before the first target with the reason
    assert svc.cmd_start(_plan(open_=True, close=True))["ok"]
    assert wait_for(lambda: svc.status()["state"] == "stopped" and svc.idle(), 20)
    assert svc.status()["plan"]["steps"][0]["status"] == "pending"
    assert svc.rig.sim.telescope.AtPark

    # cleared: resume opens, images, parks and closes over HTTP
    http_roof.refuse = None
    svc.cmd_resume()
    assert wait_for(lambda: svc.status()["state"] == "done" and svc.idle(), 90)
    assert http_roof.open is False and http_roof.posts[-1] == "/close"
