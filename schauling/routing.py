"""Order of the stops for all open hauling legs.

Each leg is picked up at one place and delivered at another, so a pickup
comes before its delivery. With few actions we try every order; beyond
that we go to the nearest useful stop each time.
"""
from dataclasses import dataclass, field

# Above this many distinct places, exact search is too slow: use the greedy
# order. Each visit does every action ready at the place, so the search
# grows with places, not actions (26 actions over 5 places is instant).
EXACT_LIMIT = 9


@dataclass
class Action:
    kind: str  # "pickup" or "dropoff"
    place: str
    leg: object
    contract: object
    after: int = None  # index of the pickup action this delivery waits for


@dataclass
class PlanStop:
    place: str
    actions: list = field(default_factory=list)
    seconds: int = 0  # travel time from the previous stop


@dataclass
class Plan:
    stops: list
    unplaced: list  # actions whose place we do not know
    total_seconds: int


def build_actions(tracker):
    """Actions still to do, and those we cannot place."""
    actions, unplaced = [], []
    for c in tracker.active():
        for leg in c.open_legs():
            pickup_index = None
            if not leg.picked:
                stop = tracker.pickup_of(c, leg)
                a = Action("pickup", stop.name if stop.place else None, leg, c)
                if a.place:
                    pickup_index = len(actions)
                    actions.append(a)
                else:
                    unplaced.append(a)
            stop = tracker.dropoff_of(c, leg)
            a = Action("dropoff", stop.name if stop.place else None, leg, c, pickup_index)
            (actions if a.place else unplaced).append(a)
    return actions, unplaced


def _next_places(actions, done):
    """Places with at least one action we can do now."""
    ready = {}
    for i, a in enumerate(actions):
        if i in done:
            continue
        if a.after is not None and a.after not in done:
            continue
        ready.setdefault(a.place, []).append(i)
    return ready


def _ready_at(actions, done, place):
    """Actions doable at `place`, pickups first so deliveries there unlock."""
    picked = set(done)
    out = []
    changed = True
    while changed:
        changed = False
        for i, a in enumerate(actions):
            if i in picked or a.place != place:
                continue
            if a.after is None or a.after in picked:
                picked.add(i)
                out.append(i)
                changed = True
    return out


def plan(actions, travel, start=None):
    """Best order of stops. `travel(a, b)` gives seconds between two places."""
    if not actions:
        return [], 0
    n = len(actions)

    def visit(place, done):
        return _ready_at(actions, done, place)

    if len({a.place for a in actions} | ({start} if start else set())) <= EXACT_LIMIT:
        best = [None, None]
        memo = {}

        def search(place, done, cost, path):
            if len(done) == n:
                if best[0] is None or cost < best[0]:
                    best[0], best[1] = cost, list(path)
                return
            if best[0] is not None and cost >= best[0]:
                return
            key = (place, frozenset(done))
            if memo.get(key, float("inf")) <= cost:
                return
            memo[key] = cost
            for nxt in sorted(_next_places(actions, done)):
                if nxt == place:
                    continue
                did = visit(nxt, done)
                t = travel(place, nxt) if place else 0
                path.append((nxt, did, t))
                search(nxt, done | set(did), cost + t, path)
                path.pop()

        start_done = set(visit(start, set())) if start else set()
        first = [(start, sorted(start_done), 0)] if start_done else []
        search(start, start_done, 0, first)
        route = best[1] or []
    else:
        done, place, route = set(), start, []
        if start:
            did = visit(start, done)
            if did:
                route.append((start, did, 0))
                done |= set(did)
        while len(done) < n:
            options = [p for p in _next_places(actions, done) if p != place] or list(_next_places(actions, done))
            nxt = min(options, key=lambda p: travel(place, p) if place else 0)
            did = visit(nxt, done)
            route.append((nxt, did, travel(place, nxt) if place else 0))
            done |= set(did)
            place = nxt

    stops = [PlanStop(p, [actions[i] for i in did], t) for p, did, t in route]
    return stops, sum(s.seconds for s in stops)


def make_plan(tracker, travel, start=None):
    actions, unplaced = build_actions(tracker)
    stops, total = plan(actions, travel, start)
    return Plan(stops, unplaced, total)
