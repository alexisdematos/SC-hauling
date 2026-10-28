"""After a game patch: does the log and global.ini still say what we read?

Every log line we rely on is looked for in the current Game.log and the
last backup. A kind of line that shows up but that our parser no longer
reads means the patch changed its format; objective markers missing in a
session with contracts accepted mean they moved or went. In global.ini the
contract title templates must still name their places. Warnings are shown
once per game build (FileVersion at the top of Game.log).
"""
import re
from pathlib import Path

from . import ships, titles
from .locations import location_code
from .logparser import parse_line
from .logsource import _lines

_QT_START = re.compile(r"CSCItemNavigation::CalculateRoute\|Projected Start Location is (.+?) for route to destination")
_VERSION = re.compile(r"FileVersion:\s*(\S+)")


def _event(kind):
    def ok(line):
        ev = parse_line(line)
        return bool(ev and ev["type"] == kind)
    return ok


# (what, the lines it is about, does our code read it)
CHECKS = [
    ("contract accepted", lambda l: 'notification "Contract Accepted' in l and "MissionId" in l,
     _event("accepted")),
    ("new objective", lambda l: 'notification "New Objective' in l and "MissionId" in l, _event("objective")),
    ("objective marker", lambda l: "CLocalMissionPhaseMarker::CreateMarker" in l, _event("marker")),
    ("marker position", lambda l: "AddToPlayerDataBank" in l and "objectiveId" in l, _event("position")),
    ("objective completed", lambda l: "<ObjectiveUpserted>" in l and "COMPLETED" in l, _event("objective_state")),
    ("mission end", lambda l: "<EndMission>" in l, _event("ended")),
    ("local inventory", lambda l: "RequestLocationInventory" in l, lambda l: bool(location_code(l))),
    ("quantum start", lambda l: "CalculateRoute|Projected Start Location" in l, lambda l: bool(_QT_START.search(l))),
    ("ship channel", lambda l: 'notification "You have joined channel' in l, lambda l: bool(ships._BOARDED.search(l))),
]
# Under this share of its lines read, a kind of line changed format.
MIN_READ = 0.5


def scan(lines):
    """{what: (lines seen, lines read)} and the game version, from log lines."""
    counts = {what: [0, 0] for what, *_ in CHECKS}
    version = None
    for line in lines:
        if version is None:
            m = _VERSION.search(line)
            if m:
                version = m.group(1)
        for what, about, read in CHECKS:
            if about(line):
                counts[what][0] += 1
                counts[what][1] += bool(read(line))
    return {k: tuple(v) for k, v in counts.items()}, version


def log_warnings(counts):
    out = []
    for what, (seen, read) in counts.items():
        if seen and read / seen < MIN_READ:
            out.append(f"log: {what} lines no longer read ({read} of {seen})")
    accepted = counts["contract accepted"][1]
    if accepted and not counts["objective marker"][0]:
        out.append(f"log: no objective marker after {accepted} contract(s) accepted")
    return out


def ini_warnings(global_ini):
    path = Path(global_ini)
    if not path.is_file():
        return []  # no global.ini there: nothing to check
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    out = []
    if not titles.TitleTemplates(text.splitlines()).matchers:
        out.append("global.ini: no contract title template names its places (~mission(Location…)/(Destination…))")
    if not re.search(r"(?m)^HaulCargo_obj_itemspecifics\w*=", text):
        out.append("global.ini: HaulCargo_obj_itemspecifics keys are gone (cargo objective texts)")
    return out


def log_lines(log_path):
    """Lines of the last backup, then of the current Game.log."""
    log_path = Path(log_path)
    files = [log_path]
    backups = sorted((log_path.parent / "logbackups").glob("*.log"), key=lambda p: p.stat().st_mtime)
    if backups:
        files.insert(0, backups[-1])
    lines = []
    for f in files:
        try:
            lines += _lines(f)[0]
        except OSError:
            pass
    return lines


def check(log_path):
    """(game version, warnings) for the current Game.log, its last backup
    and the global.ini next to it."""
    log_path = Path(log_path)
    counts, _ = scan(log_lines(log_path))
    try:
        _, version = scan(_lines(log_path)[0][:50])  # the current game's version, at the top
    except OSError:
        version = None
    return version, log_warnings(counts) + ini_warnings(titles.global_ini_for(log_path))


def new_warnings(cfg, version, warnings):
    """The warnings not shown yet for this game version; remembers them."""
    seen = cfg.get("patch_check") or {}
    if seen.get("version") != version:
        seen = {"version": version, "shown": []}
    fresh = [w for w in warnings if w not in seen["shown"]]
    seen["shown"] = sorted(set(seen["shown"]) | set(fresh))
    cfg["patch_check"] = seen
    return fresh
