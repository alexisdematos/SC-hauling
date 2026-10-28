"""Quantum travel times from the VerseTime API (SC-map)."""
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

API_URL = "https://versetime-api.ofs.workers.dev"
DEFAULT_SHIP = "Drake Caterpillar"
USER_AGENT = "SC-hauling/0.1"

# Jump point transit, gateway to gateway. Rough: the API does not model it.
JUMP_SECONDS = 600
# Cost of a leg we cannot price, so the planner still puts it somewhere.
UNKNOWN_SECONDS = 1800
# A quantum hop inside one system, when we cannot ask for the real one.
TYPICAL_HOP_SECONDS = 240
# Longest chain of jump points we consider between two systems.
MAX_JUMPS = 3


class TravelTimes:
    """Seconds between two SC-map places, cached in memory and on disk."""

    def __init__(self, places, ship=DEFAULT_SHIP, cache_file=None, fetch=None, api_url=API_URL):
        self.places = places
        self.ship = ship
        self.api_url = api_url
        self.cache_file = Path(cache_file) if cache_file else None
        self._fetch = fetch or self._fetch_http
        self._lock = threading.Lock()
        self._cache = {}
        if self.cache_file and self.cache_file.is_file():
            try:
                self._cache = json.loads(self.cache_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._cache = {}

    def _key(self, a, b):
        return f"{self.ship}|{a}|{b}"

    def _fetch_http(self, a, b):
        """(seconds, None) or (None, error text)."""
        q = urllib.parse.urlencode({"from": a, "to": b, "ship": self.ship})
        # Cloudflare answers 403 to Python's default User-Agent.
        req = urllib.request.Request(f"{self.api_url}/route?{q}", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            try:
                data = json.load(e)
            except ValueError:
                return None, str(e)
        except (urllib.error.URLError, OSError, ValueError) as e:
            return None, str(e)
        if "travel" in data:
            return data["travel"]["seconds"], None
        return None, data.get("error", "no travel time")

    def _direct(self, a, b):
        key = self._key(a, b)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        seconds, _err = self._fetch(a, b)
        with self._lock:
            self._cache[key] = seconds
        return seconds

    def _gateway(self, system, to_system):
        """Gateway station in `system` leading to `to_system`."""
        prefix = f"{to_system} gateway".lower()
        for p in self.places.places:
            if p.system == system and p.name.lower().startswith(prefix):
                return p.name
        return None

    def seconds(self, a, b):
        """Travel time from place a to place b (SC-map names)."""
        if a == b:
            return 0
        pa, pb = self.places.get(a), self.places.get(b)
        if pa and pb and pa.system != pb.system:
            costs = [self._through(a, b, path) for path in self._system_paths(pa.system, pb.system)]
            return min(costs) if costs else UNKNOWN_SECONDS + JUMP_SECONDS
        t = self._direct(a, b)
        return UNKNOWN_SECONDS if t is None else t

    def _hop(self, a, b):
        t = self._direct(a, b) if a and b else None
        return TYPICAL_HOP_SECONDS if t is None else t

    def _through(self, a, b, systems):
        """Seconds from a to b through gateways: systems = [from, ..., to]."""
        total, here = 0, a
        for s, t in zip(systems, systems[1:]):
            total += self._hop(here, self._gateway(s, t)) + JUMP_SECONDS
            here = self._gateway(t, s)
        return total + self._hop(here, b)

    def _system_paths(self, start, goal, max_jumps=MAX_JUMPS):
        """System sequences linked by a gateway on both sides, up to max_jumps."""
        systems = {p.system for p in self.places.places}
        out = []

        def walk(path):
            here = path[-1]
            if here == goal:
                out.append(path)
                return
            if len(path) > max_jumps:
                return
            for nxt in sorted(systems - set(path)):
                if self._gateway(here, nxt) and self._gateway(nxt, here):
                    walk(path + [nxt])

        walk([start])
        return out

    def prefetch(self, names):
        """Ask the API for every pair at once, in parallel."""
        names = sorted(set(names))
        pairs = [(a, b) for a in names for b in names if a != b]
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda ab: self.seconds(*ab), pairs))
        self.save()

    def save(self):
        if not self.cache_file:
            return
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            # Failures stay in memory only: the API may answer next time.
            with self._lock:
                known = {k: v for k, v in self._cache.items() if v is not None}
            self.cache_file.write_text(json.dumps(known, indent=0), encoding="utf-8")
        except OSError:
            pass
