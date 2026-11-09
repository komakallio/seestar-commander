"""Temperature log: one CSV row a minute with the sensor temperatures, ambient sources, the Sun and
what the scope is doing. Raw material for the dark harvest (sensor-ambient offset, warm-up and cool-down
time constants, which temperature bins are reachable).

Files: <data>/templog/YYYYMMDD.csv (UTC date). Columns:
  time      ISO UTC
  sensor_c  telephoto CCDTemperature (empty: no reading, 0, or the scope is locked)
  wide_c    wide camera, likewise (it reports 0 = no reading today)
  <source>  one column per [templog.sources.<name>], in config order (empty when the fetch fails)
  sun_alt   deg
  activity  idle | plan | <job name> (darks_build, flats_build, polar, park, ...) | locked:<owner> |
            harvest (the dark harvest is taking darks) | exposing (idle, but the telephoto exposes)
  exposing  1 while the telephoto exposes (from the service's own bookkeeping), else 0
  filter    wheel position name (empty while locked)

While a script holds the scope lock the SeeStar isn't touched at all (as the monitor): the row has the
ambient sources and the Sun, empty sensor and filter cells and activity locked:<owner>.

Config ([templog] in config.toml):
  interval_s = 60
  [templog.sources.<name>]
  url = "http://..."          JSON document, fetched with a 2 s timeout (several sources may share a URL:
                              it is fetched once per row)
  path = "a.b[0].c"           dot path into it; "[i]" indexes a list; keys may contain spaces (only "."
                              splits); empty = the whole document
  regex = "TEMP1=([-0-9.]+)"  optional: applied to the value as a string, group 1 -> float
A failure (network, missing key, no match, not a number) gives an empty cell and never stops the log.
If the columns change (sources added in the config), a new header line is written before the next row.
"""
import csv
import datetime as dt
import io
import json
import os
import re
import threading
import urllib.request

from . import astro

FETCH_TIMEOUT_S = 2.0
USER_AGENT = "seestar-commander-templog/1"
_PART = re.compile(r"^(.*?)((?:\[-?\d+\])*)$")


class TempLogConfig:
    def __init__(self, interval_s=60.0, sources=None, enabled=True):
        self.interval_s = max(1.0, float(interval_s))
        self.enabled = bool(enabled)
        self.sources = []                    # [(name, {"url", "path", "regex"})] in config order
        for name, src in dict(sources or {}).items():
            src = dict(src or {})
            self.sources.append((str(name), {"url": str(src.get("url", "")), "path": str(src.get("path", "")),
                                             "regex": src.get("regex") or None}))

    @classmethod
    def from_dict(cls, d):
        d = dict(d or {})
        return cls(d.get("interval_s", 60.0), d.get("sources"), d.get("enabled", True))


def dig(doc, path):
    """Value at a dot path ("a.b[0].c"; keys may contain spaces, only '.' splits). Raises KeyError,
    IndexError or TypeError when it isn't there."""
    v = doc
    if not path:
        return v
    for part in path.split("."):
        m = _PART.match(part)
        key, idx = m.group(1), m.group(2)
        if key:
            if not isinstance(v, dict):
                raise TypeError(f"{key!r}: not an object")
            v = v[key]
        for i in re.findall(r"\[(-?\d+)\]", idx):
            if not isinstance(v, list):
                raise TypeError(f"[{i}]: not a list")
            v = v[int(i)]
    return v


def extract(doc, path, regex=None):
    """Float at path (via regex group 1 when given), or None."""
    try:
        v = dig(doc, path)
        if regex:
            m = re.search(regex, v if isinstance(v, str) else json.dumps(v))
            if not m:
                return None
            v = m.group(1)
        if isinstance(v, bool) or v is None:
            return None
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    except Exception:  # noqa: BLE001 - a missing field is an empty cell
        return None


def fetch_json(url, timeout=FETCH_TIMEOUT_S):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _num(v, nd=2):
    return "" if v is None else f"{round(float(v), nd):g}"


class TempLog:
    """The logger thread of the service (svc): a row every interval_s (service clock)."""

    def __init__(self, svc, cfg: TempLogConfig, fetch=None):
        self.svc, self.cfg = svc, cfg
        self.fetch = fetch or fetch_json
        self.dir = os.path.join(svc.s.data_dir, "templog")
        self.thread = None
        self._lock = threading.Lock()
        self._header = {}                   # file -> last header written there
        self.last = None                    # last row (dict)
        self.errors = {}                    # source name -> last error (for status)

    @property
    def columns(self):
        return ["time", "sensor_c", "wide_c"] + [n for n, _ in self.cfg.sources] + \
            ["sun_alt", "activity", "exposing", "filter"]

    def start(self):
        if not self.cfg.enabled:
            return
        self.thread = threading.Thread(target=self._loop, name="templog", daemon=True)
        self.thread.start()
        self.svc.log(f"temperature log: every {self.cfg.interval_s:g} s to {self.dir}/ "
                     f"(sources: {', '.join(n for n, _ in self.cfg.sources) or 'none'})")

    def _loop(self):
        svc = self.svc
        while not svc.halt.is_set():
            try:
                self.write(self.sample())
            except Exception as e:  # noqa: BLE001 - the log never takes the service down
                svc.log(f"temperature log: {e}")
            svc.clock.wait(svc.halt, self.cfg.interval_s)

    # ---- one row ----
    def ambient(self):
        docs, out = {}, {}
        for name, src in self.cfg.sources:
            url = src["url"]
            if url not in docs:
                try:
                    docs[url] = self.fetch(url)
                except Exception as e:  # noqa: BLE001
                    docs[url] = None
                    self.errors[name] = f"{type(e).__name__}: {e}"[:200]
            doc = docs[url]
            out[name] = None if doc is None else extract(doc, src["path"], src["regex"])
            if doc is not None:
                if out[name] is None:
                    self.errors[name] = f"no number at {src['path']!r}" + (" (regex)" if src["regex"] else "")
                else:
                    self.errors.pop(name, None)
        return out

    def sample(self):
        svc = self.svc
        cols = self.columns                  # the row's columns (the config may change meanwhile)
        t = svc.clock.time()
        L = svc.lock_holder() if hasattr(svc, "lock_holder") else svc._lock_active()
        row = {"time": dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "sensor_c": None, "wide_c": None, "filter": None}
        exposing = bool(getattr(svc.imager, "exposing", False))
        if L is not None:
            # scope lent out: don't touch the SeeStar (one Alpaca bridge; the holder's calls come first)
            activity = f"locked:{L['owner']}"
        else:
            row["sensor_c"] = svc.imager.temperature()
            if svc.rig.wide is not None:
                row["wide_c"] = svc.rig.wide.temperature()
            fn = svc.imager.filter_name()
            row["filter"] = None if fn in ("?", None) else fn
            busy = svc.busy
            hv = getattr(svc, "harvest", None)
            harvesting = hv is not None and hv.active
            activity = ("plan" if busy == "run" else busy if busy else "harvest" if harvesting
                        else "exposing" if exposing else "idle")
        row.update(self.ambient())
        try:
            row["sun_alt"] = astro.sun_alt(t, svc.s.lat, svc.s.lon)
        except Exception:  # noqa: BLE001
            row["sun_alt"] = None
        row.update(activity=activity, exposing=int(exposing))
        row["t"], row["_cols"] = t, cols
        return row

    def path_for(self, t):
        return os.path.join(self.dir, dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y%m%d") + ".csv")

    def write(self, row):
        cols = row.get("_cols") or self.columns
        path = self.path_for(row["t"])
        cells = []
        for c in cols:
            v = row.get(c)
            if c not in ("time", "activity", "exposing", "filter"):
                cells.append(_num(v))
            else:
                cells.append("" if v is None else str(v))
        with self._lock:
            os.makedirs(self.dir, exist_ok=True)
            if path not in self._header:
                self._header[path] = _last_header(path)
            buf = io.StringIO()
            w = csv.writer(buf, lineterminator="\n")
            if self._header[path] != cols:
                w.writerow(cols)
                self._header[path] = cols
            w.writerow(cells)
            with open(path, "a", newline="") as fh:
                fh.write(buf.getvalue())
            self.last = {k: v for k, v in row.items() if k not in ("t", "_cols")}

    # ---- reading back ----
    def rows(self, hours=24.0, now=None):
        """Rows of the last `hours` (oldest first), values as numbers where they are numbers."""
        now = self.svc.clock.time() if now is None else now
        return self.rows_between(now - float(hours) * 3600.0, now)

    def rows_between(self, t0, t1):
        """Rows with t0 <= time <= t1 (unix seconds), oldest first."""
        day = dt.datetime.fromtimestamp(t0, dt.timezone.utc).date()
        end = dt.datetime.fromtimestamp(t1, dt.timezone.utc).date()
        out = []
        while day <= end:
            p = os.path.join(self.dir, day.strftime("%Y%m%d") + ".csv")
            if os.path.exists(p):
                for r in read_csv(p):
                    try:
                        ts = dt.datetime.strptime(r["time"], "%Y-%m-%dT%H:%M:%SZ").replace(
                            tzinfo=dt.timezone.utc).timestamp()
                    except (KeyError, ValueError):
                        continue
                    if t0 <= ts <= t1:
                        out.append(r)
            day += dt.timedelta(days=1)
        return out

    def view(self, hours=24.0, t0=None, t1=None):
        """The last `hours`, or the window t0..t1 (unix seconds; either may be None: t1 = now,
        t0 = t1 - hours)."""
        if t0 is None and t1 is None:
            rows = self.rows(hours)
        else:
            t1 = self.svc.clock.time() if t1 is None else t1
            t0 = t1 - float(hours) * 3600.0 if t0 is None else t0
            rows = self.rows_between(t0, t1)
            hours = (t1 - t0) / 3600.0
        out = {"columns": self.columns, "interval_s": self.cfg.interval_s, "hours": float(hours),
               "sources": [n for n, _ in self.cfg.sources], "errors": dict(self.errors), "rows": rows}
        if t0 is not None:
            out["from"], out["to"] = _iso_utc(t0), _iso_utc(t1)
        return out


def _iso_utc(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s):
    """ISO time ("2026-11-09T18:00:00Z", "+00:00" offsets, no zone = UTC) or unix seconds -> unix seconds.
    Raises ValueError."""
    s = str(s).strip()
    try:
        return float(s)
    except ValueError:
        pass
    d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.timestamp()


def _last_header(path):
    if not os.path.exists(path):
        return None
    last = None
    with open(path, newline="") as fh:
        for r in csv.reader(fh):
            if r and r[0] == "time":
                last = r
    return last


_TEXT = ("time", "activity", "filter")


def _cell(col, v):
    if v == "":
        return None
    if col in _TEXT:
        return v
    try:
        f = float(v)
    except ValueError:
        return v
    return int(f) if col == "exposing" else f


def read_csv(path):
    """Rows of one day file as dicts; a header line in the middle starts new columns."""
    out, cols = [], None
    with open(path, newline="") as fh:
        for r in csv.reader(fh):
            if not r:
                continue
            if r[0] == "time":
                cols = r
                continue
            if cols is None:
                continue
            out.append({c: _cell(c, v) for c, v in zip(cols, r)})
    return out
