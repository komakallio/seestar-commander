"""Imager: never trusts ImageReady before the exposure time has passed, and rejects a frame that's a
byte-identical repeat of the previous download (the SeeStar's wide camera does both)."""
import numpy as np
import pytest

from sequencer.clock import ScaledClock
from sequencer.rig import Imager


class RepeatCam:
    Connected = True
    Gain = 0

    def __init__(self, frames):
        self.frames, self.k = frames, 0
        self.ImageReady = True                 # claims ready at once, like the wide camera

    def StartExposure(self, exp, light):
        pass

    @property
    def ImageArray(self):
        f = self.frames[min(self.k, len(self.frames) - 1)]
        self.k += 1
        return f.T


def _imager(frames):
    return Imager(RepeatCam(frames), None, None, ScaledClock(1000.0, 1.8e9), log=lambda *a: None)


def test_ready_waits_out_the_exposure():
    im = _imager([np.zeros((4, 4), np.uint16)])
    im.start(30, 200)
    assert not im.ready()
    im.clock.sleep(31)
    assert im.ready()


def test_repeated_frame_is_retried_then_refused():
    a, b = np.zeros((4, 4), np.uint16), np.ones((4, 4), np.uint16)
    im = _imager([a, a, a, b])
    assert np.array_equal(im.download(), a)
    assert np.array_equal(im.download(), b)                # two repeats skipped, then the new frame
    assert im.stale_repeats == 2
    im2 = _imager([a] * 10)
    im2.download()
    with pytest.raises(RuntimeError, match="previous frame"):
        im2.download()
