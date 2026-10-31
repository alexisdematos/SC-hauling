"""Where the player is flying: the quantum destination picked last.

The game logs "Projected Start Location is X for route to destination Y"
when a destination is picked on the starmap (not when the jump starts:
nothing says that), and "Quantum Drive Arrived" when the drive stops. Y
is an internal name: cities, gateways and Lagrange stations are named
(ObjectContainer_Lorville_City, NewBabbage_LOC, rs_ext_nyx-pyro_jp1,
rs_ext_arc-l001), planets and moons by their bodies.csv name
(OOC_Stanton_4_Microtech -> microTech) or their number (pyro3 -> Bloom);
rest stops (ObjectContainer_RestStop,
the game does not say which), mission beacons and nav points cannot.
"""
import re
from datetime import datetime

from .locations import place_for_code

# The quantum drive stopped (at the destination or cut short: the game logs
# both as "arrived"). Seen in game, Bloom to Terminus, 2026-10-31.
_END = re.compile(r"<Quantum Drive Arrived")
# The jump starts: the route is used up ("FinalStop=-1", then "No Route
# loaded"), 17 to 26 s after the pick in game. Not on every jump.
_JUMP_START = re.compile(r"<Local Route Guard - Server Rerouted>.*FinalStop=-1")
# Our ship in a navigation line: "| NOT AUTH | RSI_Constellation_..._898477371874[898477371874]|".
_SHIP = re.compile(r"\| (?:NOT AUTH \| )?\w+\[(\d+)\]\|CSCItemNavigation")

# "Successfully calculated route to pyro3 fuel estimate 1142935.0": the game
# counts it from where the ship really is ("Pyro System" too), in millionths
# of fuel, so / fuelPerGm of the drive (SC-map quantum.json) gives the
# distance: Bloom -> Terminus 1142935 / 0.022 = 51.95 Gm, as on the map.
# Then the drive's flight model (SC-map js/quantum.js) gives the time. The
# jumps that went to the end in game (Constellation, Khaos drive) took
# 11 to 19 s more, from the pick: aligning and calibrating.
_FUEL = re.compile(r"CalculateRoute\|Successfully calculated route to (\S+) fuel estimate ([\d.]+)")
ALIGN_S = 15
# Stopped before this share of the jump: cut short. In 48 jumps of the
# logs, the ones that went to the end took 71 % of the estimate or more
# (a Hornet hop: 15 s of 21), the ones cut short 41 % at most (111 s of 269).
CUT_SHARE = 0.6

# "Update Inventory Location> ... Location [3184339241] -> [1299866756]": the
# inventory zone changes. Open space of a system has the same number in every
# log (July to October): out of it near the stop, the jump got there (into
# Bloom's zone, 1 to 8 s after); into it, it was cut short.
_ZONE = re.compile(r"<Update Inventory Location>.*? Location \[(\d+)\] -> \[(\d+)\]")
SPACE_ZONES = {"1902223495": "Stanton", "3184339241": "Pyro", "2627827560": "Nyx"}
ZONE_BEFORE_S = 30  # a zone change this long before the stop counts
ZONE_AFTER_S = 20  # and this long after

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
# rs_ext_pyro3_leo: Orbituary, seen in game; rs_ext_pyro3_l1 the same way.
_PYRO_STATION = re.compile(r"(?i)rs_ext_pyro(\d)_(leo|l\d)\d*$")
_NOISE = {"objectcontainer", "ooc", "loc", "oc", "city", "all"}
# OOC_Stanton_4_Microtech, OOC_Stanton_3a_Lyria: a planet or a moon.
_BODY = re.compile(r"(?i)OOC_([a-z]+)_\d+[a-z]?_(\w+)$")
# RR_S1_L2: Stanton I (Hurston) L2, seen in game as the next start
# "HUR-L2 Faithful Dream Station". RR_P6_L5: Pyro VI L5 (Megumi).
_NUMBERED = re.compile(r"(?i)RR_([SP])(\d)_(L\d|LEO)$")
_STANTON = {"1": "HUR", "2": "CRU", "3": "ARC", "4": "MIC"}
# pyro3: Pyro III (Bloom), as in "route to destination pyro3"; by the
# ordinal in bodies.csv.
_ORDINAL = re.compile(r"(?i)(stanton|pyro|nyx)(\d[a-z]?)$")


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
    place = place_for_code(f"RR_P{n}_{spot.upper()}", places)
    return place.name if place else places.body(f"PYR{n} {spot.upper()}", "Pyro") or (
        places.numbers.get(("pyro", n)) if spot.upper() == "LEO" else None)


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
    m = _ORDINAL.fullmatch(code)
    if m:
        return places.numbers.get((m.group(1).lower(), m.group(2).lower()))
    m = _JUMP.fullmatch(code)
    if m:  # rs_ext_nyx-pyro_jp1: in Nyx, towards Pyro
        here, there = (_SYSTEMS.get(s.lower()) for s in m.groups())
        return places.get(f"{there} Gateway ({here})") if here and there else None
    if code.upper().startswith("RR_"):  # RR_S1_L2, as in the local inventory lines
        return place_for_code(code, places)
    m = _PYRO_STATION.fullmatch(code)
    if m:
        return _numbered("P", m.group(1), m.group(2), places)
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
    ship = _SHIP.search(line)
    return {"from": start, "to": to, "picked_at": line_time(line), "ship": ship.group(1) if ship else None}


def _ours(line, heading):
    ship = _SHIP.search(line)
    return bool(ship) and ship.group(1) == heading.get("ship")


def ended(line, heading):
    """True when the quantum drive of this heading's ship stopped: there, or
    cut short (the log does not say which). Other ships' lines (a group) do
    not count. No line says a route was taken away: it expires."""
    return bool(_END.search(line)) and _ours(line, heading)


def jump_started(line, heading):
    """Time the jump of this heading's ship started, or None."""
    return line_time(line) if _JUMP_START.search(line) and _ours(line, heading) else None


def zone_change(line):
    """(time, True when into a system's open space) for an inventory zone
    change, or None."""
    m = _ZONE.search(line)
    if not m or m.group(2) == "0":
        return None
    return line_time(line), m.group(2) in SPACE_ZONES


def fuel(line, heading):
    """The fuel estimate of this heading's route, or None."""
    m = _FUEL.search(line)
    ship = _SHIP.search(line)
    if not m or not ship or ship.group(1) != heading.get("ship") or m.group(2) == "0":
        return None
    return float(m.group(2))


def flight_seconds(drive, km):
    """In-flight time for a distance (SC-map js/quantum.js, from
    ScDataDumper): the acceleration ramps from accel1 to accel2 up to top
    speed, cruise, the same ramp to stop."""
    vmax, a1, a2 = drive["speed"], drive["accel1"], drive["accel2"]
    distance = km * 1000
    if distance <= 0:
        return 0
    t_ramp = 2 * vmax / (a1 + a2)

    def ramp(t):
        return 0.5 * a1 * t * t + (a2 - a1) * t ** 3 / (6 * t_ramp)
    if distance >= 2 * ramp(t_ramp):
        return 2 * t_ramp + (distance - 2 * ramp(t_ramp)) / vmax
    low, high = 0, t_ramp  # never at top speed: the half-trip time
    for _ in range(60):
        mid = (low + high) / 2
        low, high = (mid, high) if ramp(mid) < distance / 2 else (low, mid)
    return low + high


def jump_seconds(fuel, drive, started=False):
    """From the pick to the arrival (from the jump start when `started`),
    for a route's fuel estimate and the ship's drive, or None."""
    if not fuel or not drive or not drive.get("fuelPerGm"):
        return None
    flight = flight_seconds(drive, fuel / drive["fuelPerGm"])
    return flight if started else ALIGN_S + drive["spool"] + flight


def cut_short(line, heading):
    """The drive stopped well before the jump could end: from its length
    by the fuel estimate, else the travel time asked for (from a place we
    may have left). Unknown: False, we take it as there."""
    t, start = line_time(line), heading.get("picked_at")
    if heading.get("seconds"):
        need = heading["seconds"]
    elif heading.get("eta") and start:
        need = heading["eta"] - start
    else:
        return False
    return bool(t and start and t - start < CUT_SHARE * need)


def expired(heading, now):
    """Past its arrival + AFTER_ETA_S, or WITHOUT_ETA_S old without one."""
    if heading.get("eta"):
        return now > heading["eta"] + AFTER_ETA_S
    return now > (heading.get("picked_at") or now) + WITHOUT_ETA_S
