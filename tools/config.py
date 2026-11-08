"""Site/device configuration. Everything site-specific lives in a TOML file, never in code.

Lookup order: $SEESTAR_CONFIG, then <repo>/config.toml. See config.example.toml.
"""
import os
import tomllib

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_path = os.environ.get("SEESTAR_CONFIG") or os.path.join(REPO, "config.toml")
if not os.path.exists(_path):
    raise SystemExit(f"no config: copy config.example.toml to {_path} (or set SEESTAR_CONFIG)")
with open(_path, "rb") as fh:
    CFG = tomllib.load(fh)


def get(section, key, default=None):
    return CFG.get(section, {}).get(key, default)


SEESTAR = get("seestar", "alpaca", "seestar.local:32323")
LAT = float(get("site", "latitude"))
LON = float(get("site", "longitude"))
ELEVATION = float(get("site", "elevation_m", 0))
SITE_NAME = get("site", "name", "My site")
HOME_HA = float(get("mount", "home_ha", -90.0))
CCW_LIMIT = float(get("mount", "ccw_limit_deg", 90.0))
CW_LIMIT = float(get("mount", "cw_limit_deg", 270.0))
LIMIT_MARGIN = float(get("mount", "limit_margin_deg", 5.0))
IPCAM_RTSP = get("ipcam", "rtsp_url", "")            # optional: camera watching the scope
ROOF_ALPACA = get("roof", "alpaca", "")              # optional: Alpaca Dome device host:port
ROOF_DOME = int(get("roof", "dome_number", 0))
# optional external safety source: HTTP JSON endpoint, unsafe when <field> is false
SAFETY_URL = get("safety", "url", "")
SAFETY_FIELD = get("safety", "field", "safe")
SAFETY_REASONS_FIELD = get("safety", "reasons_field", "reasons")
SAFETY_POLL_S = float(get("safety", "poll_s", 30))
SAFETY_STALE_S = float(get("safety", "stale_after_s", 300))
# optional hooks, called with HTTP POST after a guarded park / when leaving park
HOOK_PARKED = get("hooks", "on_parked", "")
HOOK_UNPARKED = get("hooks", "on_unparked", "")
# optional HTTP roof control as an alternative to an Alpaca Dome (POST open/close, GET state JSON)
ROOF_OPEN_URL = get("roof", "open_url", "")
ROOF_CLOSE_URL = get("roof", "close_url", "")
ROOF_STATE_URL = get("roof", "state_url", "")
DATA_DIR = os.path.join(REPO, get("paths", "data_dir", "data"))
PROJECTIONS_DIR = get("paths", "projections_dir", "") or os.environ.get("PROJECTIONS_DIR", "")
