"""Share the live state with a team, through the VerseTime API.

Off until you create or join a team. Then the same JSON as the local
GET /state goes to the API (on Cloudflare) with your pseudo: on each change
of position or route, at most every MIN_GAP_S, and every HEARTBEAT_S even
without change, so the team still sees you. The API forgets a member 2 h
after its last send. Your member id and secret stay in config.json.
"""
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

from .api import API_URL, USER_AGENT
from .liveserver import SITE
from .tracker import GUESS, UNKNOWN

MIN_GAP_S = 5
HEARTBEAT_S = 60
RETRY_S = 5  # after a 429 or a network error
READ_S = 15  # how often the Team view reads the team
# The API refuses a PUT body over 16 KB: the state stays under this, the
# rest is for the pseudo and the JSON around it.
MAX_STATE = 15 * 1024
# A contract shared in game: same MissionId in both Game.log? Not checked in
# game yet; set False if it is not, and shared contracts are not grouped.
SHARE_BY_ID = True
LOCAL_ONLY = ("teamMember",)  # in the local /state, not sent to the API
# What the API keeps at most (more contracts: refused; more cargos: cut).
MAX_CONTRACTS = 30
MAX_CARGOS = 20
MAX_NAME = 24
_CODE = re.compile(r"(?:#team=|^)([a-hjkmnp-z2-9]{10})$")
_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
_ERRORS = {403: "wrong secret", 404: "unknown team (expired?)", 409: "team full (16)",
           413: "state too big", 429: "too fast"}


class TeamError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def parse_code(text):
    """The team code in a code or an invite link, or None."""
    m = _CODE.search((text or "").strip().lower().rstrip("/"))
    return m.group(1) if m else None


def invite_link(code):
    return f"{SITE}/#team={code}"


def clean_name(name):
    return " ".join((name or "").split())[:MAX_NAME] or None


def new_member():
    """(member id, secret): drawn once per team."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(16)), secrets.token_hex(32)


def _request(method, url, body=None, secret=None, timeout=10):
    headers = {"User-Agent": USER_AGENT}  # Cloudflare refuses Python's default
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode("utf-8") or "{}"
            return json.loads(text)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace").strip()
        try:
            detail = json.loads(detail).get("error") or detail
        except (ValueError, AttributeError):
            pass
        detail = str(detail)[:120]
        if e.code == 400:
            raise TeamError(400, f"refused: {detail or 'bad format'}") from None
        raise TeamError(e.code, _ERRORS.get(e.code, f"HTTP {e.code}")) from None
    except (OSError, ValueError) as e:  # no network, timeout, bad answer
        raise TeamError(None, f"no answer ({getattr(e, 'reason', None) or e})") from None


class TeamApi:
    def __init__(self, api_url=API_URL, request=_request):
        self.url = api_url.rstrip("/")
        self.request = request

    def create(self):
        return self.request("POST", f"{self.url}/teams")["team"]

    def put(self, team, member, secret, name, state):
        self.request("PUT", f"{self.url}/teams/{team}/members/{member}", {"name": name, "state": state}, secret)

    def leave(self, team, member, secret):
        self.request("DELETE", f"{self.url}/teams/{team}/members/{member}", secret=secret)

    def members(self, team):
        """[{"id", "name", "state", "updatedAt"}] of the team."""
        return self.request("GET", f"{self.url}/teams/{team}").get("members") or []


# --- what we show the team ----------------------------------------------

def _sure(*stops):
    marks = {s.confidence for s in stops if not s.name.startswith("?")}
    if len(marks) < len(stops) or UNKNOWN in marks:
        return "?"
    return "~" if GUESS in marks else ""


def contracts_state(tracker):
    """The open contracts, for /state: id, title, tracked, and each cargo
    from/to (SC-map names, None when unknown) with how sure we are."""
    out = []
    for c in tracker.active():
        cargos = []
        for leg in c.open_legs():
            a, b = tracker.pickup_of(c, leg), tracker.dropoff_of(c, leg)
            cargos.append({"cargo": (leg.cargo or "").lstrip("~") or None, "scu": leg.total or None,
                           "from": None if a.name.startswith("?") else a.name,
                           "to": None if b.name.startswith("?") else b.name, "sure": _sure(a, b)})
        out.append({"id": c.id, "title": c.title or c.code, "tracked": c.tracked, "cargos": cargos})
    return out


def _size(state):
    return len(json.dumps(state, ensure_ascii=False).encode("utf-8"))


def fit_state(state, limit=MAX_STATE):
    """The state cut under `limit` bytes: titles first, then the oldest
    contracts (the list is in accept order). Never more than the API takes."""
    contracts = state.get("contracts") or []
    if len(contracts) > MAX_CONTRACTS or any(len(c.get("cargos") or []) > MAX_CARGOS for c in contracts):
        state = dict(state, contracts=[dict(c, cargos=(c.get("cargos") or [])[:MAX_CARGOS])
                                       for c in contracts[-MAX_CONTRACTS:]])
    if _size(state) <= limit or not state.get("contracts"):
        return state
    state = dict(state, contracts=[dict(c, title=None) for c in state["contracts"]])
    while state["contracts"] and _size(state) > limit:
        state["contracts"] = state["contracts"][1:]
    return state


def _clock(iso):
    """"21:31" in the player's time zone, from an ISO UTC time."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    except (AttributeError, ValueError):
        return None


def _seconds(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def _offset(theirs, mine):
    a, b = _seconds(theirs), _seconds(mine)
    if a is None or b is None:
        return ""
    m = round((a - b) / 60)
    return f" ({m:+d}m)" if m else " (same time)"


def crossings(mine, member):
    """What a teammate's route and contracts have in common with ours, as
    lines: places we both stop at, and contracts we both have. Our route
    stays as it is."""
    theirs = member.get("state") or {}
    name = member.get("name") or "?"
    my_stops = {}
    for stop in mine.get("route") or []:
        my_stops.setdefault(stop["place"], stop)
    out = []
    for stop in theirs.get("route") or []:
        place = stop.get("place")
        if place not in my_stops:
            continue
        kinds = {a.get("type") for a in stop.get("actions") or []}
        verb = "unloads" if kinds == {"unload"} else "loads" if kinds == {"load"} else "stops"
        clock = _clock(stop.get("arrival"))
        mine_clock = _clock(my_stops[place].get("arrival"))
        when = f" at {clock}" + (f", you {mine_clock}" if mine_clock else "") if clock else ""
        out.append(f"{name} {verb} at {place} too{_offset(stop.get('arrival'), my_stops[place].get('arrival'))}{when}")
        my_stops.pop(place)  # once per place
    return out


def shared_ids(mine, member):
    if not SHARE_BY_ID:
        return set()
    ours = {c.get("id") for c in mine.get("contracts") or []}
    return {c.get("id") for c in (member.get("state") or {}).get("contracts") or []} & ours - {None}


def member_line(member):
    """Pseudo, ship, next stop and its time, number of contracts."""
    state = member.get("state") or {}
    parts = [member.get("name") or "?", state.get("ship") or "ship ?"]
    route = state.get("route") or []
    if route:
        clock = _clock(route[0].get("arrival"))
        parts.append(f"next {route[0].get('place')}" + (f" {clock}" if clock else ""))
    elif state.get("location"):
        parts.append(f"at {state['location']}")
    parts.append(f"{len(state.get('contracts') or [])} contract(s)")
    return " · ".join(parts)


def contract_lines(member, shared=()):
    """A teammate's contracts: title, then each cargo from → to. A contract
    we have too shows its title only."""
    out = []
    for c in (member.get("state") or {}).get("contracts") or []:
        title = c.get("title") or (c.get("id") or "?")[:8]
        if c.get("id") in shared:
            out.append(f"{title}   (shared with you)")
            continue
        out.append(("▶ " if c.get("tracked") else "") + title)
        for g in c.get("cargos") or []:
            mark = g.get("sure") or ""
            scu = f"{g['scu']} SCU " if g.get("scu") else ""
            out.append(f"  {mark}{g.get('from') or '?'} → {mark}{g.get('to') or '?'} : {scu}{g.get('cargo') or 'cargo'}")
    return out


def _same(a, b):
    """Same position and route: updatedAt changes on every render."""
    def key(s):
        return {k: v for k, v in (s or {}).items() if k != "updatedAt" and k not in LOCAL_ONLY}
    return key(a) == key(b)


class TeamSharer:
    """Sends the state on its own thread. set(...) from the UI thread."""

    def __init__(self, api, clock=time.monotonic):
        self.api = api
        self.clock = clock
        self.error = None  # message of the last failed send, None after a good one
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._member = None  # (team, member id, secret, name)
        self._state = None
        self._sent = None  # last state the API has
        self._sent_at = None
        self._not_before = 0.0
        self._thread = None

    def set(self, member, state):
        """member: (team, id, secret, name), or None to stop sending."""
        with self._lock:
            if member != self._member:
                self._member, self._sent, self._sent_at, self.error = member, None, None, None
                self._not_before = 0.0
            self._state = state
        self._wake.set()
        if member and not self._thread:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def due(self, now):
        """Seconds to wait before the next send, or 0 to send now."""
        with self._lock:
            if not self._member or self._state is None:
                return None
            if self._sent_at is None:
                wait = 0
            elif _same(self._state, self._sent):
                wait = self._sent_at + HEARTBEAT_S - now
            else:
                wait = self._sent_at + MIN_GAP_S - now
            return max(wait, self._not_before - now, 0)

    def send_once(self):
        """One send of the current state. True when the API has it."""
        with self._lock:
            member, state = self._member, self._state
        if not member or state is None:
            return False
        team, mid, secret, name = member
        body = {k: v for k, v in state.items() if k not in LOCAL_ONLY}
        try:
            self.api.put(team, mid, secret, name, body)
        except TeamError as e:
            with self._lock:
                if member == self._member:
                    self.error = str(e)
                    self._not_before = self.clock() + RETRY_S
                    # 403/404/409 will not get better: stop hammering, a new
                    # set() (another team) starts again.
                    if e.status in (403, 404, 409):
                        self._not_before = float("inf")
            return False
        with self._lock:
            if member == self._member:
                self._sent, self._sent_at, self.error = state, self.clock(), None
        return True

    def _run(self):
        while True:
            wait = self.due(self.clock())
            if wait == 0:
                self.send_once()
                continue
            self._wake.wait(None if wait is None else min(wait, HEARTBEAT_S))
            self._wake.clear()
