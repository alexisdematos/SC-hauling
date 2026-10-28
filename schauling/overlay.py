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
from tkinter import messagebox

from . import config, keys, windows
from .picker import pick
from .places import START_RADIUS_KM, parse_coordinates
from .report import cargo_lines, route_lines

BG = "#0b1620"
FG = "#d8e6f0"
DIM = "#6f8796"
ACCENT = "#e8b54a"
DONE = "#5fbf7a"
FONT = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")

POLL_MS = 1000
FOCUS_MS = 250  # how often we check which window is in front
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
        self._planning = False
        self._replan = False
        self._last_location = float("-inf")  # monotonic time of the last position
        self._clipboard = None
        self.status = None  # one-line message under the header

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
        self.menu.add_command(label="Cargo / Route", command=self.toggle_view)
        self.menu.add_command(label="Start location…", command=self.ask_start)
        self.menu.add_command(label="Ship…", command=self.ask_ship)
        self.menu.add_command(label="More / less transparent", command=self.cycle_alpha)
        self._auto_var = tk.BooleanVar(value=bool(cfg.get("auto_showlocation")))
        self.menu.add_checkbutton(label="Auto /showlocation when boarding a ship (sends keys to the game)",
                                  variable=self._auto_var, command=self.toggle_auto_showlocation)
        self._options = {}
        for key, label in (("hide_outside_game", "Show only over the game"),
                           ("close_with_game", "Close when the game closes")):
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
        self.render()
        self.request_plan()
        self.root.after(POLL_MS, self.tick)
        self.watch_game()  # first check now, then every GAME_CHECK_MS
        self.root.after(FOCUS_MS, self.follow_focus)

    def _toggle_option(self, key):
        self.cfg[key] = bool(self._options[key].get())
        config.save(self.cfg)

    # --- game window -------------------------------------------------------

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
        self.cfg["view"] = "route" if self.cfg.get("view") == "cargo" else "cargo"
        config.save(self.cfg)
        self.render()

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
        if self.session.place_changed:
            self.session.place_changed = False
            self._last_location = time.monotonic()
            self.set_start(self.session.here, "log")
            self.request_plan()
        self.check_clipboard()
        while not self._results.empty():
            self.plan = self._results.get()
            self._planning = False
            if self._replan:
                self.request_plan()
            self.render()
        self.root.after(POLL_MS, self.tick)

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

    def render(self):
        for w in self.body.winfo_children():
            w.destroy()
        contracts = self.session.tracker.active()
        scu = sum(l.total for c in contracts for l in c.open_legs())
        view = self.cfg.get("view", "cargo")
        self.header.configure(text=f"Hauling · {len(contracts)} contract(s) · {scu} SCU · {view}")
        start = self.cfg.get("start")
        where = self.status or (f"Start: {start}" if start else "Start: /showlocation in game, or right-click")
        status = f"{where}   ·   Ship: {self.session.travel.ship}"
        tk.Label(self.body, text=status, bg=BG, fg=DIM, font=FONT, anchor="w").pack(anchor="w")
        if not contracts:
            tk.Label(self.body, text="No hauling contract in Game.log", bg=BG, fg=DIM, font=FONT).pack(anchor="w")
            return
        if view == "route":
            lines = route_lines(self.plan) if self.plan else ["Planning…"]
            for text in lines:
                color = ACCENT if text[:1].isdigit() else (DIM if text.startswith("Total") else FG)
                tk.Label(self.body, text=text, bg=BG, fg=color, font=FONT, anchor="w", justify="left").pack(anchor="w")
            return
        for text, done, contract, leg in cargo_lines(self.session.tracker):
            if leg is None:
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
