"""Temperature log (sequencer/templog.py) and the darks build's frame archive, on the simulator."""
import json
import os
import urllib.error
import urllib.request

import pytest
from astropy.io import fits

from sequencer.darklib import Key
from sequencer.httpapi import serve
from sequencer.templog import TempLogConfig, dig, extract, read_csv

from conftest import wait_for

DOC = {"status": "RAIN=0,TEMP1=5.00,HUM=81", "piers": {"West pier": {"park_sensors": [
    {"temperature_c": 6.9}, {"temperature_c": "7.25"}]}}, "list": [[1, 2], [3, 4]], "flag": True}


def test_dig_and_extract():
    assert dig(DOC, "piers.West pier.park_sensors[0].temperature_c") == 6.9
    assert dig(DOC, "list[1][0]") == 3
    assert dig(DOC, "") is DOC
    assert extract(DOC, "piers.West pier.park_sensors[1].temperature_c") == 7.25    # numeric string
    assert extract(DOC, "status", r"TEMP1=([-0-9.]+)") == 5.0
    assert extract({"s": "TEMP1=-12.5"}, "s", r"TEMP1=([-0-9.]+)") == -12.5
    assert extract(DOC, "status", r"TEMP9=([-0-9.]+)") is None                     # no match
    assert extract(DOC, "status") is None                                           # not a number
    assert extract(DOC, "piers.East pier.park_sensors[0].temperature_c") is None    # missing key
    assert extract(DOC, "piers.West pier.park_sensors[5].temperature_c") is None    # index out of range
    assert extract(DOC, "status[0]") is None                                        # not a list
    assert extract(DOC, "flag") is None


def test_config():
    c = TempLogConfig.from_dict({"interval_s": 30, "sources": {
        "outside": {"url": "http://a.invalid/x", "path": "status", "regex": "TEMP1=([-0-9.]+)"},
        "inside": {"url": "http://a.invalid/x", "path": "piers.West pier.park_sensors[0].temperature_c"}}})
    assert c.interval_s == 30 and [n for n, _ in c.sources] == ["outside", "inside"]
    assert TempLogConfig.from_dict(None).interval_s == 60 and TempLogConfig.from_dict(None).sources == []


SOURCES = {"outside": {"url": "http://site.invalid/status", "path": "status", "regex": r"TEMP1=([-0-9.]+)"},
           "inside": {"url": "http://site.invalid/status", "path": "piers.West pier.park_sensors[0].temperature_c"},
           "dead": {"url": "http://127.0.0.1:9/nothing", "path": "t"}}


def _svc(make_service, interval=60):
    svc = make_service(speed=300, extra={"templog": {"interval_s": interval, "sources": SOURCES}})
    real = svc.templog.fetch
    fetched = []

    def fetch(url):
        fetched.append(url)
        if "site.invalid" in url:
            return DOC
        return real(url)                     # the dead one: connection refused, fast
    svc.templog.fetch = fetch
    return svc, fetched


def test_rows_written_and_api(make_service):
    svc, fetched = _svc(make_service)
    svc.rig.sim.camera.set_temp(9.5)
    tl = svc.templog
    assert tl.columns == ["time", "sensor_c", "wide_c", "outside", "inside", "dead", "sun_alt", "activity",
                          "exposing", "filter"]
    assert wait_for(lambda: len(tl.rows()) >= 3, 20)
    path = tl.path_for(svc.clock.time())
    assert os.path.basename(path) == "20261108.csv" and os.path.dirname(path).endswith("templog")
    rows = read_csv(path)
    r = rows[-1]
    assert r["outside"] == 5.0 and r["inside"] == 6.9 and r["dead"] is None
    assert r["sensor_c"] == pytest.approx(9.5, abs=0.5) and r["wide_c"] is not None
    svc.rig.sim.wide_camera.set_temp(0.0)                        # like the real wide camera: 0 = no reading
    assert tl.sample()["wide_c"] is None
    assert r["activity"] == "idle" and r["exposing"] == 0 and r["filter"] == "IR"
    assert -60 < r["sun_alt"] < -20 and r["time"].startswith("2026-11-08T")
    assert fetched.count("http://site.invalid/status") <= fetched.count("http://127.0.0.1:9/nothing")  # once a row
    assert "dead" in tl.errors and "outside" not in tl.errors
    with open(path) as fh:
        assert sum(1 for line in fh if line.startswith("time,")) == 1                 # one header

    srv = serve(svc, host="127.0.0.1", port=0)
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        with urllib.request.urlopen(base + "/api/templog?hours=1", timeout=10) as resp:
            body = json.loads(resp.read())
        assert body["columns"] == tl.columns and body["sources"] == ["outside", "inside", "dead"]
        assert len(body["rows"]) >= 3 and body["rows"][-1]["inside"] == 6.9
        try:
            urllib.request.urlopen(base + "/api/templog?hours=x", timeout=10)
            assert False, "expected 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        srv.shutdown()


def test_locked_row_does_not_touch_the_scope(make_service):
    svc, _ = _svc(make_service, interval=36000)              # the thread stays quiet; rows by hand
    assert svc.cmd_lock("tester", "templog test")["ok"]
    cam = svc.rig.sim.camera
    calls = []
    orig = type(cam).CCDTemperature

    class Spy(type(cam)):
        @property
        def CCDTemperature(self):
            calls.append(1)
            return orig.fget(self)
    cam.__class__ = Spy
    row = svc.templog.sample()
    assert calls == [] and row["sensor_c"] is None and row["filter"] is None
    assert row["activity"] == "locked:tester" and row["outside"] == 5.0
    svc.templog.write(row)
    assert svc.templog.rows()[-1]["activity"] == "locked:tester"
    svc.cmd_unlock("tester")
    row = svc.templog.sample()
    assert calls and row["sensor_c"] is not None and row["activity"] == "idle"


def test_new_columns_get_a_new_header(make_service):
    svc, _ = _svc(make_service, interval=36000)
    tl = svc.templog
    tl.write(tl.sample())
    tl.cfg.sources.append(("extra", {"url": "http://site.invalid/status", "path": "list[0][1]", "regex": None}))
    tl.write(tl.sample())
    rows = read_csv(tl.path_for(svc.clock.time()))
    assert "extra" not in rows[-2] and rows[-1]["extra"] == 2


def test_darks_build_archives_frames(make_service):
    svc = make_service(speed=300)
    svc.rig.sim.camera.set_temp(10.0)
    assert svc.cmd_darks_build(exposure=20, count=3)["ok"]
    assert wait_for(lambda: svc.darklib.entries(), 60) and wait_for(svc.idle, 30)
    cdir = os.path.join(svc.s.data_dir, "darkarchive", "tele")
    with open(os.path.join(cdir, "index.jsonl")) as fh:
        recs = [json.loads(line) for line in fh]
    assert len(recs) == 3
    r = recs[0]
    assert r["file"].startswith("20261108/") and r["file"].endswith("_e20_g200_o0_b1.fits")
    assert r["exposure"] == 20 and r["gain"] == 200 and r["offset"] == 0 and r["binning"] == 1
    assert r["temp_c"] == pytest.approx((r["temp_start"] + r["temp_end"]) / 2, abs=1e-3)
    assert r["end_unix"] - r["start_unix"] == pytest.approx(20) and r["downloaded_unix"] >= r["end_unix"]
    assert r["start"].startswith("2026-11-08T")
    assert 300 < r["median"] < 1000 and r["identity"].get("Name") == "Sim telephoto"
    assert len({x["file"] for x in recs}) == 3
    h = fits.getheader(os.path.join(cdir, r["file"]))
    assert h["IMAGETYP"] == "DARK" and h["EXPTIME"] == 20 and h["GAIN"] == 200 and h["FILTER"] == "Dark"
    assert h["TEMP-BEG"] == r["temp_start"] and h["CCD-TEMP"] == r["temp_c"] and "DATE-END" in h
    assert fits.getdata(os.path.join(cdir, r["file"])).shape == tuple(r["shape"])


def test_darks_archive_off(make_service):
    svc = make_service(speed=300, dark_archive=False)
    assert svc.cmd_darks_build(exposure=5, count=2)["ok"]
    assert wait_for(lambda: svc.darklib.entries(), 60) and wait_for(svc.idle, 30)
    assert not os.path.exists(os.path.join(svc.s.data_dir, "darkarchive"))
    assert Key("tele", 5.0, 200) == svc.dark_key(5.0, 200)
