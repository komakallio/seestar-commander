"""ASTAP solver wrapper (subprocess mocked: no ASTAP binary needed), frame-centre evaluation and the
FITS header units."""
import os
import subprocess

import numpy as np
import pytest

from sequencer import astro, imaging
from sequencer import rig as R

# a real ASTAP D05 solution of a 2160 x 3840 telephoto frame (M31, 2026-11-08)
INI = """PLTSOLVD=T
CRPIX1= 1.0805000000000000E+003
CRPIX2= 1.9205000000000000E+003
CRVAL1= 1.0906455295997308E+001
CRVAL2= 4.1272084893033082E+001
CDELT1= 1.0194928778946444E-003
CDELT2= 1.0193681138055894E-003
CROTA1=-1.7791079798197472E+002
CROTA2=-1.7791056752976959E+002
CD1_1=-1.0188152025438525E-003
CD1_2= 3.7165994449673556E-005
CD2_1=-3.7165543453631862E-005
CD2_2=-1.0186903719106024E-003
CMDLINE=astap_cli -f frame.fits
"""


@pytest.fixture
def fake_astap(tmp_path):
    exe = tmp_path / "astap_cli"
    exe.write_text("#!/bin/sh\nexit 1\n")
    exe.chmod(0o755)
    db = tmp_path / "db"
    db.mkdir()
    return str(exe), str(db)


def _cards(text):
    return R.parse_astap_ini(text)


def test_missing_binary_is_unavailable(tmp_path):
    s = R.AstapSolver(str(tmp_path / "nope" / "astap_cli"), "", 3.74)
    assert not s.available and "ASTAP CLI not found" in s.error
    assert s.solve(np.zeros((8, 8), np.uint16)) is None
    s = R.AstapSolver("astap_cli_definitely_not_on_path", "", 3.74)
    assert not s.available


def test_missing_db_dir_is_unavailable(fake_astap, tmp_path):
    exe, _ = fake_astap
    s = R.AstapSolver(exe, str(tmp_path / "missing"), 3.74)
    assert not s.available and "database" in s.error


def test_make_solver_kinds(fake_astap):
    exe, db = fake_astap
    assert isinstance(R.make_solver({"kind": "astap", "astap_path": exe, "db_dir": db}, 3.74), R.AstapSolver)
    assert R.make_solver({"kind": "none"}, 3.74).name == "none"
    old = R.make_solver({"kind": "astrometry"}, 3.74)
    assert not old.available and "astrometry was removed" in old.error


def test_solve_parses_ini_and_command_line(fake_astap, monkeypatch):
    exe, db = fake_astap
    s = R.AstapSolver(exe, db, 3.74, log=lambda m: None)
    assert s.available
    seen = {}

    def run(cmd, **kw):
        seen["cmd"] = cmd
        f = cmd[cmd.index("-f") + 1]
        assert os.path.exists(f)
        with open(os.path.splitext(f)[0] + ".ini", "w") as fh:
            fh.write(INI)
        return subprocess.CompletedProcess(cmd, 0, "Solution found", "")
    monkeypatch.setattr(R.subprocess, "run", run)
    sol = s.solve(np.zeros((3840, 2160), np.uint16), ra_hint=10.68, dec_hint=41.27)
    cmd = seen["cmd"]
    arg = lambda k: cmd[cmd.index(k) + 1]   # noqa: E731
    assert float(arg("-ra")) == pytest.approx(10.68 / 15.0, abs=1e-6)        # hours
    assert float(arg("-spd")) == pytest.approx(131.27, abs=1e-6)             # Dec + 90
    assert float(arg("-fov")) == pytest.approx(3840 * 3.74 / 3600.0, abs=1e-3)   # image height
    assert arg("-r") == "6" and arg("-z") == "2" and arg("-d") == db and arg("-D") == "d05"
    # ASTAP puts CRPIX at the exact frame centre, so the centre equals CRVAL
    assert sol["ra"] == pytest.approx(10.906455296, abs=1e-7)
    assert sol["dec"] == pytest.approx(41.272084893, abs=1e-7)
    assert sol["scale"] == pytest.approx(3.668, abs=0.01)
    cd = np.array(sol["cd"])
    assert cd.shape == (2, 2) and abs(np.linalg.det(cd)) ** 0.5 * 3600 == pytest.approx(3.668, abs=0.01)


def test_blind_without_hint(fake_astap):
    exe, db = fake_astap
    cmd = R.AstapSolver(exe, db, 3.74).command("f.fits", 3840)
    assert cmd[cmd.index("-r") + 1] == "180" and "-ra" not in cmd


def test_unsolved_returns_none(fake_astap, monkeypatch):
    exe, db = fake_astap
    logs = []
    s = R.AstapSolver(exe, db, 3.74, log=logs.append)

    def run(cmd, **kw):
        with open(os.path.splitext(cmd[cmd.index("-f") + 1])[0] + ".ini", "w") as fh:
            fh.write("PLTSOLVD=F\nERROR=No solution found!\n")
        return subprocess.CompletedProcess(cmd, 1, "", "")
    monkeypatch.setattr(R.subprocess, "run", run)
    assert s.solve(np.zeros((100, 60), np.uint16), 10, 40) is None
    assert "No solution" in logs[-1]

    def timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 30)
    monkeypatch.setattr(R.subprocess, "run", timeout)
    assert s.solve(np.zeros((100, 60), np.uint16), 10, 40) is None


def test_frame_centre_from_wcs_not_crval():
    """With the reference pixel in a corner, the reported position must still be the frame centre."""
    from astropy.wcs import WCS
    c = _cards(INI)
    # a solution whose reference pixel is the first pixel (FITS 1, 1), as some solvers write it
    corner = dict(c, CRPIX1="1", CRPIX2="1", CRVAL1="10.0", CRVAL2="40.0")
    sol = R.wcs_solution(corner, 2160, 3840)
    ref = WCS(naxis=2)
    ref.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    ref.wcs.crpix, ref.wcs.crval = [1.0, 1.0], [10.0, 40.0]
    ref.wcs.cd = [[float(c["CD1_1"]), float(c["CD1_2"])], [float(c["CD2_1"]), float(c["CD2_2"])]]
    # the geometric centre of a W x H frame is FITS pixel ((W + 1) / 2, (H + 1) / 2)
    ra_c, dec_c = ref.wcs_pix2world([[2161 / 2, 3841 / 2]], 1)[0]
    assert astro.separation(sol["ra"], sol["dec"], ra_c, dec_c) * 3600 < 0.01
    assert astro.separation(sol["ra"], sol["dec"], 10.0, 40.0) > 1.5          # not CRVAL (the corner)
    # ...and at the true centre solution, ra/dec is exactly CRVAL
    sol0 = R.wcs_solution(c, 2160, 3840)
    assert (sol0["ra"], sol0["dec"]) == pytest.approx((float(c["CRVAL1"]), float(c["CRVAL2"])), abs=1e-9)


def test_sexagesimal():
    assert astro.ra_sexagesimal(10.6847) == "00 42 44.33"
    assert astro.ra_sexagesimal(359.9999999) == "00 00 00.00"            # rounding carries over 24 h
    assert astro.ra_sexagesimal(56.75) == "03 47 00.00"
    assert astro.dec_sexagesimal(41.269) == "+41 16 08.4"
    assert astro.dec_sexagesimal(-5.391) == "-05 23 27.6"
    assert astro.dec_sexagesimal(-0.00001) == "+00 00 00.0"
    assert astro.dec_sexagesimal(29.99999999) == "+30 00 00.0"


def test_light_header_units(make_service, tmp_path):
    from astropy.io import fits
    from sequencer import plan as P
    svc = make_service()
    step = P.loads('[[step]]\ntarget = "M31"\nexposure = 10\ncount = 1\ncentre = "none"\n').steps[0]
    pos = {"ra_h": 0.75, "dec": 41.42, "ha": 17.3, "alt": 68.4, "az": 217.4}
    h = svc._header(10, 80, "IR", "LIGHT", "2026-11-08T21:04:31Z", 9.9, step, pos, None, 17.3, None)
    p = str(tmp_path / "x.fits")
    imaging.save_fits(p, np.zeros((4, 4), np.uint16), h)
    hd = fits.getheader(p)
    assert hd["OBJCTRA"] == astro.ra_sexagesimal(step.ra) and hd["OBJCTRA"].startswith("00 42")
    assert hd["OBJCTDEC"] == astro.dec_sexagesimal(step.dec) and hd["OBJCTDEC"].startswith("+41 16")
    assert hd["RA"] == pytest.approx(step.ra, abs=1e-5) and hd["DEC"] == pytest.approx(step.dec, abs=1e-5)
    assert hd["EQUINOX"] == 2000.0
    assert hd["MNT_RA"] == pytest.approx(0.75)                              # mount pointing, hours
    # round trip: sexagesimal hours -> degrees
    hh, mm, ss = (float(v) for v in hd["OBJCTRA"].split())
    assert (hh + mm / 60 + ss / 3600) * 15 == pytest.approx(step.ra, abs=1e-4)
