"""The stops the game adds to get past a planet, from the detours its log shows (see heading.detour).

Kept free of imports: the live server uses it, and it must not pull in the overlay's tkinter code."""
import re

MAX_VIA = 8


def _body_of(entity):
    """"OOC_Stanton_2_Crusader" -> "Crusader", "pyro5" -> "pyro5": the last word of the entity's name."""
    return re.split(r"[_\s]+", entity.strip())[-1] if entity else "?"


def via_stops(detours):
    """The stops the game added to get past planets, in order, from the detours of a route [(from, to, entity)]:
    each detour puts an OM point of its planet after its "from". The log's own name for such a point is always
    "OM-1" (seen in game: the panel said OM-6, OM-3, OM-5), so it is never used: a point reads "OM point (Crusader)"."""
    nodes, open_point = [], False
    for start, _end, entity in detours[:MAX_VIA]:
        if not open_point and (not nodes or nodes[-1] != start):
            nodes.append(start)  # a detour that starts from the point the last one put there adds none
        nodes.append(f"OM point ({_body_of(entity)})")
        open_point = True
    if detours:
        nodes.append(detours[-1][1])  # the stop after the last detour
    return nodes
