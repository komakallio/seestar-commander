"""Status page v2: headline text, the Server-Sent Events stream and the night timeline."""
import json
import re
import time
import urllib.request

from sequencer.httpapi import serve
from sequencer.statusview import headline

from conftest import NIGHT, ra_at_ha, wait_for


def _s(state="idle", action=None, busy=None, safe=True, reasons=(), now=NIGHT):
    return {"state": state, "busy": busy, "action": action, "time_unix": now,
            "safety": {"safe": safe, "reasons": list(reasons)}}


def test_headline_main_states():
    t = NIGHT
    assert headline(_s()) == "Idle"
    assert headline(_s("done")) == "Done"
    assert headline(_s("running", {"kind": "slew", "target": "M45"}, "run")) == "Slewing to M45"
    assert headline(_s("running", {"kind": "centre", "target": "M45", "solve": 1, "of": 4,
                                   "off_arcmin": None}, "run")) == "Centring M45 (solve 1/4)"
    assert headline(_s("running", {"kind": "centre", "target": "M45", "solve": 2, "of": 4,
                                   "off_arcmin": 8.4}, "run")) == "Centring M45 (solve 2/4, 8.4′ off)"
    exp = {"kind": "expose", "target": "M45", "frame": 1, "count": 3, "start": t - 37.4, "exposure": 60.0}
    assert headline(_s("running", exp, "run")) == "M45 exposure 1/3 (37 / 60 s)"
    assert headline(_s("running", dict(exp, start=t - 61), "run")) == "Downloading M45 1/3"
    assert headline(_s("running", {"kind": "download", "target": "M45", "frame": 1, "count": 3},
                       "run")) == "Downloading M45 1/3"
    assert headline(_s("parking", {"kind": "park"}, "park")) == "Parking"
    assert headline(_s("parked", {"kind": "roof_close"}, "roof_close")) == "Closing roof"
    assert headline(_s("parked", safe=False, reasons=["RAIN"])) == "Waiting: unsafe (RAIN)"
    assert headline(_s("stopping", exp, "run")) == "Stopping"


def _plan(count=3, exposure=60):
    return (f'[plan]\nname="t"\npark_at_end=false\n[[step]]\nname="M45"\nra={ra_at_ha(-20)}\ndec=45\n'
            f'exposure={exposure}\ncount={count}\ncentre="none"\n')


def test_headline_follows_the_worker(make_service):
    svc = make_service(speed=50)
    from sequencer import plan as planmod
    assert svc.cmd_start(planmod.loads(_plan(), "toml"))["ok"]
    assert wait_for(lambda: (svc.status()["action"] or {}).get("kind") == "expose", 30)
    st = svc.status()
    assert re.fullmatch(r"(M45 exposure 1/3 \(\d+ / 60 s\)|Downloading M45 1/3)", st["headline"]), st["headline"]
    assert st["plan"]["eta_unix"] > st["time_unix"]
    svc.cmd_stop()
    assert wait_for(lambda: svc.idle() and svc.status()["headline"] == "Stopped", 20)


def _events(base, timeout=20):
    """Yield (event, data) from /api/events."""
    r = urllib.request.urlopen(base + "/api/events", timeout=timeout)
    assert r.headers["Content-Type"].startswith("text/event-stream")
    ev = None
    for raw in r:
        line = raw.decode().rstrip("\n")
        if line.startswith("event: "):
            ev = line[7:]
        elif line.startswith("data: "):
            yield ev, json.loads(line[6:])


def test_sse_pushes_state_changes(make_service):
    svc = make_service(speed=50)
    srv = serve(svc, host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        stream = _events(base)
        ev, first = next(e for e in stream if e[0] == "status")
        assert first["state"] == "idle" and first["headline"] == "Idle" and "darklib" in first
        req = urllib.request.Request(base + "/api/plan", data=json.dumps({"text": _plan()}).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        assert json.loads(urllib.request.urlopen(req, timeout=10).read())["ok"]
        t0, seen, logs = time.time(), set(), []
        for ev, d in stream:
            if ev == "status":
                seen.add((d.get("action") or {}).get("kind"))
            elif ev == "log":
                logs += d["lines"]
            if "expose" in seen or time.time() - t0 > 20:
                break
        assert "expose" in seen and "slew" in seen
        assert any("plan 't' loaded" in x for x in logs)
        # the timeline covers the plan's target for the rest of the night
        tl = json.loads(urllib.request.urlopen(base + "/api/timeline", timeout=10).read())
        assert tl["targets"][0]["name"] == "M45" and len(tl["t"]) == len(tl["targets"][0]["alt"]) > 10
        assert tl["start"] <= tl["now"] < tl["end"] and tl["sun_limit"] > tl["now"]
    finally:
        svc.cmd_stop()
        srv.shutdown()
