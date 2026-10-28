"""Place names and positions, from the SC-map data files.

The game names a place in several ways ("Teasa Spaceport in Lorville",
"Teasa Spaceport", "Lorville"); SC-map knows it as "Lorville". `resolve`
turns any of these into the SC-map name. `match_position` finds the place
under a mission marker: marker positions are in metres, relative to the
planet or moon they are on, the same frame as SC-map (in km).
"""
import csv
import difflib
import math
import re
from dataclasses import dataclass
from pathlib import Path

# Spaceports are not in SC-map: they live in the landing zone of the city.
SPACEPORTS = {
    "teasa spaceport": "Lorville",
    "august dunlow spaceport": "Orison",
    "riker memorial spaceport": "Area18",
    "nb int. spaceport": "New Babbage",
    "nb int spaceport": "New Babbage",
    "new babbage interstellar spaceport": "New Babbage",
}

# Names that cover a whole system or planet: not a stop.
VAGUE = re.compile(r"(?i)^(.+ system|stanton|pyro|nyx)$")

# Farther than this from every known place: no match. Cities spread over
# ~10 km (Orison markers sit 9 km from its SC-map point); event sites
# missing from SC-map sit 20+ km from the nearest known place.
MATCH_RADIUS_KM = 15.0


@dataclass(frozen=True)
class Place:
    name: str
    type: str
    body: str
    system: str
    x: float
    y: float
    z: float

    def distance_km(self, x, y, z):
        return math.dist((self.x, self.y, self.z), (x, y, z))


def _key(name):
    return re.sub(r"\s+", " ", name.strip().lower())


def _xyz(row):
    try:
        return tuple(float(row[k]) for k in ("coordinateX", "coordinateY", "coordinateZ"))
    except (TypeError, ValueError):
        return None


# Beyond this, a marker position is in the system frame, not a body's.
SYSTEM_FRAME_KM = 100_000
# Largest match distance in a body frame.
BODY_FRAME_MAX_KM = 300


class Places:
    def __init__(self, places, bodies=None):
        self.places = list(places)
        # Body positions in their system, km: {"Delamar": (x, y, z)}.
        self.bodies = bodies or {}
        self._by_key = {_key(p.name): p for p in self.places}

    @classmethod
    def load(cls, data_dir):
        rows, bodies = [], {}
        with open(Path(data_dir) / "locations.csv", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                xyz = _xyz(r)
                if xyz:
                    rows.append(Place(r["name"], r["type"], r["parentBody"], r["parentStar"], *xyz))
        with open(Path(data_dir) / "bodies.csv", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                xyz = _xyz(r)
                if xyz:
                    bodies[r["name"]] = xyz
        return cls(rows, bodies)

    def get(self, name):
        return self._by_key.get(_key(name)) if name else None

    def resolve(self, text):
        """SC-map place for a name shown in game, or None."""
        if not text:
            return None
        text = re.sub(r"<[^>]+>", "", text).strip(" .")
        if not text or VAGUE.match(text):
            return None
        gateway = self._jump_point_gateway(text)
        if gateway:
            return gateway
        candidates = [text]
        # "Teasa Spaceport in Lorville", "Area18 on ArcCorp",
        # "Orbituary above Pyro III", "Megumi Refueling at the L5 Lagrange of Pyro VI",
        # "Grim HEX within Yela's asteroid belt"
        m = re.match(r"(?i)^(.+?) (?:in|on|above|at the|at|within|near|inside|orbiting|around) (.+)$", text)
        if m:
            candidates += [m.group(1), m.group(2)]
        for c in candidates:
            p = self.get(SPACEPORTS.get(_key(c), c))
            if p:
                return p
        # Last resort: a single place whose name starts with the text.
        k = _key(text)
        hits = [p for key, p in self._by_key.items() if key.startswith(k)]
        return hits[0] if len(hits) == 1 else None

    def closest(self, text):
        """resolve(), forgiving OCR misreads ("Port Tresler", "HUR-LI")."""
        place = self.resolve(text)
        if place or not text:
            return place
        text = re.sub(r"(?i)\s+(?:in|on|above|at the|at|within|near|inside|orbiting|around)\s+.*$", "", text)
        text = re.sub(r"(?<=-L)[Il|](?=\b)", "1", text)  # HUR-LI → HUR-L1
        place = self.resolve(text)
        if place:
            return place
        names = {_key(p.name): p for p in self.places}
        names.update({_key(k): self.get(v) for k, v in SPACEPORTS.items() if self.get(v)})
        hit = difflib.get_close_matches(_key(text), list(names), n=1, cutoff=0.8)
        return names[hit[0]] if hit else None

    def _jump_point_gateway(self, text):
        """"Nyx - Pyro Jump Point" is the gateway station SC-map calls
        "Pyro Gateway" (in Nyx) or "Pyro Gateway (Stanton)" (in Stanton)."""
        m = re.match(r"(?i)^(?P<here>[\w' ]+?)\s*-\s*(?P<to>[\w' ]+?) jump point$", text)
        if not m:
            return None
        here, to = m.group("here").strip(), m.group("to").strip()
        for name in (f"{to} Gateway ({here})", f"{to} Gateway"):
            p = self.get(name)
            if p and p.system.lower() == here.lower():
                return p
        return None

    def match_position(self, x, y, z):
        """Place under a marker position (metres), or None.

        The game writes a marker in the frame of the zone the player is in:
        the system (far from its origin), a planet or moon (a few thousand
        km), or a station or city (close to its origin, unnamed).
        """
        return self.nearest(x, y, z, MATCH_RADIUS_KM)[0]

    def nearest(self, x, y, z, radius_km):
        """(place, distance in km) closest to a position in metres, or (None, None)."""
        here = (x / 1000, y / 1000, z / 1000)
        dist0 = math.dist((0, 0, 0), here)
        if dist0 > SYSTEM_FRAME_KM:
            return self._nearest(here, True, radius_km)
        # Close to the origin: inside a station or a city, whose own frame
        # we cannot name.
        if dist0 < 50:
            return None, None
        # Every body has its own frame, so the same numbers fit places on
        # other bodies too: stay close.
        return self._nearest(here, False, min(radius_km, BODY_FRAME_MAX_KM))

    def _nearest(self, here, system_frame, radius_km):
        best, best_d = None, radius_km
        for p in self.places:
            if system_frame:
                # Planets turn, so only stations and places at a body's centre
                # (Levski in Delamar) keep a fixed system position.
                body = self.bodies.get(p.body)
                if body is None:
                    continue
                at = (body[0] + p.x, body[1] + p.y, body[2] + p.z)
            else:
                # Places at 0,0,0 sit at their body's centre: no body-frame
                # marker can be there.
                if p.x == p.y == p.z == 0:
                    continue
                at = (p.x, p.y, p.z)
            d = math.dist(at, here)
            if d < best_d:
                best, best_d = p, d
        return (best, best_d) if best else (None, None)


# What /showlocation puts in the clipboard:
# "Coordinates: x:-18930668553.912502 y:-2609269001.429949 z:-1249452.071622"
_COORDS = re.compile(r"(?i)x\s*:\s*(-?[\d.]+)[\s,]+y\s*:\s*(-?[\d.]+)[\s,]+z\s*:\s*(-?[\d.]+)")

# A start point can be farther than a marker: in quantum, between two places.
START_RADIUS_KM = 5_000_000


def parse_coordinates(text):
    """(x, y, z) in metres from /showlocation text, or None."""
    m = _COORDS.search(text or "")
    return tuple(float(v) for v in m.groups()) if m else None
