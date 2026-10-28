"""Active hauling contracts, rebuilt from mission events.

A hauling contract has one pickup and one delivery objective per cargo:
`pickup_<phase>_<n>` pairs with `dropoff_<phase>_<n>`. We call each pair
a leg.
"""
import re
from dataclasses import dataclass, field

from .logparser import parse_delivery, parse_line, parse_pickup, strip_tags, title_origins, title_route

_OBJECTIVE_ID = re.compile(r"^(?P<role>pickup|dropoff)_(?P<phase>[0-9a-f-]+)_(?P<n>\d+)$")

ENDED_STATES = {"completed", "failed", "abandoned"}

# Abandoning a contract in the mobiGlas logs nothing we can tell apart from
# untracking it (both only take its markers out of the player's DataBank).
# The player hides an abandoned contract by hand.



# How sure we are of a place name.
EXACT, GUESS, UNKNOWN = "exact", "guess", "unknown"

# Contract codes end with the cargo, abbreviated, in leg order:
# HaulCargo_AToB_Interstellar_Medium_DistSpri_PartyFav_ElecEq_Souv.
# These tokens are the rest of the code.
_CODE_WORDS = {
    "haulcargo", "interstellar", "local", "planetary", "stellar", "solar", "extrasmall",
    "small", "medium", "large", "bulk", "supply", "intro", "rehire", "stanton", "pyro", "nyx",
}
_ROUTE_SHAPE = re.compile(r"(?i)^(?:atob|(?:single|multi)\d*to(?:single|multi)\d*|linearchain|rounddelivery)$")

# Contract shapes with a single destination for every cargo.
SINGLE_DROPOFF = re.compile(r"(?i)_(?:atob|multi\d*tosingle)_")
# AToB contracts define one PickupLocation in the game data: every cargo
# is picked up there.
SINGLE_PICKUP = re.compile(r"(?i)_atob_")

# Seen in game; anything else is learnt from the log as contracts come.
KNOWN_ITEMS = {
    "DistSpri": "Distilled Spirits",
    "PartyFav": "Party Favors",
    "ElecEq": "Audio-Visual Equipment",
    "Souv": "Souvenirs",
}


def cargo_tokens(code):
    """Abbreviated cargo names from a contract code, in leg order."""
    parts = (code or "").split("_")
    if not parts or parts[0].lower() != "haulcargo":
        parts = parts[parts.index("HaulCargo"):] if "HaulCargo" in parts else []
    return [p for p in parts[1:]
            if p and p.lower() not in _CODE_WORDS and not _ROUTE_SHAPE.match(p) and not p.isdigit()]


# FTL_Courier_Nyx_MicroSatellite_Rank0: system and package, nothing else is logged.
_COURIER_CODE = re.compile(r"(?i)courier_(?:(?P<system>stanton|pyro|nyx)_)?(?P<item>[a-z][a-z0-9]*?)(?:_|$)")


def courier_hint(code):
    """(system, package) from a courier contract code, or (None, None)."""
    m = _COURIER_CODE.search(code or "")
    if not m:
        return None, None
    item = m.group("item")
    if item.lower().startswith("rank"):
        item = None
    else:
        item = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", item)
    system = m.group("system")
    return (system.capitalize() if system else None), item


def _abbreviates(token, name):
    """True if each capitalised chunk of the token starts a word of the name in order
    ("DistSpri" -> "Distilled Spirits")."""
    chunks = re.findall(r"[A-Z][a-z0-9]*|[a-z0-9]+", token)
    words = re.findall(r"[A-Za-z0-9]+", name)
    if not chunks or len(chunks) > len(words):
        return False
    return all(w.lower().startswith(c.lower()) for c, w in zip(chunks, words))


@dataclass
class Leg:
    key: tuple
    item: str = None
    amount: int = 0
    total: int = 0
    dest_text: str = None
    pickup_text: str = None
    # Every position the game gave for the marker: the frame changes with the
    # zone the player is in, and only some frames can be named.
    pickup_positions: list = field(default_factory=list)
    dropoff_positions: list = field(default_factory=list)
    # Cargo name read from the contract code, when the log has no objective text.
    guessed_item: str = None
    picked: bool = False
    delivered: bool = False
    withdrawn: bool = False

    @property
    def index(self):
        return self.key[1]

    @property
    def pickup_pos(self):
        return self.pickup_positions[-1] if self.pickup_positions else None

    @property
    def dropoff_pos(self):
        return self.dropoff_positions[-1] if self.dropoff_positions else None

    @property
    def cargo(self):
        """Cargo name, "~" in front when read from the contract code."""
        if self.item:
            return self.item
        return f"~{self.guessed_item}" if self.guessed_item else None


@dataclass
class Contract:
    id: str
    title: str = None
    code: str = None
    accepted: str = None
    legs: dict = field(default_factory=dict)
    # Markers in the player's DataBank: only the tracked contract has some.
    markers: set = field(default_factory=set)

    @property
    def tracked(self):
        return bool(self.markers)

    def leg(self, objective_id):
        m = _OBJECTIVE_ID.match(objective_id or "")
        if not m:
            return None, None
        key = (m.group("phase"), int(m.group("n")))
        if key not in self.legs:
            self.legs[key] = Leg(key)
        return m.group("role"), self.legs[key]

    @property
    def is_hauling(self):
        code = (self.code or "").lower()
        if "haulcargo" in code or "courier" in code:
            return True
        return any(l.item or l.pickup_text or l.pickup_positions for l in self.legs.values())

    def open_legs(self):
        return [l for l in sorted(self.legs.values(), key=lambda l: l.key) if not (l.delivered or l.withdrawn)]


@dataclass
class Stop:
    """A place where a leg starts or ends, with how sure we are of it."""
    name: str
    place: object = None
    confidence: str = UNKNOWN


class Tracker:
    def __init__(self, places=None, manual=None, items=None, hidden=None):
        self.places = places
        self.contracts = {}
        # Pickups the player set by hand: {"<mission>|<phase>|<n>": "Seraphim Station"}.
        self.manual = manual if manual is not None else {}
        # Cargo names learnt from contract codes: {"DistSpri": "Distilled Spirits"}.
        # Kept across sessions by the caller.
        self.items = items if items is not None else {}
        for token, name in KNOWN_ITEMS.items():
            self.items.setdefault(token, name)
        # Contracts hidden by hand (abandoned): mission ids, never reused.
        self.hidden = hidden if hidden is not None else set()

    def feed(self, line):
        ev = parse_line(line)
        if ev:
            self.apply(ev)
        return ev

    def apply(self, ev):
        mid = ev["mission"]
        kind = ev["type"]
        if kind in ENDED_STATES or (kind == "ended"):
            self.contracts.pop(mid, None)
            return
        c = self.contracts.get(mid)
        if c is None and kind == "marker_removed":
            return
        if kind == "position":
            c = c or self.contracts.setdefault(mid, Contract(mid))
            c.markers.add(ev["marker"])
        elif kind == "marker_removed":
            c.markers.discard(ev["marker"])
            return
        if c is None:
            c = self.contracts[mid] = Contract(mid)
        if kind == "accepted":
            c.title = strip_tags(ev["text"])
            c.accepted = ev["ts"]
            return
        if kind == "marker":
            c.code = ev["contract"]
        role, leg = c.leg(ev.get("objective"))
        if leg is None:
            return
        if kind == "objective":
            d = parse_delivery(ev["text"])
            if role == "dropoff" and d:
                leg.item, leg.amount, leg.total, leg.dest_text = d["item"], d["amount"], d["total"], d["dest"]
            elif role == "pickup":
                p = parse_pickup(ev["text"])
                if p:
                    leg.pickup_text = p["origin"]
                    leg.item = leg.item or p["item"]
        elif kind in ("marker", "position"):
            positions = leg.pickup_positions if role == "pickup" else leg.dropoff_positions
            if ev["pos"] not in positions:
                positions.append(ev["pos"])
        elif kind == "objective_state" and ev["state"] == "COMPLETED":
            if role == "pickup":
                leg.picked = True
            else:
                leg.delivered = True
                leg.picked = True
        elif kind == "objective_done":
            if role == "dropoff":
                leg.delivered = True
        elif kind == "objective_withdrawn":
            leg.withdrawn = True

    def hide(self, contract):
        """Drop a contract the player abandoned."""
        self.hidden.add(contract.id)

    def active(self):
        out = [c for c in self.contracts.values()
               if c.is_hauling and c.open_legs() and c.id not in self.hidden]
        for c in self.contracts.values():
            self._learn_items(c)
        for c in out:
            self._guess_items(c)
        return out

    # --- cargo names --------------------------------------------------------

    def _learn_items(self, c):
        """Pair the code's cargo tokens with the names the objectives gave."""
        tokens = cargo_tokens(c.code)
        legs = sorted(c.legs.values(), key=lambda l: l.key)
        named = [l for l in legs if l.item]
        if tokens and named and len(tokens) == len(legs) == len(named):
            for token, leg in zip(tokens, legs):
                self.items[token] = leg.item

    def _item_for(self, token):
        if token in self.items:
            return self.items[token]
        hits = {name for name in self.items.values() if _abbreviates(token, name)}
        return hits.pop() if len(hits) == 1 else token

    def _guess_items(self, c):
        tokens = cargo_tokens(c.code)
        if not tokens:
            _, package = courier_hint(c.code)
            for leg in c.legs.values():
                if not leg.item and package:
                    leg.guessed_item = package
            return
        legs = sorted(c.legs.values(), key=lambda l: l.key)
        for i, leg in enumerate(legs):
            if leg.item:
                continue
            # Only when the code lists exactly one cargo per leg: a
            # Multi2ToSingle "_Souv" contract carries four cargos in game.
            if len(tokens) == len(legs):
                leg.guessed_item = self._item_for(tokens[i])

    # --- places -----------------------------------------------------------

    def _stop(self, text, positions, confidence):
        place = None
        if self.places is not None:
            if text:
                place = self.places.resolve(text)
            # Newest first: the last frame the game used is often the best one.
            for pos in reversed(positions or []):
                if place is not None:
                    break
                place = self.places.match_position(*pos)
        if place is not None:
            return Stop(place.name, place, confidence)
        if text:
            return Stop(text, None, confidence)
        return None

    def _manual_key(self, contract, leg):
        return f"{contract.id}|{leg.key[0]}|{leg.key[1]}"

    def set_pickup(self, contract, leg, name):
        key = self._manual_key(contract, leg)
        if name:
            self.manual[key] = name
        else:
            self.manual.pop(key, None)

    def _own_pickup(self, contract, leg):
        """Pickup from this leg's own data: set by hand, objective text, marker."""
        manual = self.manual.get(self._manual_key(contract, leg))
        if manual:
            return self._stop(manual, None, EXACT)
        return self._stop(leg.pickup_text, leg.pickup_positions, EXACT)

    def pickup_of(self, contract, leg):
        stop = self._own_pickup(contract, leg)
        if stop:
            return stop
        origins = [self._stop(o, None, GUESS) for o in title_origins(contract.title)]
        if SINGLE_PICKUP.search(contract.code or ""):
            # One pickup for the whole contract: any leg's, else the title's.
            for other in sorted(contract.legs.values(), key=lambda l: l.key):
                found = self._own_pickup(contract, other) if other is not leg else None
                if found and found.place is not None:
                    return Stop(found.name, found.place, EXACT)
            if len(origins) == 1:
                return Stop(origins[0].name, origins[0].place, EXACT)
        if len(origins) > 1:
            # Titles listing every pickup (MultiToSingle): remove the ones the
            # other legs already have; one left means it is this leg's.
            taken = set()
            for other in contract.legs.values():
                if other is not leg:
                    found = self._own_pickup(contract, other)
                    if found:
                        taken.add(found.name)
            left = [o for o in origins if o.name not in taken]
            if len(left) == 1:
                return Stop(left[0].name, left[0].place, EXACT)
            if left:
                return Stop(" / ".join(o.name for o in left), None, GUESS)
        # A title with one pickup shows the mission's first pickup only: the
        # first leg's, when all legs share one phase. With one phase per
        # pickup we cannot tell which leg it is.
        phases = {l.key[0] for l in contract.legs.values()}
        if len(phases) == 1:
            # Multi2ToSingle: pickup_<n> are places, dropoff_<n> cargos
            # (2 pickups, 4 cargos). In game the first cargo comes from the
            # first place and the others from the last one: Levski, then
            # Baijini Point x3 (seen on three contracts).
            known = [(l.index, self._own_pickup(contract, l)) for l in contract.legs.values()
                     if l is not leg and l.index < leg.index]
            known = [(i, s) for i, s in known if s and s.place is not None]
            if known:
                _, last = max(known, key=lambda k: k[0])
                return Stop(last.name, last.place, GUESS)
        if origins and leg.index == 0 and len(phases) == 1:
            return origins[0]
        return self._unknown(contract)

    def _unknown(self, contract):
        # Courier markers sit inside stations: at least say which system.
        system, _ = courier_hint(contract.code)
        return Stop(f"? ({system})" if system else "?", None, UNKNOWN)

    def dropoff_of(self, contract, leg):
        stop = self._own_dropoff(contract, leg)
        if stop.confidence == EXACT or not SINGLE_DROPOFF.search(contract.code or ""):
            return stop
        # One destination for the whole contract: any leg that knows it, or
        # the title, gives it for all.
        for other in sorted(contract.legs.values(), key=lambda l: l.key):
            if other is not leg:
                found = self._own_dropoff(contract, other, use_title=False)
                if found.place is not None:
                    return Stop(found.name, found.place, EXACT)
        _, dest = title_route(contract.title)
        found = self._stop(dest, None, EXACT) if dest else None
        return found if found and found.place is not None else stop

    def _own_dropoff(self, contract, leg, use_title=True):
        stop = None
        if leg.dest_text and self.places is not None and self.places.resolve(leg.dest_text):
            stop = self._stop(leg.dest_text, None, EXACT)
        if stop is None and leg.dropoff_positions:
            stop = self._stop(None, leg.dropoff_positions, EXACT)
        if stop is None and use_title:
            _, dest = title_route(contract.title)
            stop = self._stop(dest, None, GUESS) if dest else None
        if stop is None and leg.dest_text:
            stop = Stop(leg.dest_text, None, UNKNOWN)
        return stop or (self._unknown(contract) if use_title else Stop("?", None, UNKNOWN))
