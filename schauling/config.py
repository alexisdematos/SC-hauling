"""User settings, kept in %APPDATA%\\SC-hauling\\config.json."""
import json
import os
import sys
from pathlib import Path

from .liveserver import OLD_SITE, SITE

DEFAULTS = {
    "log": None,
    "scmap_data": None,
    "ship": "Drake Caterpillar",
    "start": None,
    "geometry": "+40+200",
    "alpha": 0.85,
    "view": "cargo",
    "manual_pickups": {},
    "item_names": {},
    "hidden": [],
    "contract_pages": {},  # mission id: objectives read on its page (OCR), applied again at start
    "auto_showlocation": False,
    "ship_auto": True,  # follow the last ship flown, from Game.log
    "hide_outside_game": True,  # hidden while another app is in front of the game
    "close_with_game": True,  # quit when StarCitizen.exe closes
    "read_contract_page": True,  # OCR of the contract page when a contract is accepted
    "click_through": True,  # clicks outside the texts go to the game
    "auto_update": True,  # the exe downloads new versions from GitHub releases
    "live_server": True,  # 127.0.0.1 server for the VerseTime map: position and route
    "live_port": 47823,
    "site_url": SITE,  # VerseTime site: map, invite links, allowed origin
    "team": None,  # {"code", "member", "secret"} while in a team: the state goes to the API
    "team_name": "",  # pseudo shown to the team
    "trade": False,  # goods to buy and sell on the way (UEX prices, from the API): off, no call
    "trade_budget": 100000,  # aUEC we may tie up in goods at once
    "trade_illegal": False,  # include illegal goods
    "team_api": None,  # another API for teams (tests: http://localhost:8787)
}


def folder():
    base = os.environ.get("APPDATA") or Path.home()
    return Path(base) / "SC-hauling"


def load():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads((folder() / "config.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return migrate(cfg)


def migrate(cfg):
    """The site moved to Cloudflare: the old default goes, a URL set by hand stays."""
    if (cfg.get("site_url") or "").rstrip("/") == OLD_SITE:
        cfg["site_url"] = SITE
    return cfg


def save(cfg):
    try:
        folder().mkdir(parents=True, exist_ok=True)
        (folder() / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass


def data_cache():
    """Copy of the SC-map data kept in step with the API (see datasync)."""
    return folder() / "data"


def find_scmap_data(path=None):
    """SC-map data folder, best first: given, the copy synced with the API
    (same names as the API), bundled in the exe, SC-map next to this project."""
    here = Path(__file__).resolve().parent.parent
    bundled = Path(getattr(sys, "_MEIPASS", here)) / "data"  # PyInstaller unpacks here
    for p in [path, data_cache(), bundled, here.parent / "SC-map" / "data", here / "data"]:
        if p and (Path(p) / "locations.csv").is_file():
            return Path(p)
    return None
