"""Darks page (web/darks.html): served, linked from the main page, and its endpoints
(/api/darks/archive aggregates, /api/templog step thinning)."""
import json
import os
import urllib.error
import urllib.request

import pytest

from sequencer import darkview
from sequencer.httpapi import serve

from conftest import wait_for

DAY = 86400.0
T0 = 1_762_600_000.0          # 2025-11-08 UTC, any fixed time


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return r.headers.get("Content-Type", ""), r.read()


def _write_index(data_dir, cam, recs):
    d = os.path.join(data_dir, "darkarchive", cam)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "index.jsonl"), "w") as fh:
        for r in recs:
            fh.write((r if isinstance(r, str) else json.dumps(r)) + "\n")


def test_archive_stats(tmp_path):
    rec = lambda t, exp, temp: {"camera": "tele", "exposure": exp, "gain": 200, "offset": 0, "binning": 1,
                                "start_unix": t, "temp_c": temp}
    _write_index(str(tmp_path), "tele", [rec(T0 - 40 * DAY, 60, 5.0),           # outside 30 d
                                         rec(T0 - 2 * DAY, 60, 11.0), rec(T0 - 2 * DAY + 70, 60, 12.5),
                                         rec(T0 - 1 * DAY, 30, 9.0), "not json", rec(T0, 120, None)])
    a = darkview.archive_stats(str(tmp_path), 30, now=T0)
    assert a["frames"] == 4 and a["cameras"] == ["tele"] and a["unreadable_lines"] == 1
    assert [d["frames"] for d in a["per_day"]] == [2, 1, 1]
    assert a["per_day"][0]["by_exposure"] == {"60": 2}
    assert [d["date"] for d in a["per_day"]] == sorted(d["date"] for d in a["per_day"])
    k60 = next(k for k in a["keys"] if k["exposure"] == 60)
    assert k60["frames"] == 2 and k60["temp_min"] == 11.0 and k60["temp_max"] == 12.5
    assert k60["label"] == "tele 60s g200 o0 b1"
    assert next(k for k in a["keys"] if k["exposure"] == 120)["temp_min"] is None
    assert darkview.archive_stats(str(tmp_path / "none"), 30, now=T0)["frames"] == 0


def test_thin_keeps_exposing_periods():
    cols = ["time", "sensor_c", "wide_c", "outside", "sun_alt", "activity", "exposing", "filter"]
    rows = [{"time": f"t{i}", "sensor_c": 10.0 + i, "wide_c": None, "outside": 2.0, "sun_alt": -30.0,
             "activity": "darks_build" if i == 4 else "idle", "exposing": 1 if i == 4 else 0, "filter": "IR"}
            for i in range(10)]
    v = darkview.thin({"columns": cols, "rows": rows}, 3)
    assert v["thinned"] == 3 and len(v["rows"]) == 4
    r = v["rows"][1]
    assert r["time"] == "t3" and r["sensor_c"] == pytest.approx(14.0) and r["wide_c"] is None
    assert r["exposing"] == 1 and r["activity"] == "darks_build"
    assert v["rows"][0]["exposing"] == 0 and v["rows"][0]["activity"] == "idle"
    assert darkview.thin({"columns": cols, "rows": rows}, 1)["rows"] is rows


def test_darks_page_and_endpoints(make_service):
    svc = make_service(speed=300)
    svc.rig.sim.camera.set_temp(10.0)
    assert svc.cmd_darks_build(exposure=20, count=3)["ok"]
    assert wait_for(lambda: svc.darklib.entries(), 60) and wait_for(svc.idle, 30)
    assert wait_for(lambda: len(svc.templog.rows()) >= 4, 30)
    srv = serve(svc, host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        ct, html = _get(base, "/darks")
        assert ct.startswith("text/html")
        for el in (b'id="grid"', b'id="tl"', b'id="lib"', b'id="arcday"', b'id="harvest"', b"/api/darks/harvest",
                   b"/api/darks/archive", b"/api/templog", b"/api/events", b'href="/"', b"phase 5"):
            assert el in html, el
        assert b"cdn" not in html.lower() and b"<script src" not in html
        _, main = _get(base, "/")
        assert b'href="/darks"' in main

        _, body = _get(base, "/api/darks/archive?days=30")
        a = json.loads(body)
        assert a["frames"] == 3 and a["cameras"] == ["tele"] and len(a["per_day"]) == 1
        assert a["per_day"][0]["by_exposure"] == {"20": 3}
        assert a["keys"][0]["label"] == "tele 20s g200 o0 b1" and a["keys"][0]["temp_min"] is not None
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(base, "/api/darks/archive?days=x")
        assert e.value.code == 400

        full = json.loads(_get(base, "/api/templog?hours=2")[1])
        thin = json.loads(_get(base, "/api/templog?hours=2&step=2")[1])
        assert thin["thinned"] == 2 and len(thin["rows"]) == (len(full["rows"]) + 1) // 2
        assert set(thin["rows"][0]) >= {"time", "sensor_c", "exposing", "activity"}
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(base, "/api/templog?hours=2&step=x")
        assert e.value.code == 400

        # what the page reads from the status: the dark library with policy and entries
        dl = json.loads(_get(base, "/api/status")[1])["darklib"]
        assert {"temp", "policy", "entries", "missing", "pool"} <= set(dl) and "pools" in dl["pool"]
        assert {"temp_bin", "temp_min", "temp_max", "frames", "age_days", "status", "label"} <= set(dl["entries"][0])
        assert {"temp_bin_c", "max_age_days", "expiring_days"} <= set(dl["policy"])
    finally:
        srv.shutdown()


def test_harvest_view_has_what_the_page_reads(make_service):
    """The page's harvestNorm/coverageHarvest read these fields of GET /api/darks/harvest."""
    svc = make_service(speed=300)
    svc.rig.sim.camera.set_temp(10.0)
    srv = serve(svc, host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        h = json.loads(_get(base, "/api/darks/harvest?log=200")[1])
        assert {"enabled", "enabled_source", "state", "headline", "reason", "since", "sensor", "current", "blockers",
                "holdoff_until", "last_frame", "counters", "coverage", "sentinel", "stall", "log"} <= set(h)
        assert {"stalled", "reason", "hours_since_frame"} <= set(h["stall"])
        assert {"temp", "bin", "bin_pos", "trend_c_h"} <= set(h["sensor"])
        for k in ("today", "week"):
            assert {"frames", "masters", "harvest_h", "preempted_h", "bins"} <= set(h["counters"][k])
        cov = h["coverage"]
        assert {"bins", "rows", "percent", "expiring_7d"} <= set(cov) and cov["rows"]
        row = cov["rows"][0]
        assert {"label", "kind", "exposure", "gain", "offset", "cells"} <= set(row)
        assert row["label"].split(" ")[0] == "tele"               # the page takes the camera from the label
        if row["cells"]:
            assert {"temp_bin", "reached", "state", "pool_n", "count", "building"} <= set(row["cells"][0])
        assert {"label", "every_days", "common_bin", "last_age_days"} <= set(h["sentinel"])
        brief = json.loads(_get(base, "/api/status")[1])["harvest"]
        assert {"enabled", "state", "headline"} <= set(brief)
    finally:
        srv.shutdown()
