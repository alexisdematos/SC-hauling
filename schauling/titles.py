"""Contract routes read with the game's own title templates.

Every *HaulCargo*_title line of global.ini (stock, Smart Citizen or our
user.ini) says where the places sit in the title:

    ~mission(ReputationRank) Rank - ~mission(CargoGradeToken) Cargo Haul -
    ~mission(Location|Address) > ~mission(Destination|Address), ~mission(Destination1|Address)

Each template becomes a regex with one group per place, so any title format
works, and we know which place is Location1 or Destination2. Idea from
Starlogger (chrisrico/Starlogger, haul_titles.py).
"""

import re
from pathlib import Path

_EM = re.compile(r"</?EM\d+>")
_PLACEHOLDER = re.compile(r"~mission\(([^)]*)\)")
_TITLE_KEY = re.compile(r"(?i)haulcargo.*title")
_PLACE = re.compile(r"(?i)^(location|destination)(\d*)$")


def global_ini_for(log_path):
    """The global.ini the game reads, next to the Game.log it writes."""
    return Path(log_path).parent / "data" / "Localization" / "english" / "global.ini"


def _clean(text):
    return re.sub(r"\s+", " ", _EM.sub("", text or "")).strip()


def _literal(text):
    # Spaces may vary, or vanish around a place the contract does not have.
    return re.sub(r"(?:\\ )+", r"\\s*", re.escape(text))


def template_to_regex(template):
    """(regex, literal length) for a title template, or None without places.

    Groups are named o<n> (Location<n>, Location = 0) and d<n>. Numbered
    places may come out empty when the contract has fewer of them.
    """
    template = _clean(template)
    parts, last, seen, literal = [], 0, set(), 0
    for m in _PLACEHOLDER.finditer(template):
        text = template[last:m.start()]
        parts.append(_literal(text))
        literal += len(text.strip())
        place = _PLACE.match(m.group(1).split("|")[0].strip())
        name = None
        if place:
            name = ("o" if place.group(1).lower() == "location" else "d") + (place.group(2) or "0")
        if name and name not in seen:
            seen.add(name)
            parts.append(f"(?P<{name}>.+?)" if name.endswith("0") else f"(?P<{name}>.*?)")
        else:
            parts.append(r".+?")
        last = m.end()
    parts.append(_literal(template[last:]))
    literal += len(template[last:].strip())
    if not seen:
        return None
    # The notification ends with ": " after the title.
    return re.compile(r"^\s*" + "".join(parts) + r"\s*:?\s*$"), literal


class TitleTemplates:
    def __init__(self, lines=()):
        self.matchers = []
        for line in lines:
            key, sep, value = line.partition("=")
            if sep and _TITLE_KEY.search(key):
                found = template_to_regex(value)
                if found:
                    self.matchers.append(found)
        # Most literal text first: a precise template beats a loose one.
        self.matchers.sort(key=lambda m: -m[1])

    @classmethod
    def load(cls, path):
        """Templates from a global.ini; none when it is missing."""
        try:
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                return cls(f)
        except OSError:
            return cls()

    def route(self, title):
        """(origins, destinations) in Location, Location1… order, or None."""
        clean = _clean(title)
        for rx, _ in self.matchers:
            m = rx.match(clean)
            if not m:
                continue
            groups = m.groupdict()
            origins, dests = [], []
            for name in sorted(groups, key=lambda n: int(n[1:])):
                value = (groups[name] or "").strip(" ,")
                if value:
                    (origins if name[0] == "o" else dests).append(value)
            if origins or dests:
                return origins, dests
        return None


def load_for_log(log_path):
    return TitleTemplates.load(global_ini_for(log_path))
