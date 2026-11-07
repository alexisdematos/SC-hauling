"""The hauling engine, without a window: Game.log session, plan, contracts, team, trade, live map.

Overlay (tkinter) and the service (Tauri front end) both sit on it. A front end gives it: after() to run
something later on its loop, own_hwnd(), clipboard_text(), copy_text(), report_error(), update_ready(); and reads
what changed through listeners (called after each render) or by calling poll() itself.
"""
import queue
import sys
import threading
import time
import traceback

from . import config, keys, liveserver, ocr, team, trade, updater, windows
from .pagewatch import PageWatcher
from .places import START_RADIUS_KM, parse_coordinates

POLL_MS = 1000
# Screenshots of the contract page from the menu (ms), once the menu has
# closed. On accept, pagewatch takes them.
MENU_SHOTS_MS = (400, 900)
GAME_CHECK_MS = 5000  # how often we check that the game still runs
QUIT_AFTER_S = 10  # quit this long after the game closed (a crash reporter may linger)
AUTO = "\0auto"  # ship picker value: follow the last ship flown
# Auto /showlocation: at most once in this many seconds, this long after boarding.
SHOWLOCATION_EVERY_S = 300
AFTER_BOARDING_S = 2.0

# Options the front end may switch (config keys), and what each needs besides saving it
BOOL_OPTIONS = ("trade", "trade_illegal", "auto_showlocation", "live_server", "hide_outside_game",
                "close_with_game", "read_contract_page", "click_through", "auto_update")


class Controller:
    updates = True  # check_update installs new versions: off in service mode (Tauri updates itself)
    live_port = None  # the service picks its port; else live_port of the config
    live_always = False  # the service is always on: its API is the live server
    quit_with_game = True  # watch_game quits some time after the game closed (option close_with_game)

    def __init__(self, session, cfg):
        self.session = session
        self.cfg = cfg
        self.plan = None
        self._results = queue.Queue()
        self._pages = queue.Queue()  # (mission, pages, error, final): one objectives list per screenshot
        self._collected = {}  # mission: pages read so far, while its screenshots come
        self._planning = False
        self._replan = False
        self._last_location = float("-inf")  # monotonic time of the last position
        self._clipboard = None
        self.status = None  # one-line message under the header
        self._start_source = "log"  # log, showlocation or manual: for the live map
        self._live = {}  # the /state answer, rebuilt on this thread at each render
        liveserver.set_site(cfg.get("site_url"))
        self.live = liveserver.LiveServer(lambda: self._live)
        self.team = team.TeamSharer(team.TeamApi(cfg.get("team_api") or team.API_URL))
        self._team_error = None  # the error on the status line, to redraw when it changes
        self._calls = queue.Queue()  # functions from other threads, run on this one
        self._members = None  # the others in the team, last read (Team view); None before the first read
        self._members_error = None
        self._reading_team = False
        self._open_members = set()  # member ids whose contracts are shown
        self.prices = trade.Prices(cfg.get("team_api") or team.API_URL)
        self._fetching_prices = False
        self.listeners = []  # called with no argument after each render
        self._timers = []  # (due, func) for the default after()
        self._timers_lock = threading.Lock()
        self._running = True
        self._shown = True  # the window is visible (read_team skips the Team view otherwise)
        self._game_seen = False
        self._game_gone_at = None

    def start(self):
        """First pass, once the front end can draw."""
        self.session.travel.ship = self._ship()
        if self.session.here:  # the current session's log knows where we are
            self.set_start(self.session.here, "log")
        self.session.heading_expired(time.time())  # one from the replay may be old
        self.find_eta()
        if self.live_always or self.cfg.get("live_server", True):
            self.start_live()
        self.render()
        self.request_plan()
        self.check_update()

    def start_page_watcher(self, hwnd=lambda: None):
        """Its own thread: screenshots the instant a contract is accepted. hwnd(): the window to leave out of them."""
        if windows.available():
            PageWatcher(self.session.source.path, hwnd, lambda: self.cfg.get("read_contract_page", True),
                        lambda *read: self._pages.put(read)).start()

    def start_loops(self):
        """The recurring jobs, when the front end has no loop of its own: poll() is called by run()."""
        self.after(team.READ_S * 1000, self.read_team_every)
        self.watch_game()  # first check now, then every GAME_CHECK_MS

    def run(self):
        """Headless loop: poll, run what after() scheduled, until quit()."""
        self.start_loops()
        last = float("-inf")
        while self._running:
            if time.monotonic() - last >= POLL_MS / 1000:
                last = time.monotonic()
                self._guard(self.poll)
            self._run_timers()
            time.sleep(0.05)  # an action waits at most this long

    def _guard(self, func):
        """Run func; a failure is reported and the loop goes on (under Tk a failing callback does not stop it)."""
        try:
            func()
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            try:
                self.report_error(e)
            except Exception:
                pass

    # --- what a front end provides ----------------------------------------

    def after(self, ms, func):
        """Run func later, on the loop thread. Default: run() does it."""
        with self._timers_lock:
            self._timers.append((time.monotonic() + ms / 1000, func))

    def _run_timers(self):
        now = time.monotonic()
        with self._timers_lock:
            due = [t for t in self._timers if t[0] <= now]
            self._timers = [t for t in self._timers if t[0] > now]
        for _, func in sorted(due, key=lambda t: t[0]):
            self._guard(func)

    def own_hwnd(self):
        """Our window, left out of screenshots; None when there is none to hide."""
        return None

    def clipboard_text(self):
        """Text on the clipboard, or None."""
        return windows.clipboard_text()

    def copy_text(self, text):
        """Put text on the clipboard. True when done."""
        return False

    def report_error(self, error):
        self.status = f"Error: {error}"

    def update_ready(self, exe):
        """A new version was downloaded (status says so)."""

    def quit(self):
        self.live.stop()
        self.team.set(None, None)
        self._running = False

    # --- render ---------------------------------------------------------------

    def refresh_live(self):
        """Rebuild what the map and the team get (the /state answer)."""
        code = self._team_code()
        self._live = team.fit_state(liveserver.build_state(
            self.session.travel.ship, self.cfg.get("start"), self._start_source, self.plan, time.time(), code,
            team.contracts_state(self.session.tracker), (self.cfg.get("team") or {}).get("member"),
            # Aboard someone's ship we do not fly it: their heading counts.
            None if self.session.aboard else self.session.heading, self.session.handle, self.session.aboard,
            self.session.shard))
        t = self.cfg.get("team")
        name = self.cfg.get("team_name")
        self.team.set((t["code"], t["member"], t["secret"], name) if t and name else None, self._live)
        self._team_error = self.team.error

    def render(self):
        self.refresh_live()
        self.notify()

    def notify(self):
        for listener in list(self.listeners):
            try:
                listener()
            except Exception as e:  # a listener never stops the engine, but its failure shows
                traceback.print_exc(file=sys.stderr)
                self.status = f"Error: {e}"

    # --- actions (what a menu, a dialog or a widget asks for) ---------------------

    def apply_start(self, name):
        """Start the route at `name` picked by hand ("" or None: anywhere, the first stop)."""
        self.cfg["start"] = self.session.start = name or None
        self._start_source = "manual"
        self.status = None
        config.save(self.cfg)
        self.request_plan()

    def choose_ship(self, name):
        """A ship by name, or AUTO for the last one flown."""
        self.cfg["ship_auto"] = name == AUTO
        if name != AUTO:
            self.cfg["ship"] = name
        self.apply_ship()

    def hide_contract(self, contract):
        self.session.tracker.hide(contract)
        self.cfg["hidden"] = sorted(self.session.hidden)
        config.save(self.cfg)
        self.request_plan()

    def set_pickup(self, contract, leg, name):
        """A pickup by hand; "" clears it (back to what the log says)."""
        self.session.tracker.set_pickup(contract, leg, name)
        self.cfg["manual_pickups"] = self.session.manual
        config.save(self.cfg)
        self.request_plan()

    def set_trade_budget(self, text):
        """aUEC to spend at once, as typed ("50k", "1 200 000"). True when it was a budget."""
        try:
            budget = int(float((text or "").replace(" ", "").replace(",", "").lower().replace("k", "e3")))
        except ValueError:
            return False
        if budget <= 0:
            return False
        self.cfg["trade_budget"] = budget
        config.save(self.cfg)
        self.render()
        return True

    def set_team_name(self, name):
        """The pseudo the team sees. True when there is one."""
        if name is not None and team.clean_name(name):
            self.cfg["team_name"] = team.clean_name(name)
            config.save(self.cfg)
            self.render()  # sends it at once
        return bool(self.cfg.get("team_name"))

    def join_team(self, text):
        """Join with a code or an invite link. True when joined."""
        code = team.parse_code(text or "")
        if not code:
            self.status = "Not a team code or invite link"
            self.render()
            return False
        if not self.cfg.get("team_name"):
            self.status = "Team: set your pseudo first"
            self.render()
            return False
        self._enter_team(code)
        return True

    def copy_invite(self):
        code = self._team_code()
        if code:
            link = team.invite_link(code)
            ok = self.copy_text(link)
            self.status = f"Team {code}: invite link copied" if ok else f"Team {code}: invite link {link}"
            self.render()

    def set_option(self, key, value):
        """A yes/no option of the config; some act at once."""
        if key not in BOOL_OPTIONS:
            raise KeyError(key)
        self.cfg[key] = bool(value)
        config.save(self.cfg)
        if key in ("trade", "trade_illegal"):
            self.refresh_prices()
            self.render()
        elif key == "live_server":
            if self.cfg[key]:
                self.start_live()
            else:
                self.live.stop()
            self.render()

    # --- game, contracts, route ----------------------------------------------------

    def _ship(self):
        if self.cfg.get("ship_auto") and self.session.last_ship:
            return self.session.last_ship
        return self.cfg.get("ship")

    def apply_ship(self):
        """Use the chosen ship, or the last one flown when set to auto."""
        ship = self._ship()
        if ship != self.session.travel.ship:
            self.session.travel.ship = ship
            self.request_plan()
        config.save(self.cfg)

    def _place_items(self):
        return [(f"{p.name}   ·   {p.body}, {p.system}", p.name)
                for p in sorted(self.session.places.places, key=lambda p: p.name)]

    def toggle_view(self):
        views = ["cargo", "route"] + (["team"] if self._team_code() else [])
        view = self.cfg.get("view")
        self.cfg["view"] = views[(views.index(view) + 1) % len(views)] if view in views else "cargo"
        config.save(self.cfg)
        self.render()
        self.read_team()

    def check_update(self):
        """The exe updates itself from GitHub (menu option, on by default):
        the new one runs from the next start, or at once with Restart."""
        exe = updater.running_exe()
        if not self.updates or not exe or not self.cfg.get("auto_update", True):
            return
        updater.cleanup(exe)

        def done(status, _error):
            if not status:
                return
            self.status = status
            if status.startswith("Updated"):
                self.update_ready(exe)
            self.render()
        self._in_background(lambda: updater.update(exe), done)

    def restart(self, exe):
        updater.restart(exe)
        self.quit()

    def poll(self):
        try:
            if self.session.update():
                self.request_plan()
        except Exception as e:  # keep the window alive on a bad line
            self.report_error(e)
        if self.session.boarded:
            # Boarding wins over a ship picked by hand: the pick holds until
            # the next boarding, then the log decides again.
            self.session.boarded = False
            self.session.ship_changed = False
            self.cfg["ship_auto"] = True
            self.apply_ship()
            self.render()
            self.auto_showlocation()
        elif self.session.ship_changed:
            self.session.ship_changed = False
            self.apply_ship()
            self.render()
        if self.session.aboard_changed:
            self.session.aboard_changed = False
            self.render()  # aboard someone's ship: the team sees it
        if self.session.shard_changed:
            self.session.shard_changed = False
            self.render()  # another game server: the team sees who is on ours
        if self.session.heading_changed:
            self.session.heading_changed = False
            self.find_eta()
            self.render()  # a new heading goes to the team
        if self.session.heading_expired(time.time()):
            self.render()
        if self.session.place_changed:
            self.session.place_changed = False
            self._last_location = time.monotonic()
            self.set_start(self.session.here, "log")
            self.request_plan()
        self.session.accepted.clear()  # read by the page watcher, the instant they come
        while not self._pages.empty():
            self._apply_pages(*self._pages.get())
        while not self._calls.empty():
            self._calls.get()()
        if self.team.error != self._team_error:
            self.render()
        self.check_clipboard()
        self.refresh_prices()
        while not self._results.empty():
            self.plan = self._results.get()
            self._planning = False
            if self._replan:
                self.request_plan()
            self.render()

    def tick(self):
        self.poll()
        self.after(POLL_MS, self.tick)

    def _apply_pages(self, mission, pages, error, final):
        """Pages read by OCR, one per screenshot: from the page watcher as
        each is read (final at the end), or all at once from the menu
        (mission None). Applied as they come, so the overlay fills in fast."""
        tracker = self.session.tracker
        if not error and mission is None:  # read from the menu: which contract is it?
            mission = tracker.match_page(ocr.merge_pages(pages))
            error = None if mission else "no open contract with these cargos"
        if mission is not None and not error:
            pages = self._collected.setdefault(mission, []) + [p for p in pages if p]
            self._collected[mission] = pages
        # Only the screenshots of this contract's page: the player may have
        # clicked on to another one meanwhile.
        entries = ocr.merge_pages(tracker.pages_for(mission, pages)) if mission and not error and pages else []
        if final:
            self._collected.pop(mission, None)
        if not entries:
            if final:
                error = error or ("the page on screen was another contract's" if pages else "no objectives on screen")
                ocr.debug(f"apply to {str(mission)[:8]}: {error}")
                self.status = f"Contract page not read: {error}"
                self.render()
            return
        title = (tracker.contracts[mission].title or "")[:50] if mission in tracker.contracts else "?"
        ocr.debug(f"apply to {mission[:8]} ({title}): {ocr.describe(entries)}")
        n = self.session.keep_page(mission, entries)
        self.status = f"Contract page read: {n} pickup(s) of {len(entries)} cargo(s)"
        config.save(self.cfg)
        self.request_plan()

    def read_contract_page(self, mission, delays, from_menu=False):
        """Screenshots of the game while a contract page is on screen, then
        OCR on a worker thread. Right after "Contract Accepted" (the game in
        front), or from the menu (the overlay in front: we capture the game
        window under it; `mission` None, found from the cargos). The overlay
        is left out of the screenshots (it may sit over the page)."""
        shots = []

        def shoot(n):
            game = windows.game_window() if from_menu else None
            if not (game if from_menu else windows.game_in_front()):
                return finish()
            path = ocr.temp_image(n)
            hwnd = self.own_hwnd()
            windows.exclude_from_capture(hwnd, True)
            try:
                if ocr.capture_window(game, path) if from_menu else ocr.capture_foreground(path):
                    shots.append(path)
            finally:
                windows.exclude_from_capture(hwnd, False)
            if n + 1 < len(delays):
                self.after(delays[n + 1] - delays[n], lambda: shoot(n + 1))
            else:
                finish()

        def finish():
            if not shots:
                if from_menu:
                    self.status = "Contract page not read: no game window"
                    self.render()
                return
            self.status = "Reading the contract page…"
            self.render()

            def work():
                try:
                    read = ocr.read_images(shots)
                    pages = [ocr.parse_objectives(lines) for lines in read]
                    for n, (lines, page) in enumerate(zip(read, pages)):
                        ocr.debug(f"menu: shot {n}: {ocr.describe(page)}  [raw: {ocr.raw_objectives(lines)}]")
                    self._pages.put((mission, pages, None if any(pages) else "no objectives on screen", True))
                except Exception as e:  # PowerShell missing, OCR error…
                    self._pages.put((mission, [], str(e)[:80], True))
                finally:
                    for p in shots:
                        p.unlink(missing_ok=True)

            threading.Thread(target=work, daemon=True).start()

        self.after(delays[0], lambda: shoot(0))

    def read_contract_page_now(self):
        """Menu: read the contract page open in the mobiGlas now."""
        self.status = "Reading the contract page…"
        self.render()
        self.read_contract_page(None, MENU_SHOTS_MS, from_menu=True)

    def auto_showlocation(self):
        """Type /showlocation in the game chat when the player boards a ship
        with hauling contracts open, unless a position came in the last few
        minutes. Opt-in.

        Not on accept: contracts are accepted in the mobiGlas, where the chat
        cannot open, and the log does not say when the mobiGlas closes.
        Boarding means the mobiGlas is closed and the ship is where the route
        starts."""
        if not self.cfg.get("auto_showlocation") or not keys.available():
            return
        if not self.session.tracker.active():
            return
        if time.monotonic() - self._last_location < SHOWLOCATION_EVERY_S:
            return
        self._last_location = time.monotonic()

        def work():
            time.sleep(AFTER_BOARDING_S)  # the boarding notification settles
            if not keys.chat_command("/showlocation"):
                self._last_location = float("-inf")  # game not in front: try next time

        threading.Thread(target=work, daemon=True).start()

    def check_clipboard(self):
        """Start the route where /showlocation says the player is."""
        text = self.clipboard_text()
        if text is None:  # empty, or not text
            return
        if text == self._clipboard:
            return
        first = self._clipboard is None
        self._clipboard = text
        pos = parse_coordinates(text)
        if pos is None or first:  # ignore what was there before we started
            return
        self._last_location = time.monotonic()
        place, km = self.session.places.nearest(*pos, START_RADIUS_KM)
        if place is None:
            self.status = "/showlocation: inside a station or city, set the start by hand"
            self.render()
            return
        self.set_start(place.name, "/showlocation" + (f", {km:,.0f} km away" if km >= 1 else ""))
        self.request_plan()

    def set_start(self, name, source):
        """Start the route at `name`; `source` says where we got it."""
        self._start_source = "showlocation" if source.startswith("/showlocation") else "log"
        self.cfg["start"] = self.session.start = name
        self.status = f"Start: {name} ({source})" if name else "In space: quantum stopped on the way"
        config.save(self.cfg)

    def request_plan(self):
        self.render()
        if self._planning:
            self._replan = True
            return
        self._replan = False
        self._planning = True
        snapshot = self.session.snapshot()

        def work():
            try:
                self._results.put(self.session.plan(snapshot))
            except Exception:
                self._results.put(None)

        threading.Thread(target=work, daemon=True).start()

    def start_live(self):
        port = int(self.live_port or self.cfg.get("live_port") or liveserver.DEFAULT_PORT)
        try:
            self.live.start(port)
        except OSError as e:
            self.status = f"Live map off: port {port} in use ({e.strerror or e})"

    def open_map(self):
        """VerseTime on where we are, following the live position when the
        server runs."""
        port = int(self.live_port or self.cfg.get("live_port") or liveserver.DEFAULT_PORT) if self.live.running else None
        liveserver.open_map(self.cfg.get("start") or self.session.here, port, self._team_code())

    def _team_code(self):
        return (self.cfg.get("team") or {}).get("code")

    def refresh_prices(self):
        """Prices of the route's stops, when trade is on and they are missing
        or old. Nothing asked while trade is off."""
        if not self.cfg.get("trade") or not self.plan or self._fetching_prices:
            return
        missing = self.prices.missing(s.place for s in self.plan.stops)
        if not missing:
            return
        self._fetching_prices = True

        def done(_result, _error):
            self._fetching_prices = False
            self.render()
        self._in_background(lambda: self.prices.fetch(missing), done)

    def _in_background(self, work, done):
        """work() on a thread (network), then done(result, error) on this one."""
        def run():
            try:
                result, error = work(), None
            except team.TeamError as e:
                result, error = None, str(e)
            self._calls.put(lambda: done(result, error))
        threading.Thread(target=run, daemon=True).start()

    def _enter_team(self, code):
        member, secret = team.new_member()
        self.cfg["team"] = {"code": code, "member": member, "secret": secret}
        self._members, self._members_error, self._open_members = None, None, set()
        config.save(self.cfg)
        self.status = f"Team {code}: invite link in the Team menu"
        self.render()

    def create_team(self):
        if not self.cfg.get("team_name"):
            self.status = "Team: set your pseudo first"
            self.render()
            return
        self.status = "Creating the team…"
        self.render()

        def done(code, error):
            if error:
                self.status = f"Team not created: {error}"
                self.render()
            else:
                self._enter_team(code)
                self.copy_invite()
        self._in_background(self.team.api.create, done)

    def leave_team(self):
        t = self.cfg.get("team")
        if not t:
            return
        self.cfg["team"] = None  # forget it at once: nothing more is sent
        self._members, self._members_error, self._open_members = None, None, set()
        if self.cfg.get("view") == "team":
            self.cfg["view"] = "cargo"
        config.save(self.cfg)
        self.status = f"Left team {t['code']}"
        self.render()

        def done(_result, error):
            if error:
                self.status = f"Left team {t['code']} (the API keeps you 2 h at most: {error})"
                self.render()
        self._in_background(lambda: self.team.api.leave(t["code"], t["member"], t["secret"]), done)

    def find_eta(self):
        """Arrival time of the quantum jump picked: its travel time, on a
        thread (the API may be asked)."""
        h = self.session.heading
        if not h or not h["from"] or not h["picked_at"] or h.get("eta"):
            return  # an eta already: from the fuel estimate, better

        def done(seconds, _error):
            if self.session.heading is h and seconds is not None and not h.get("eta"):
                h["eta"] = h["picked_at"] + seconds
                self.render()
        self._in_background(lambda: self.session.travel.quantum_seconds(h["from"], h["to"]), done)

    def read_team_every(self):
        self.read_team()
        self.after(team.READ_S * 1000, self.read_team_every)

    def read_team(self):
        """The others in the team: for the Team view while it shows, and to
        fill a shared contract our log knows little of (no SCU, no cargo)."""
        t = self.cfg.get("team")
        wanted = (self.cfg.get("view") == "team" and self._shown) or self.session.tracker.missing_info()
        if not t or not wanted or self._reading_team:
            return
        self._reading_team = True

        def done(members, error):
            self._reading_team = False
            if (self.cfg.get("team") or {}).get("code") != t["code"]:
                return  # left or changed team meanwhile
            self._members_error = error
            if members is not None:
                self._members = [m for m in members if m.get("id") != t["member"]]
                if self.fill_from_team():
                    self.request_plan()  # SCU or pickups changed the route
            self.render()
        self._in_background(lambda: self.team.api.members(t["code"]), done)

    def fill_from_team(self):
        """Contracts a teammate has too (same id, shared in game): their copy
        names the cargo and SCU our log lacks. True when something changed."""
        if not team.SHARE_BY_ID:
            return False
        tracker, changed = self.session.tracker, 0
        changed += self._drop_stale_team_tags()
        for m in self._members or []:
            for c in (m.get("state") or {}).get("contracts") or []:
                if c.get("id") in tracker.contracts:
                    changed += tracker.apply_team(c["id"], c.get("cargos"), m.get("name") or "?")
        return bool(changed)

    def _drop_stale_team_tags(self):
        """A teammate who filled a leg ("from X") and no longer lists that contract (cancelled it): the tag goes,
        and the status says so, since the log does not tell us. The cargo it gave stays. Returns how many."""
        if self._members_error or self._members is None:
            return 0
        names = [m.get("name") or "?" for m in self._members]
        # Only members who publish their contracts (older versions do not), and with a pseudo of their own
        listing = {(m.get("name") or "?"): {c.get("id") for c in m["state"]["contracts"]}
                   for m in self._members
                   if isinstance((m.get("state") or {}).get("contracts"), list) and names.count(m.get("name") or "?") == 1}
        dropped = []
        for c in self.session.tracker.contracts.values():
            for leg in c.legs.values():
                who = getattr(leg, "from_team", None)
                if who and who in listing and c.id not in listing[who]:
                    leg.from_team = None
                    dropped.append((who, c.title or c.code))
        if dropped:
            who, title = dropped[0]
            self.status = f"{who} no longer has «{title}»: hide it if it was cancelled"
        return len(dropped)

    def toggle_member(self, member_id):
        self._open_members ^= {member_id}
        self.render()

    def _shared_with(self, contract_id):
        """Pseudos of the teammates who have this contract too."""
        if not team.SHARE_BY_ID:
            return []
        return [m.get("name") or "?" for m in self._members or []
                if any(c.get("id") == contract_id for c in (m.get("state") or {}).get("contracts") or [])]

    def watch_game(self):
        """Note when the game runs; quit a while after it closes."""
        if windows.available():
            if windows.game_running():
                self._game_seen, self._game_gone_at = True, None
            elif self._game_seen:
                self._game_gone_at = self._game_gone_at or time.monotonic()
                if self.quit_with_game and self.cfg.get("close_with_game", True) \
                        and time.monotonic() - self._game_gone_at >= QUIT_AFTER_S:
                    self.quit()
                    return
                # Game closed and option off: back to always visible.
                self._game_seen = self.cfg.get("close_with_game", True)
        self.after(GAME_CHECK_MS, self.watch_game)

