"""Views for the Darks page (web/darks.html): aggregates of the dark frame archive and a thinned
temperature log for the long graph ranges. Pure functions of files on disk, so they are cheap to test.

Archive: <data>/darkarchive/<camera>/index.jsonl, one line per kept frame (see Service._archive_dark).
"""
import datetime as dt
import json
import os

_NUM_SKIP = ("time", "activity", "filter", "exposing")


def _day(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d")


def archive_stats(data_dir, days=30.0, now=None):
    """Frames archived in the last `days`: per UTC day and exposure, and per key (camera, exposure,
    gain, offset, binning) with the temperature span. Counts only; never the frames themselves."""
    import time as _t
    now = _t.time() if now is None else now
    t_min = now - float(days) * 86400.0
    root = os.path.join(data_dir, "darkarchive")
    per_day, per_key, total, cameras, bad = {}, {}, 0, [], 0
    if os.path.isdir(root):
        cameras = sorted(c for c in os.listdir(root) if os.path.isfile(os.path.join(root, c, "index.jsonl")))
    for cam in cameras:
        with open(os.path.join(root, cam, "index.jsonl")) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    t = float(r.get("start_unix"))
                    exp = float(r["exposure"])
                except (ValueError, TypeError, KeyError):
                    bad += 1
                    continue
                if t < t_min:
                    continue
                total += 1
                d = per_day.setdefault(_day(t), {"date": _day(t), "frames": 0, "by_exposure": {}})
                d["frames"] += 1
                d["by_exposure"][f"{exp:g}"] = d["by_exposure"].get(f"{exp:g}", 0) + 1
                gain, off, b = r.get("gain"), r.get("offset", 0), r.get("binning", 1)
                k = (r.get("camera", cam), exp, gain, off, b)
                e = per_key.setdefault(k, {"camera": k[0], "exposure": exp, "gain": gain, "offset": off,
                                           "binning": b, "label": f"{k[0]} {exp:g}s g{gain} o{off} b{b}",
                                           "frames": 0, "temp_min": None, "temp_max": None,
                                           "first_unix": t, "last_unix": t})
                e["frames"] += 1
                e["first_unix"], e["last_unix"] = min(e["first_unix"], t), max(e["last_unix"], t)
                tc = r.get("temp_c")
                if isinstance(tc, (int, float)):
                    e["temp_min"] = tc if e["temp_min"] is None else min(e["temp_min"], tc)
                    e["temp_max"] = tc if e["temp_max"] is None else max(e["temp_max"], tc)
    keys = sorted(per_key.values(), key=lambda e: (e["camera"], e["gain"] or 0, e["exposure"]))
    return {"days": float(days), "since_unix": round(t_min, 1), "cameras": cameras, "frames": total,
            "per_day": [per_day[k] for k in sorted(per_day)], "keys": keys, "unreadable_lines": bad}


def thin(view, step):
    """Combine every `step` rows of a templog view into one: the first row's time, the mean of each
    numeric column (empty cells skipped), exposing = 1 if any row exposed, activity = the first that
    isn't idle. Keeps the long graph ranges small without losing the exposing periods."""
    step = int(step)
    rows = view.get("rows") or []
    if step <= 1 or not rows:
        return view
    out = []
    for i in range(0, len(rows), step):
        grp = rows[i:i + step]
        r = {"time": grp[0].get("time")}
        for c in view.get("columns") or grp[0].keys():
            if c in _NUM_SKIP:
                continue
            vs = [g.get(c) for g in grp if isinstance(g.get(c), (int, float))]
            r[c] = round(sum(vs) / len(vs), 3) if vs else None
        r["exposing"] = 1 if any(g.get("exposing") for g in grp) else 0
        busy = [g.get("activity") for g in grp if g.get("activity") not in (None, "idle")]
        r["activity"] = busy[0] if busy else grp[0].get("activity")
        r["filter"] = grp[-1].get("filter")
        out.append(r)
    return dict(view, rows=out, thinned=step)
