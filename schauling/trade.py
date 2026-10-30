"""Fill the free cargo space on the way: goods bought at one stop of the
route and sold at a later one, with UEX prices from the VerseTime API.

The route stays as it is: we only look at what its stops can buy and sell.
Off by default; no network call while it is off.
"""
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime

from .api import API_URL
from .routing import loads
from .team import TeamError, _request

LIMIT = 3  # suggestions at most
MIN_PROFIT = 2000  # aUEC: below that, not worth the stop at the terminal
CACHE_S = 30 * 60  # the API caches UEX for 30 to 60 min: no point asking more often
OLD_S = 7 * 24 * 3600  # prices older than this get "old prices" (UEX ones are often days old)
RETRY_S = 60  # after a failed ask
MAX_PLACES = 60  # per ask: the API answers 400 above


@dataclass
class Trade:
    buy_at: int  # index of the stop in plan.stops
    sell_at: int
    name: str
    scu: int
    buy: float  # aUEC per SCU
    sell: float
    old: bool = False

    @property
    def profit(self):
        return (self.sell - self.buy) * self.scu


def free_space(plan, capacity):
    """Free SCU on leaving each stop (after its unloads and loads)."""
    return [capacity - load for load in loads(plan)[1:]]


def _seconds(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _fit(q, sizes):
    """q rounded down to what the containers sold there can make."""
    sizes = [int(s) for s in sizes or [] if s and int(s) > 0]
    step = min(sizes) if sizes else 1
    return int(q) // step * step


def _limit(value):
    return float("inf") if value is None else value


def suggest(plan, capacity, prices, budget, illegal=False, now=None, limit=LIMIT):
    """Best trades on the route, greedy: the one that earns most, then its
    space, money, stock and demand are taken, and again. prices: {place:
    [{name, buy, sell, stockBuy, demandSell, containerSizes, illegal,
    buyUpdatedAt, sellUpdatedAt}]} (parse_prices). budget: aUEC we may tie up in goods at once."""
    stops = plan.stops if plan else []
    if not capacity or not stops or not prices:
        return []
    now = time.time() if now is None else now
    free = free_space(plan, capacity)
    cash = [budget or 0] * len(stops)
    stock, demand = {}, {}
    by_place = {p: {c["name"]: c for c in goods or [] if c.get("name")} for p, goods in prices.items()}
    out = []
    while len(out) < limit:
        best = None
        for i, a in enumerate(stops):
            for name, c in by_place.get(a.place, {}).items():
                if not c.get("buy") or (c.get("illegal") and not illegal):
                    continue
                for j in range(i + 1, len(stops)):
                    if stops[j].place == a.place:
                        continue
                    d = by_place.get(stops[j].place, {}).get(name)
                    if not d or not d.get("sell") or d["sell"] <= c["buy"]:
                        continue
                    q = min(min(free[i:j]), min(cash[i:j]) // c["buy"],
                            stock.get((a.place, name), _limit(c.get("stockBuy"))),
                            demand.get((stops[j].place, name), _limit(d.get("demandSell"))))
                    q = _fit(q, c.get("containerSizes"))
                    if q <= 0 or (d["sell"] - c["buy"]) * q < MIN_PROFIT:
                        continue
                    t = Trade(i, j, name, q, c["buy"], d["sell"])
                    if best is None or t.profit > best.profit:
                        best = t
                        times = [_seconds(c.get("buyUpdatedAt", c.get("updatedAt"))),
                                 _seconds(d.get("sellUpdatedAt", d.get("updatedAt")))]
                        best.old = any(s is None or now - s > OLD_S for s in times)
        if best is None:
            break
        out.append(best)
        for k in range(best.buy_at, best.sell_at):
            free[k] -= best.scu
            cash[k] -= best.scu * best.buy
        a, b, name = stops[best.buy_at].place, stops[best.sell_at].place, best.name
        stock[(a, name)] = stock.get((a, name), _limit(by_place[a][name].get("stockBuy"))) - best.scu
        demand[(b, name)] = demand.get((b, name), _limit(by_place[b][name].get("demandSell"))) - best.scu
    return out


def money(x):
    """45000 -> "45k", 1250000 -> "1.25M"."""
    x = round(x)
    if abs(x) >= 1_000_000:
        return f"{x / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    if abs(x) >= 1000:
        return f"{round(x / 1000)}k"
    return str(x)


def lines_at(trades, index):
    """Route view lines for the stop at `index`: buys, then sells."""
    out = []
    for t in trades:
        tag = "(trade, old prices)" if t.old else "(trade)"
        if t.buy_at == index:
            out.append(f"     ↑ buy {t.scu} SCU {t.name} {tag}")
        if t.sell_at == index:
            out.append(f"     ↓ sell {t.scu} SCU {t.name} +{money(t.profit)} {tag}")
    return out


def _none_if_zero(n):
    return n or None  # UEX gives 0 when it does not know


def parse_prices(data):
    """{place: [commodity]} from the API answer {"places": {place: [rows,
    one per terminal and good]}}, keyed by the names we sent. Terminals of
    one place are merged: we buy at the cheapest, sell at the best paid,
    each with its own stock, demand, containers and date. Unknown places:
    no entry."""
    out = {}
    places = data.get("places") if isinstance(data, dict) else None
    for name, rows in (places if isinstance(places, dict) else {}).items():
        if not name or not isinstance(rows, list):
            continue
        goods = {}
        for c in rows:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            g = goods.setdefault(c["name"], {"name": c["name"], "buy": None, "sell": None,
                                             "illegal": bool(c.get("illegal"))})
            if c.get("buy") and (g["buy"] is None or c["buy"] < g["buy"]):
                g.update(buy=c["buy"], stockBuy=_none_if_zero(c.get("stockBuy")),
                         containerSizes=c.get("containerSizes"), buyUpdatedAt=c.get("updatedAt"))
            if c.get("sell") and (g["sell"] is None or c["sell"] > g["sell"]):
                g.update(sell=c["sell"], demandSell=_none_if_zero(c.get("demandSell")),
                         sellUpdatedAt=c.get("updatedAt"))
        out[name] = list(goods.values())
    return out


class Prices:
    """Prices of the route's places, asked again after CACHE_S."""

    def __init__(self, api_url=API_URL, request=_request, clock=time.monotonic):
        self.url = api_url.rstrip("/")
        self.request = request
        self.clock = clock
        self.data = {}
        self.error = None
        self._asked = {}  # place: clock time of the answer

    def missing(self, places):
        """Places not asked yet, or asked too long ago."""
        now = self.clock()
        return sorted(p for p in set(places) if now - self._asked.get(p, float("-inf")) > CACHE_S)

    def fetch(self, places):
        """Ask for `places` (network: on a worker thread). Error text or None."""
        if not places:
            return None
        data = {}
        try:
            for k in range(0, len(places), MAX_PLACES):
                q = urllib.parse.urlencode({"places": ",".join(places[k:k + MAX_PLACES])})
                data.update(parse_prices(self.request("GET", f"{self.url}/commodities?{q}")))
        except TeamError as e:
            self.error = str(e)
            for p in places:  # again in RETRY_S, not at the next tick
                self._asked[p] = self.clock() - CACHE_S + RETRY_S
            return self.error
        now = self.clock()
        for p in places:
            self.data[p] = data.get(p, [])
            self._asked[p] = now
        self.error = None
        return None

