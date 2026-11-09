"""Heartbeat hook: tells an outside watcher (a roof controller, healthchecks.io, ...) that the sequencer
is alive and what it is doing. Configured in [hooks]: heartbeat_url, heartbeat_interval_s (60),
heartbeat_method (POST with a JSON body, or GET: the URL only), heartbeat_while (plan | job | always).

While active it sends a beat every interval; when a plan or park job ends it sends one final beat
(event "final") with the resulting state. Sending runs in its own thread with a short timeout, so a dead
receiver never delays imaging; failures are logged at most every 10 minutes. The payload is documented
in docs/design.md (Heartbeat).
"""
import datetime as dt
import json
import threading
import time
import urllib.request

LOG_EVERY_S = 600.0
TIMEOUT_S = 5.0


def _iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def payload(svc, event="beat", seq=0):
    """The heartbeat JSON (a dict) from the service's current status."""
    s = svc.status(log=False, darklib=False)
    p, nl, m = s.get("plan") or {}, s.get("night_limit") or {}, s.get("mount") or {}
    imaging = svc.busy == "run" and s.get("state") not in ("stopping", "parking")
    ends = [t for t in (nl.get("at_unix"), p.get("eta_unix")) if t]
    try:
        at_park = bool(svc.scope.t.AtPark)
    except Exception:  # noqa: BLE001
        at_park = m.get("at_park")
    return {
        "service": svc.s.heartbeat_id, "kind": "seestar-commander", "version": 1,
        "event": event, "seq": seq,
        "time": s["time"], "time_unix": s["time_unix"],
        "state": s.get("state"), "busy": svc.busy, "headline": s.get("headline"),
        "plan": p.get("name"), "target": s.get("target") if svc.busy == "run" else None,
        "mount": {"at_park": at_park, "tracking": m.get("tracking")},
        "roof_wish": "open" if imaging else "closed",
        "safe": (s.get("safety") or {}).get("safe"),
        "night_limit": {"sun_limit_deg": nl.get("sun_limit_deg"), "latest_end": nl.get("latest_end"),
                        "reached": nl.get("reached"), "at_unix": nl.get("at_unix")},
        "plan_eta_unix": p.get("eta_unix"),
        "expected_end_unix": min(ends) if ends and imaging else None,
        "expected_end": _iso(min(ends)) if ends and imaging else None,
        "interval_s": svc.s.heartbeat_interval_s, "sim": bool(s.get("sim")),
    }


def send(url, method, body, timeout=TIMEOUT_S):
    """One request; raises on a network error or a non-2xx answer."""
    if method == "GET":
        req = urllib.request.Request(url, method="GET")
    else:
        req = urllib.request.Request(url, data=json.dumps(body, default=str).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()
        if not 200 <= r.status < 300:
            raise RuntimeError(f"HTTP {r.status}")


class Heartbeat:
    def __init__(self, svc):
        self.svc, self.s = svc, svc.s
        self.url = (self.s.heartbeat_url or "").strip()
        self.method = "GET" if str(self.s.heartbeat_method).upper() == "GET" else "POST"
        self.interval = max(1.0, float(self.s.heartbeat_interval_s))
        self.wake = threading.Event()
        self._final = 0
        self._lock = threading.Lock()
        self.seq = 0
        self.stats = {"sent": 0, "failed": 0, "last_ok": None, "last_error": None, "last_event": None}
        self._failing_since = None
        self._logged_fail = 0.0
        self.thread = None

    def view(self):
        """For status(): no URL (it can hold a token, e.g. a healthchecks.io check id)."""
        with self._lock:
            return {"configured": bool(self.url), "method": self.method, "interval_s": self.interval,
                    "while": self.s.heartbeat_while, **self.stats}

    def start(self):
        if not self.url:
            return
        self.thread = threading.Thread(target=self._loop, name="heartbeat", daemon=True)
        self.thread.start()
        self.svc.log(f"heartbeat: {self.method} every {self.interval:g} s while {self.s.heartbeat_while}")

    def final(self):
        """A plan or park ended: send one beat with the resulting state (from the heartbeat thread)."""
        if self.url:
            with self._lock:
                self._final += 1
            self.wake.set()

    def kick(self):
        """A job started: re-check right away (the first beat of a plan goes out at once)."""
        if self.url:
            self.wake.set()

    def _active(self):
        w = self.s.heartbeat_while
        busy = self.svc.busy
        return w == "always" or (w == "job" and busy is not None) or busy == "run"

    def _loop(self):
        svc, last, was = self.svc, None, False
        while not svc.halt.is_set():
            with self._lock:
                final, self._final = self._final > 0, 0
            active = self._active()
            now = svc.clock.time()
            due = active and (not was or last is None or now - last >= self.interval)
            if final or due:
                self.beat("final" if final and not active else "beat")
                last = svc.clock.time()
            was = active
            self.wake.clear()
            svc.clock.wait(self.wake, min(self.interval, 5.0) if active else self.interval)

    def beat(self, event="beat"):
        self.seq += 1
        try:
            body = payload(self.svc, event, self.seq)
            send(self.url, self.method, body)
        except Exception as e:  # noqa: BLE001 - never let the receiver affect imaging
            self._failed(e)
            return False
        with self._lock:
            self.stats.update(sent=self.stats["sent"] + 1, last_ok=body["time"], last_event=event)
        if self._failing_since is not None:
            self.svc.log(f"heartbeat: OK again ({self.stats['failed']} failed so far)")
            self._failing_since = None
        return True

    def _failed(self, e):
        now = time.time()
        with self._lock:
            self.stats.update(failed=self.stats["failed"] + 1, last_error=f"{type(e).__name__}: {e}"[:200])
        if self._failing_since is None:
            self._failing_since = now
        if now - self._logged_fail >= LOG_EVERY_S:
            self._logged_fail = now
            self.svc.log(f"heartbeat failed ({e}); imaging continues, next report in "
                         f"{LOG_EVERY_S / 60:.0f} min if it keeps failing")
