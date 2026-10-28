"""Ship names (SC-map's list) and the ship the player is in (Game.log).

Boarding a ship joins its comms channel, named after the ship as SC-map
names it: "You have joined channel 'RSI Zeus Mk II ES : Player'". Leaving
the pilot seat logs the entity class instead:
"releasing control token for 'RSI_Zeus_ES_905331888284'".
"""
import json
import re
from pathlib import Path

_RELEASE = re.compile(r"<Vehicle Control Flow> .*?releasing control token for '(?P<cls>[A-Za-z0-9_]+?)_\d+'")
_BOARDED = re.compile(r"<SHUDEvent_OnNotification> Added notification \"You have joined channel '(?P<ship>.+?) : [^']*'")


def boarded_ship(line, names):
    """Ship the player just boarded (a name from `names`), or None."""
    m = _BOARDED.search(line)
    if m and m.group("ship") in names:
        return m.group("ship")
    return None

_ROMAN = {"1": "i", "2": "ii", "3": "iii", "4": "iv", "5": "v", "6": "vi"}
# Entity class bits that are not in the ship's name.
_SKIP = {"gs", "pu", "ai", "ea", "civ", "unmanned"}


def load_ships(data_dir):
    """Ship names that have a quantum drive in SC-map, sorted."""
    try:
        data = json.loads((Path(data_dir) / "quantum.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return sorted({s["name"] for s in data.get("ships", [])})


def piloted_class(line):
    """Entity class of the ship the player just left, or None."""
    m = _RELEASE.search(line)
    return m.group("cls") if m else None


def _words(text):
    return re.findall(r"[a-z0-9]+", text.lower())


def class_words(cls):
    """'ANVL_Hornet_F7A_Mk2' -> ['hornet', 'f7a', 'mk', 'ii'] (maker code dropped)."""
    out = []
    for part in cls.split("_")[1:]:
        m = re.fullmatch(r"(?i)mk(\d)", part)
        if m:
            out += ["mk", _ROMAN.get(m.group(1), m.group(1))]
        elif part.lower() not in _SKIP:
            out += _words(part)
    return out


def ship_for_class(cls, names):
    """SC-map ship name for an entity class, or None.

    Every word of the class must be in the name; among those, the shortest
    name wins (the base model before its variants).
    """
    words = class_words(cls or "")
    if not words:
        return None
    hits = [n for n in names if all(w in _words(n) for w in words)]
    return min(hits, key=len) if hits else None
