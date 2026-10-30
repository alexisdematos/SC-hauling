"""Always-on-top window with the hauling contracts.

Run the game in borderless window mode so it can sit on top. Drag it with
the mouse; right-click for the menu; click a cargo line to set its pickup.
Type /showlocation in the game chat: the overlay reads the clipboard and
starts the route where you are.
"""
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog

from . import config, keys, liveserver, ocr, team, trade, windows
from .pagewatch import PageWatcher
from .picker import pick
from .places import START_RADIUS_KM, parse_coordinates
from .report import cargo_lines, route_lines

BG = "#0b1620"
FG = "#d8e6f0"
DIM = "#6f8796"
ACCENT = "#e8b54a"
DONE = "#5fbf7a"
WARN = "#e8664a"
TRADE = "#7fc8e8"  # goods bought and sold on the way, not contract cargo
FONT = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")

POLL_MS = 1000
FOCUS_MS = 250  # how often we check which window is in front
CURSOR_MS = 50  # how often we check what the mouse is over (click-through)
# Screenshots of the contract page from the menu (ms), once the menu has
# closed. On accept, pagewatch takes them.
MENU_SHOTS_MS = (400, 900)
GAME_CHECK_MS = 5000  # how often we check that the game still runs
QUIT_AFTER_S = 10  # quit this long after the game closed (a crash reporter may linger)
AUTO = "\0auto"  # ship picker value: follow the last ship flown
# Auto /showlocation: at most once in this many seconds, this long after boarding.
SHOWLOCATION_EVERY_S = 300
AFTER_BOARDING_S = 2.0


class Overlay:
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

        self.root = tk.Tk()
        self.root.title("SC Hauling")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", cfg.get("alpha", 0.85))
        self.root.configure(bg=BG)
        self.root.geometry(cfg.get("geometry", "+40+200"))

        self.header = tk.Label(self.root, bg=BG, fg=ACCENT, font=FONT_BOLD, anchor="w", padx=10, pady=4)
        self.header.pack(fill="x")
        self.body = tk.Frame(self.root, bg=BG, padx=10, pady=4)
        self.body.pack(fill="both")

        for w in (self.root, self.header):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag)
            w.bind("<ButtonRelease-1>", self._drag_end)
            w.bind("<Button-3>", self._menu)

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Cargo / Route / Team", command=self.toggle_view)
        self.menu.add_command(label="Start location…", command=self.ask_start)
        self.menu.add_command(label="Ship…", command=self.ask_ship)
        self.menu.add_command(label="More / less transparent", command=self.cycle_alpha)
        self.menu.add_command(label="Read the contract page now (open it in the mobiGlas first)",
                              command=self.read_contract_page_now)
        self.menu.add_command(label="Open the map (VerseTime)", command=self.open_map)
        self.team_menu = tk.Menu(self.menu, tearoff=0, postcommand=self._team_menu_state)
        self.team_menu.add_command(label="Create a team", command=self.create_team)
        self.team_menu.add_command(label="Join a team… (code or invite link)", command=self.join_team)
        self.team_menu.add_command(label="Pseudo…", command=self.ask_team_name)
        self.team_menu.add_command(label="Copy the invite link", command=self.copy_invite)
        self.team_menu.add_command(label="Leave the team", command=self.leave_team)
        self.menu.add_cascade(label="Team (shares position and route on the Internet)", menu=self.team_menu)
        self._trade_var = tk.BooleanVar(value=bool(cfg.get("trade")))
        self._illegal_var = tk.BooleanVar(value=bool(cfg.get("trade_illegal")))
        trade_menu = tk.Menu(self.menu, tearoff=0)
        trade_menu.add_checkbutton(label="Suggest goods to buy and sell on the route", variable=self._trade_var,
                                   command=self.toggle_trade)
        trade_menu.add_checkbutton(label="Include illegal goods", variable=self._illegal_var,
                                   command=self.toggle_trade)
        trade_menu.add_command(label="Budget…", command=self.ask_trade_budget)
        self.menu.add_cascade(label="Trade on the way (UEX prices)", menu=trade_menu)
        self._auto_var = tk.BooleanVar(value=bool(cfg.get("auto_showlocation")))
        self.menu.add_checkbutton(label="Auto /showlocation when boarding a ship (sends keys to the game)",
                                  variable=self._auto_var, command=self.toggle_auto_showlocation)
        self._options = {}
        self._live_var = tk.BooleanVar(value=bool(cfg.get("live_server", True)))
        self.menu.add_checkbutton(label="Live position for the VerseTime map (local server)",
                                  variable=self._live_var, command=self.toggle_live)
        for key, label in (("hide_outside_game", "Show only over the game"),
                           ("close_with_game", "Close when the game closes"),
                           ("read_contract_page", "Read the contract page on accept (screenshot + OCR)"),
                           ("click_through", "Clicks go through, except on the texts")):
            var = self._options[key] = tk.BooleanVar(value=bool(cfg.get(key, True)))
            self.menu.add_checkbutton(label=label, variable=var,
                                      command=lambda k=key: self._toggle_option(k))
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=self.quit)

        self._shown = True
        self._game_seen = False
        self._game_gone_at = None

        self.session.travel.ship = self._ship()
        if self.session.here:  # the current session's log knows where we are
            self.set_start(self.session.here, "log")
        self.session.heading_expired(time.time())  # one from the replay may be old
        self.find_eta()
        if cfg.get("live_server", True):
            self.start_live()
        self.render()
        self.request_plan()
        if windows.available():
            # Its own thread: screenshots the instant a contract is accepted.
            self.root.update_idletasks()
            hwnd = int(self.root.wm_frame(), 16)
            PageWatcher(self.session.source.path, lambda: hwnd,
                        lambda: self.cfg.get("read_contract_page", True),
                        lambda *read: self._pages.put(read)).start()
        self.root.after(POLL_MS, self.tick)
        self.root.after(team.READ_S * 1000, self.read_team_every)
        self.watch_game()  # first check now, then every GAME_CHECK_MS
        self.root.after(FOCUS_MS, self.follow_focus)
        if windows.available():
            self._through, self._right = None, False
            self.root.after(CURSOR_MS, self.follow_cursor)

    def _toggle_option(self, key):
        self.cfg[key] = bool(self._options[key].get())
        config.save(self.cfg)

    # --- game window -------------------------------------------------------

    def follow_cursor(self):
        """Left clicks go through the overlay to the game, except on its
        texts (lines to click, the header to drag). Windows lets all clicks
        through a part of a window or none, so the right button is watched
        by hand: pressed anywhere over the overlay, it opens the menu (the
        game gets that click too)."""
        right = windows.right_button_down()
        if self.cfg.get("click_through", True) and self._shown:
            x, y = self.root.winfo_pointerxy()
            if right and not self._right and self._through and self._over_overlay(x, y):
                self.menu.tk_popup(x, y)
            if not windows.mouse_button_down():
                through = not self._text_at(x, y)
                if through != self._through:
                    self._through = through
                    windows.set_click_through(int(self.root.wm_frame(), 16), through)
        elif not self.cfg.get("click_through", True) and self._through:
            self._through = False
            windows.set_click_through(int(self.root.wm_frame(), 16), False)
        self._right = right
        self.root.after(CURSOR_MS, self.follow_cursor)

    def _text_at(self, x, y):
        """Is a text of ours (a line, the header) under the screen point?
        By position: once the window lets clicks through, Windows no longer
        finds it under the mouse, so winfo_containing() gives None."""
        for w in [self.header] + self.body.winfo_children():
            if isinstance(w, tk.Label) and w.cget("text") and                     w.winfo_rootx() <= x < w.winfo_rootx() + w.winfo_width() and                     w.winfo_rooty() <= y < w.winfo_rooty() + w.winfo_height():
                return True
        return False

    def _over_overlay(self, x, y):
        r = self.root
        return r.winfo_rootx() <= x < r.winfo_rootx() + r.winfo_width() and             r.winfo_rooty() <= y < r.winfo_rooty() + r.winfo_height()

    def follow_focus(self):
        """Hide while another app is in front of a running game."""
        show = True
        if self.cfg.get("hide_outside_game", True) and windows.available() and self._game_seen:
            show = windows.game_in_front() or windows.we_are_in_front()
        if show != self._shown:
            self._shown = show
            if show:
                self.root.deiconify()
                self.root.attributes("-topmost", True)
                self._through = None  # set it again on the shown window
            else:
                self.root.withdraw()
        self.root.after(FOCUS_MS, self.follow_focus)

    def watch_game(self):
        """Note when the game runs; quit a while after it closes."""
        if windows.available():
            if windows.game_running():
                self._game_seen, self._game_gone_at = True, None
            elif self._game_seen:
                self._game_gone_at = self._game_gone_at or time.monotonic()
                if self.cfg.get("close_with_game", True) and time.monotonic() - self._game_gone_at >= QUIT_AFTER_S:
                    self.quit()
                    return
                # Game closed and option off: back to always visible.
                self._game_seen = self.cfg.get("close_with_game", True)
        self.root.after(GAME_CHECK_MS, self.watch_game)

    def _ship(self):
        if self.cfg.get("ship_auto") and self.session.last_ship:
            return self.session.last_ship
        return self.cfg.get("ship")

    # --- window ----------------------------------------------------------

    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _drag_end(self, _e):
        self.cfg["geometry"] = f"+{self.root.winfo_x()}+{self.root.winfo_y()}"
        config.save(self.cfg)

    def _menu(self, e):
        self.menu.tk_popup(e.x_root, e.y_root)

    def cycle_alpha(self):
        steps = [0.95, 0.85, 0.7, 0.55]
        a = self.cfg.get("alpha", 0.85)
        nxt = steps[(steps.index(a) + 1) % len(steps)] if a in steps else 0.85
        self.cfg["alpha"] = nxt
        self.root.attributes("-alpha", nxt)
        config.save(self.cfg)

    def toggle_view(self):
        views = ["cargo", "route"] + (["team"] if self._team_code() else [])
        view = self.cfg.get("view")
        self.cfg["view"] = views[(views.index(view) + 1) % len(views)] if view in views else "cargo"
        config.save(self.cfg)
        self.render()
        self.read_team()

    def _pick(self, title, items, current=None, extra=()):
        self.root.attributes("-topmost", False)
        try:
            return pick(self.root, title, items, current, extra)
        finally:
            self.root.attributes("-topmost", True)

    def _place_items(self):
        return [(f"{p.name}   ·   {p.body}, {p.system}", p.name)
                for p in sorted(self.session.places.places, key=lambda p: p.name)]

    def ask_start(self):
        name = self._pick("Where are you now?", self._place_items(), self.cfg.get("start"),
                          extra=[("(anywhere: start at the first stop)", "")])
        if name is None:
            return
        self.cfg["start"] = self.session.start = name or None
        self._start_source = "manual"
        self.status = None
        config.save(self.cfg)
        self.request_plan()

    def ask_ship(self):
        last = self.session.last_ship
        auto = f"(last ship flown: {last})" if last else "(last ship flown, from Game.log)"
        name = self._pick("Ship for travel times", [(n, n) for n in self.session.ships],
                          None if self.cfg.get("ship_auto") else self.cfg.get("ship"), extra=[(auto, AUTO)])
        if name is None:
            return
        self.cfg["ship_auto"] = name == AUTO
        if name != AUTO:
            self.cfg["ship"] = name
        self.apply_ship()

    def apply_ship(self):
        """Use the chosen ship, or the last one flown when set to auto."""
        ship = self._ship()
        if ship != self.session.travel.ship:
            self.session.travel.ship = ship
            self.request_plan()
        config.save(self.cfg)

    def ask_hide(self, contract):
        # The log does not tell an abandon from an untrack: the player says.
        self.root.attributes("-topmost", False)
        try:
            ok = messagebox.askyesno("Hide contract", f"Abandoned? Hide it:\n{contract.title or contract.code}",
                                     parent=self.root)
        finally:
            self.root.attributes("-topmost", True)
        if not ok:
            return
        self.session.tracker.hide(contract)
        self.cfg["hidden"] = sorted(self.session.hidden)
        config.save(self.cfg)
        self.request_plan()

    def ask_pickup(self, contract, leg):
        current = self.session.tracker.pickup_of(contract, leg).name
        name = self._pick(f"Pickup for {leg.cargo or 'this cargo'}", self._place_items(), current,
                          extra=[("(clear: back to what the log says)", "")])
        if name is None:
            return
        self.session.tracker.set_pickup(contract, leg, name)
        self.cfg["manual_pickups"] = self.session.manual
        config.save(self.cfg)
        self.request_plan()

    def quit(self):
        self.live.stop()
        self.team.set(None, None)
        self._drag_end(None)
        self.root.destroy()

    # --- data ------------------------------------------------------------

    def tick(self):
        try:
            if self.session.update():
                self.request_plan()
        except Exception as e:  # keep the window alive on a bad line
            self.header.configure(text=f"Error: {e}")
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
        self.root.after(POLL_MS, self.tick)

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
            hwnd = int(self.root.wm_frame(), 16)
            windows.exclude_from_capture(hwnd, True)
            try:
                if ocr.capture_window(game, path) if from_menu else ocr.capture_foreground(path):
                    shots.append(path)
            finally:
                windows.exclude_from_capture(hwnd, False)
            if n + 1 < len(delays):
                self.root.after(delays[n + 1] - delays[n], lambda: shoot(n + 1))
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

        self.root.after(delays[0], lambda: shoot(0))

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

    def toggle_auto_showlocation(self):
        self.cfg["auto_showlocation"] = bool(self._auto_var.get())
        config.save(self.cfg)

    def check_clipboard(self):
        """Start the route where /showlocation says the player is."""
        try:
            text = self.root.clipboard_get()
        except tk.TclError:  # empty, or not text
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
        self.status = f"Start: {name} ({source})"
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

    # --- live map ----------------------------------------------------------

    def start_live(self):
        port = int(self.cfg.get("live_port") or liveserver.DEFAULT_PORT)
        try:
            self.live.start(port)
        except OSError as e:
            self.status = f"Live map off: port {port} in use ({e.strerror or e})"

    def toggle_live(self):
        self.cfg["live_server"] = bool(self._live_var.get())
        config.save(self.cfg)
        if self.cfg["live_server"]:
            self.start_live()
        else:
            self.live.stop()
        self.render()

    def open_map(self):
        """VerseTime on where we are, following the live position when the
        server runs."""
        port = int(self.cfg.get("live_port") or liveserver.DEFAULT_PORT) if self.live.running else None
        liveserver.open_map(self.cfg.get("start") or self.session.here, port, self._team_code())

    # --- team --------------------------------------------------------------

    def _team_code(self):
        return (self.cfg.get("team") or {}).get("code")

    def _team_menu_state(self):
        inside = bool(self._team_code())
        for label in ("Create a team", "Join a team… (code or invite link)"):
            self.team_menu.entryconfigure(label, state="disabled" if inside else "normal")
        for label in ("Copy the invite link", "Leave the team"):
            self.team_menu.entryconfigure(label, state="normal" if inside else "disabled")

    def toggle_trade(self):
        self.cfg["trade"] = bool(self._trade_var.get())
        self.cfg["trade_illegal"] = bool(self._illegal_var.get())
        config.save(self.cfg)
        self.refresh_prices()
        self.render()

    def ask_trade_budget(self):
        text = self._ask("Trade", "aUEC you may spend on goods at once:", str(self.cfg.get("trade_budget") or ""))
        try:
            budget = int(float((text or "").replace(" ", "").replace(",", "").lower().replace("k", "e3")))
        except ValueError:
            return
        if budget > 0:
            self.cfg["trade_budget"] = budget
            config.save(self.cfg)
            self.render()

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

    def _ask(self, title, prompt, initial=""):
        self.root.attributes("-topmost", False)
        try:
            return simpledialog.askstring(title, prompt, initialvalue=initial, parent=self.root)
        finally:
            self.root.attributes("-topmost", True)

    def ask_team_name(self):
        """The pseudo the team sees. True when there is one."""
        name = self._ask("Team", f"Your pseudo for the team (1 to {team.MAX_NAME} characters):",
                         self.cfg.get("team_name") or "")
        if name is not None and team.clean_name(name):
            self.cfg["team_name"] = team.clean_name(name)
            config.save(self.cfg)
            self.render()  # sends it at once
        return bool(self.cfg.get("team_name"))

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
        if not self.cfg.get("team_name") and not self.ask_team_name():
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

    def join_team(self):
        text = self._ask("Join a team", "Team code or invite link:")
        if text is None:
            return
        code = team.parse_code(text)
        if not code:
            self.status = "Not a team code or invite link"
            self.render()
            return
        if not self.cfg.get("team_name") and not self.ask_team_name():
            return
        self._enter_team(code)

    def copy_invite(self):
        code = self._team_code()
        if code:
            self.root.clipboard_clear()
            self.root.clipboard_append(team.invite_link(code))
            self.status = f"Team {code}: invite link copied"
            self.render()

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
        if not h or not h["from"] or not h["picked_at"]:
            return

        def done(seconds, _error):
            if self.session.heading is h and seconds is not None:
                h["eta"] = h["picked_at"] + seconds
                self.render()
        self._in_background(lambda: self.session.travel.quantum_seconds(h["from"], h["to"]), done)

    def read_team_every(self):
        self.read_team()
        self.root.after(team.READ_S * 1000, self.read_team_every)

    def read_team(self):
        """The others in the team, for the Team view: read only while it shows."""
        t = self.cfg.get("team")
        if not t or self.cfg.get("view") != "team" or not self._shown or self._reading_team:
            return
        self._reading_team = True

        def done(members, error):
            self._reading_team = False
            if (self.cfg.get("team") or {}).get("code") != t["code"]:
                return  # left or changed team meanwhile
            self._members_error = error
            if members is not None:
                self._members = [m for m in members if m.get("id") != t["member"]]
            self.render()
        self._in_background(lambda: self.team.api.members(t["code"]), done)

    def toggle_member(self, member_id):
        self._open_members ^= {member_id}
        self.render()

    def _shared_with(self, contract_id):
        """Pseudos of the teammates who have this contract too."""
        if not team.SHARE_BY_ID:
            return []
        return [m.get("name") or "?" for m in self._members or []
                if any(c.get("id") == contract_id for c in (m.get("state") or {}).get("contracts") or [])]

    def render_team(self):
        if self._members is None:
            text = f"Team: {self._members_error}" if self._members_error else "Reading the team…"
            tk.Label(self.body, text=text, bg=BG, fg=DIM, font=FONT, anchor="w").pack(anchor="w")
            return
        if self._members_error:
            tk.Label(self.body, text=f"Not read again: {self._members_error}", bg=BG, fg=WARN, font=FONT,
                     anchor="w").pack(anchor="w")
        if not self._members:
            tk.Label(self.body, text="Nobody else in the team yet: Team → Copy the invite link", bg=BG, fg=DIM,
                     font=FONT, anchor="w").pack(anchor="w")
        for m in self._members:
            line = tk.Label(self.body, text=team.member_line(m), bg=BG, fg=ACCENT, font=FONT, anchor="w",
                            cursor="hand2")
            line.pack(anchor="w", pady=(6, 0))
            line.bind("<Button-1>", lambda _e, i=m.get("id"): self.toggle_member(i))
            line.bind("<Button-3>", self._menu)
            for text in team.crossings(self._live, m):
                tk.Label(self.body, text="  " + text, bg=BG, fg=DONE, font=FONT, anchor="w").pack(anchor="w")
            if m.get("id") in self._open_members:
                shared = team.shared_ids(self._live, m)
                for text in team.contract_lines(m, shared) or ["no contract"]:
                    tk.Label(self.body, text="  " + text, bg=BG, fg=DIM if text[:1] != " " else FG, font=FONT,
                             anchor="w", wraplength=460, justify="left").pack(anchor="w")

    def render(self):
        code = self._team_code()
        self._live = team.fit_state(liveserver.build_state(
            self.session.travel.ship, self.cfg.get("start"), self._start_source, self.plan, time.time(), code,
            team.contracts_state(self.session.tracker), (self.cfg.get("team") or {}).get("member"),
            self.session.heading))
        t = self.cfg.get("team")
        name = self.cfg.get("team_name")
        self.team.set((t["code"], t["member"], t["secret"], name) if t and name else None, self._live)
        self._team_error = self.team.error
        for w in self.body.winfo_children():
            w.destroy()
        contracts = self.session.tracker.active()
        scu = sum(l.total for c in contracts for l in c.open_legs())
        view = self.cfg.get("view", "cargo")
        self.header.configure(text=f"Hauling · {len(contracts)} contract(s) · {scu} SCU · {view}")
        start = self.cfg.get("start")
        where = self.status or (f"Start: {start}" if start else "Start: /showlocation in game, or right-click")
        capacity = self.session.capacity()
        status = f"{where}   ·   Ship: {self.session.travel.ship}" + (f" ({capacity} SCU)" if capacity else "")
        tk.Label(self.body, text=status, bg=BG, fg=DIM, font=FONT, anchor="w").pack(anchor="w")
        if code:
            error = self._team_error
            tk.Label(self.body, text=f"Team {code} · " + (f"not shared: {error}" if error else "sharing on"),
                     bg=BG, fg=WARN if error else DIM, font=FONT, anchor="w").pack(anchor="w")
        if view == "team" and code:
            self.render_team()
            return
        if not contracts:
            tk.Label(self.body, text="No hauling contract in Game.log", bg=BG, fg=DIM, font=FONT).pack(anchor="w")
            return
        if view == "route":
            trades = []
            if self.cfg.get("trade") and self.plan:
                trades = trade.suggest(self.plan, capacity, self.prices.data, self.cfg.get("trade_budget"),
                                       self.cfg.get("trade_illegal"))
            lines = route_lines(self.plan, capacity, trades) if self.plan else ["Planning…"]
            if self.cfg.get("trade") and self.plan:
                if not capacity:
                    lines.append("Trade: cargo SCU of the ship unknown")
                elif self.prices.error:
                    lines.append(f"Trade: no prices ({self.prices.error})")
                elif not trades and not self._fetching_prices:
                    lines.append("Trade: nothing worth it on this route")
            for text in lines:
                color = ACCENT if text[:1].isdigit() else (DIM if text.startswith(("Total", "Max")) else FG)
                color = WARN if text.startswith("Too much") else color
                color = TRADE if "(trade" in text or text.startswith("Trade") else color
                tk.Label(self.body, text=text, bg=BG, fg=color, font=FONT, anchor="w", justify="left").pack(anchor="w")
            return
        for text, done, contract, leg in cargo_lines(self.session.tracker):
            if leg is None:
                shared = self._shared_with(contract.id) if code else []
                text += f"   · shared with {', '.join(shared)}" if shared else ""
                title = tk.Label(self.body, text=text, bg=BG, fg=DIM, font=FONT, anchor="w", wraplength=460,
                                 justify="left", cursor="hand2")
                title.pack(anchor="w", pady=(6, 0))
                title.bind("<Button-1>", lambda _e, c=contract: self.ask_hide(c))
                title.bind("<Button-3>", self._menu)
                continue
            lbl = tk.Label(self.body, text="  " + text, bg=BG, fg=DONE if done else FG, font=FONT,
                           anchor="w", cursor="hand2")
            lbl.pack(anchor="w")
            lbl.bind("<Button-1>", lambda _e, c=contract, l=leg: self.ask_pickup(c, l))
            lbl.bind("<Button-3>", self._menu)

    def run(self):
        self.root.mainloop()
