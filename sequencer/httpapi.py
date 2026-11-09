"""JSON control API and the status page, on the standard library's threading HTTP server.

GET  /                      status page (web/sequencer.html)
GET  /api/status            everything the page shows (also the fallback when the event stream drops)
GET  /api/events            Server-Sent Events: `status` (status without log/darklib-refresh) on every
                            change, `log` (new lines), `ping` every 5 s when nothing changes
GET  /api/timeline          altitude curves of the plan targets for the rest of the night
GET  /api/safety            last safety poll
GET  /img/latest.jpg        latest frame preview;  /img/stack.jpg  running stack;  /img/wide.jpg  last wide frame
POST /api/plan              {"text": "...", "format": "toml"|"yaml", "base_dir": "..."} or {"path": "..."}
POST /api/stop | /api/resume | /api/park | /api/roof/close | /api/roof/open (refused while unsafe)
GET  /api/darks             dark library: entries, status, missing for the active/planned plans
POST /api/darks/build       {"exposure": 60, "gain": 80, "count": 15} | {"plan_path": "..."} | {"missing": true}
GET  /api/flats             flat library: entries, status, missing for the active/planned plans
POST /api/flats/build       {"filters": ["IR", "LP"], "gain": 80, "count": 15} | {"plan_path": "..."} |
                            {"missing": true}; "wide": true adds the wide camera
POST /api/flats/confirm     manual light source: the panel is on, go on
POST /api/sim/panel         {"on": true, "brightness": 255}                        (simulator only)
POST /api/sim/safety        {"safe": false, "reasons": ["RAIN"]}   (simulator only)
POST /api/sim/roof/open                                            (simulator only)
GET  /polar                 polar alignment page (web/polar.html);  GET /api/polar  its state
POST /api/polar/start       {"dec": 65, "span_deg": 30, ...} | /api/polar/stop | /api/polar/done
POST /api/sim/polar         {"dalt": -10, "daz": 5} or {"alt": .., "az": ..} arcmin  (simulator: the knobs)
GET  /api/lock              exclusive-use lock state (also in /api/status as "lock")
POST /api/lock              {"owner": "sattrack", "reason": "...", "ttl_s": 120}: take or renew (same owner);
                            "renew": true only renews (fails once released, revoked or expired)
POST /api/unlock            {"owner": "sattrack"} or {"force": true}

If [sequencer] api_token is set, POSTs need "Authorization: Bearer <token>" (or ?token=).
"""
import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import plan as planmod
from . import statusview

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
            if u.path == "/api/events":
                return self._events()
            if u.path == "/api/timeline":
                return self._send(200, statusview.timeline(svc))
            if u.path == "/api/safety":
                return self._send(200, svc.cmd_safety())
            if u.path == "/api/darks":
                return self._send(200, svc.darklib.summary(svc.dark_keys(), svc.sensor_temp))
            if u.path == "/api/flats":
                return self._send(200, svc.flats.summary())
            if u.path in ("/polar", "/polar.html"):
                return self._file(os.path.join(WEB, "polar.html"), "text/html; charset=utf-8")
            if u.path == "/api/polar":
                return self._send(200, svc.polar.status())
            if u.path == "/api/lock":
                return self._send(200, svc.lock_view())
            if u.path == "/img/polar_crop.jpg":
                return self._file(os.path.join(svc.out, "polar_crop.jpg"), "image/jpeg")
            if u.path in ("/img/latest.jpg", "/img/stack.jpg", "/img/wide.jpg"):
                return self._file(os.path.join(svc.out, os.path.basename(u.path)), "image/jpeg")
            return self._send(404, {"error": "not found"})

        def _sse(self, event, data, eid=None):
            msg = (f"id: {eid}\n" if eid is not None else "") + f"event: {event}\n" + \
                "data: " + json.dumps(data, default=str) + "\n\n"
            self.wfile.write(msg.encode())

        def _events(self, heartbeat_s=5.0, coalesce_s=0.1, darklib_s=10.0):
            """Push the state on every change (coalesced to ~10/s). Ends when the client goes away."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")       # don't buffer behind a proxy
            self.end_headers()
            seq, log_sent, dl, fl, dl_t = None, None, None, None, 0.0
            try:
                self.wfile.write(b"retry: 2000\n\n")
                while not svc.halt.is_set():
                    with svc.changed:
                        if seq == svc.change_seq:
                            svc.changed.wait(heartbeat_s)
                        changed = seq != svc.change_seq
                    if not changed:
                        self._sse("ping", {"time_unix": round(svc.clock.time(), 2)})
                        self.wfile.flush()
                        continue
                    if seq is not None:
                        time.sleep(coalesce_s)
                    seq = svc.change_seq
                    st = svc.status(log=False, darklib=False)
                    if dl is None or time.time() - dl_t > darklib_s:
                        try:
                            dl = svc.darklib.summary(svc.dark_keys(), svc.sensor_temp)
                        except Exception as e:  # noqa: BLE001
                            dl = {"error": str(e)}
                        fl = svc.flats.summary()
                        dl_t = time.time()
                    st["darklib"], st["flatlib"] = dl, fl
                    with svc.lock:
                        n, lines = svc.log_n, list(svc.st["log"])
                    if log_sent is None or n - log_sent > len(lines):
                        self._sse("log", {"reset": True, "lines": lines, "n": n})
                    elif n > log_sent:
                        self._sse("log", {"reset": False, "lines": lines[len(lines) - (n - log_sent):], "n": n})
                    log_sent = n
                    self._sse("status", st, eid=seq)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            self.close_connection = True

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
                if p == "/api/lock":
                    return self._send(200, svc.cmd_lock(body.get("owner"), body.get("reason", ""),
                                                        body.get("ttl_s", 300), bool(body.get("renew"))))
                if p == "/api/unlock":
                    return self._send(200, svc.cmd_unlock(body.get("owner"), bool(body.get("force"))))
                if p == "/api/roof/close":
                    return self._send(200, svc.cmd_roof_close())
                if p == "/api/roof/open":
                    return self._send(200, svc.cmd_roof_open())
                if p == "/api/darks/build":
                    pl = planmod.load(body["plan_path"]) if body.get("plan_path") else None
                    return self._send(200, svc.cmd_darks_build(body.get("exposure"), body.get("gain"),
                                                               body.get("count"), pl, bool(body.get("missing"))))
                if p.startswith("/api/flats/") or p == "/api/sim/panel":
                    return self._send(*svc.flats.http_post(p, body))
                if p.startswith("/api/polar/") or p == "/api/sim/polar":
                    return self._send(*svc.polar.http_post(p, body))
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
