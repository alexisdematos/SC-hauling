"""Always-on-top window with the hauling contracts (tkinter), on top of the engine in controller.py.

Run the game in borderless window mode so it can sit on top. Drag it with
the mouse; right-click for the menu; click a cargo line to set its pickup.
Type /showlocation in the game chat: the overlay reads the clipboard and
starts the route where you are.
"""
import contextlib
import tkinter as tk
from tkinter import messagebox, simpledialog

from . import config, team, trade, windows
from .controller import AUTO, Controller, POLL_MS  # noqa: F401  (AUTO: ship picker value)
from .picker import pick
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

FOCUS_MS = 250  # how often we check which window is in front
CURSOR_MS = 50  # how often we check what the mouse is over (click-through)


class Overlay(Controller):
    def __init__(self, session, cfg):
        super().__init__(session, cfg)
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
        self.menu.add_command(label="Start location…", command=self._later(self.ask_start))
        self.menu.add_command(label="Ship…", command=self._later(self.ask_ship))
        self.menu.add_command(label="More / less transparent", command=self.cycle_alpha)
        self.menu.add_command(label="Read the contract page now (open it in the mobiGlas first)",
                              command=self._later(self.read_contract_page_now))
        self.menu.add_command(label="Open the map (VerseTime)", command=self.open_map)
        self.team_menu = tk.Menu(self.menu, tearoff=0, postcommand=self._team_menu_state)
        self.team_menu.add_command(label="Create a team", command=self._later(self.ask_create_team))
        self.team_menu.add_command(label="Join a team… (code or invite link)", command=self._later(self.ask_join_team))
        self.team_menu.add_command(label="Pseudo…", command=self._later(self.ask_team_name))
        self.team_menu.add_command(label="Copy the invite link", command=self._later(self.copy_invite))
        self.team_menu.add_command(label="Leave the team", command=self._later(self.leave_team))
        self.menu.add_cascade(label="Team (shares position and route on the Internet)", menu=self.team_menu)
        self._trade_var = tk.BooleanVar(value=bool(cfg.get("trade")))
        self._illegal_var = tk.BooleanVar(value=bool(cfg.get("trade_illegal")))
        trade_menu = tk.Menu(self.menu, tearoff=0)
        trade_menu.add_checkbutton(label="Suggest goods to buy and sell on the route", variable=self._trade_var,
                                   command=self.toggle_trade)
        trade_menu.add_checkbutton(label="Include illegal goods", variable=self._illegal_var,
                                   command=self.toggle_trade)
        trade_menu.add_command(label="Budget…", command=self._later(self.ask_trade_budget))
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
                           ("click_through", "Clicks go through, except on the texts"),
                           ("auto_update", "Update automatically (new versions on GitHub)")):
            var = self._options[key] = tk.BooleanVar(value=bool(cfg.get(key, True)))
            self.menu.add_checkbutton(label=label, variable=var,
                                      command=lambda k=key: self._toggle_option(k))
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=self.quit)

        self._dialog_open = False
        self._dialogs = 0
        self._focused_dialog = False

        self.start()
        if windows.available():
            self.root.update_idletasks()
            hwnd = int(self.root.wm_frame(), 16)
            self.start_page_watcher(lambda: hwnd)
        self.root.after(POLL_MS, self.tick)
        self.root.after(team.READ_S * 1000, self.read_team_every)
        self.watch_game()  # first check now, then every GAME_CHECK_MS
        self.root.after(FOCUS_MS, self.follow_focus)
        if windows.available():
            self._through, self._right = None, False
            self.root.after(CURSOR_MS, self.follow_cursor)

    def _toggle_option(self, key):
        self.set_option(key, bool(self._options[key].get()))

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
        if self._dialog_open:
            pass  # never hide under an open dialog
        elif self.cfg.get("hide_outside_game", True) and windows.available() and self._game_seen:
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

    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _drag_end(self, _e):
        self.cfg["geometry"] = f"+{self.root.winfo_x()}+{self.root.winfo_y()}"
        config.save(self.cfg)

    def _later(self, func):
        """A menu command that runs once the menu is closed: Tk runs commands inside its native popup loop, where
        a dialog (own loop and grab) or starting the browser breaks."""
        return lambda: self.root.after(50, func)

    def _menu(self, e):
        self.menu.tk_popup(e.x_root, e.y_root)

    def cycle_alpha(self):
        steps = [0.95, 0.85, 0.7, 0.55]
        a = self.cfg.get("alpha", 0.85)
        nxt = steps[(steps.index(a) + 1) % len(steps)] if a in steps else 0.85
        self.cfg["alpha"] = nxt
        self.root.attributes("-alpha", nxt)
        config.save(self.cfg)

    def _pick(self, title, items, current=None, extra=()):
        with self._modal():
            return pick(self.root, title, items, current, extra)

    def ask_start(self):
        name = self._pick("Where are you now?", self._place_items(), self.cfg.get("start"),
                          extra=[("(anywhere: start at the first stop)", "")])
        if name is not None:
            self.apply_start(name)

    def ask_ship(self):
        last = self.session.last_ship
        auto = f"(last ship flown: {last})" if last else "(last ship flown, from Game.log)"
        name = self._pick("Ship for travel times", [(n, n) for n in self.session.ships],
                          None if self.cfg.get("ship_auto") else self.cfg.get("ship"), extra=[(auto, AUTO)])
        if name is not None:
            self.choose_ship(name)

    def ask_hide(self, contract):
        # The log does not tell an abandon from an untrack: the player says.
        with self._modal():
            ok = messagebox.askyesno("Hide contract", f"Abandoned? Hide it:\n{contract.title or contract.code}",
                                     parent=self.root)
        if ok:
            self.hide_contract(contract)

    def ask_pickup(self, contract, leg):
        current = self.session.tracker.pickup_of(contract, leg).name
        name = self._pick(f"Pickup for {leg.cargo or 'this cargo'}", self._place_items(), current,
                          extra=[("(clear: back to what the log says)", "")])
        if name is not None:
            self.set_pickup(contract, leg, name)

    def toggle_auto_showlocation(self):
        self.set_option("auto_showlocation", bool(self._auto_var.get()))

    def toggle_live(self):
        self.set_option("live_server", bool(self._live_var.get()))

    def open_map(self):
        # Menu commands run inside Tk's native popup loop: starting the browser from there killed Python
        # (fatal "GIL released" error). Run it once the menu is closed.
        self.root.after(50, super().open_map)

    def _team_menu_state(self):
        inside = bool(self._team_code())
        for label in ("Create a team", "Join a team… (code or invite link)"):
            self.team_menu.entryconfigure(label, state="disabled" if inside else "normal")
        for label in ("Copy the invite link", "Leave the team"):
            self.team_menu.entryconfigure(label, state="normal" if inside else "disabled")

    def toggle_trade(self):
        self.cfg["trade_illegal"] = bool(self._illegal_var.get())
        self.set_option("trade", bool(self._trade_var.get()))

    def ask_trade_budget(self):
        text = self._ask("Trade", "aUEC you may spend on goods at once:", str(self.cfg.get("trade_budget") or ""))
        self.set_trade_budget(text)

    @contextlib.contextmanager
    def _modal(self):
        """Around a dialog: the dialog stays above the game and the overlay stays shown, even after Alt+Tab.

        The overlay is not topmost while a dialog is open, and the dialog itself is made topmost once (a dialog
        that is not goes behind the game after Alt+Tab, with the overlay holding a grab nobody can reach)."""
        self._dialogs += 1
        self._dialog_open = True
        self._focused_dialog = False
        self.root.attributes("-topmost", False)

        def top():  # once the dialog exists; no repeated calls, they upset focus
            if self._dialog_open:
                for w in self.root.winfo_children():
                    if isinstance(w, tk.Toplevel):
                        try:
                            w.attributes("-topmost", True)
                            if windows.available() and not self._focused_dialog:
                                # Windows keeps the game active otherwise: the dialog shows but takes no input
                                # until the user clicks away and back.
                                self._focused_dialog = True
                                windows.bring_to_front(int(w.wm_frame(), 16))
                                w.focus_force()
                        except (tk.TclError, ValueError, OSError):
                            pass

        self.root.after(50, top)
        self.root.after(300, top)
        try:
            yield
        finally:
            self._dialogs -= 1
            self._dialog_open = self._dialogs > 0
            if not self._dialog_open:
                self.root.attributes("-topmost", True)

    def _ask(self, title, prompt, initial=""):
        with self._modal():
            return simpledialog.askstring(title, prompt, initialvalue=initial, parent=self.root)

    def ask_team_name(self):
        """The pseudo the team sees. True when there is one."""
        name = self._ask("Team", f"Your pseudo for the team (1 to {team.MAX_NAME} characters):",
                         self.cfg.get("team_name") or "")
        return self.set_team_name(name)

    def ask_create_team(self):
        if self.cfg.get("team_name") or self.ask_team_name():
            self.create_team()

    def ask_join_team(self):
        text = self._ask("Join a team", "Team code or invite link:")
        if text is None:
            return
        if team.parse_code(text) and not self.cfg.get("team_name") and not self.ask_team_name():
            return
        self.join_team(text)

    # --- what the engine asks of the window ---------------------------------------

    def after(self, ms, func):
        self.root.after(ms, func)

    def own_hwnd(self):
        return int(self.root.wm_frame(), 16)

    def clipboard_text(self):
        try:
            return self.root.clipboard_get()
        except tk.TclError:  # empty, or not text
            return None

    def copy_text(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        return True

    def report_error(self, error):  # keep the window alive on a bad line
        self.header.configure(text=f"Error: {error}")

    def update_ready(self, exe):
        self.menu.insert_command(self.menu.index("end"), label="Restart (new version ready)",
                                 command=lambda: self.restart(exe))

    def quit(self):
        super().quit()
        self._drag_end(None)
        self.root.destroy()

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
        self.refresh_live()
        code = self._team_code()
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
