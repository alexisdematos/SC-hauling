"""Where the player is flying: the quantum destination picked last.

The game logs "Projected Start Location is X for route to destination Y"
when a destination is picked on the starmap (not when the jump starts). Y
is an internal name: cities, gateways and Lagrange stations are named
(ObjectContainer_Lorville_City, NewBabbage_LOC, rs_ext_nyx-pyro_jp1,
rs_ext_arc-l001), planets and moons by their bodies.csv name
(OOC_Stanton_4_Microtech -> microTech); rest stops (ObjectContainer_RestStop,
the game does not say which), mission beacons and nav points cannot.
"""
import re
from datetime import datetime

from .locations import place_for_code

# A heading ends this long after its arrival time, or after this long
# without one.
AFTER_ETA_S = 10 * 60
WITHOUT_ETA_S = 30 * 60

_QT = re.compile(r"CSCItemNavigation::CalculateRoute\|Projected Start Location is (.+?) for route to destination (\S+)")
_TIME = re.compile(r"^<(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?)Z>")
_SYSTEMS = {"stan": "Stanton", "nyx": "Nyx", "pyro": "Pyro", "castra": "Castra", "magnus": "Magnus",
            "terra": "Terra"}
_JUMP = re.compile(r"(?i)rs_ext_([a-z]+)-([a-z]+)_jp\d")
_STATION = re.compile(r"(?i)rs_ext_([a-z]{3})-(?:l0*(\d)|leo\d*)$")
_NOISE = {"objectcontainer", "ooc", "loc", "oc", "city", "all"}
# OOC_Stanton_4_Microtech, OOC_Stanton_3a_Lyria: a planet or a moon.
_BODY = re.compile(r"(?i)OOC_([a-z]+)_\d+[a-z]?_(\w+)$")
# RR_S1_L2: Stanton I (Hurston) L2, seen in game as the next start
# "HUR-L2 Faithful Dream Station". RR_P6_L5: Pyro VI L5 (Megumi).
_NUMBERED = re.compile(r"(?i)RR_([SP])(\d)_(L\d|LEO)$")
_STANTON = {"1": "HUR", "2": "CRU", "3": "ARC", "4": "MIC"}


def line_time(line):
    """Unix seconds of a log line, or None."""
    m = _TIME.match(line)
    if not m:
        return None
    return datetime.fromisoformat(m.group(1) + "+00:00").timestamp()


def _numbered(star, n, spot, places):
    """RR_S1_L2 / RR_P6_L5: the station there, else the Lagrange point."""
    if star.upper() == "S":
        abbr = _STANTON.get(n)
        place = place_for_code(f"RR_{abbr}_{spot.upper()}", places) if abbr else None
        return place.name if place else places.body(f"{abbr}-{spot.upper()}") if abbr else None
    body = places.body(f"PYR{n} {spot.upper()}", "Pyro")
    stations = [p for p in places.places if body and p.body == body]
    return stations[0].name if len(stations) == 1 else body


def destination(code, places):
    """SC-map name for a quantum destination code: a place, else a body of
    bodies.csv (planet, moon, Lagrange point). None when there is none."""
    place = _place(code, places)
    if isinstance(place, str) or place is None:
        return place
    return place.name


def _place(code, places):
    code = re.sub(r"(?i)^loc_", "", code)
    m = _BODY.fullmatch(code)
    if m:
        return places.body(m.group(2).replace("_", " "), m.group(1))  # Pyro_V -> Pyro V
    m = _NUMBERED.fullmatch(code)
    if m:
        return _numbered(*m.groups(), places)
    m = _JUMP.fullmatch(code)
    if m:  # rs_ext_nyx-pyro_jp1: in Nyx, towards Pyro
        here, there = (_SYSTEMS.get(s.lower()) for s in m.groups())
        return places.get(f"{there} Gateway ({here})") if here and there else None
    if code.upper().startswith("RR_"):  # RR_S1_L2, as in the local inventory lines
        return place_for_code(code, places)
    m = _STATION.fullmatch(code)
    if m:  # rs_ext_arc-l001 -> RR_ARC_L1, rs_ext_cru-leo1 -> RR_CRU_LEO
        spot = f"L{m.group(2)}" if m.group(2) else "LEO"
        return place_for_code(f"RR_{m.group(1).upper()}_{spot}", places)
    words = [w for w in re.split(r"[_\-]", code) if w and not w.isdigit() and w.lower() not in _NOISE]
    if not words or len(words) > 3:  # beacons, nav points: long codes, no place
        return None
    name = " ".join(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", w) for w in words)  # NewBabbage -> New Babbage
    return places.get(name) or places.resolve(name)


def parse(line, places, gateway=None):
    """{"from", "to", "picked_at"} for a quantum route line, or None (other
    line, or a destination with no SC-map name). gateway(name, code): the
    place for a start "Stanton Gateway", which is in Pyro and in Nyx."""
    m = _QT.search(line)
    if not m:
        return None
    to = destination(m.group(2), places)
    if not to:
        return None
    start = places.resolve(m.group(1)) or (gateway(m.group(1), m.group(2)) if gateway else None)
    start = start.name if start else places.body(m.group(1))  # "Hurston", "Crusader": a planet
    return {"from": start, "to": to, "picked_at": line_time(line)}


def expired(heading, now):
    """Past its arrival + AFTER_ETA_S, or WITHOUT_ETA_S old without one."""
    if heading.get("eta"):
        return now > heading["eta"] + AFTER_ETA_S
    return now > (heading.get("picked_at") or now) + WITHOUT_ETA_S
