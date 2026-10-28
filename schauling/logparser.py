"""Turn Game.log lines into mission events.

Only reads the log the game writes for itself: no memory reading, no
injection. Each parser returns an event dict or None.
"""
import re

_TS = re.compile(r"^<(?P<ts>[^>]+)>")

_NOTIFICATION = re.compile(
    r'<SHUDEvent_OnNotification> Added notification "(?P<kind>[^:"]+):\s*(?P<text>.*?):?\s*" '
    r"\[\d+\] to queue\..*?MissionId: \[(?P<mission>[^\]]*)\], ObjectiveId: \[(?P<objective>[^\]]*)\]"
)
_MARKER = re.compile(
    r"<CLocalMissionPhaseMarker::CreateMarker> .*?missionId \[(?P<mission>[^\]]+)\], "
    r"generator name \[(?P<generator>[^\]]*)\], contract \[(?P<contract>[^\]]*)\].*?"
    r"objectiveId \[(?P<objective>[^\]]+)\].*?"
    r"position \[x: (?P<x>[-\d.]+), y: (?P<y>[-\d.]+), z: (?P<z>[-\d.]+)\]"
)
_DATABANK = re.compile(
    r"<CObjectiveMarkerComponent::AddToPlayerDataBank> MissionObjectiveMarker_\d+\[(?P<marker>\d+)\]"
    r".*?ZonePos: x: (?P<x>[-\d.]+), "
    r"y: (?P<y>[-\d.]+), z: (?P<z>[-\d.]+), missionId\[(?P<mission>[^\]]+)\], "
    r"objectiveId\[(?P<objective>[^\]]+)\]"
)
# Abandoning a contract in the mobiGlas logs nothing else: its markers go away.
_DATABANK_REMOVE = re.compile(
    r"<CObjectiveMarkerComponent::RemoveFromPlayerDataBank> MissionObjectiveMarker_\d+\[(?P<marker>\d+)\]"
    r".*?missionId\[(?P<mission>[^\]]+)\], objectiveId\[(?P<objective>[^\]]+)\]"
)
_END_MISSION = re.compile(
    r"<EndMission> .*?MissionId\[(?P<mission>[^\]]+)\].*?CompletionType\[(?P<state>\w+)\]"
)
_OBJECTIVE_STATE = re.compile(
    r"<ObjectiveUpserted> .*?mission_id (?P<mission>\S+) - objective_id (?P<objective>\S+) - "
    r"state MISSION_OBJECTIVE_STATE_(?P<state>\w+)"
)
_MISSION_ENDED = re.compile(
    r"<MissionEnded> .*?mission_id (?P<mission>\S+) - mission_state MISSION_STATE_(?P<state>\w+)"
)

# Notification kinds we keep, mapped to event names.
_KINDS = {
    "Contract Accepted": "accepted",
    "Contract Complete": "completed",
    "Contract Failed": "failed",
    "Contract Abandoned": "abandoned",
    "New Objective": "objective",
    "Objective Complete": "objective_done",
    "Objective Withdrawn": "objective_withdrawn",
}


def parse_line(line):
    """Event dict for one log line, or None when the line is not about a mission."""
    if "ission" not in line and "bjective" not in line:
        return None
    ts = _TS.match(line)
    ts = ts.group("ts") if ts else None

    m = _NOTIFICATION.search(line)
    if m:
        kind = _KINDS.get(m.group("kind").strip())
        if not kind or not m.group("mission"):
            return None
        return {
            "type": kind,
            "ts": ts,
            "mission": m.group("mission"),
            "objective": m.group("objective") or None,
            "text": m.group("text").strip(),
        }

    for regex, kind in ((_MARKER, "marker"), (_DATABANK, "position")):
        m = regex.search(line)
        if m:
            ev = {
                "type": kind,
                "ts": ts,
                "mission": m.group("mission"),
                "objective": m.group("objective"),
                "pos": (float(m.group("x")), float(m.group("y")), float(m.group("z"))),
            }
            if kind == "marker":
                ev["contract"] = m.group("contract")
                ev["generator"] = m.group("generator")
            else:
                ev["marker"] = m.group("marker")
            return ev

    m = _DATABANK_REMOVE.search(line)
    if m:
        return {"type": "marker_removed", "ts": ts, "mission": m.group("mission"),
                "objective": m.group("objective"), "marker": m.group("marker")}

    m = _OBJECTIVE_STATE.search(line)
    if m:
        return {
            "type": "objective_state",
            "ts": ts,
            "mission": m.group("mission"),
            "objective": m.group("objective"),
            "state": m.group("state"),
        }

    m = _MISSION_ENDED.search(line)
    if m:
        return {"type": "ended", "ts": ts, "mission": m.group("mission"), "state": m.group("state")}

    m = _END_MISSION.search(line)
    if m:
        return {"type": "ended", "ts": ts, "mission": m.group("mission"), "state": m.group("state").upper()}
    return None


def strip_tags(text):
    return re.sub(r"<[^>]+>", "", text or "").strip()


# Delivery objective texts. The game text comes from global.ini, so it
# depends on the strings the player uses (stock, Smart Citizen, our own).
_DELIVERY_FORMS = [
    # Stock: "Deliver 0/24 SCU of Distilled Spirits to Teasa Spaceport"
    re.compile(r"^Deliver (?P<amount>\d+)/(?P<total>\d+)(?: SCU)?(?: of)? (?P<item>.+?) to (?P<dest>.+)$", re.I),
    # Short: "0/24 Distilled Spirits > Teasa Spaceport"
    re.compile(r"^(?P<amount>\d+)/(?P<total>\d+)(?: SCU)?(?: of)? (?P<item>.+?) > (?P<dest>.+)$"),
    # Route first: "Levski > Teasa Spaceport : 0/24 SCU Distilled Spirits"
    re.compile(r"^(?P<origin>.+?) > (?P<dest>.+?) : (?P<amount>\d+)/(?P<total>\d+)(?: SCU)?(?: of)? (?P<item>.+?)\.?$"),
    # Courier, one item: "Deliver Constellation Mk V Cntr. Gold Box To RSI Event Platform"
    re.compile(r"^Deliver (?P<item>.+?) [Tt]o (?P<dest>.+)$"),
]

_PICKUP_FORMS = [
    # Courier: "Collect Constellation Mk V Cntr. Gold Box From RSI Event Platform"
    # Package: "Pick up Package #4821 from Area18.", box run: "Pick Up from Bountiful Harvest"
    re.compile(r"^(?:Collect|Pick [Uu]p)(?: the)?(?: (?P<item>.+?))? [Ff]rom (?P<origin>.+?)\.?$"),
    # "Go to Shubin Mining Facility SAL-5 to pick up cargo."
    re.compile(r"^Go [Tt]o (?P<origin>.+?) to (?:pick up|retrieve) .+$"),
]


def parse_pickup(text):
    """{item, origin} for a pickup objective text (item may be None), or None."""
    text = strip_tags(text)
    for form in _PICKUP_FORMS:
        m = form.match(text)
        if m:
            d = m.groupdict()
            return {"item": (d.get("item") or "").strip(" .") or None, "origin": d["origin"].strip(" .")}
    return None


def parse_delivery(text):
    """{item, amount, total, dest[, origin]} for a delivery objective text, or None."""
    text = strip_tags(text)
    for form in _DELIVERY_FORMS:
        m = form.match(text)
        if m:
            d = m.groupdict()
            d["amount"], d["total"] = int(d.get("amount") or 0), int(d.get("total") or 0)
            d["dest"] = d["dest"].strip(" .")
            d["item"] = d["item"].strip(" .")
            return d
    return None


def title_route(title):
    """(origin, destination) from a contract title with a route, else (None, None).

    Smart Citizen's route titles end with "- Levski > August Dunlow Spaceport in Orison",
    followed by tags such as [BP] or [100 Rep].
    """
    text = strip_tags(title)
    text = re.sub(r"\s*\[[^\]]*\]", "", text).strip()
    last = re.split(r"\s+[-|]\s+", text)[-1]
    m = re.match(r"^(?:from\s+)?(?P<origin>.+?)\s*(?:>|→|->)\s*(?P<dest>.+)$", last, re.I)
    if m:
        origin = m.group("origin").strip()
        return origin, m.group("dest").strip()
    m = re.match(r"^from\s+(?P<origin>.+)$", last, re.I)
    return (m.group("origin").strip(), None) if m else (None, None)


def title_origins(title):
    """Every pickup a title lists: "- Levski, Seraphim Station > Teasa Spaceport"."""
    origin, _ = title_route(title)
    return [o.strip() for o in origin.split(",") if o.strip()] if origin else []


def title_destinations(title):
    """Every drop-off a title lists: "- Levski > Teasa Spaceport, Seraphim Station" (SingleToMulti)."""
    _, dest = title_route(title)
    return [d.strip() for d in dest.split(",") if d.strip()] if dest else []
