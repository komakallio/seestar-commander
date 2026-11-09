"""Orbital elements: fetch as OMM JSON, cache on disk, build Skyfield satellites.

The source is a URL template with {norad} and optional {source}, e.g. CelesTrak's
  https://celestrak.org/NORAD/elements/gp.php?CATNR={norad}&FORMAT=json
or any orbit API returning a CelesTrak-style OMM JSON list. An optional bearer key is read from the
environment variable named in the config, never from the config itself.

Runs that move the mount read only the cache (fetch before the run): the site link may be down.
Cached elements are data you may not be allowed to redistribute; data/ is git-ignored.
"""
import json
import os
import time
import urllib.request

DEFAULT_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR={norad}&FORMAT=json"
DEFAULT_UA = "seestar-commander-sattrack/0.1"


class ElementsError(RuntimeError):
    pass


def cache_path(cache_dir, norad, source=""):
    return os.path.join(cache_dir, source or "default", f"{int(norad)}.json")


def parse_omm(text):
    """OMM JSON (a list or one object) -> the first record as a dict."""
    d = json.loads(text)
    if isinstance(d, dict) and "results" in d:
        d = d["results"]
    if isinstance(d, list):
        if not d:
            raise ElementsError("no elements in response")
        d = d[0]
    if not isinstance(d, dict) or "MEAN_MOTION" not in d:
        raise ElementsError("response is not OMM JSON")
    return d


def fetch(norad, cache_dir, url=DEFAULT_URL, source="", key_env="", user_agent=DEFAULT_UA, timeout=20):
    """Fetch the latest elements for one object and write them to the cache. Returns the OMM dict."""
    u = url.format(norad=int(norad), source=source)
    req = urllib.request.Request(u, headers={"User-Agent": user_agent, "Accept": "application/json"})
    key = os.environ.get(key_env, "") if key_env else ""
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            omm = parse_omm(r.read().decode())
    except ElementsError:
        raise
    except Exception as e:  # noqa: BLE001 - network errors are reported as one type
        raise ElementsError(f"fetch {norad}: {e}") from e
    p = cache_path(cache_dir, norad, source)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"fetched": time.time(), "source": source, "omm": omm}, fh)
    os.replace(tmp, p)
    return omm


def load(norad, cache_dir, source="", max_age_h=None, now=None):
    """OMM dict from the cache. max_age_h limits the age of the element epoch, not of the fetch."""
    p = cache_path(cache_dir, norad, source)
    if not os.path.exists(p):
        raise ElementsError(f"no cached elements for {norad} ({source or 'default'}): fetch first")
    with open(p) as fh:
        omm = json.load(fh)["omm"]
    if max_age_h is not None:
        age = epoch_age_h(omm, now)
        if age > max_age_h:
            raise ElementsError(f"elements for {norad} are {age:.0f} h old (limit {max_age_h:.0f} h)")
    return omm


def epoch_unix(omm):
    from datetime import datetime, timezone
    e = omm["EPOCH"].rstrip("Z")
    return datetime.fromisoformat(e).replace(tzinfo=timezone.utc).timestamp()


def epoch_age_h(omm, now=None):
    return ((now if now is not None else time.time()) - epoch_unix(omm)) / 3600.0


def satellite(omm, ts=None):
    from skyfield.api import EarthSatellite, load as skyload
    ts = ts or skyload.timescale()
    return EarthSatellite.from_omm(ts, {k: str(v) for k, v in omm.items()})
