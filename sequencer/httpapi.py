"""JSON control API and the status page, on the standard library's threading HTTP server.

GET  /                      status page (web/sequencer.html)
GET  /api/status            everything the page shows
GET  /api/safety            last safety poll
GET  /img/latest.jpg        latest frame preview;  /img/stack.jpg  running stack
POST /api/plan              {"text": "...", "format": "toml"|"yaml", "base_dir": "..."} or {"path": "..."}
POST /api/stop | /api/resume | /api/park | /api/roof/close
GET  /api/darks             dark library: entries, status, missing for the active/planned plans
POST /api/darks/build       {"exposure": 60, "gain": 80, "count": 15} | {"plan_path": "..."} | {"missing": true}
POST /api/sim/safety        {"safe": false, "reasons": ["RAIN"]}   (simulator only)
POST /api/sim/roof/open                                            (simulator only)

If [sequencer] api_token is set, POSTs need "Authorization: Bearer <token>" (or ?token=).
"""
import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import plan as planmod

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


def make_handler(svc):
    token = svc.s.api_token

    class H(BaseHTTPRequestHandler):
        server_version = "seestar-commander/1"

        def log_message(self, fmt, *args):  # quiet
            pass

        def _send(self, code, body, ctype="application/json"):
            if not isinstance(body, (bytes, bytearray)):
                body = json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path, ctype):
            if not os.path.exists(path):
                return self._send(404, {"error": "not found"})
            with open(path, "rb") as fh:
                self._send(200, fh.read(), ctype)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path in ("/", "/index.html"):
                return self._file(os.path.join(WEB, "sequencer.html"), "text/html; charset=utf-8")
            if u.path == "/api/status":
                return self._send(200, svc.status())
            if u.path == "/api/safety":
                return self._send(200, svc.cmd_safety())
            if u.path == "/api/darks":
                return self._send(200, svc.darklib.summary(svc.dark_keys(), svc.sensor_temp))
            if u.path in ("/img/latest.jpg", "/img/stack.jpg"):
                return self._file(os.path.join(svc.out, os.path.basename(u.path)), "image/jpeg")
            return self._send(404, {"error": "not found"})

        def _authorised(self, u):
            if not token:
                return True
            q = urllib.parse.parse_qs(u.query)
            return self.headers.get("Authorization", "") == f"Bearer {token}" or q.get("token", [""])[0] == token

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            if not self._authorised(u):
                return self._send(401, {"ok": False, "msg": "token required"})
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                return self._send(400, {"ok": False, "msg": "body must be JSON"})
            p = u.path.rstrip("/")
            try:
                if p == "/api/plan":
                    if body.get("path"):
                        pl = planmod.load(body["path"])
                    else:
                        pl = planmod.loads(body.get("text", ""), body.get("format", "toml"),
                                           base_dir=body.get("base_dir", "."), source=body.get("source", "api"))
                    return self._send(200, svc.cmd_start(pl))
                if p == "/api/stop":
                    return self._send(200, svc.cmd_stop())
                if p == "/api/resume":
                    return self._send(200, svc.cmd_resume())
                if p == "/api/park":
                    return self._send(200, svc.cmd_park())
                if p == "/api/roof/close":
                    return self._send(200, svc.cmd_roof_close())
                if p == "/api/darks/build":
                    pl = planmod.load(body["plan_path"]) if body.get("plan_path") else None
                    return self._send(200, svc.cmd_darks_build(body.get("exposure"), body.get("gain"),
                                                               body.get("count"), pl, bool(body.get("missing"))))
                if p.startswith("/api/sim/"):
                    sim = svc.rig.sim
                    if sim is None:
                        return self._send(404, {"ok": False, "msg": "not a simulator"})
                    if p == "/api/sim/safety":
                        sim.safety.set(body.get("safe", True), body.get("reasons", []))
                        svc.log(f"SIM: safety source set to {'safe' if body.get('safe', True) else 'UNSAFE'} "
                                f"{body.get('reasons', [])}")
                        return self._send(200, {"ok": True})
                    if p == "/api/sim/roof/open" and sim.dome:
                        sim.dome.OpenShutter()
                        svc.log("SIM: roof opening")
                        return self._send(200, {"ok": True})
            except planmod.PlanError as e:
                return self._send(400, {"ok": False, "msg": f"plan: {e}"})
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"ok": False, "msg": str(e)})
            return self._send(404, {"ok": False, "msg": "unknown command"})

    return H


def serve(svc, host=None, port=None):
    """Start the HTTP server in a daemon thread; returns the server (server.server_address)."""
    srv = ThreadingHTTPServer((host or svc.s.http_host, svc.s.http_port if port is None else port),
                              make_handler(svc))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="http", daemon=True).start()
    return srv
