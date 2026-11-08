"""Panorama geometry and state for the SeeStar S30 Pro wide camera on an EQ wedge.

Frames are planned in alt/az, converted to hour angle / declination (time independent:
with tracking off, a fixed HA/Dec is a fixed alt/az). Footprints are computed in the
camera's tangent plane assuming one sensor axis lies along the declination axis
(true for a camera fixed to an EQ-mounted arm; which axis is CAMERA_LONG_AXIS, to
be confirmed from the first sky frame).

State lives in data/pano/state.json; the monitor reads data/pano/status.js
(a JS file, so the page works from file:// without a web server).
"""
import json
import math
import os
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANO_DIR = os.path.join(ROOT, "data", "pano")  # NOTE: data dir; see config.DATA_DIR
STATE = os.path.join(PANO_DIR, "state.json")
STATUS_JS = os.path.join(PANO_DIR, "status.js")

import config

LAT = config.LAT
LON = config.LON

# Wide camera: 2160 x 3840 px, 1.6 um, 6 mm lens (rectilinear assumed)
FOCAL_MM = 6.0
SENSOR_W_MM = 2160 * 1.6e-3  # short side
SENSOR_H_MM = 3840 * 1.6e-3  # long side
CAMERA_LONG_AXIS = "dec"     # "dec" or "ra": which mount axis the long sensor side follows
FOV_SHORT = math.degrees(2 * math.atan(SENSOR_W_MM / 2 / FOCAL_MM))
FOV_LONG = math.degrees(2 * math.atan(SENSOR_H_MM / 2 / FOCAL_MM))

D2R = math.pi / 180


def altaz_to_hadec(alt, az, lat=LAT):
    a, A, p = alt * D2R, az * D2R, lat * D2R
    sd = math.sin(p) * math.sin(a) + math.cos(p) * math.cos(a) * math.cos(A)
    dec = math.asin(max(-1, min(1, sd)))
    ha = math.atan2(-math.sin(A) * math.cos(a),
                    math.cos(p) * math.sin(a) - math.sin(p) * math.cos(a) * math.cos(A))
    return ha / D2R, dec / D2R


def hadec_to_altaz(ha, dec, lat=LAT):
    h, d, p = ha * D2R, dec * D2R, lat * D2R
    sa = math.sin(p) * math.sin(d) + math.cos(p) * math.cos(d) * math.cos(h)
    alt = math.asin(max(-1, min(1, sa)))
    az = math.atan2(-math.cos(d) * math.sin(h),
                    math.sin(d) * math.cos(p) - math.cos(d) * math.cos(h) * math.sin(p))
    return alt / D2R, (az / D2R) % 360


def _vec(ha, dec):
    h, d = ha * D2R, dec * D2R
    return np.array([math.cos(d) * math.cos(h), -math.cos(d) * math.sin(h), math.sin(d)])


def _hadec(v):
    v = v / np.linalg.norm(v)
    return math.atan2(-v[1], v[0]) / D2R, math.asin(max(-1, min(1, v[2]))) / D2R


def footprint(ha, dec, n_per_edge=8, long_axis=CAMERA_LONG_AXIS):
    """Frame outline as a list of [alt, az], walking the four edges."""
    c = _vec(ha, dec)
    # unit vectors: towards increasing Dec (north) and along the RA direction
    h, d = ha * D2R, dec * D2R
    n = np.array([-math.sin(d) * math.cos(h), math.sin(d) * math.sin(h), math.cos(d)])  # d(vec)/d(dec)
    e = np.cross(n, c)
    hx = math.tan(FOV_SHORT / 2 * D2R)
    hy = math.tan(FOV_LONG / 2 * D2R)
    if long_axis == "ra":
        hx, hy = hy, hx
    pts = []
    corners = [(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)]
    for i in range(4):
        (x0, y0), (x1, y1) = corners[i], corners[(i + 1) % 4]
        for k in range(n_per_edge):
            t = k / n_per_edge
            x, y = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
            h, d = _hadec(c + x * e + y * n)
            alt, az = hadec_to_altaz(h, d)
            pts.append([round(alt, 2), round(az, 2)])
    return pts


def plan_grid(rows=(5, 25, 45, 65), spacing_short=22.0, zenith=True):
    """Alt rows; azimuth step sized so the short side (worst case) overlaps ~30 %.

    Cable-safe ordering on the EQ wedge: azimuths are offset by half a step so no frame
    sits exactly on the lower meridian (HA +-180, the cable seam). Within each row frames
    are sorted by hour angle, alternating direction per row (serpentine), so the HA axis
    sweeps monotonically and never crosses the seam.
    """
    frames = []
    for r, alt in enumerate(rows):
        step = spacing_short / max(math.cos(alt * D2R), 0.2)
        n = max(1, math.ceil(360 / step))
        row = []
        for k in range(n):
            az = ((k + 0.5) * 360 / n) % 360
            ha, dec = altaz_to_hadec(alt, az)
            # cable window is unwrapped HA [-175, 175]; seam at the lower meridian
            ha = max(-175.0, min(175.0, ha))
            alt_, az_ = hadec_to_altaz(ha, dec)
            u = ha
            row.append({"row": r, "alt": round(alt_, 2), "az": round(az_, 2), "ha": ha, "dec": dec, "u": u})
        row.sort(key=lambda f: f["u"], reverse=(r % 2 == 1))
        frames += row
    if zenith:
        ha, dec = altaz_to_hadec(88, 180.0)
        frames.append({"row": len(rows), "alt": 88, "az": 180.0, "ha": ha, "dec": dec, "u": ha})
    out = []
    for i, f in enumerate(frames):
        ha, dec = f["ha"], f["dec"]
        out.append({
            "id": i, "row": f["row"], "status": "planned",
            "planned": {"alt": f["alt"], "az": f["az"], "ha": round(ha, 3), "dec": round(dec, 3), "ha_u": round(f["u"], 3)},
            "planned_footprint": footprint(ha, dec),
            "actual": None, "actual_footprint": None,
            "file": None, "time": None, "note": None,
        })
    return out


def sun_moon():
    try:
        from astropy.coordinates import AltAz, EarthLocation, get_body
        from astropy.time import Time
        import astropy.units as u
        loc = EarthLocation(lat=LAT * u.deg, lon=LON * u.deg)
        t = Time.now()
        fr = AltAz(obstime=t, location=loc)
        res = {}
        for b in ("sun", "moon"):
            aa = get_body(b, t, loc).transform_to(fr)
            res[b] = {"alt": round(float(aa.alt.deg), 2), "az": round(float(aa.az.deg), 2)}
        return res
    except Exception:
        return {}


def load_state():
    with open(STATE) as fh:
        return json.load(fh)


def save_state(state, message=None):
    os.makedirs(PANO_DIR, exist_ok=True)
    if message is not None:
        state["message"] = message
    state["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["updated_epoch"] = time.time()
    state["bodies"] = sun_moon()
    tmp = STATE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE)
    tmp = STATUS_JS + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("window.PANO_STATUS = " + json.dumps(state) + ";\n")
    os.replace(tmp, STATUS_JS)


def new_state(frames):
    return {
        "site": {"lat": LAT, "lon": LON},
        "camera": {"fov_short": round(FOV_SHORT, 2), "fov_long": round(FOV_LONG, 2),
                   "long_axis": CAMERA_LONG_AXIS},
        "frames": frames, "message": "plan created",
    }
