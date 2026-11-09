"""SeeStar app protocol (the phone app's control channel, TCP port 4700): newline-delimited JSON-RPC.

Firmware 7.18+ gates it behind a challenge-response: get_verify_str returns a string, the client signs
it with SHA1withRSA / PKCS#1 v1.5 using the RSA private key embedded in the official Android app
(lib/<abi>/libopenssllib.so inside the APK), and sends verify_client{sign, data}. The key is the same
for every unit; it is the app's interoperability key, kept out of this repo and loaded from a file the
operator points to (config [sattrack] native_pem). Extraction is for interoperability with one's own
device; see the handover.

This is the ONLY route that can drive both axes at once: scope_speed_move{speed, angle, dur_sec} is a
single speed in one direction for a fixed time, self-stopping. It does NOT respect our cable bookkeeping
(ra_unwrap.json), so callers keep moves small and re-sync afterwards, exactly like a firmware goto.
"""
import base64
import json
import socket
import time


class NativeError(RuntimeError):
    pass


class SeestarNative:
    def __init__(self, host, pem_path, port=4700, timeout=8.0, log=print):
        self.host, self.port, self.timeout, self.log = host, port, timeout, log
        self._buf = ""
        self._id = 1000
        self.s = None
        from cryptography.hazmat.primitives import serialization
        with open(pem_path, "rb") as fh:
            self.key = serialization.load_pem_private_key(fh.read(), password=None)

    def connect(self):
        self.s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.s.settimeout(self.timeout)
        return self

    def _send(self, method, params=None):
        self._id += 1
        msg = {"id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self.s.sendall((json.dumps(msg) + "\r\n").encode())
        return self._id

    def _recv(self, want, timeout=12.0):
        t = time.time()
        while time.time() - t < timeout:
            while "\r\n" in self._buf:
                line, self._buf = self._buf.split("\r\n", 1)
                if not line.strip():
                    continue
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                if j.get("id") == want and "Event" not in j:
                    return j
            try:
                d = self.s.recv(65536)
            except socket.timeout:
                continue
            if not d:
                break
            self._buf += d.decode("utf-8", "replace")
        return None

    def call(self, method, params=None, timeout=12.0):
        r = self._recv(self._send(method, params), timeout)
        if r is None:
            raise NativeError(f"{method}: no reply")
        return r

    def authenticate(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        r = self.call("get_verify_str")
        ch = (r.get("result") or {}).get("str", "")
        if not ch:
            raise NativeError(f"no auth challenge (scope busy?): {r}")
        sig = base64.b64encode(self.key.sign(ch.encode(), padding.PKCS1v15(), hashes.SHA1())).decode()
        vr = self.call("verify_client", {"sign": sig, "data": ch})
        res = vr.get("result", vr.get("code", -1))
        ok = res == 0 or (isinstance(res, dict) and "key_envelope" in res and vr.get("code", 0) == 0)
        if not ok:
            raise NativeError(f"verify_client failed: {vr}")
        return vr

    def device_state(self):
        return self.call("get_device_state").get("result", {})

    def equ_coord(self):
        r = self.call("scope_get_equ_coord").get("result", {})
        return r.get("ra"), r.get("dec")

    def speed_move(self, angle, speed, dur_sec):
        """One move: speed (steps/s, ~240 ≈ 1°/s), angle (deg, direction), dur_sec (int). Self-stops."""
        return self.call("scope_speed_move", {"speed": int(speed), "angle": int(angle),
                                              "dur_sec": int(dur_sec)})

    def stop(self):
        try:
            return self.call("scope_speed_move", {"speed": 0, "angle": 0, "dur_sec": 0}, timeout=5.0)
        except NativeError:
            return None

    def close(self):
        try:
            if self.s:
                self.s.close()
        except OSError:
            pass
