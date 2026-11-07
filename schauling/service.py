"""Service mode: the engine (controller.py) plus a local API for the SC-overlay window (Tauri).

python -m schauling --service --port P --token T --parent-pid N

GET /state   public, unchanged (the VerseTime site and the team read it)
GET /ui      token: what the widgets show (see build_ui)
GET /events  token, Server-Sent Events: the /ui answer now and at each change
POST /action token: {"type": ..., ...} (see ACTIONS), answered {"ok": true} or {"ok": false, "error": ...}

The engine runs on one thread (run()); the HTTP threads never touch it: /ui is the last answer built there,
and an action runs there too, the HTTP thread waiting for its result.
"""
import json
import os
import queue
import sys
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout

from . import liveserver, team, trade
from .controller import AUTO, BOOL_OPTIONS, Controller
from .liveserver import _iso
from .report import route_lines

UI_VERSION = 1
MAX_TRADE_TICKS = 64
ACTION_TIMEOUT_S = 5
PARENT_CHECK_S = 2
EVENT_BACKLOG = 4  # a slow window gets the latest, not every one


def _name(stop):
    """None for a place the map does not know ("?...")."""
    return None if stop.name.startswith("?") else stop.name


def _route_with_local_time(c):
    """The /state route, each stop with `local` {clock, phase, day}: the time at the place on arrival, or None."""
    route = [dict(r) for r in c._live.get("route", [])]
    stops = c.plan.stops if c.plan else []
    aligned = len(stops) == len(route) and all(r.get("place") == st.place for r, st in zip(route, stops))
    for i, r in enumerate(route):
        local = stops[i].local if aligned else None
        r["local"] = {"clock": local[0], "phase": local[1], "day": bool(local[2])} if local else None
    return route


def build_ui(c, now=None):
    """The /ui answer: everything the widgets draw, from the engine's state (called on the engine's thread)."""
    now = time.time() if now is None else now
    s = c.session
    code = c._team_code()
    capacity = s.capacity()
    contracts, total = [], 0
    for ct in s.tracker.active():
        legs = []
        for leg in ct.open_legs():
            a, b = s.tracker.pickup_of(ct, leg), s.tracker.dropoff_of(ct, leg)
            total += leg.total or 0
            legs.append({"id": f"{ct.id}:{leg.index}", "cargo": (leg.cargo or "").lstrip("~") or None,
                         "scu": leg.total or None, "from": _name(a), "to": _name(b), "sure": team._sure(a, b),
                         "fromTeam": leg.from_team, "picked": leg.picked, "delivered": leg.delivered})
        contracts.append({"id": ct.id, "title": ct.title or ct.code, "code": ct.code, "tracked": ct.tracked,
                          "sharedWith": c._shared_with(ct.id) if code else [], "legs": legs})

    trades = []
    if c.cfg.get("trade") and c.plan and capacity:
        suggested = trade.suggest(c.plan, capacity, c.prices.data, c.cfg.get("trade_budget"),
                                  c.cfg.get("trade_illegal"))
        stops = c.plan.stops
        done = getattr(c, "_trade_done", set())
        for t in suggested:
            where = (stops[t.buy_at].place, stops[t.sell_at].place, t.name)
            trades.append({"buyAt": t.buy_at, "sellAt": t.sell_at, "buyPlace": where[0], "sellPlace": where[1],
                           "name": t.name, "scu": t.scu, "buy": t.buy, "sell": t.sell, "profit": t.profit,
                           "old": t.old, "done": {"buy": where + ("buy",) in done, "sell": where + ("sell",) in done}})
        if suggested:
            # Ticks belong to one plan of trades: another route or other trades start clean, so a loop done
            # before does not hide today's reminder. (An empty list may be a price refresh: it changes nothing.)
            signature = (tuple(st.place for st in stops), tuple((s["buyPlace"], s["sellPlace"], s["name"], s["scu"]) for s in trades))
            before = getattr(c, "_trade_signature", None)
            c._trade_signature = signature
            if before is not None and before != signature:
                c._trade_done = set()
                for entry in trades:
                    entry["done"] = {"buy": False, "sell": False}

    reason = None
    if c.cfg.get("trade") and not trades and not c._fetching_prices and not c.prices.error:
        reason = trade.why_empty(c.plan, capacity, c.prices.data, c.cfg.get("trade_budget"), c.cfg.get("trade_illegal"))

    members = []
    for m in c._members or []:
        members.append({"id": m.get("id"), "name": m.get("name"), "updatedAt": m.get("updatedAt"),
                        "state": m.get("state") or {}, "crossings": team.crossings(c._live, m),
                        "shared": sorted(team.shared_ids(c._live, m))})
    own = c.cfg.get("team") or {}

    places = getattr(s.places, "places", [])
    return {
        "version": UI_VERSION,
        "updatedAt": _iso(now),
        "status": c.status,
        "warnings": list(getattr(c, "warnings", [])),
        "ship": {"name": s.travel.ship, "capacity": capacity or None, "auto": bool(c.cfg.get("ship_auto"))},
        "start": {"place": c.cfg.get("start"), "source": c._start_source},
        "contracts": contracts,
        "total": {"scu": total, "capacity": capacity or None},
        "route": _route_with_local_time(c),
        "routeLines": route_lines(c.plan, capacity, []) if c.plan else [],
        "planning": bool(c._planning),
        "trade": {"on": bool(c.cfg.get("trade")), "suggestions": trades, "error": c.prices.error,
                  "fetching": bool(c._fetching_prices), "budget": c.cfg.get("trade_budget"), "reason": reason},
        "location": {"name": c._live.get("location"), "source": c._live.get("locationSource")},
        "heading": c._live.get("heading"),
        "aboard": c._live.get("aboard"),
        "shard": c._live.get("shard"),
        "team": {"code": code, "name": c.cfg.get("team_name"), "self": own.get("member"), "error": c._team_error,
                 "reading": bool(c._reading_team), "readError": c._members_error, "members": members},
        "settings": dict({k: bool(c.cfg.get(k, True if k in ("hide_outside_game", "close_with_game",
                                                              "read_contract_page", "auto_update",
                                                              "live_server") else False))
                          for k in BOOL_OPTIONS}, view=c.cfg.get("view", "cargo"), alpha=c.cfg.get("alpha", 0.85)),
        "lists": {"places": [{"name": p.name, "body": p.body, "system": p.system} for p in places],
                  "ships": sorted(s.ships)},
    }


class Hub:
    """The latest /ui answer and who listens for changes (the HTTP threads read it, the engine's thread writes)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._text = "null"
        self._body = None
        self._counter = 0
        self._subscribers = []

    def publish(self, ui):
        """True when it differs from the last one (updatedAt aside): listeners are told."""
        probe = dict(ui, updatedAt=None)
        text = json.dumps(probe, ensure_ascii=False, sort_keys=True)
        with self._lock:
            if text == self._text:
                return False
            self._text, self._body = text, ui
            self._counter += 1
            event = self._event(ui)
            for q in self._subscribers:
                while q.qsize() >= EVENT_BACKLOG:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break
                q.put(event)
        return True

    def _event(self, ui):
        return f"id: {self._counter}\nevent: ui\ndata: {json.dumps(ui, ensure_ascii=False)}\n\n".encode("utf-8")

    def snapshot(self):
        with self._lock:
            return self._body

    def snapshot_event(self):
        with self._lock:
            return self._event(self._body) if self._body is not None else None

    def subscribe(self):
        q = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)


class ActionError(Exception):
    pass


def _text(request, key, limit=200):
    value = request.get(key)
    if not isinstance(value, str) or len(value) > limit:
        raise ActionError(f"{key}: text expected")
    return value


def _contract_leg(c, request, with_leg=True):
    cid = _text(request, "contract", 80)
    ct = c.session.tracker.contracts.get(cid)
    if ct is None:
        raise ActionError("unknown contract")
    if not with_leg:
        return ct, None
    index = _text(request, "leg", 80)
    for leg in ct.legs.values():
        if f"{ct.id}:{leg.index}" == index:
            return ct, leg
    raise ActionError("unknown leg")


def _place(c, request, key="place", allow_empty=True):
    name = _text(request, key, 120)
    if name and name not in {p.name for p in c.session.places.places}:
        raise ActionError("unknown place")
    if not name and not allow_empty:
        raise ActionError(f"{key}: a place expected")
    return name


# type -> handler(controller, request); runs on the engine's thread
def _set_ship(c, r):
    if r.get("auto") is True:
        return c.choose_ship(AUTO)
    ship = _text(r, "ship", 80)
    if ship not in c.session.ships:
        raise ActionError("unknown ship")
    c.choose_ship(ship)


def _hide(c, r):
    ct, _ = _contract_leg(c, r, with_leg=False)
    c.hide_contract(ct)


def _set_pickup(c, r):
    ct, leg = _contract_leg(c, r)
    c.set_pickup(ct, leg, _place(c, r))


def _set_option(c, r):
    key = _text(r, "key", 40)
    if key not in BOOL_OPTIONS or key == "live_server":  # the service is the live server
        raise ActionError("unknown option")
    if not isinstance(r.get("value"), bool):
        raise ActionError("value: true or false expected")
    c.set_option(key, r["value"])


def _pseudo(c, r):
    """The pseudo of the request, saved. A blank one is refused, whatever was saved before."""
    name = _text(r, "name", 80)
    if not team.clean_name(name):
        raise ActionError("name: 1 to %d characters" % team.MAX_NAME)
    c.set_team_name(name)


def _team_create(c, r):
    if "name" in r:
        _pseudo(c, r)
    if not c.cfg.get("team_name"):
        raise ActionError("team_name: set a pseudo first")
    c.create_team()


def _team_join(c, r):
    if "name" in r:
        _pseudo(c, r)
    if not c.join_team(_text(r, "code", 200)):
        raise ActionError(c.status or "not joined")


def _trade_budget(c, r):
    if not c.set_trade_budget(_text(r, "text", 40)):
        raise ActionError("text: an amount expected")


def _trade_done(c, r):
    """Tick (or untick, done: false) the buy or the sell of a suggested trade, for the reminder."""
    side = _text(r, "side", 4)
    if side not in ("buy", "sell"):
        raise ActionError("side: buy or sell expected")
    key = (_text(r, "buyPlace", 120), _text(r, "sellPlace", 120), _text(r, "name", 120), side)
    done = set(getattr(c, "_trade_done", set()))  # a new set each time: readers never see one change under them
    if r.get("done", True) is False:
        done.discard(key)
    elif key not in done and len(done) >= MAX_TRADE_TICKS:
        raise ActionError("too many ticks")
    else:
        done.add(key)
    c._trade_done = done
    c.render()


def _team_name(c, r):
    _pseudo(c, r)


ACTIONS = {
    "set_start": lambda c, r: c.apply_start(_place(c, r)),
    "set_ship": _set_ship,
    "set_pickup": _set_pickup,
    "hide_contract": _hide,
    "read_page": lambda c, r: c.read_contract_page_now(),
    "team_create": _team_create,
    "team_join": _team_join,
    "team_leave": lambda c, r: c.leave_team(),
    "team_name": _team_name,
    "set_option": _set_option,
    "trade_budget": _trade_budget,
    "trade_done": _trade_done,
}


class Service:
    """Glue between the engine and the HTTP API."""

    def __init__(self, controller, token):
        self.controller = controller
        self.token = token
        self.hub = Hub()
        controller.updates = False  # the window updates itself
        controller.live = liveserver.LiveServer(lambda: controller._live, self)
        controller.listeners.append(self._changed)

    # engine thread
    def _changed(self):
        self.hub.publish(build_ui(self.controller))

    # HTTP threads
    def snapshot(self):
        return self.hub.snapshot()

    def snapshot_event(self):
        return self.hub.snapshot_event()

    def subscribe(self):
        return self.hub.subscribe()

    def unsubscribe(self, q):
        self.hub.unsubscribe(q)

    def act(self, request):
        """(HTTP status, answer). The handler runs on the engine's thread."""
        handler = ACTIONS.get(request.get("type"))
        if handler is None:
            return 400, {"ok": False, "error": "unknown action"}
        done = Future()
        lock = threading.Lock()
        state = {"started": False, "cancelled": False}

        def run():
            with lock:
                if state["cancelled"]:  # the client was told it failed: it must not happen later
                    return
                state["started"] = True
            try:
                handler(self.controller, request)
                done.set_result(None)
            except ActionError as e:
                done.set_exception(e)
            except Exception as e:  # a bug in an action must not stop the engine
                done.set_exception(RuntimeError(str(e)[:120]))
        self.controller.after(0, run)
        try:
            try:
                done.result(timeout=ACTION_TIMEOUT_S)
            except FutureTimeout:
                with lock:
                    if not state["started"]:
                        state["cancelled"] = True
                        return 504, {"ok": False, "error": "timeout"}
                done.result(timeout=ACTION_TIMEOUT_S)  # it is running: wait for its end, then say how it went
        except FutureTimeout:
            return 504, {"ok": False, "error": "timeout"}
        except ActionError as e:
            return 400, {"ok": False, "error": str(e)}
        except RuntimeError as e:
            return 500, {"ok": False, "error": str(e)}
        return 200, {"ok": True}


def parent_alive(pid):
    """False once process `pid` has ended (Windows: OpenProcess + exit code; elsewhere: kill 0)."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # windll does not keep the last error
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it runs, we may not look; anything else: gone
        try:
            code = wintypes.DWORD()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def watch_parent(pid, on_gone):
    """Call on_gone() once when the parent process is gone (the service must not outlive the window)."""
    def run():
        while parent_alive(pid):
            time.sleep(PARENT_CHECK_S)
        on_gone()
    threading.Thread(target=run, daemon=True).start()


def serve(session, cfg, port, token, parent_pid=None, warnings=()):
    """Run until quit (the parent window closed, or the game did). Blocks."""
    controller = Controller(session, cfg)
    controller.live_port = port  # not saved in the config: the window picks a free one each time
    controller.live_always = True
    controller.warnings = list(warnings)
    Service(controller, token)
    controller.start()
    if not controller.live.running:  # port taken: a service without its API is useless, the window must know
        print(f"service: cannot listen on port {port}: {controller.status}", file=sys.stderr)
        sys.exit(3)
    controller.start_page_watcher()  # no window of ours in the screenshots: the window excludes itself
    controller.quit_with_game = False  # the window decides what the game closing means
    if parent_pid:
        watch_parent(parent_pid, lambda: _stop(controller))
    controller.run()


def _stop(controller):
    """The window is gone: stop the engine on its own thread, and leave anyway if it does not answer."""
    controller.after(0, controller.quit)
    threading.Timer(5, lambda: os._exit(0)).start()
