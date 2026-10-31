"""Glue: log → tracker → plan. Shared by the overlay and the console."""
import re
import time
from pathlib import Path

from . import config, heading, titles
from .api import TravelTimes, local_times
from .locations import location_code, place_for_code
from .logparser import parse_line
from .logsource import LogRestarted, LogSource
from .places import Places
from .routing import Plan, build_actions, plan
from .ships import boarded_ship, channel_change, load_cargo, load_drives, load_ships, login_handle, piloted_class, ship_for_class
from .tracker import PAGE_FIT, Tracker


# Previous sessions searched for the last ship flown (short sessions, such
# as a quick contract test, often have none).
BACKUPS_FOR_SHIP = 20


# Time spent at each stop (approach, landing, cargo, take-off), for the
# arrival times: the quantum times do not count it.
STOP_SECONDS = 300

_QT_START = re.compile(r"CSCItemNavigation::CalculateRoute\|Projected Start Location is (.+?) for route to destination (\S+)")


SPACE = object()  # the quantum drive stopped between two places

class Session:
    def __init__(self, log_path, scmap_data, ship, start=None, manual=None, items=None, hidden=None, pages=None):
        self.places = Places.load(scmap_data)
        self.ships = load_ships(scmap_data)
        self._ship_names = set(self.ships)
        self.drives = load_drives(scmap_data)  # {ship: quantum drive}, for the jump length
        self.cargo = load_cargo(scmap_data)  # {ship: SCU}, empty with older SC-map data
        # Last ship the player flew (SC-map name), from the logs.
        self.last_ship = self._last_ship_in_backups(log_path)
        self.manual = manual if manual is not None else {}
        self.items = items if items is not None else {}
        self.hidden = set(hidden or ())
        # What the OCR read on contract pages, {mission: objectives}: the log
        # replay does not bring it back, so we apply it again.
        self.pages = pages if pages is not None else {}
        self.start = start
        # Contracts accepted while we follow the log (not on replay): their
        # page is on screen right now.
        self.accepted = []
        # Contracts never carry over to the next game session: the current
        # Game.log is enough.
        self.source = LogSource(log_path, backups=False)
        # Contract title templates of the global.ini the game reads.
        self.titles = titles.load_for_log(log_path)
        self.travel = TravelTimes(self.places, ship, cache_file=config.folder() / "travel-cache.json")
        self._rebuild()

    def _rebuild(self):
        self.tracker = Tracker(self.places, self.manual, items=self.items, hidden=self.hidden,
                               titles=self.titles)
        self.here = None
        self.heading = None
        self.handle = None
        self.aboard = None
        for line in self.source.initial():
            self.tracker.feed(line)
            self._see_ship(line)
            self._see_crew(line)
            self._see_heading(line)
            self._see_place(line)
        for mission in list(self.pages):
            fit = self.tracker.page_fit(mission, self.pages[mission]) if mission in self.tracker.contracts else 0
            if fit is not None and fit < PAGE_FIT and mission in self.tracker.contracts:
                # Another contract's page, read by mistake: not again, nor
                # the pickups it set.
                origins = {e.get("origin") for e in self.pages.pop(mission)}
                for key in [k for k, place in self.manual.items() if k.startswith(f"{mission}|") and place in origins]:
                    del self.manual[key]
            elif mission in self.tracker.contracts:
                self.tracker.apply_page(mission, self.pages[mission])
            else:  # contract over, or from an older game session
                del self.pages[mission]

    def _see_place(self, line):
        """Where the player is: the local inventory of a place loads, or a
        quantum route is plotted ("Projected Start Location is Levski for
        route to destination …", logged when a destination is picked)."""
        arrived, self._arrived = self._arrived, None
        if arrived:  # set by _see_heading on this line
            self.here = None if arrived is SPACE else arrived
            return True
        zone = heading.zone_change(line)
        if zone:
            self._zone = zone
            stop = self._stop
            if stop and stop["at"] and zone[0] - stop["at"] <= heading.ZONE_AFTER_S and zone[1] != stop["cut"]:
                # Into the destination's zone right after the stop: there
                # (or back into open space: cut short after all).
                stop["cut"] = zone[1]
                self.here = None if zone[1] else stop["to"]
                return True
            return False
        m = _QT_START.search(line)
        if m:
            place = self.places.resolve(m.group(1)) or self._gateway(m.group(1), m.group(2))
            body = None if place else self.places.body(m.group(1))  # "Bloom": near a planet
            here = self.places.get(self.here) if self.here else None
            if body and not (here and here.body == body):  # from Orbituary the game says "Bloom"
                self.here = body
                return True
        elif "RequestLocationInventory" in line:
            place = place_for_code(location_code(line), self.places)
        else:
            return False
        if place:
            self.here = place.name
            return True
        return False

    def _see_heading(self, line):
        """The quantum destination picked last, until we get there. True
        when it changed. Before _see_place: a start "Stanton Gateway" needs
        the last place."""
        new = heading.parse(line, self.places, self._gateway)
        if new:
            # "Pyro System": in open space, so just left the last place.
            new["from"] = new["from"] or self.here
            new["eta"] = None  # the overlay asks the travel time
            # When the ship really starts moving: after aligning and spooling.
            drive = self.drives.get(self.travel.ship)
            new["jump_at"] = (new["picked_at"] + heading.ALIGN_S + drive["spool"]
                              if drive and new.get("picked_at") else None)
            self.heading = new
            self._stop = None  # a new route: the last stop is settled
            return True
        if self.heading and not self.heading.get("fuel"):
            self.heading["fuel"] = heading.fuel(line, self.heading)
            seconds = heading.jump_seconds(self.heading["fuel"], self.drives.get(self.travel.ship))
            if seconds and self.heading.get("picked_at"):
                # The distance from where the ship really is (open space
                # too): better than the API's from the last place.
                self.heading["seconds"] = seconds
                self.heading["eta"] = self.heading["picked_at"] + seconds
                return True
        started = heading.jump_started(line, self.heading) if self.heading else None
        if started and not self.heading.get("started"):
            # The real start: the dot leaves now, arrives a flight later.
            self.heading["started"] = self.heading["jump_at"] = started
            flight = heading.jump_seconds(self.heading.get("fuel"), self.drives.get(self.travel.ship), True)
            if flight is not None:
                self.heading["eta"] = started + flight
            return True
        if self.heading and heading.ended(line, self.heading):
            # The drive stopped: there, or cut short on the way (before the
            # estimated arrival): then in space, no place.
            # The zone we went into last, just before the stop, says it
            # better than the time; one coming right after corrects it
            # (_see_place).
            t = heading.line_time(line)
            zone = self._zone if self._zone and t and t - self._zone[0] <= heading.ZONE_BEFORE_S else None
            cut = zone[1] if zone else heading.cut_short(line, self.heading)
            self._stop = {"at": t, "to": self.heading["to"], "cut": cut}
            self._arrived = SPACE if cut else self.heading["to"]
            self.heading = None
            return True
        if self.heading and "RequestLocationInventory" in line:
            place = place_for_code(location_code(line), self.places)
            if place and place.name == self.heading["to"]:
                self.heading = None  # arrived
                return True
        return False

    def heading_expired(self, now):
        """Drop a heading too old to be true. True when it did."""
        if self.heading and heading.expired(self.heading, now):
            self.heading = None
            return True
        return False

    def _gateway(self, name, destination):
        """"Stanton Gateway" is in Pyro and in Nyx: the system is the one the
        destination code names (rs_ext_nyx-castra_jp1), else the last place's."""
        here = self.places.system_of(self.here) if self.here else None  # a place or a planet
        systems = [s for s in ("Stanton", "Pyro", "Nyx") if s.lower() in destination.lower()]
        systems += [here] if here else []
        for system in systems:
            place = self.places.get(f"{name} ({system})")
            if place:
                return place
        return None

    def _see_ship(self, line):
        name = self._ship_in(line)
        # Snubs and ground vehicles have no quantum drive: keep the last ship.
        if name and name != self.last_ship:
            self.last_ship = name
            return True
        return False

    def _see_crew(self, line):
        """Our in-game name, and whose ship we are aboard (another player's:
        the team map shows us with them). True when that changed."""
        handle = login_handle(line)
        if handle:
            self.handle = handle
            return False
        change = channel_change(line)
        if not change:
            return False
        act, ship, owner = change
        if act == "joined":
            aboard = {"owner": owner, "ship": ship} if owner != self.handle else None
        elif self.aboard == {"owner": owner, "ship": ship}:
            aboard = None
        else:
            return False
        changed, self.aboard = aboard != self.aboard, aboard
        return changed

    def _ship_in(self, line):
        if "channel" in line:
            return boarded_ship(line, self._ship_names)
        cls = piloted_class(line)
        return ship_for_class(cls, self.ships) if cls else None

    def _last_ship_in_backups(self, log_path):
        """Last ship flown in the previous sessions, newest first."""
        folder = Path(log_path).parent / "logbackups"
        try:
            files = sorted(folder.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:BACKUPS_FOR_SHIP]
        except OSError:
            return None
        for f in files:
            found = None
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        found = self._ship_in(line) or found
            except OSError:
                continue
            if found:
                return found
        return None

    def update(self):
        """Read new log lines. True when a contract changed."""
        try:
            lines = self.source.poll()
        except LogRestarted:
            self._rebuild()
            return True
        changed = False
        for line in lines:
            if self._see_ship(line):
                self.ship_changed = True
            if self._see_crew(line):
                self.aboard_changed = True
            if "channel" in line and boarded_ship(line, self._ship_names):
                self.boarded = True
            if self._see_heading(line):
                self.heading_changed = True
            if self._see_place(line):
                self.place_changed = True
            if "Contract Accepted" in line:
                ev = parse_line(line)
                if ev and ev["type"] == "accepted" and ev.get("mission"):
                    self.accepted.append(ev["mission"])
            if self.tracker.feed(line):
                changed = True
        return changed

    # Set when the player boards a ship while we follow the log (not on replay).
    boarded = False
    # Set when the ship to use changes (boarded, or left the pilot seat).
    ship_changed = False
    # Set when the log says where the player is (local inventory of a place).
    place_changed = False
    here = None
    # Quantum destination: {"from", "to", "picked_at", "eta"} (unix seconds), or None.
    heading = None
    heading_changed = False
    aboard_changed = False
    handle = None  # our in-game name
    aboard = None  # {"owner", "ship"}: another player's ship we are in
    _arrived = None  # where the quantum drive stopped, for _see_place on the same line
    _zone = None  # (time, into open space) of the last inventory zone change
    _stop = None  # the last quantum stop: {"at", "to", "cut"}, corrected by a zone change after it

    def snapshot(self):
        """Actions to plan, read on the thread that feeds the tracker."""
        return build_actions(self.tracker)

    def keep_page(self, mission, entries):
        """Remember a page read, then apply it."""
        self.pages[mission] = entries
        return self.tracker.apply_page(mission, entries)

    def capacity(self):
        """Cargo SCU of the ship we plan for, or None when unknown."""
        return self.cargo.get(self.travel.ship) or None

    def plan(self, snapshot=None):
        """Route for a snapshot. Slow (API calls): fine on a worker thread."""
        actions, unplaced = snapshot or self.snapshot()
        start = self.start if self.start and self.places.get(self.start) else None
        self.travel.prefetch({a.place for a in actions} | ({start} if start else set()))
        stops, total = plan(actions, self.travel.seconds, start)
        prev, at = start, time.time()
        for stop in stops:
            stop.via = self.travel.systems_between(prev, stop.place) if prev else None
            at += stop.seconds
            stop.arrival = at
            # Landing, loading or unloading, take-off: before the next leg.
            at += STOP_SECONDS
            prev = stop.place
        for stop, local in zip(stops, local_times([(s.place, s.arrival) for s in stops])):
            stop.local = local
        return Plan(stops, unplaced, total)
