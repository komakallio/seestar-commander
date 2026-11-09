"""Exclusive use of the SeeStar between the sequencer service and scripts.

Two layers, so two programs never command the scope at the same time:

- **API lock** (the service runs): `POST /api/lock {"owner", "reason", "ttl_s"}` on the sequencer. While it
  is held the service refuses plans, park, roof open, darks and polar jobs, and its monitor only reads.
  It expires after ttl_s unless renewed (lock again with the same owner); `POST /api/unlock` releases.
  The service revokes it if safety turns unsafe, then parks.
- **File lock** (fallback, and the service's own): an exclusive `fcntl.flock` on `<data_dir>/scope.lock`.
  The service holds it while it runs a job or while an API lock is granted, so a script that couldn't
  reach the service still can't start moving while the service does. The kernel drops it when the
  holder dies. The file's content names the holder (owner, reason, pid) for messages.

Use from a script:

    from scopelock import ScopeLock
    with ScopeLock("sattrack", "satellite pass test", ttl_s=120) as lk:
        ...               # lk.lost is set (and on_lost called) if the lock is revoked or can't be renewed

If the service stops while a script holds the API lock, the script takes over the file lock at its next
renewal (every ttl/3, at most 5 s), or loses the lock if someone else got the file lock first.

or `Scope(lock_owner="sattrack", lock_reason="...")` / env `SEESTAR_LOCK_OWNER=sattrack` for tools/scope.py,
which then stops any axis move once the lock is lost.

Service URL and token: arguments, else $SEQ_URL / $SEQ_TOKEN, else config.toml [sequencer] host/port and
api_token. Data dir: argument, else $SEESTAR_DATA_DIR, else config.toml [paths] data_dir.
"""
import errno
import fcntl
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request

LOCK_FILE = "scope.lock"


class LockBusy(RuntimeError):
    """Someone else has the scope (the message says who)."""


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, TypeError):
        return True


class FileLock:
    """Non-blocking exclusive flock on a file; the content names the holder while it is held."""

    def __init__(self, path):
        self.path = path
        self._fd = None

    @property
    def held(self):
        return self._fd is not None

    def acquire(self, owner, reason="", **info):
        """True if we hold it now (also when we already did)."""
        if self._fd is not None:
            return True
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                return False
            raise
        self._fd = fd
        rec = {"owner": owner, "reason": reason, "pid": os.getpid(), "host": socket.gethostname(),
               "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **info}
        os.ftruncate(fd, 0)
        os.pwrite(fd, (json.dumps(rec) + "\n").encode(), 0)
        return True

    def release(self):
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            os.ftruncate(fd, 0)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def holder(self):
        """Who holds it, from the file's content: a dict, or None (free, or the holder died)."""
        try:
            with open(self.path) as fh:
                txt = fh.read().strip()
        except OSError:
            return None
        if not txt:
            return None
        try:
            rec = json.loads(txt)
        except ValueError:
            return {"owner": "?", "reason": txt[:80]}
        if rec.get("host") == socket.gethostname() and not _pid_alive(rec.get("pid")):
            return None
        return rec


def describe(h):
    if not h:
        return "someone"
    return f"{h.get('owner', '?')}" + (f": {h['reason']}" if h.get("reason") else "") + \
        (f" (pid {h['pid']})" if h.get("pid") else "")


def _config():
    try:
        import config as C           # tools/config.py: exits if there is no config file
        return C
    except (ImportError, SystemExit):
        return None


def default_url():
    if os.environ.get("SEQ_URL"):
        return os.environ["SEQ_URL"]
    C = _config()
    host, port = "127.0.0.1", 8640
    if C is not None:
        host = C.get("sequencer", "host", host)
        port = int(C.get("sequencer", "port", port))
    if host in ("0.0.0.0", "", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def default_token():
    if "SEQ_TOKEN" in os.environ:
        return os.environ["SEQ_TOKEN"]
    C = _config()
    return C.get("sequencer", "api_token", "") if C is not None else ""


def default_data_dir():
    if os.environ.get("SEESTAR_DATA_DIR"):
        return os.environ["SEESTAR_DATA_DIR"]
    C = _config()
    if C is not None:
        return C.DATA_DIR
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


class Unreachable(RuntimeError):
    pass


class ScopeLock:
    """Take the scope: the service's API lock if it answers, else the file lock. Renews in a thread.

      owner        who you are (shown on the status page); renewing uses the same owner
      reason       what for
      ttl_s        the service drops the lock this long after the last renewal (renewed every ttl/3)
      on_lost      callable(msg), called once if the lock is revoked or can't be renewed
      require_service  True: don't fall back to the file lock when the service is unreachable
    """

    def __init__(self, owner, reason="", ttl_s=120.0, url=None, token=None, data_dir=None, on_lost=None,
                 require_service=False, timeout=5.0, log=print):
        self.owner, self.reason, self.ttl_s = str(owner), str(reason), float(ttl_s)
        self.url = (url or default_url()).rstrip("/")
        self.token = default_token() if token is None else token
        self.data_dir = data_dir or default_data_dir()
        self.on_lost, self.require_service, self.timeout, self.log = on_lost, require_service, timeout, log
        self.mode = None                  # "api" | "file" | None
        self.lost = threading.Event()
        self.lost_msg = None
        self._stop = threading.Event()
        self._thread = None
        self._file = FileLock(os.path.join(self.data_dir, LOCK_FILE))

    # ---- transport ---------------------------------------------------------------------------------
    def _post(self, path, body):
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                d = json.loads(e.read())
            except ValueError:
                d = {}
            if e.code == 404:
                raise Unreachable(f"{self.url} has no lock API (HTTP 404)") from e
            return {"ok": False, "msg": d.get("msg") or f"HTTP {e.code}", "code": e.code}
        except (urllib.error.URLError, OSError) as e:      # refused, timeout, DNS
            raise Unreachable(f"{self.url}: {getattr(e, 'reason', e)}") from e

    def _lock_body(self, renew=False):
        return {"owner": self.owner, "reason": self.reason, "ttl_s": self.ttl_s, "renew": renew}

    # ---- public ------------------------------------------------------------------------------------
    def acquire(self):
        if self.mode:
            return self
        try:
            r = self._post("/api/lock", self._lock_body())
        except Unreachable as e:
            if self.require_service:
                raise LockBusy(f"sequencer service not reachable ({e}) and require_service is set") from e
            if not self._file.acquire(self.owner, self.reason, via="scopelock"):
                raise LockBusy(f"scope in use by {describe(self._file.holder())} (file lock "
                               f"{self._file.path}; the sequencer service at {self.url} is not reachable: {e})")
            self.mode = "file"
            self.log(f"scope lock: file lock {self._file.path} (service not reachable: {e})")
            return self
        if not r.get("ok"):
            raise LockBusy(f"sequencer refused the lock: {r.get('msg')}")
        self.mode = "api"
        self.log(f"scope lock: held via {self.url} ({r.get('msg')})")
        self._thread = threading.Thread(target=self._renew_loop, name="scopelock", daemon=True)
        self._thread.start()
        return self

    def _lose(self, msg):
        if self.lost.is_set():
            return
        self.lost_msg = msg
        self.lost.set()
        self.log(f"scope lock LOST: {msg}")
        if self.on_lost:
            try:
                self.on_lost(msg)
            except Exception:  # noqa: BLE001
                pass

    def _renew_loop(self):
        every = max(1.0, min(self.ttl_s / 3.0, 5.0))      # short: a revoked lock (unsafe) is noticed fast
        last_ok = time.time()
        while not self._stop.wait(every):
            try:
                r = self._post("/api/lock", self._lock_body(renew=True))
            except Unreachable as e:
                # The API lock lives in the service: if the service stopped, it's gone and so is the file
                # lock the service held for us, so someone else could start moving. Take the file lock
                # at once. Got it: the service is gone, carry on in file mode. Held by the (slow but alive)
                # service: keep retrying until the TTL. Held by anyone else: lost.
                if self._file.acquire(self.owner, self.reason, via="scopelock-failover"):
                    self.mode = "file"
                    self.log(f"scope lock: service unreachable ({e}); took over the file lock {self._file.path}")
                    return
                h = self._file.holder() or {}
                if h.get("via") != "service":
                    return self._lose(f"service unreachable ({e}) and the file lock is held by {describe(h)}")
                if time.time() - last_ok > self.ttl_s:
                    return self._lose(f"service unreachable for longer than the TTL ({e})")
                continue
            if not r.get("ok"):
                return self._lose(r.get("msg") or "renewal refused")
            last_ok = time.time()

    def check(self):
        """Raise LockBusy if the lock was lost (for scripts that poll)."""
        if self.lost.is_set():
            raise LockBusy(f"scope lock lost: {self.lost_msg}")

    def release(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.timeout + 1)
            self._thread = None
        mode, self.mode = self.mode, None
        if mode == "file":
            self._file.release()
        elif mode == "api" and not self.lost.is_set():
            try:
                self._post("/api/unlock", {"owner": self.owner})
            except Unreachable as e:
                self.log(f"scope lock: unlock failed ({e}); it expires after {self.ttl_s:.0f} s")

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False
