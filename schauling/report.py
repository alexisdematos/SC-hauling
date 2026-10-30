"""Plain-text lines for the overlay and the console."""
from .routing import peak_load
from .trade import lines_at, money
from .tracker import GUESS, UNKNOWN


def duration(seconds):
    seconds = int(seconds or 0)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m{s:02d}"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}"


def _mark(stop):
    if stop.name.startswith("?"):
        return ""
    return {GUESS: "~", UNKNOWN: "?"}.get(stop.confidence, "")


def _cargo(leg):
    scu = f"{leg.total} SCU " if leg.total else ""
    # The game logs cargo names only for a contract tracked when accepted.
    return f"{scu}{leg.cargo or 'cargo (not in log)'}"


def cargo_lines(tracker):
    """One line per open leg, origin first: (text, done, contract, leg)."""
    out = []
    for c in tracker.active():
        mark = "▶ " if c.tracked else ""
        out.append((mark + (c.title or c.code or c.id[:8]), None, c, None))
        for leg in c.open_legs():
            a, b = tracker.pickup_of(c, leg), tracker.dropoff_of(c, leg)
            tick = "✓ " if leg.picked else ""
            out.append((f"{tick}{_mark(a)}{a.name} → {_mark(b)}{b.name} : {_cargo(leg)}", leg.picked, c, leg))
    return out


def route_lines(plan, capacity=None, trades=()):
    """The stops in order; with the ship's cargo SCU, the most aboard at once.
    trades: goods to buy and sell on the way (trade.suggest), after the
    contract cargos of each stop."""
    out = []
    for i, stop in enumerate(plan.stops, 1):
        jump = f", jump {' → '.join(stop.via)}" if stop.via else ""
        hop = f"  (+{duration(stop.seconds)}{jump})" if stop.seconds else ""
        # Local time on arrival: will it be day? ☾ when the star is down.
        when = ""
        if stop.local:
            clock, phase, is_day = stop.local
            when = f"  · {clock} {phase.lower()}" + ("" if is_day else " ☾")
        out.append(f"{i}. {stop.place}{hop}{when}")
        for a in stop.actions:
            arrow = "↑ load" if a.kind == "pickup" else "↓ unload"
            out.append(f"     {arrow} {_cargo(a.leg)}")
        out.extend(lines_at(trades, i - 1))
    for a in plan.unplaced:
        what = "pickup" if a.kind == "pickup" else "delivery"
        out.append(f"?  {what} place unknown: {_cargo(a.leg)}")
    if plan.stops:
        out.append(f"Total quantum: ~{duration(plan.total_seconds)}")
        peak = peak_load(plan)
        if capacity and peak > capacity:
            out.append(f"Too much: up to {peak} SCU aboard, the ship holds {capacity}")
        elif capacity:
            out.append(f"Max aboard: {peak} / {capacity} SCU")
        if trades:
            out.append(f"Trade on the way: +{money(sum(t.profit for t in trades))} (UEX prices)")
    return out
