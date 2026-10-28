"""Glue: log → tracker → plan. Shared by the overlay and the console."""
from pathlib import Path

from . import config
from .api import TravelTimes
from .locations import location_code, place_for_code
from .logsource import LogRestarted, LogSource
from .places import Places
from .routing import Plan, build_actions, plan
from .ships import boarded_ship, load_ships, piloted_class, ship_for_class
from .tracker import Tracker


# Previous sessions searched for the last ship flown (short sessions, such
# as a quick contract test, often have none).
BACKUPS_FOR_SHIP = 20


class Session:
    def __init__(self, log_path, scmap_data, ship, start=None, manual=None, items=None, hidden=None):
        self.places = Places.load(scmap_data)
        self.ships = load_ships(scmap_data)
        self._ship_names = set(self.ships)
        # Last ship the player flew (SC-map name), from the logs.
        self.last_ship = self._last_ship_in_backups(log_path)
        self.manual = manual if manual is not None else {}
        self.items = items if items is not None else {}
        self.hidden = set(hidden or ())
        self.start = start
        # Contracts never carry over to the next game session: the current
        # Game.log is enough.
        self.source = LogSource(log_path, backups=False)
        self.travel = TravelTimes(self.places, ship, cache_file=config.folder() / "travel-cache.json")
        self._rebuild()

    def _rebuild(self):
        self.tracker = Tracker(self.places, self.manual, items=self.items, hidden=self.hidden)
        self.here = None
        for line in self.source.initial():
            self.tracker.feed(line)
            self._see_ship(line)
            self._see_place(line)

    def _see_place(self, line):
        """Where the player is, when the local inventory of a place loads."""
        if "RequestLocationInventory" not in line:
            return False
        place = place_for_code(location_code(line), self.places)
        if place:
            self.here = place.name
            return True
        return False

    def _see_ship(self, line):
        name = self._ship_in(line)
        # Snubs and ground vehicles have no quantum drive: keep the last ship.
        if name and name != self.last_ship:
            self.last_ship = name
            return True
        return False

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
            if "channel" in line and boarded_ship(line, self._ship_names):
                self.boarded = True
            if self._see_place(line):
                self.place_changed = True
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

    def snapshot(self):
        """Actions to plan, read on the thread that feeds the tracker."""
        return build_actions(self.tracker)

    def plan(self, snapshot=None):
        """Route for a snapshot. Slow (API calls): fine on a worker thread."""
        actions, unplaced = snapshot or self.snapshot()
        start = self.start if self.start and self.places.get(self.start) else None
        self.travel.prefetch({a.place for a in actions} | ({start} if start else set()))
        stops, total = plan(actions, self.travel.seconds, start)
        return Plan(stops, unplaced, total)
