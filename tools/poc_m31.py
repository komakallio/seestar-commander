"""Proof of concept: M31 with the SeeStar S30 Pro telephoto over Alpaca, run on an always-on host on the scope's LAN.

Phases (run one at a time and check in between):
  goto    cable-safe move to M31, tracking on, dew heater on, centre M31 on the telephoto
  test    one test light: HFR, eccentricity, star count, preview
  lights  N x EXP s lights with per-frame QA and drift from registration
  darks   wheel to Dark, N darks at the same settings, wheel back
  stack   dark-subtract, debayer, register, sigma-clip stack, stretched PNG
  home    tracking off, RA back to the home angle (HA -90), Dec folded to -89.5
  roof    close the roof through the configured Alpaca Dome (scope parked first)

Status for the browser: data/poc/status.json + latest.jpg + stack.png (serve data/poc).
"""
import argparse
import glob
import json
import math
import os
import sys
import time

import numpy as np
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.time import Time
import astropy.units as u
from PIL import Image
from scipy import ndimage as ndi

sys.path.insert(0, os.path.dirname(__file__))
import panolib as pl
from scope import Scope, camsnap, lst_deg, wrap180
from alpaca.camera import Camera
from alpaca.filterwheel import FilterWheel
from alpaca.switch import Switch
from alpaca.dome import Dome

import config
HOST = config.SEESTAR
ROOF_ALPACA = config.ROOF_ALPACA
M31 = SkyCoord("00h42m44.3s +41d16m09s")
POC = os.path.join(pl.ROOT, "data", "poc")
PIX_ARCSEC = 2.9e-6 / 160e-3 * 206265  # 2.9 um at 160 mm = 3.74"/px


def status(**kw):
    os.makedirs(POC, exist_ok=True)
    p = os.path.join(POC, "status.json")
    try:
        s = json.load(open(p))
    except Exception:
        s = {"log": []}
    s.update(kw)
    if "msg" in kw:
        s["log"] = (s.get("log", []) + [time.strftime("%H:%M:%S ") + kw["msg"]])[-300:]
        print(kw["msg"], flush=True)
    s["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    json.dump(s, open(p + ".tmp", "w"), indent=1)
    os.replace(p + ".tmp", p)


def green(d):
    d = d.astype(np.float32)
    return (d[0::2, 0::2] + d[1::2, 1::2]) / 2  # GRBG


def stars(g, n=80):
    """Bright unsaturated stars: list of (y, x, flux, hfr, ecc)."""
    bg = ndi.median_filter(g[::8, ::8], 9)
    bg = ndi.zoom(bg, (g.shape[0] / bg.shape[0], g.shape[1] / bg.shape[1]), order=1)[:g.shape[0], :g.shape[1]]
    r = g - bg
    sig = 1.4826 * np.median(np.abs(r - np.median(r))) + 1e-6
    mx = ndi.maximum_filter(r, 7)
    cand = np.argwhere((r == mx) & (r > 15 * sig) & (g < 60000))
    b = 8
    cand = [p for p in cand if b <= p[0] < g.shape[0] - b and b <= p[1] < g.shape[1] - b]
    cand.sort(key=lambda p: -r[p[0], p[1]])
    yy, xx = np.mgrid[-b:b + 1, -b:b + 1]
    out = []
    for y, x in cand[:n]:
        s = np.clip(r[y - b:y + b + 1, x - b:x + b + 1], 0, None)
        t = s.sum()
        if t <= 0:
            continue
        cy, cx = (s * yy).sum() / t, (s * xx).sum() / t
        dy, dx = yy - cy, xx - cx
        hfr = (s * np.hypot(dy, dx)).sum() / t
        myy, mxx, mxy = (s * dy * dy).sum() / t, (s * dx * dx).sum() / t, (s * dx * dy).sum() / t
        tr = myy + mxx
        det = math.sqrt(max((mxx - myy) ** 2 + 4 * mxy * mxy, 0))
        l1, l2 = (tr + det) / 2, max((tr - det) / 2, 1e-6)
        out.append((y + cy, x + cx, t, hfr, math.sqrt(1 - l2 / l1)))
    return out


def qa(g):
    st = stars(g)
    if not st:
        return {"stars": 0}
    a = np.array(st)
    return {"stars": len(st), "hfr_px": round(float(np.median(a[:, 3])) * 2, 2),
            "ecc": round(float(np.median(a[:, 4])), 2), "bg": round(float(np.median(g)), 1)}


def preview(g, path, size=720):
    lo, hi = np.percentile(g, 5), np.percentile(g, 99.7)
    s = np.clip((g - lo) / max(hi - lo, 1), 0, 1) ** 0.45
    im = Image.fromarray((s * 255).astype(np.uint8))
    im.thumbnail((size, size))
    im.save(path, quality=85)


def shift(ref, g):
    """Translation (dy, dx) of g relative to ref: phase correlation with sub-pixel peak."""
    A = ref - ndi.gaussian_filter(ref, 20)
    B = g - ndi.gaussian_filter(g, 20)
    F = np.fft.fft2(A) * np.conj(np.fft.fft2(B))
    c = np.fft.ifft2(F / (np.abs(F) ** 0.5 + 1e-9)).real  # partial whitening: robust to sub-px shifts
    y, x = np.unravel_index(np.argmax(c), c.shape)

    def sub(cm, c0, cp):
        d = cm - 2 * c0 + cp
        return 0.5 * (cm - cp) / d if d != 0 else 0.0
    H, W = c.shape
    fy = y + sub(c[(y - 1) % H, x], c[y, x], c[(y + 1) % H, x])
    fx = x + sub(c[y, (x - 1) % W], c[y, x], c[y, (x + 1) % W])
    if fy > H / 2:
        fy -= H
    if fx > W / 2:
        fx -= W
    return -fy, -fx


class Tele:
    def __init__(self):
        self.s = Scope()
        self.c = Camera(HOST, 0)
        self.fw = FilterWheel(HOST, 0)
        for d in (self.c, self.fw):
            d.Connected = True

    def expose(self, exp, gain):
        c = self.c
        c.Gain = int(gain)
        c.StartExposure(float(exp), True)
        t0 = time.time()
        time.sleep(min(exp, 2))
        while not c.ImageReady:
            if time.time() - t0 > exp + 120:
                raise RuntimeError("exposure timeout")
            time.sleep(0.5)
        return np.array(c.ImageArray).T.astype(np.uint16)

    def save(self, path, d, exp, gain, kind):
        p = self.s.position()
        h = fits.Header()
        h["DATE-OBS"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        h["EXPTIME"], h["GAIN"], h["IMAGETYP"] = exp, gain, kind
        h["BAYERPAT"] = "GRBG"
        h["CCD-TEMP"] = self.c.CCDTemperature
        h["FILTER"] = self.fw.Names[self.fw.Position]
        h["OBJECT"] = "M31"
        for k, key in (("ra_h", "MNT_RA"), ("dec", "MNT_DEC"), ("ha", "MNT_HA"), ("alt", "MNT_ALT"), ("az", "MNT_AZ")):
            h[key] = p[k]
        fits.writeto(path, d, h, overwrite=True)

    def set_filter(self, name):
        i = self.fw.Names.index(name)
        self.fw.Position = i
        t0 = time.time()
        while self.fw.Position != i:
            if time.time() - t0 > 30:
                raise RuntimeError("filter wheel timeout")
            time.sleep(0.5)


def m31_hadec():
    ra = M31.ra.deg  # J2000 is close enough for a 2x4 deg field; centring fixes the rest
    return wrap180(lst_deg() - ra), M31.dec.deg


def phase_goto(t, a):
    try:
        sw = Switch(HOST, 0)
        sw.Connected = True
        for i in range(sw.MaxSwitch):
            if "dew" in sw.GetSwitchName(i).lower():
                sw.SetSwitch(i, True)
                status(msg=f"dew heater ({sw.GetSwitchName(i)}) on")
    except Exception as e:
        status(msg=f"dew heater: {e}")
    ha, dec = m31_hadec()
    status(phase="goto", msg=f"moving to M31: HA {ha:.1f} Dec {dec:.1f}")
    camsnap(os.path.join(POC, "cam_goto_before.jpg"))
    p = t.s.move_hadec(ha, dec)
    status(msg=f"at alt {p['alt']:.1f} az {p['az']:.1f} HA {p['ha']:.1f}; tracking on")
    t.s.t.Tracking = True
    camsnap(os.path.join(POC, "cam_goto_after.jpg"))
    # centre: M31 core = brightest extended blob on short telephoto frames. Learn the image
    # direction of +1 deg Dec and +1 deg HA with two probe moves, then solve the 2x2 system.
    def core():
        g = green(t.expose(5, a.gain))
        preview(g, os.path.join(POC, "latest.jpg"))
        sm = ndi.gaussian_filter(g, 25)
        y, x = np.unravel_index(np.argmax(sm), sm.shape)
        return np.array([y, x], float), g.shape

    def nudge(dha, ddec):
        p0 = t.s.position()
        t.s.t.Tracking = False
        t.s.move_hadec(p0["ha"] + dha, p0["dec"] + ddec)
        t.s.t.Tracking = True

    c0, shp = core()
    nudge(0, 0.5)
    c1, _ = core()
    nudge(0.5 / max(math.cos(math.radians(M31.dec.deg)), 0.2), 0)
    c2, _ = core()
    v_dec = (c1 - c0) / 0.5
    v_ha = (c2 - c1) / (0.5 / max(math.cos(math.radians(M31.dec.deg)), 0.2))
    status(msg=f"probe: +1 deg Dec -> {v_dec.round(1)} superpx, +1 deg HA -> {v_ha.round(1)} superpx")
    M = np.array([v_dec, v_ha]).T
    cur = c2
    for it in range(3):
        centre = np.array([shp[0] / 2, shp[1] / 2])
        off = centre - cur
        off_arcmin = np.hypot(*off) * 2 * PIX_ARCSEC / 60
        status(msg=f"centring {it + 1}: core offset {off_arcmin:.1f}'")
        if off_arcmin < 3:
            break
        ddec, dha = np.linalg.solve(M, off)
        nudge(dha, ddec)
        status(msg=f"correction dDec {ddec:+.2f}, dHA {dha:+.2f} deg")
        cur, _ = core()
    status(phase="goto done")


def phase_test(t, a):
    d = t.expose(a.exp, a.gain)
    g = green(d)
    q = qa(g)
    t.save(os.path.join(POC, "test.fits"), d, a.exp, a.gain, "LIGHT")
    preview(g, os.path.join(POC, "latest.jpg"))
    status(phase="test", test=q, msg=f"test {a.exp}s gain {a.gain}: {q}")


def phase_lights(t, a):
    os.makedirs(os.path.join(POC, "lights"), exist_ok=True)
    ref = None
    drift = []
    t0 = time.time()
    for i in range(a.n):
        d = t.expose(a.exp, a.gain)
        g = green(d)
        q = qa(g)
        path = os.path.join(POC, "lights", f"m31_{i:03d}.fits")
        t.save(path, d, a.exp, a.gain, "LIGHT")
        if ref is None:
            ref = g
            dy = dx = 0.0
        else:
            dy, dx = shift(ref, g)
        mins = (time.time() - t0) / 60
        drift.append({"i": i, "min": round(mins, 2), "dx_as": round(dx * 2 * PIX_ARCSEC, 1),
                      "dy_as": round(dy * 2 * PIX_ARCSEC, 1), **q})
        rate = ""
        if len(drift) >= 3:
            m = np.array([[x["min"], x["dx_as"], x["dy_as"]] for x in drift])
            rx = np.polyfit(m[:, 0], m[:, 1], 1)[0]
            ry = np.polyfit(m[:, 0], m[:, 2], 1)[0]
            rate = f"; drift {math.hypot(rx, ry):.1f}\"/min (x {rx:+.1f}, y {ry:+.1f})"
        preview(g, os.path.join(POC, "latest.jpg"))
        status(phase="lights", frame=i + 1, of=a.n, drift=drift,
               msg=f"light {i + 1}/{a.n}: stars {q.get('stars')} HFR {q.get('hfr_px')} ecc {q.get('ecc')} "
                   f"shift ({dx * 2 * PIX_ARCSEC:+.0f}\", {dy * 2 * PIX_ARCSEC:+.0f}\"){rate}")


def phase_darks(t, a):
    os.makedirs(os.path.join(POC, "darks"), exist_ok=True)
    t.set_filter("Dark")
    status(phase="darks", msg="filter wheel on Dark")
    try:
        for i in range(a.n):
            d = t.expose(a.exp, a.gain)
            t.save(os.path.join(POC, "darks", f"dark_{i:03d}.fits"), d, a.exp, a.gain, "DARK")
            status(msg=f"dark {i + 1}/{a.n}: median {np.median(d):.0f}")
    finally:
        t.set_filter("IR")
        status(msg="filter wheel back on IR")


def phase_stack(a):
    darks = sorted(glob.glob(os.path.join(POC, "darks", "*.fits")))
    lights = sorted(glob.glob(os.path.join(POC, "lights", "*.fits")))
    md = np.median([fits.getdata(f).astype(np.float32) for f in darks], axis=0) if darks else 0
    status(phase="stack", msg=f"stacking {len(lights)} lights with a {len(darks)}-frame master dark")
    rgb = []
    ref = None
    for f in lights:
        d = fits.getdata(f).astype(np.float32) - md
        r, g1, b, g2 = d[0::2, 1::2], d[0::2, 0::2], d[1::2, 0::2], d[1::2, 1::2]
        im = np.stack([r, (g1 + g2) / 2, b], -1)
        if ref is None:
            ref = im[..., 1]
            rgb.append(im)
            continue
        dy, dx = shift(ref, im[..., 1])
        rgb.append(ndi.shift(im, (-dy, -dx, 0), order=1))
    cube = np.array(rgb)
    med = np.median(cube, 0)
    sd = 1.4826 * np.median(np.abs(cube - med), 0) + 1e-3
    w = np.abs(cube - med) < 3 * sd
    st = (cube * w).sum(0) / np.maximum(w.sum(0), 1)
    fits.writeto(os.path.join(POC, "stack.fits"), st.transpose(2, 0, 1).astype(np.float32), overwrite=True)
    # stretch: background neutralise + asinh
    out = np.zeros_like(st)
    for k in range(3):
        c = st[..., k]
        bgv = np.percentile(c, 20)
        c = np.clip(c - bgv, 0, None)
        out[..., k] = c / (np.percentile(c, 99.95) + 1e-6)
    out = np.arcsinh(out * 25) / np.arcsinh(25)
    Image.fromarray((np.clip(out, 0, 1) * 255).astype(np.uint8)).save(os.path.join(POC, "stack.png"))
    status(msg="stack.png written")


def phase_home(t, a):
    from scope import SECONDARY
    status(phase="home", msg="tracking off, RA to home angle (HA -90), Dec down to about -89.5")
    t.s.t.Tracking = False
    t.s.move_hadec(-90.0, -84.9)
    try:
        t.s.t.MoveAxis(SECONDARY, -2.0)
        time.sleep(2.2)
    finally:
        t.s.t.MoveAxis(SECONDARY, 0.0)
    time.sleep(1)
    t.s.ha_unwrapped()
    p = t.s.position()
    camsnap(os.path.join(POC, "cam_home.jpg"))
    status(msg=f"home: HA {p['ha']:.1f} Dec {p['dec']:.1f} alt {p['alt']:.1f}")


def phase_roof(a):
    """Close the roof through the configured Alpaca Dome (optional). Park the scope first."""
    if not ROOF_ALPACA:
        raise SystemExit("no [roof] alpaca configured")
    dome = Dome(ROOF_ALPACA, config.ROOF_DOME)
    dome.Connected = True
    status(phase="roof", msg=f"dome ShutterStatus {dome.ShutterStatus} (0 open, 1 closed)")
    camsnap(os.path.join(POC, "cam_roof_before.jpg"))
    dome.CloseShutter()
    status(msg="CloseShutter sent")
    t0 = time.time()
    while time.time() - t0 < 300 and not (dome.ShutterStatus == 1 and not dome.Slewing):
        status(roof=f"ShutterStatus {dome.ShutterStatus}")
        time.sleep(5)
    camsnap(os.path.join(POC, "cam_roof_after.jpg"))
    status(msg=f"roof: ShutterStatus {dome.ShutterStatus}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["goto", "test", "lights", "darks", "stack", "home", "roof"])
    ap.add_argument("--exp", type=float, default=60)
    ap.add_argument("--gain", type=int, default=80)
    ap.add_argument("-n", type=int, default=12)
    a = ap.parse_args()
    if a.phase == "stack":
        phase_stack(a)
    elif a.phase == "roof":
        phase_roof(a)
    else:
        t = Tele()
        {"goto": phase_goto, "test": phase_test, "lights": phase_lights,
         "darks": phase_darks, "home": phase_home}[a.phase](t, a)
