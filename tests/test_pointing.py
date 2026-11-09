"""Pointing memory: the firmware Park drops the sync, so without memory the first solve after every
unpark is ~100' off (seen on three sky sessions). With memory the first slew is offset by the model and
lands a few arcmin from the target. The model fit recovers the simulator's index and polar terms."""
import json
import math
import os

import numpy as np

from sequencer import astro, sim
from sequencer import plan as P
from sequencer.pointing import PointingConfig, PointingMemory, fit, predict

from conftest import LAT, LON, NIGHT, ra_at_ha, wait_for

SKY_LIKE = (-0.9, 1.55)          # index error HA/Dec (deg): ~100' at Dec 45, like the real first solves


class _Clock:
    def __init__(self, t=NIGHT):
        self.t = t

    def time(self):
        return self.t


def _plan(ha, dec, park=True):
    return P.loads(f'[plan]\npark_at_end = {"true" if park else "false"}\n[[step]]\ntarget = "T{ha}"\n'
                   f'ra = {ra_at_ha(ha)}\ndec = {dec}\nexposure = 5\ncount = 1\ncentre = "auto"\n'
                   'centre_tol_arcmin = 3\n')


def _run(svc, pl):
    assert svc.cmd_start(pl)["ok"]
    assert wait_for(lambda: svc.idle() and svc.status()["state"] in ("done", "parked", "error"), 300)
    st = svc.status()
    assert st["state"] != "error", st["log"][-10:]
    return st["plan"]["steps"][0]["centre"]


def _two_nights(svc):
    c1 = _run(svc, _plan(-30, 45))                   # learns the offset, parks (firmware drops the sync)
    assert svc.rig.sim.telescope.AtPark
    c2 = _run(svc, _plan(-10, 40))                   # first solve after the unpark
    return c1, c2


def test_first_solve_after_unpark_without_memory_is_far_off(make_service):
    svc = make_service(speed=300, world={"pointing_error": SKY_LIKE},
                       extra={"pointing": {"enabled": False}})
    c1, c2 = _two_nights(svc)
    assert c1["first_offset_arcmin"] > 80, c1
    assert c2["first_offset_arcmin"] > 80, c2        # the park forgot the sync
    tel = svc.rig.sim.telescope
    assert tel.firmware_gotos == 0 and not tel.cable_violation


def test_first_solve_after_unpark_with_memory_is_close(make_service):
    svc = make_service(speed=300, world={"pointing_error": SKY_LIKE})
    c1, c2 = _two_nights(svc)
    assert c1["first_offset_arcmin"] > 80, c1        # nothing remembered yet
    assert c2["first_offset_arcmin"] < 6, c2
    assert c2["iterations"] == 1, c2
    pt = svc.status()["pointing"]
    assert pt["kind"] == "constant" and pt["n"] >= 2, pt
    assert abs(pt["terms_arcmin"]["dDec"] - SKY_LIKE[1] * 60) < 3, pt
    assert pt["last_correction"]["applied"], pt
    recs = [json.loads(x) for x in open(os.path.join(svc.s.data_dir, "pointing", "solves.jsonl"))]
    # solves taken while synced still give the raw offset (the sync shift is taken off)
    for r in recs:
        assert abs(r["off_dec"] - SKY_LIKE[1]) < 0.05, r
    tel = svc.rig.sim.telescope
    assert tel.firmware_gotos == 0 and not tel.cable_violation


def test_memory_survives_a_restart(make_service, tmp_path):
    svc = make_service(speed=300, world={"pointing_error": SKY_LIKE})
    _run(svc, _plan(-30, 45))
    svc.shutdown()
    svc2 = make_service(speed=300, start=NIGHT + 3600, world={"pointing_error": SKY_LIKE})   # same data dir
    c = _run(svc2, _plan(-20, 50))
    assert c["first_offset_arcmin"] < 6, c


def _sample(tel, clock, has, decs, noise_arcsec=5.0, seed=3):
    """(ha, dec, dH, dD) raw offsets from the simulated mount over a grid (as the solver would see)."""
    rng = np.random.default_rng(seed)
    pts = []
    for h in has:
        for d in decs:
            tel._u, tel._dec = float(h), float(d)
            ra, dec = tel.true_radec()
            hs = astro.wrap180(astro.lst_deg(clock.time(), LON) - ra)
            n = rng.normal(0, noise_arcsec / 3600.0, 2)
            pts.append((float(h), float(d), astro.wrap180(hs - h) + n[0] / math.cos(math.radians(d)),
                        dec - d + n[1]))
    return pts


def test_fit_recovers_simulated_terms():
    clock = _Clock()
    alt_e, az_e = 70.0, -50.0                         # arcmin, polar.py's convention
    tel = sim.SimTelescope(clock, LAT, LON, pointing_error=SKY_LIKE, polar_error=(alt_e, az_e),
                           start_parked=False)
    pts = _sample(tel, clock, np.arange(-75, 76, 15), (15, 35, 55, 70))
    f = fit(pts)
    t = {k: v * 60 for k, v in f["terms"].items()}
    assert abs(t["ME"] - alt_e) < 3, t                # polar elevation error
    assert abs(t["MA"] - az_e) < 3, t                 # polar azimuth error
    assert abs(t["ID"] - SKY_LIKE[1] * 60) < 4, t     # Dec index (plus a little refraction)
    assert f["rms_arcmin"] < 2.0, f["rms_arcmin"]
    # the constant-offset model is much worse over this HA range
    ch = float(np.median([p[2] for p in pts])); cd = float(np.median([p[3] for p in pts]))
    rms_c = math.sqrt(np.mean([((p[2] - ch) * math.cos(math.radians(p[1]))) ** 2 + (p[3] - cd) ** 2
                               for p in pts])) * 60
    assert rms_c > 3 * f["rms_arcmin"], (rms_c, f["rms_arcmin"])
    # and predicts an unseen point
    h, d = 33.0, 48.0
    (q,) = _sample(tel, clock, [h], [d], noise_arcsec=0)
    ph, pd = predict(f["terms"], h, d)
    assert math.hypot((ph - q[2]) * math.cos(math.radians(d)), pd - q[3]) * 60 < 2.0


def test_memory_switches_from_constant_to_fit(tmp_path):
    clock = _Clock()
    tel = sim.SimTelescope(clock, LAT, LON, pointing_error=SKY_LIKE, polar_error=(60.0, 40.0),
                           start_parked=False)
    cfg = PointingConfig(min_solves_for_fit=8, min_ha_span_deg=40)
    mem = PointingMemory(str(tmp_path / "pointing"), cfg, clock, log=lambda m: None)
    mem.note_parked()
    shift = (0.4, -0.2)                                # pretend the mount is synced: reported = raw + shift
    mem.shift = shift
    pts = _sample(tel, clock, np.arange(-60, 61, 20), (20, 50))
    for i, (h, d, dh, dd) in enumerate(pts):
        clock.t = NIGHT + 60 * i
        rec = mem.add_solve(clock.t, "x", (h + shift[0], d + shift[1]), (h + dh, d + dd))
        assert abs(rec["off_dec"] - dd) < 1e-4
        if i == 2:
            assert mem.model()["kind"] == "constant"
    m = mem.model()
    assert m["kind"] == "fit", m
    assert abs(m["terms"]["ME"] * 60 - 60) < 3 and abs(m["terms"]["MA"] * 60 - 40) < 3, m["terms"]
    s = mem.summary()
    assert s["kind"] == "fit" and s["rms_arcmin"] < 2 and s["residuals"]
    # correction = model - shift, and refused when the mount's sync state is unknown
    dh, dd, why = mem.correction(10.0, 45.0)
    ph, pd = mem.predict(10.0, 45.0)
    assert abs(dh - (ph - shift[0])) < 1e-9 and abs(dd - (pd - shift[1])) < 1e-9 and why == "fit"
    mem.note_unknown("test")
    assert mem.correction(10.0, 45.0)[0] is None
    assert mem.add_solve(clock.t, "x", (0, 40), (0.1, 40.1)) is None
    # old solves age out
    clock.t += 31 * 86400
    mem.invalidate()
    assert mem.model()["kind"] == "none"


def test_polar_alignment_done_resets_the_model(tmp_path):
    clock = _Clock()
    since = {"t": 0.0}
    mem = PointingMemory(str(tmp_path / "p"), PointingConfig(), clock, log=lambda m: None,
                         since=lambda: since["t"])
    mem.note_parked()
    mem.add_solve(clock.t, "a", (-20.0, 40.0), (-20.5, 41.5))
    assert mem.model()["kind"] == "constant"
    since["t"] = clock.t + 1
    clock.t += 10
    mem.invalidate()
    assert mem.model()["kind"] == "none"
