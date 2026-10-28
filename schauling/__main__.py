"""python -m schauling [--once] [--log PATH] [--ship NAME] [--from PLACE]"""
import argparse
import sys
import time

from . import config
from .logsource import find_log
from .report import cargo_lines, route_lines
from .session import Session


def main(argv=None):
    cfg = config.load()
    ap = argparse.ArgumentParser(prog="schauling", description="Star Citizen hauling overlay from Game.log")
    ap.add_argument("--log", default=cfg.get("log"), help="path to Game.log")
    ap.add_argument("--scmap", default=cfg.get("scmap_data"), help="SC-map data folder")
    ap.add_argument("--ship", default=cfg.get("ship"), help="ship for travel times")
    ap.add_argument("--from", dest="start", default=cfg.get("start"), help="where you are now")
    ap.add_argument("--once", action="store_true", help="print the contracts and the route, then exit")
    ap.add_argument("--watch", action="store_true", help="print to the console on each change")
    args = ap.parse_args(argv)
    # The Windows console defaults to cp1252, which has no arrows.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    console = args.once or args.watch

    log = find_log(args.log)
    if (not log or not log.is_file()) and not console:
        log = _ask_log()  # the exe has no console to print to
        if log:
            cfg["log"] = str(log)
            config.save(cfg)
    if not log or not log.is_file():
        _fail("Game.log not found: give its path with --log", console)
    data = config.find_scmap_data(args.scmap)
    if not data:
        _fail("SC-map data not found: give the data folder with --scmap", console)

    cfg.setdefault("item_names", {})
    session = Session(log, data, args.ship, args.start, manual=cfg.get("manual_pickups", {}),
                      items=cfg["item_names"], hidden=cfg.get("hidden", []))

    if cfg.get("ship_auto") and session.last_ship and args.ship == cfg.get("ship"):
        session.travel.ship = session.last_ship  # no --ship given: the last ship flown

    if args.once or args.watch:
        while True:
            print("\n".join(text for text, *_ in cargo_lines(session.tracker)) or "No hauling contract.")
            print()
            print("\n".join(route_lines(session.plan())))
            if args.once:
                return
            while not session.update():
                time.sleep(1)
            print("-" * 40)

    from .overlay import Overlay

    cfg["ship"], cfg["start"] = args.ship, args.start
    Overlay(session, cfg).run()


def _ask_log():
    """Let the player pick Game.log (in the game's LIVE folder)."""
    import tkinter as tk
    from pathlib import Path
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    path = filedialog.askopenfilename(
        title="Where is Star Citizen's Game.log? (StarCitizen\\LIVE\\Game.log)",
        filetypes=[("Game.log", "Game.log"), ("Log files", "*.log")])
    root.destroy()
    return Path(path) if path else None


def _fail(message, console):
    if not console:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("SC Hauling", message)
        root.destroy()
    sys.exit(message)


if __name__ == "__main__":
    main()
