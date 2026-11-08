"""Centring converges below the plan tolerance even when the residual after a sync is smaller than the
coarse axis tolerance (first real run 2026-11-08: 9-13' residuals left by a 0.3 deg axis tolerance)."""
from sequencer import plan as P

from conftest import ra_at_ha, wait_for


def test_solve_centring_converges_inside_tolerance(make_service):
    # small pointing error (0.2 deg): smaller than the coarse 0.3 deg axis tolerance
    svc = make_service(speed=300, world={"pointing_error": (0.2, -0.15)})
    pl = P.loads(f'[plan]\npark_at_end = false\n[[step]]\nra = {ra_at_ha(-30)}\ndec = 45\n'
                 'exposure = 10\ncount = 1\ncentre = "auto"\ncentre_tol_arcmin = 3\n')
    assert svc.cmd_start(pl)["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done", 120)
    st = svc.status()["plan"]["steps"][0]
    c = st.get("centring") or st.get("centre") or {}
    assert c.get("method") == "solve", st
    assert c.get("converged", True) and c["offset_arcmin"] <= 3.0, c
    tel = svc.rig.sim.telescope
    assert tel.firmware_gotos == 0 and not tel.cable_violation
