import json
import urllib.error
import urllib.request

from sequencer.httpapi import serve

from conftest import ra_at_ha, wait_for


def _req(base, method, path, body=None, token=None):
    data = json.dumps(body or {}).encode() if method == "POST" else None
    r = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _base(svc):
    srv = serve(svc, host="127.0.0.1", port=0)
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_api_roundtrip(make_service):
    svc = make_service()
    srv, base = _base(svc)
    try:
        code, page = _req(base, "GET", "/")
        assert code == 200 and b"Dark library" in page
        code, body = _req(base, "GET", "/api/status")
        st = json.loads(body)
        assert code == 200 and st["sim"] and st["state"] == "idle" and "darklib" in st
        code, body = _req(base, "POST", "/api/plan", {"text": "[[step]]\nfilter = 'Dark'\ntarget='M31'\n"})
        assert code == 400 and b"Dark" in body
        plan = f'[plan]\npark_at_end=false\n[[step]]\nra={ra_at_ha(-20)}\ndec=45\nexposure=60\ncount=20\ncentre="none"\n'
        code, body = _req(base, "POST", "/api/plan", {"text": plan})
        assert code == 200 and json.loads(body)["ok"]
        assert wait_for(lambda: svc.status()["phase"] == "lights", 30)
        code, body = _req(base, "POST", "/api/stop")
        assert json.loads(body)["ok"]
        assert wait_for(lambda: svc.status()["state"] == "stopped", 20)
        code, body = _req(base, "POST", "/api/sim/safety", {"safe": False, "reasons": ["TEST"]})
        assert code == 200
        assert wait_for(lambda: svc.status()["state"] == "parked" and svc.idle(), 60)
        code, body = _req(base, "GET", "/api/safety")
        assert json.loads(body)["safe"] is False
        code, body = _req(base, "POST", "/api/roof/close")
        assert json.loads(body)["ok"]
        code, body = _req(base, "GET", "/api/darks")
        assert code == 200 and "entries" in json.loads(body)
        assert _req(base, "GET", "/img/latest.jpg")[0] in (200, 404)
    finally:
        srv.shutdown()


def test_token_required_for_commands(make_service):
    svc = make_service(api_token="s3cret")
    srv, base = _base(svc)
    try:
        assert _req(base, "GET", "/api/status")[0] == 200
        assert _req(base, "POST", "/api/park")[0] == 401
        assert _req(base, "POST", "/api/park", token="s3cret")[0] == 200
    finally:
        srv.shutdown()
