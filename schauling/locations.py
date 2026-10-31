"""Where the player is, from the location inventory the game loads.

"<RequestLocationInventory> Player[...] requested inventory for
Location[Nyx_Levski]" comes when the local inventory of a station, city or
outpost loads around the player. The code is an internal name; this maps it
to an SC-map place.
"""
import re

from .picker import matches

_REQUEST = re.compile(r"<RequestLocationInventory> .*?requested inventory for Location\[(?P<code>[^\]]+)\]")

# Codes of the log -> SC-map name, before any pattern (rules.json fills it).
ALIASES = {}
# Rest stops in low orbit: RR_<body>_LEO.
_LEO = {"ARC": "Baijini Point", "CRU": "Seraphim Station", "HUR": "Everus Harbor", "MIC": "Port Tressler"}
# Landing zones: Stanton<n>_<city>.
_CITIES = {"newbabbage": "New Babbage"}


def location_code(line):
    """Location code of a local inventory request, or None."""
    m = _REQUEST.search(line)
    return m.group("code") if m else None


def _split_camel(text):
    """'HDMSEdmond' -> 'HDMS Edmond', 'DistributionCentre' -> 'Distribution Centre'."""
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    return re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", text)


def place_for_code(code, places):
    """SC-map place for a location code, or None."""
    if not code:
        return None
    if code in ALIASES:
        return places.get(ALIASES[code])
    parts = code.split("_")
    if parts[0] == "RR" and len(parts) == 3 and _PYRO.fullmatch(parts[1]):
        return _pyro_station(_PYRO.fullmatch(parts[1]).group(1), parts[2], places)
    if parts[0] == "RR" and len(parts) == 3:
        body, spot = parts[1], parts[2]
        if spot == "LEO":
            return places.get(_LEO.get(body))
        if body == "JP":
            # RR_JP_NyxPyro: the gateway in Nyx towards Pyro.
            here, to = _split_camel(spot).split(" ", 1) if " " in _split_camel(spot) else (spot, "")
            return places.resolve(f"{here} - {to} Jump Point")
        m = re.fullmatch(r"L(\d)", spot)
        if m:  # RR_HUR_L2 -> "HUR-L2 Faithful Dream Station"
            return _only(matches(f"{body} L{m.group(1)}", _items(places, "Space station")), places)
        return None
    # Stanton1_Lorville, Nyx_Levski, Stanton1_HurdynMining_HDMSEdmond,
    # Stanton1_DistributionCentre_Covalex_S1DC06, Stanton3a_Shubin_SAL2.
    name = parts[-1]
    direct = places.get(_CITIES.get(name.lower(), name))
    if direct:
        return direct
    query = _split_camel(name)
    hits = matches(query, _items(places)) or matches(re.sub(r"(?<=[A-Za-z])(?=\d)", " ", name), _items(places))
    return _only(hits, places)


# Pyro planets by number, as Stanton's go by HUR, CRU...: RR_P3_LEO,
# RR_PYR3_L1, RR_Pyro3_LEO. Not seen in a log yet: Stanton's pattern.
_PYRO = re.compile(r"(?i)(?:p|pyr|pyro)(\d)")


def _pyro_station(n, spot, places):
    """The one station in low orbit of Pyro planet n (LEO: Orbituary for
    Bloom), or at its Lagrange point (L1: Starlight Service Station)."""
    planet = getattr(places, "numbers", {}).get(("pyro", n))
    m = re.fullmatch(r"(?i)L(\d)", spot)
    body = f"PYR{n} L{m.group(1)}" if m else planet if spot.upper() == "LEO" else None
    stations = [p for p in places.places if body and p.body == body and p.type.lower() == "space station"]
    return stations[0] if len(stations) == 1 else None


def _items(places, type_=None):
    return [(p.name, p.name) for p in places.places if type_ is None or p.type.lower() == type_.lower()]


def _only(hits, places):
    """The best match when it is clear: one hit, or a name much closer than the next."""
    if not hits:
        return None
    return places.get(hits[0][1]) if len(hits) == 1 else None
