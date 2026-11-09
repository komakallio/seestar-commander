"""Motion limits shared by the planner, the bench tests and the tracker.

Axis angles: u = unwrapped hour angle of the RA axis (deg, positive rate = increasing HA), dec in deg.

Runaway rule: Alpaca HTTP has no keepalive, so if commands stop arriving the mount may hold the last
rate indefinitely. Every rate must therefore be safe to hold for runaway_s seconds: the axis must stay
inside its window over that time. Dec only needs the upper side checked when dec_low_stop is true
(the firmware stops Dec MoveAxis near the arm fold by itself).
"""
from dataclasses import dataclass

import numpy as np


class GuardError(RuntimeError):
    pass


@dataclass
class Limits:
    u_min: float                  # unwrapped HA window (already including the cable margin)
    u_max: float
    dec_min: float = -80.0
    dec_max: float = 85.0
    max_rate: float = 5.0         # deg/s per axis; set from the measured top speed minus a margin
    runaway_ra_s: float = 30.0
    runaway_dec_s: float = 10.0
    dec_low_stop: bool = True     # firmware stops Dec MoveAxis at the low end

    @classmethod
    def from_config(cls, cfg_get, u_min, u_max):
        g = lambda k, d: float(cfg_get("sattrack", k, d))  # noqa: E731
        return cls(u_min=u_min, u_max=u_max, dec_min=g("dec_min", -80.0), dec_max=g("dec_max", 85.0),
                   max_rate=g("max_rate_deg_s", 5.0), runaway_ra_s=g("runaway_ra_s", 30.0),
                   runaway_dec_s=g("runaway_dec_s", 10.0),
                   dec_low_stop=bool(cfg_get("sattrack", "dec_low_stop", True)))

    # vectorised: arrays in, boolean arrays out (True = violates)
    def position_bad(self, u, dec):
        u, dec = np.asarray(u, float), np.asarray(dec, float)
        return {"cable": (u < self.u_min) | (u > self.u_max),
                "dec": (dec < self.dec_min) | (dec > self.dec_max)}

    def rate_bad(self, u, dec, ru, rdec):
        u, dec, ru, rdec = (np.asarray(x, float) for x in (u, dec, ru, rdec))
        ue = u + ru * self.runaway_ra_s
        de = dec + rdec * self.runaway_dec_s
        dec_run = de > self.dec_max
        if not self.dec_low_stop:
            dec_run = dec_run | (de < self.dec_min)
        return {"rate": (np.abs(ru) > self.max_rate) | (np.abs(rdec) > self.max_rate),
                "runaway_ra": (ue < self.u_min) | (ue > self.u_max),
                "runaway_dec": dec_run}

    def all_bad(self, u, dec, ru, rdec):
        d = self.position_bad(u, dec)
        d.update(self.rate_bad(u, dec, ru, rdec))
        return d

    def check(self, u, dec, ru, rdec):
        """Scalar check before a MoveAxis. Raises GuardError naming every violated rule."""
        bad = [k for k, v in self.all_bad(u, dec, ru, rdec).items() if bool(np.any(v))]
        if bad:
            raise GuardError(f"guard: {', '.join(bad)} (u={u:.2f} dec={dec:.2f} ru={ru:+.3f} rdec={rdec:+.3f})")
