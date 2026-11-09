"""Exclusive use of the scope: the service's API lock (TTL, renewal, refusals, revocation on unsafe) and
the file lock that keeps scripts and the service apart when the API isn't used."""
import json
import os
import time
import urllib.request

import pytest

from sequencer import plan as P
from sequencer.httpapi import serve
from sequencer.statusview import headline

from conftest import ra_at_ha, wait_for

import scope as scopemod
from scope import Interrupted
from scopelock import FileLock, LockBusy, ScopeLock


def _plan(count=20, exposure=60):
    return P.loads(f'[plan]\npark_at_end=false\n[[step]]\nra={ra_at_ha(-20)}\ndec=45\nexposure={exposure}\n'
                   f'count={count}\ncentre="none"\n')


def _base(svc):
    srv = serve(svc, host="127.0.0.1", port=0)
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _quiet(*a):
    pass


def test_lock_refuses_jobs_and_shows_owner(make_service):
    svc = make_service()
    r = svc.cmd_lock("sattrack", "ISS pass test", 600)
    assert r["ok"], r
    st = svc.status()
    assert st["lock"]["locked"] and st["lock"]["owner"] == "sattrack"
    assert headline(st) == "Scope in use by sattrack"
    for res in (svc.cmd_start(_plan()), svc.cmd_park(), svc.cmd_roof_open(), svc.cmd_darks_build(10, 80, 2),
                svc.polar.cmd_start({})):
        assert not res["ok"] and "sattrack" in res["msg"], res
    assert svc.idle() and svc.rig.sim.telescope.AtPark           # nothing moved
    # another owner can't take or release it; the same owner renews
    assert not svc.cmd_lock("other", "x", 60)["ok"]
    assert not svc.cmd_unlock("other")["ok"]
    until = svc.hwlock["until"]
    time.sleep(0.05)
    assert svc.cmd_lock("sattrack", "", 900)["msg"].startswith("renewed")
    assert svc.hwlock["until"] > until and svc.hwlock["reason"] == "ISS pass test"
    assert svc.cmd_unlock("sattrack")["ok"]
    assert not svc.status()["lock"]["locked"]
    assert svc.cmd_start(_plan(count=1, exposure=5))["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done", 60)


def test_lock_refused_while_a_plan_runs(make_service):
    svc = make_service()
    assert svc.cmd_start(_plan())["ok"]
    assert wait_for(lambda: svc.status()["phase"] == "lights", 30)
    r = svc.cmd_lock("sattrack", "test", 60)
    assert not r["ok"] and "busy" in r["msg"], r
    svc.cmd_stop()
    assert wait_for(lambda: svc.idle(), 30)
    assert svc.cmd_lock("sattrack", "test", 60)["ok"]


def test_lock_expires_and_force_unlock(make_service):
    svc = make_service()
    assert svc.cmd_lock("a", "short", 1)["ok"]
    assert not svc.cmd_start(_plan())["ok"]
    assert wait_for(lambda: not svc.status()["lock"]["locked"], 5)
    assert any("expired" in ln for ln in svc.status()["log"])
    assert not svc.cmd_lock("a", renew=True)["ok"]                # renewal can't resurrect it
    assert svc.cmd_lock("a", "again", 60)["ok"]
    assert svc.cmd_unlock(force=True)["ok"]
    r = svc.cmd_lock("a", renew=True)
    assert not r["ok"] and "released" in r["msg"], r


def test_service_holds_file_lock_while_locked_and_while_running(make_service):
    svc = make_service()
    path = os.path.join(svc.s.data_dir, "scope.lock")
    other = FileLock(path)
    assert other.acquire("script", "idle check")                  # idle service: free
    other.release()
    assert svc.cmd_lock("sattrack", "pass", 60)["ok"]
    assert not other.acquire("script", "x")                       # granted API lock = file lock held
    assert other.holder()["owner"] == "sequencer"
    svc.cmd_unlock("sattrack")
    assert svc.cmd_start(_plan())["ok"]
    assert wait_for(lambda: svc.status()["phase"] == "lights", 30)
    assert not other.acquire("script", "x")                       # a running job holds it
    svc.cmd_stop()
    assert wait_for(lambda: svc.idle(), 30)
    assert other.acquire("script", "x")
    other.release()


def test_job_refused_while_a_script_holds_the_file_lock(make_service):
    svc = make_service()
    lk = ScopeLock("polar-script", "manual test", url="http://127.0.0.1:9", data_dir=svc.s.data_dir, log=_quiet)
    with lk:
        assert lk.mode == "file"
        assert svc.status()["lock"]["file_holder"]["owner"] == "polar-script"
        assert svc.cmd_start(_plan(count=1, exposure=5))["ok"]    # queued...
        assert wait_for(lambda: svc.idle(), 10)
        assert any("REFUSED" in ln and "polar-script" in ln for ln in svc.status()["log"])
        assert svc.rig.sim.telescope.AtPark                       # ...but nothing moved
        assert not svc.cmd_lock("sattrack", "x", 60)["ok"]        # nor can the API hand it out
        # a second script falling back to the file lock is refused too
        with pytest.raises(LockBusy, match="polar-script"):
            ScopeLock("sattrack", url="http://127.0.0.1:9", data_dir=svc.s.data_dir, log=_quiet).acquire()
    assert svc.cmd_start(_plan(count=1, exposure=5))["ok"]
    assert wait_for(lambda: svc.status()["state"] == "done", 60)


def test_client_helper_through_the_api(make_service):
    svc = make_service(api_token="tok")
    srv, base = _base(svc)
    try:
        with pytest.raises(LockBusy, match="token"):
            ScopeLock("sattrack", url=base, token="", data_dir=svc.s.data_dir, log=_quiet).acquire()
        lk = ScopeLock("sattrack", "pass", ttl_s=3, url=base, token="tok", data_dir=svc.s.data_dir, log=_quiet)
        with lk:
            assert lk.mode == "api" and svc.status()["lock"]["owner"] == "sattrack"
            time.sleep(4.5)                                       # longer than the TTL: renewed meanwhile
            assert svc.status()["lock"]["locked"] and not lk.lost.is_set()
            with pytest.raises(LockBusy, match="sattrack"):
                ScopeLock("other", url=base, token="tok", data_dir=svc.s.data_dir, log=_quiet).acquire()
            with urllib.request.urlopen(base + "/api/lock") as r:
                assert json.loads(r.read())["owner"] == "sattrack"
        assert not svc.status()["lock"]["locked"]                 # released on exit
    finally:
        srv.shutdown()


def test_unsafe_revokes_the_lock_and_parks(make_service):
    svc = make_service(lock_unsafe_grace_s=0.5)
    srv, base = _base(svc)
    try:
        svc.cmd_start(_plan(count=1, exposure=5))                 # unpark and leave it pointing at the sky
        assert wait_for(lambda: svc.status()["state"] == "done", 60)
        lost = []
        lk = ScopeLock("sattrack", "pass", ttl_s=3, url=base, data_dir=svc.s.data_dir, log=_quiet,
                       on_lost=lost.append).acquire()
        svc.rig.sim.safety.set(False, ["RAIN"])
        assert wait_for(lambda: lk.lost.is_set(), 10)
        assert "RAIN" in lost[0]
        assert wait_for(lambda: svc.status()["state"] == "parked" and svc.idle(), 60)
        assert svc.status()["lock"]["revoked"]["owner"] == "sattrack"
        lk.release()
    finally:
        srv.shutdown()


def test_scope_opt_in_stops_moving_when_the_lock_is_lost(make_service, monkeypatch):
    svc = make_service()
    srv, base = _base(svc)
    try:
        monkeypatch.setenv("SEQ_URL", base)
        monkeypatch.setenv("SEESTAR_DATA_DIR", svc.s.data_dir)
        tel = svc.rig.sim.telescope
        sc = scopemod.Scope(log=_quiet, telescope=tel, clock=svc.clock, lst=svc.scope._lst,
                            unwrap_path=svc.scope.unwrap_path, window=svc.s.window,
                            lock_owner="goto-script", lock_reason="test", lock_ttl_s=3)
        assert svc.status()["lock"]["owner"] == "goto-script"
        assert not svc.cmd_park()["ok"]
        tel.Unpark()
        svc.cmd_unlock(force=True)                                # the operator takes it back
        assert wait_for(lambda: sc.scopelock.lost.is_set(), 5)
        with pytest.raises(Interrupted, match="lock lost"):
            sc.move_hadec(-60, 30)
        sc.close()
        # without the flag nothing changes for existing scripts
        assert scopemod.Scope(log=_quiet, telescope=tel, clock=svc.clock, lst=svc.scope._lst,
                              unwrap_path=svc.scope.unwrap_path, window=svc.s.window).scopelock is None
    finally:
        srv.shutdown()


def test_monitor_leaves_the_seestar_alone_while_locked(make_service):
    """While the scope is lent out the monitor must not read the mount or write ra_unwrap.json: the
    holder owns the bookkeeping, and polling slowed its MoveAxis calls on the real bridge."""
    svc = make_service()
    svc._monitor_once()
    assert "stale" not in svc.status()["mount"]
    assert svc.cmd_lock("sattrack", "test", 600)["ok"]
    calls = []
    orig_pos, orig_u = svc.scope.position, svc.scope.ha_unwrapped
    svc.scope.position = lambda: calls.append("position") or orig_pos()
    svc.scope.ha_unwrapped = lambda: calls.append("u") or orig_u()
    mtime = os.path.getmtime(svc.scope.unwrap_path)
    time.sleep(0.02)
    for _ in range(3):
        svc._monitor_once(roof=True)
    assert calls == []
    assert os.path.getmtime(svc.scope.unwrap_path) == mtime
    st = svc.status()
    assert "sattrack" in st["mount"]["stale"] and "sattrack" in st["cable"]["stale"]
    assert svc.cmd_unlock("sattrack")["ok"]
    svc._monitor_once()
    assert calls and "stale" not in svc.status()["mount"]


def _stop_service(svc, srv):
    """What stopping the systemd unit does to the lock: the API goes away and the kernel drops the
    service's file lock with the process."""
    srv.shutdown()
    srv.server_close()
    svc.flock.release()


def test_script_takes_over_the_file_lock_when_the_service_stops(make_service):
    svc = make_service()
    srv, base = _base(svc)
    lk = ScopeLock("camcheck", "test", ttl_s=3, url=base, data_dir=svc.s.data_dir, log=_quiet)
    with lk:
        assert lk.mode == "api"
        _stop_service(svc, srv)
        assert wait_for(lambda: lk.mode == "file" or lk.lost.is_set(), 10)
        assert lk.mode == "file" and not lk.lost.is_set()
        # anyone else (e.g. a script that stopped the service to run on its own) is now refused
        with pytest.raises(LockBusy, match="camcheck"):
            ScopeLock("sattrack", url=base, data_dir=svc.s.data_dir, log=_quiet).acquire()
    assert FileLock(os.path.join(svc.s.data_dir, "scope.lock")).acquire("next", "x")


def test_script_loses_the_lock_if_someone_else_got_the_file_lock_first(make_service):
    svc = make_service()
    srv, base = _base(svc)
    lk = ScopeLock("camcheck", "test", ttl_s=3, url=base, data_dir=svc.s.data_dir, log=_quiet)
    with lk:
        _stop_service(svc, srv)
        other = FileLock(os.path.join(svc.s.data_dir, "scope.lock"))
        assert other.acquire("sattrack", "bench run", via="bench")
        assert wait_for(lambda: lk.lost.is_set(), 10)
        assert "sattrack" in lk.lost_msg
        other.release()
