"""Clocks. Everything in the sequencer asks a clock for the time, so the simulator can run faster
than real time (and start at night) while the real service uses the wall clock."""
import threading
import time


class RealClock:
    speed = 1.0

    def time(self):
        return time.time()

    def sleep(self, s):
        if s > 0:
            time.sleep(s)

    def wait(self, event: threading.Event, s):
        """Sleep up to s seconds; returns True early if event is set."""
        return event.wait(max(0.0, s))


class ScaledClock:
    """Virtual time = start + (real elapsed) * speed."""

    def __init__(self, speed=1.0, start=None):
        self.speed = float(speed)
        self._r0 = time.time()
        self._v0 = float(start) if start is not None else self._r0

    def time(self):
        return self._v0 + (time.time() - self._r0) * self.speed

    def sleep(self, s):
        if s > 0:
            time.sleep(s / self.speed)

    def wait(self, event: threading.Event, s):
        return event.wait(max(0.0, s) / self.speed)
