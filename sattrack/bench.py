"""Bench harness for axis tests: every Alpaca call logged and timed, every rate checked by the guard.

Bench(scope, limits, log) wraps a tools/scope.Scope. Use it as a context manager: on exit, on an
exception, on SIGINT or SIGTERM it stops both axes (retrying until acknowledged). A position read
that fails or is slower than read_timeout counts as a bad link: both axes are stopped and LinkError
is raised. Unwrapped-HA bookkeeping goes through the Scope's own unwrap file, so the sequencer and the
bench agree on where the cable is.
"""
import signal
import time

from .guard import GuardError  # noqa: F401  (re-exported for callers)

PRIMARY, SECONDARY = 0, 1


class LinkError(RuntimeError):
    pass


class Stopped(RuntimeError):
    pass


def wrap180(x):
    return (x + 180.0) % 360.0 - 180.0


class Bench:
    def __init__(self, scope, limits, log, read_timeout=1.0, clock=time):
        self.s, self.t, self.lim, self.log = scope, scope.t, limits, log
        self.read_timeout = read_timeout
        self.clock = clock
        self.rate_cmd = [0.0, 0.0]
        self.last = None          # last full position read: dict(u, dec, t)
        self._old = {}

    # ---- logged calls ------------------------------------------------------------------------
    def _call(self, name, fn, *args):
        t0m, t0 = time.monotonic(), self.clock.time()
        try:
            v = fn(*args)
            err = None
        except Exception as e:  # noqa: BLE001 - logged and re-raised
            v, err = None, f"{type(e).__name__}: {e}"
        t1m = time.monotonic()
        self.log.write(c=name, a=list(args) or None, v=v if isinstance(v, (int, float, bool, str)) else None,
                       t0=t0, dt=round(t1m - t0m, 5), m0=round(t0m, 5), err=err)
        if err:
            raise RuntimeError(f"{name}: {err}")
        return v, t0, t1m - t0m

    def get(self, prop):
        return self._call(prop, lambda: getattr(self.t, prop))

    def _move(self, axis, rate):
        self._call(f"MoveAxis{axis}", self.t.MoveAxis, axis, float(rate))
        self.rate_cmd[axis] = float(rate)

    # ---- position ----------------------------------------------------------------------------
    def read_ra(self):
        """Unwrapped HA from one RightAscension read; returns (u, t_mid). Updates the unwrap file."""
        try:
            ra_h, t0, dt = self.get("RightAscension")
        except RuntimeError as e:
            self._bad_link(str(e))
        if dt > self.read_timeout:
            self._bad_link(f"RightAscension took {dt:.2f} s")
        tm = t0 + dt / 2
        with self.s._ulock:
            u_prev = self.s._load_u()
            ha = wrap180(self.s._lst() - ra_h * 15.0)
            u = u_prev + wrap180(ha - wrap180(u_prev))
            self.s._save_u(u, note="bench")
        self.log.write(c="u", u=round(u, 5), t=tm)
        if self.last:
            self.last.update(u=u, t=tm)
        return u, tm

    def read_dec(self):
        try:
            dec, t0, dt = self.get("Declination")
        except RuntimeError as e:
            self._bad_link(str(e))
        if dt > self.read_timeout:
            self._bad_link(f"Declination took {dt:.2f} s")
        self.log.write(c="d", d=round(dec, 5), t=t0 + dt / 2)
        if self.last:
            self.last.update(dec=dec)
        return dec, t0 + dt / 2

    def read(self):
        u, tu = self.read_ra()
        dec, td = self.read_dec()
        self.last = {"u": u, "dec": dec, "t": (tu + td) / 2, "skew": td - tu}
        return self.last

    # ---- motion ------------------------------------------------------------------------------
    def rate(self, axis, r, pos=None):
        """Command a rate after the guard check (position: pos, else the last read, else a new read)."""
        p = pos or self.last or self.read()
        ru, rd = (r, self.rate_cmd[1]) if axis == PRIMARY else (self.rate_cmd[0], r)
        if r != 0:
            self.lim.check(p["u"], p["dec"], ru, rd)
        self._move(axis, r)

    def stop(self, deadline_s=10.0):
        """Both axes to 0, retried until both calls succeed or the deadline passes."""
        t_end = time.monotonic() + deadline_s
        pending = [PRIMARY, SECONDARY]
        while pending and time.monotonic() < t_end:
            for ax in list(pending):
                try:
                    self._move(ax, 0.0)
                    pending.remove(ax)
                except Exception:  # noqa: BLE001 - keep trying
                    pass
            if pending:
                time.sleep(0.2)
        self.log.write(c="stop", ok=not pending, pending=pending or None)
        return not pending

    def _bad_link(self, why):
        self.log.write(c="bad_link", why=why)
        self.stop()
        raise LinkError(why)

    # ---- context: signals and cleanup --------------------------------------------------------
    def _on_signal(self, signum, frame):
        raise Stopped(f"signal {signum}")

    def __enter__(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._old[sig] = signal.signal(sig, self._on_signal)
            except ValueError:      # not the main thread (tests)
                pass
        return self

    def __exit__(self, et, ev, tb):
        self.stop()
        for sig, h in self._old.items():
            signal.signal(sig, h)
        if et is not None:
            self.log.write(c="exit", err=f"{et.__name__}: {ev}")
        return False
