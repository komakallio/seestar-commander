"""Run the panorama grid from data/pano/state.json (create it with plan_grid.py).

Resumable: frames already 'acquired' are skipped. Updates status.js for monitor.html
after every step. Ctrl-C aborts the slew and leaves the scope where it is.

usage: capture.py --exp 10 --gain 100 [--limit N] [--only 0,1,2] [--retry-failed]
                  [--camsnap-every 6] [--track] [--home-at-end]
"""
import argparse
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(__file__))
import panolib as pl
from scope import Scope, camsnap

ap = argparse.ArgumentParser()
ap.add_argument("--exp", type=float, required=True)
ap.add_argument("--gain", type=int, default=100)
ap.add_argument("--limit", type=int, help="stop after N frames")
ap.add_argument("--only", help="comma-separated frame ids")
ap.add_argument("--retry-failed", action="store_true")
ap.add_argument("--camsnap-every", type=int, default=6, help="IP-camera frame every N frames (0=off)")
ap.add_argument("--track", action="store_true", help="keep sidereal tracking during exposures")
ap.add_argument("--home-at-end", action="store_true", help="slew back to HA 0 Dec 30 at the end")
a = ap.parse_args()

st = pl.load_state()
st.setdefault("log", [])


def log(msg):
    line = time.strftime("%H:%M:%SZ ", time.gmtime()) + msg
    print(line, flush=True)
    st["log"] = (st["log"] + [line])[-200:]
    pl.save_state(st, msg)


frames_dir = os.path.join(pl.PANO_DIR, "frames")
cam_dir = os.path.join(pl.PANO_DIR, "cam")
todo = [f for f in st["frames"]
        if f["status"] in ("planned", "current") or (a.retry_failed and f["status"] in ("failed", "skipped"))]
if a.only:
    ids = {int(x) for x in a.only.split(",")}
    todo = [f for f in todo if f["id"] in ids]
if a.limit:
    todo = todo[:a.limit]
st["capture"] = {"exp": a.exp, "gain": a.gain, "track": a.track}
log(f"start: {len(todo)} frames, exp {a.exp}s gain {a.gain}")

s = Scope(log=lambda m: log(m))
n = 0
try:
    for f in todo:
        p = f["planned"]
        f["status"] = "current"
        log(f"#{f['id']} row {f['row']}: slew to alt {p['alt']} az {p['az']} (HA {p['ha']:.1f} Dec {p['dec']:.1f})")
        try:
            pos = s.slew_hadec(p["ha"], p["dec"], track_after=a.track)
            date_obs = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
            pos_start = pos
            data = s.expose(a.exp, a.gain)
            pos_end = s.position()
            name = f"frame_{f['id']:03d}.fits"
            s.save_fits(os.path.join(frames_dir, name), data, a.exp, a.gain, pos_start, date_obs,
                        {"FRAMEID": f["id"], "ROW": f["row"], "PLAN_ALT": p["alt"], "PLAN_AZ": p["az"],
                         "END_ALT": pos_end["alt"], "END_AZ": pos_end["az"]})
            f.update(status="acquired", time=date_obs, file=f"frames/{name}",
                     actual={k: round(v, 3) for k, v in pos_start.items()},
                     actual_footprint=pl.footprint(pos_start["ha"], pos_start["dec"]), note=None)
            d_alt, d_az = pos_start["alt"] - p["alt"], (pos_start["az"] - p["az"] + 180) % 360 - 180
            log(f"#{f['id']} acquired: alt {pos_start['alt']:.2f} az {pos_start['az']:.2f} "
                f"(d {d_alt:+.2f}/{d_az:+.2f}) HA {pos_start['ha']:.1f}")
        except KeyboardInterrupt:
            raise
        except Exception as e:
            f.update(status="failed", note=f"{type(e).__name__}: {e}")
            log(f"#{f['id']} FAILED: {e}")
            traceback.print_exc()
            if "slew" in str(e).lower() or "limit" in str(e).lower():
                raise SystemExit("stopping on slew/limit error")
        n += 1
        if a.camsnap_every and n % a.camsnap_every == 1:
            try:
                camsnap(os.path.join(cam_dir, f"after_{f['id']:03d}.jpg"))
            except Exception as e:
                log(f"camsnap failed: {e}")
    if a.home_at_end:
        log("returning to HA 0 Dec 30")
        s.slew_hadec(0, 30)
    log(f"done: {sum(x['status'] == 'acquired' for x in st['frames'])}/{len(st['frames'])} acquired")
except KeyboardInterrupt:
    s.abort()
    for f in st["frames"]:
        if f["status"] == "current":
            f["status"] = "planned"
    log("interrupted by user, slew aborted")
