"""Frame QA, registration, previews and a running dark-subtracted, sigma-clipped stack.

Raw frames are GRBG Bayer, uint16, [y][x]. QA and registration work on green superpixels
(half resolution). Generalised from tools/poc_m31.py.
"""
import math
import os

import numpy as np
from astropy.io import fits
from PIL import Image
from scipy import ndimage as ndi


def green(d):
    d = d.astype(np.float32)
    return (d[0::2, 0::2] + d[1::2, 1::2]) / 2  # GRBG


def rgb(d):
    d = d.astype(np.float32)
    r, g1, b, g2 = d[0::2, 1::2], d[0::2, 0::2], d[1::2, 0::2], d[1::2, 1::2]
    return np.stack([r, (g1 + g2) / 2, b], -1)


def stars(g, n=80):
    """Bright unsaturated stars in a green-superpixel image: list of (y, x, flux, hfr, ecc)."""
    step = max(1, min(8, min(g.shape) // 64))
    bg = ndi.median_filter(g[::step, ::step], 9)
    bg = ndi.zoom(bg, (g.shape[0] / bg.shape[0], g.shape[1] / bg.shape[1]), order=1)
    bg = np.pad(bg, ((0, max(0, g.shape[0] - bg.shape[0])), (0, max(0, g.shape[1] - bg.shape[1]))),
                mode="edge")[:g.shape[0], :g.shape[1]]
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
        out.append((y + cy, x + cx, t, hfr, math.sqrt(max(0.0, 1 - l2 / l1))))
    return out


def qa(g, st=None):
    """Star count, median HFR (full-resolution px), median eccentricity, background."""
    st = stars(g) if st is None else st
    if not st:
        return {"stars": 0, "hfr_px": None, "ecc": None, "bg": round(float(np.median(g)), 1)}
    a = np.array(st)
    return {"stars": len(st), "hfr_px": round(float(np.median(a[:, 3])) * 2, 2),
            "ecc": round(float(np.median(a[:, 4])), 2), "bg": round(float(np.median(g)), 1)}


def qa_ok(q, rules):
    """(ok, reasons) against a plan.QA."""
    why = []
    if q["stars"] < rules.min_stars:
        why.append(f"stars {q['stars']} < {rules.min_stars}")
    if q["hfr_px"] is not None and q["hfr_px"] > rules.max_hfr:
        why.append(f"HFR {q['hfr_px']} > {rules.max_hfr}")
    if q["ecc"] is not None and q["ecc"] > rules.max_ecc:
        why.append(f"ecc {q['ecc']} > {rules.max_ecc}")
    return not why, why


def stretch(g):
    lo, hi = np.percentile(g, 5), np.percentile(g, 99.7)
    return np.clip((g - lo) / max(hi - lo, 1e-6), 0, 1) ** 0.45


def preview(g, path, size=720):
    im = Image.fromarray((stretch(g) * 255).astype(np.uint8))
    im.thumbnail((size, size))
    _save_atomic(im, path)


def _save_atomic(im, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    im.save(tmp, format="JPEG" if path.endswith(".jpg") else "PNG", quality=85)
    os.replace(tmp, path)


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


def drift_rate(drift):
    """Linear drift ("/min) from a list of {"min", "dx_as", "dy_as"}; None with < 3 points."""
    if len(drift) < 3:
        return None
    m = np.array([[x["min"], x["dx_as"], x["dy_as"]] for x in drift], float)
    if np.ptp(m[:, 0]) <= 0:
        return None
    rx = np.polyfit(m[:, 0], m[:, 1], 1)[0]
    ry = np.polyfit(m[:, 0], m[:, 2], 1)[0]
    return {"x": round(float(rx), 2), "y": round(float(ry), 2), "total": round(float(math.hypot(rx, ry)), 2)}


class RunningStack:
    """Dark-subtract, debayer to RGB superpixels, register (translation) on the first frame, then a
    running per-pixel mean with kappa-sigma rejection once 3 frames are in (Welford statistics)."""

    def __init__(self, dark=None, kappa=3.0):
        self.dark = dark
        self.kappa = kappa
        self.ref = None
        self.n = 0
        self.cnt = self.mean = self.m2 = None
        self.rejected = 0.0
        self.n_uncal = 0

    def add(self, raw, offset=None, dark=None):
        """dark: per-frame master (overrides the stack's own); a frame without one counts as uncalibrated."""
        d = raw.astype(np.float32)
        dk = dark if dark is not None else self.dark
        if dk is not None and dk.shape == d.shape:
            d = d - dk
        else:
            self.n_uncal += 1
        im = rgb(d)
        g = im[..., 1]
        if offset is not None:          # caller registered against its own reference
            dy, dx = offset
        elif self.ref is None:
            self.ref = g
            dy = dx = 0.0
        else:
            dy, dx = shift(self.ref, g)
        if dy or dx:
            im = ndi.shift(im, (-dy, -dx, 0), order=1, mode="nearest")
        if self.mean is None:
            self.cnt = np.zeros(im.shape, np.float32)
            self.mean = np.zeros(im.shape, np.float32)
            self.m2 = np.zeros(im.shape, np.float32)
        if self.n >= 3:
            sd = np.sqrt(self.m2 / np.maximum(self.cnt - 1, 1))
            keep = np.abs(im - self.mean) <= self.kappa * sd + 1e-3
            self.rejected = float(1 - keep.mean())
        else:
            keep = np.ones(im.shape, bool)
        self.cnt += keep
        delta = np.where(keep, im - self.mean, 0)
        self.mean += delta / np.maximum(self.cnt, 1)
        self.m2 += np.where(keep, delta * (im - self.mean), 0)
        self.n += 1
        return dy, dx

    def image(self):
        return self.mean

    def save(self, fits_path=None, png_path=None):
        if self.mean is None:
            return
        if fits_path:
            os.makedirs(os.path.dirname(fits_path), exist_ok=True)
            h = fits.Header()
            h["NCOMBINE"] = self.n
            h["NUNCAL"] = self.n_uncal
            fits.writeto(fits_path, self.mean.transpose(2, 0, 1).astype(np.float32), h, overwrite=True)
        if png_path:
            out = np.zeros_like(self.mean)
            for k in range(3):
                c = self.mean[..., k]
                c = np.clip(c - np.percentile(c, 20), 0, None)
                out[..., k] = c / (np.percentile(c, 99.95) + 1e-6)
            out = np.arcsinh(out * 25) / np.arcsinh(25)
            im = Image.fromarray((np.clip(out, 0, 1) * 255).astype(np.uint8))
            im.thumbnail((900, 900))
            _save_atomic(im, png_path)


def save_fits(path, data, header):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    h = fits.Header()
    for k, v in header.items():
        if v is None:
            continue
        if isinstance(v, float) and not math.isfinite(v):
            continue
        h[k] = v
    fits.writeto(path, data, h, overwrite=True)
