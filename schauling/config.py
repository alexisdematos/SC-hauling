"""User settings, kept in %APPDATA%\\SC-hauling\\config.json."""
import json
import os
import sys
from pathlib import Path

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
    "live_server": True,  # 127.0.0.1 server for the VerseTime map: position and route
    "live_port": 47823,
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
