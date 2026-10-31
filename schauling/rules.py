"""Tables that change with the game, updated without a new exe.

rules.json (next to this file, in the exe too) holds codes and numbers read
from Game.log: station codes, inventory zones of open space, the share of a
jump below which it was cut short... A newer copy is fetched from GitHub in
the background at start (data only: nothing in it runs) and kept in
%APPDATA%\SC-hauling\rules.json; the one with the highest "version" wins.
"""
import json
import urllib.error
import urllib.request
from pathlib import Path

from . import config, heading, locations
from .api import USER_AGENT

URL = "https://raw.githubusercontent.com/alexisdematos/SC-hauling/main/schauling/rules.json"
BUNDLED = Path(__file__).resolve().parent / "rules.json"
TIMEOUT_S = 10
# Key: (module, attribute, type). Dicts are filled in place, so code that
# holds them sees the new ones.
_TABLES = {
    "aliases": (locations, "ALIASES", dict),
    "leo": (locations, "_LEO", dict),
    "cities": (locations, "_CITIES", dict),
    "space_zones": (heading, "SPACE_ZONES", dict),
    "align_s": (heading, "ALIGN_S", (int, float)),
    "cut_share": (heading, "CUT_SHARE", (int, float)),
    "zone_before_s": (heading, "ZONE_BEFORE_S", (int, float)),
    "zone_after_s": (heading, "ZONE_AFTER_S", (int, float)),
}
applied = None  # version in use


def _read(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if valid(data) else None


def valid(data):
    """A rules dict we can use: a version, and each table of the right type."""
    if not isinstance(data, dict) or not isinstance(data.get("version"), int):
        return False
    for key, (_module, _name, kind) in _TABLES.items():
        value = data.get(key)
        if value is not None and (not isinstance(value, kind) or isinstance(value, bool)):
            return False
        if isinstance(value, dict) and not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
            return False
    return True


def cache():
    return config.folder() / "rules.json"


def load():
    """The newest of the bundled rules and the downloaded copy."""
    found = [r for r in (_read(BUNDLED), _read(cache())) if r]
    return max(found, key=lambda r: r["version"]) if found else None


def apply(data):
    global applied
    if not data:
        return
    for key, (module, name, _kind) in _TABLES.items():
        if key not in data:
            continue
        if isinstance(data[key], dict):
            table = getattr(module, name)
            table.clear()
            table.update(data[key])
        else:
            setattr(module, name, data[key])
    applied = data["version"]


def _get(url, timeout=TIMEOUT_S):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def refresh(get=_get):
    """Fetch the rules on GitHub; keep and apply them when newer. Status
    text, or None when nothing changed. Network: on a worker thread."""
    try:
        data = json.loads(get(URL))
    except (urllib.error.URLError, OSError, ValueError) as e:
        return f"rules not checked: {getattr(e, 'reason', None) or e}"
    if not valid(data):
        return "rules on GitHub not valid: kept ours"
    if applied is not None and data["version"] <= applied:
        return None
    try:
        config.folder().mkdir(parents=True, exist_ok=True)
        tmp = cache().with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(cache())
    except OSError:
        pass  # used now, fetched again next time
    apply(data)
    return f"rules updated to {data['version']}"
