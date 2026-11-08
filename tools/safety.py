"""External safety source and park/roof hooks (all optional, configured in config.toml).

safety_state() -> (safe: bool, reasons: list[str]); an unreachable or stale endpoint counts as
unsafe once nothing good has been heard for [safety] stale_after_s.

usage (read-only watch): safety.py [--once]
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
import config

_last_ok = None


def _http(url, method="GET", timeout=10):
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode()
    try:
        return json.loads(body)
    except ValueError:
        return body


def safety_state():
    """(safe, reasons). Without a configured safety URL: always safe (no external source)."""
    global _last_ok
    if not config.SAFETY_URL:
        return True, []
    try:
        d = _http(config.SAFETY_URL)
        safe = bool(d.get(config.SAFETY_FIELD))
        reasons = list(d.get(config.SAFETY_REASONS_FIELD) or [])
        _last_ok = time.time()
        return safe, reasons
    except Exception as e:
        if _last_ok is None or time.time() - _last_ok > config.SAFETY_STALE_S:
            return False, [f"SAFETY_UNREACHABLE: {e}"]
        return True, [f"safety endpoint error (within grace): {e}"]


def hook(url):
    """POST to an optional hook URL; returns the response or None. Never raises."""
    if not url:
        return None
    try:
        return _http(url, method="POST")
    except Exception as e:
        return f"hook failed: {e}"


def parked():
    return hook(config.HOOK_PARKED)


def unparked():
    return hook(config.HOOK_UNPARKED)


def roof_state():
    if not config.ROOF_STATE_URL:
        return None
    try:
        return _http(config.ROOF_STATE_URL)
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    while True:
        s, r = safety_state()
        print(time.strftime("%H:%M:%S"), "SAFE" if s else "UNSAFE", r, "| roof:", roof_state(), flush=True)
        if "--once" in sys.argv:
            break
        time.sleep(config.SAFETY_POLL_S)
