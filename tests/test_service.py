"""The service API: /ui, /events, /action behind a token, CORS for the SC-overlay window only, the actions."""
import http.client
import json
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from schauling import controller as controller_module
from schauling import liveserver, service
from schauling.controller import AUTO, Controller

TOKEN = "s3cret-token"
TAURI = "http://tauri.localhost"


class FakeApi:
    token = TOKEN

    def __init__(self):
        self.hub = service.Hub()
        self.actions = []

    def snapshot(self):
        return self.hub.snapshot()

    def snapshot_event(self):
        return self.hub.snapshot_event()

    def subscribe(self):
        return self.hub.subscribe()

    def unsubscribe(self, q):
        self.hub.unsubscribe(q)

    def act(self, request):
        self.actions.append(request)
        return (200, {"ok": True}) if request.get("type") == "good" else (400, {"ok": False, "error": "unknown action"})


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.api.hub.publish({"version": 1, "updatedAt": "t", "status": "hello"})
        self.server = liveserver.LiveServer(lambda: {"version": 1, "route": []}, self.api)
        self.server.start(0)
        self.addCleanup(self.server.stop)
        self.port = self.server.port

    def call(self, method, path, body=None, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = dict(headers or {})
        if host:
            h["Host"] = host
        conn.request(method, path, body=body, headers=h)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response, data

    def auth(self, token=TOKEN, **more):
        return dict({"Authorization": f"Bearer {token}"}, **more)

    def test_state_is_public_and_unchanged(self):
        response, data = self.call("GET", "/state")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(data)["version"], 1)

    def test_ui_needs_the_token(self):
        self.assertEqual(self.call("GET", "/ui")[0].status, 401)
        self.assertEqual(self.call("GET", "/ui", headers=self.auth("wrong"))[0].status, 401)
        self.assertEqual(self.call("GET", "/ui", headers={"Authorization": TOKEN})[0].status, 401)  # no Bearer
        response, data = self.call("GET", "/ui", headers=self.auth())
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(data)["status"], "hello")

    def test_ui_before_the_first_render(self):
        empty = FakeApi()
        server = liveserver.LiveServer(lambda: {}, empty)
        server.start(0)
        self.addCleanup(server.stop)
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.request("GET", "/ui", headers=self.auth())
        response = conn.getresponse()
        self.assertEqual((response.status, json.loads(response.read())), (503, {"error": "starting"}))
        conn.close()

    def test_token_in_the_query_only_for_events(self):
        self.assertEqual(self.call("GET", f"/ui?token={TOKEN}")[0].status, 401)

    def test_action_needs_the_token_and_a_json_object(self):
        body = json.dumps({"type": "good"})
        self.assertEqual(self.call("POST", "/action", body)[0].status, 401)
        self.assertEqual(self.call("POST", "/action", body, self.auth("wrong"))[0].status, 401)
        self.assertEqual(self.api.actions, [])
        response, data = self.call("POST", "/action", body, self.auth())
        self.assertEqual((response.status, json.loads(data)), (200, {"ok": True}))
        self.assertEqual(self.call("POST", "/action", "{not json", self.auth())[0].status, 400)
        self.assertEqual(self.call("POST", "/action", "[1]", self.auth())[0].status, 400)
        self.assertEqual(self.call("POST", "/action", json.dumps({"type": "nope"}), self.auth())[0].status, 400)
        self.assertEqual(self.call("POST", "/action", "x" * (liveserver.MAX_BODY + 1), self.auth())[0].status, 413)

    def test_get_on_action_and_post_elsewhere_are_not_found(self):
        self.assertEqual(self.call("GET", "/action", headers=self.auth())[0].status, 404)
        self.assertEqual(self.call("POST", "/state", "{}", self.auth())[0].status, 404)

    def test_cors_only_for_the_overlay_window(self):
        response, _ = self.call("GET", "/ui", headers=self.auth(Origin=TAURI))
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), TAURI)
        response, _ = self.call("GET", "/ui", headers=self.auth(Origin="https://tauri.localhost"))
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "https://tauri.localhost")
        for origin in ("https://evil.example", liveserver.SITE, "http://localhost:8093"):
            response, _ = self.call("GET", "/ui", headers=self.auth(Origin=origin))
            self.assertIsNone(response.getheader("Access-Control-Allow-Origin"), origin)
        # and /state keeps its own list: the overlay window is not on it
        response, _ = self.call("GET", "/state", headers={"Origin": TAURI})
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        response, _ = self.call("GET", "/state", headers={"Origin": liveserver.SITE})
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), liveserver.SITE)

    def test_preflight(self):
        response, _ = self.call("OPTIONS", "/action", headers={"Origin": TAURI})
        self.assertEqual(response.status, 204)
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), TAURI)
        self.assertIn("POST", response.getheader("Access-Control-Allow-Methods"))
        self.assertIn("Authorization", response.getheader("Access-Control-Allow-Headers"))
        response, _ = self.call("OPTIONS", "/action", headers={"Origin": "https://evil.example"})
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))

    def test_host_check_stays(self):
        self.assertEqual(self.call("GET", "/ui", headers=self.auth(), host="evil.example")[0].status, 403)
        self.assertEqual(self.call("POST", "/action", "{}", self.auth(), host="evil.example")[0].status, 403)

    def test_events_push_each_change(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", f"/events?token={TOKEN}", headers={"Origin": TAURI})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type").split(";")[0], "text/event-stream")
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), TAURI)

        def event():
            block = []
            while True:
                line = response.fp.readline().decode("utf-8").rstrip("\n")
                if line == "":
                    if any(x.startswith("data:") for x in block):
                        return block
                    block = []
                    continue
                block.append(line)

        first = event()
        self.assertEqual(json.loads([x for x in first if x.startswith("data:")][0][5:])["status"], "hello")
        self.api.hub.publish({"version": 1, "updatedAt": "t", "status": "changed"})
        second = event()
        self.assertEqual(json.loads([x for x in second if x.startswith("data:")][0][5:])["status"], "changed")
        self.assertNotEqual(first[0], second[0])  # the id moves
        conn.close()

    def test_events_need_the_token(self):
        self.assertEqual(self.call("GET", "/events")[0].status, 401)
        self.assertEqual(self.call("GET", "/events?token=wrong")[0].status, 401)

    def test_without_an_api_the_routes_are_off(self):
        plain = liveserver.LiveServer(lambda: {"version": 1})
        plain.start(0)
        self.addCleanup(plain.stop)
        conn = http.client.HTTPConnection("127.0.0.1", plain.port, timeout=5)
        conn.request("GET", "/ui", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(conn.getresponse().status, 404)
        conn.close()


class TokenTest(unittest.TestCase):
    def test_token_ok(self):
        self.assertTrue(liveserver.token_ok("a", "a"))
        self.assertFalse(liveserver.token_ok("a", "b"))
        self.assertFalse(liveserver.token_ok(None, "b"))
        self.assertFalse(liveserver.token_ok("", ""))
        self.assertFalse(liveserver.token_ok("a", None))


class HubTest(unittest.TestCase):
    def test_publish_only_changes(self):
        hub = service.Hub()
        self.assertTrue(hub.publish({"a": 1, "updatedAt": "x"}))
        self.assertFalse(hub.publish({"a": 1, "updatedAt": "y"}))  # the time alone is no change
        self.assertTrue(hub.publish({"a": 2, "updatedAt": "y"}))

    def test_a_slow_listener_gets_the_latest(self):
        hub = service.Hub()
        q = hub.subscribe()
        for n in range(10):
            hub.publish({"n": n})
        self.assertLessEqual(q.qsize(), service.EVENT_BACKLOG)
        last = None
        while not q.empty():
            last = q.get()
        self.assertIn(b'"n": 9', last)
        hub.unsubscribe(q)
        hub.publish({"n": 99})
        self.assertTrue(q.empty())


def fake_world():
    place = lambda name: SimpleNamespace(name=name, body="Hurston", system="Stanton")
    leg = SimpleNamespace(key=("m1", 0), index=0, cargo="Agricium", total=11, from_team=None, picked=False,
                          delivered=False)
    contract = SimpleNamespace(id="m1", title="Haul", code="H1", tracked=True, legs={("m1", 0): leg},
                               open_legs=lambda: [leg])
    calls = []
    tracker = SimpleNamespace(
        contracts={"m1": contract}, active=lambda: [contract],
        pickup_of=lambda c, l: SimpleNamespace(name="Lorville", confidence=2),
        dropoff_of=lambda c, l: SimpleNamespace(name="?Somewhere", confidence=0),
        hide=lambda c: calls.append(("hide", c.id)),
        set_pickup=lambda c, l, name: calls.append(("pickup", c.id, name)))
    session = SimpleNamespace(
        tracker=tracker, hidden=set(), manual={}, start=None, last_ship="Caterpillar", ships={"Cutlass": 1, "Hull C": 2},
        travel=SimpleNamespace(ship="Cutlass"), places=SimpleNamespace(places=[place("Lorville"), place("Area18")]),
        capacity=lambda: 46, aboard=None, heading=None, handle=None, shard="pub_test_1_1")
    return session, contract, calls


class ServiceTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(controller_module.config, "save")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.session, self.contract, self.calls = fake_world()
        self.controller = Controller(self.session, {"ship": "Cutlass"})
        self.controller.request_plan = lambda: None
        self.controller.render = lambda: None
        self.service = service.Service(self.controller, TOKEN)
        # no engine thread here: what is scheduled runs at once, like a loop that is quick
        self.controller.after = lambda ms, fn: threading.Thread(target=fn).start()

    def act(self, **request):
        return self.service.act(request)

    def test_service_turns_updates_off_and_takes_the_live_server(self):
        self.assertFalse(self.controller.updates)
        self.assertIs(self.controller.live.api, self.service)

    def test_ui_shape(self):
        ui = service.build_ui(self.controller, now=1_700_000_000)
        self.assertEqual(ui["version"], service.UI_VERSION)
        self.assertIn("shard", ui)  # the game server, for the team widget
        json.dumps(ui)  # all of it is plain JSON
        self.assertEqual(ui["ship"], {"name": "Cutlass", "capacity": 46, "auto": False})
        self.assertEqual(ui["total"], {"scu": 11, "capacity": 46})
        leg = ui["contracts"][0]["legs"][0]
        self.assertEqual((leg["id"], leg["cargo"], leg["scu"], leg["from"], leg["to"]),
                         ("m1:0", "Agricium", 11, "Lorville", None))
        self.assertEqual(ui["lists"]["ships"], ["Cutlass", "Hull C"])
        self.assertEqual([p["name"] for p in ui["lists"]["places"]], ["Lorville", "Area18"])
        self.assertIsNone(ui["team"]["code"])
        self.assertIs(ui["settings"]["auto_update"], True)
        self.assertIs(ui["settings"]["trade"], False)

    def test_route_stops_carry_their_local_time(self):
        c = self.controller
        c._live = {"route": [{"place": "Lorville", "arrival": None}, {"place": "Area18", "arrival": None}]}
        c.plan = SimpleNamespace(stops=[SimpleNamespace(place="Lorville", local=("14:20", "Afternoon", True)),
                                        SimpleNamespace(place="Area18", local=("03:10", "Night", False))])
        route = service._route_with_local_time(c)
        self.assertEqual(route[0]["local"], {"clock": "14:20", "phase": "Afternoon", "day": True})
        self.assertEqual(route[1]["local"], {"clock": "03:10", "phase": "Night", "day": False})
        self.assertNotIn("local", c._live["route"][0])  # /state is left as it was
        c.plan.stops[1].local = None  # not fetched yet
        self.assertIsNone(service._route_with_local_time(c)[1]["local"])

    def test_local_time_is_dropped_when_route_and_plan_disagree(self):
        c = self.controller
        c._live = {"route": [{"place": "Lorville"}]}
        c.plan = SimpleNamespace(stops=[SimpleNamespace(place="Area18", local=("1:00", "x", True))])
        self.assertIsNone(service._route_with_local_time(c)[0]["local"])
        c.plan = None
        self.assertIsNone(service._route_with_local_time(c)[0]["local"])

    def trade_world(self):
        """A plan of two stops and one suggested trade between them (suggest and route_lines are faked)."""
        from schauling.trade import Trade
        c = self.controller
        c.cfg["trade"] = True
        c.plan = SimpleNamespace(stops=[SimpleNamespace(place="Lorville", local=None),
                                        SimpleNamespace(place="Area18", local=None)])
        c._live = {"route": [], "location": "Lorville", "locationSource": "log"}
        self.trades = [Trade(0, 1, "Agricium", 32, 25.0, 30.0)]
        for patcher in (mock.patch.object(service.trade, "suggest", lambda *a, **k: self.trades),
                        mock.patch.object(service, "route_lines", lambda *a, **k: [])):
            patcher.start()
            self.addCleanup(patcher.stop)
        return c

    def test_ui_has_the_location_and_the_trade_places(self):
        c = self.trade_world()
        ui = service.build_ui(c, now=1_700_000_000)
        self.assertEqual(ui["location"], {"name": "Lorville", "source": "log"})
        t = ui["trade"]["suggestions"][0]
        self.assertEqual((t["buyAt"], t["sellAt"], t["buyPlace"], t["sellPlace"]), (0, 1, "Lorville", "Area18"))
        self.assertEqual(t["done"], {"buy": False, "sell": False})

    def test_trade_done_ticks_unticks_and_forgets_gone_trades(self):
        c = self.trade_world()
        request = dict(type="trade_done", buyPlace="Lorville", sellPlace="Area18", name="Agricium", side="buy")
        self.assertEqual(self.act(**request), (200, {"ok": True}))
        self.assertTrue(service.build_ui(c)["trade"]["suggestions"][0]["done"]["buy"])
        self.assertFalse(service.build_ui(c)["trade"]["suggestions"][0]["done"]["sell"])
        self.assertEqual(self.act(**request, done=False)[0], 200)
        self.assertFalse(service.build_ui(c)["trade"]["suggestions"][0]["done"]["buy"])
        self.act(**request)
        self.trades = []  # an empty answer (prices refreshing) keeps the ticks
        service.build_ui(c)
        self.assertEqual(len(c._trade_done), 1)
        from schauling.trade import Trade
        self.trades = [Trade(0, 1, "Agricium", 32, 25.0, 30.0)]  # the same trades again: still ticked
        self.assertTrue(service.build_ui(c)["trade"]["suggestions"][0]["done"]["buy"])
        self.trades = [Trade(0, 1, "Quantanium", 8, 10.0, 20.0)]  # other trades: a clean start
        ui = service.build_ui(c)
        self.assertEqual(c._trade_done, set())
        self.assertEqual(ui["trade"]["suggestions"][0]["done"], {"buy": False, "sell": False})

    def test_ticks_start_clean_when_the_route_changes(self):
        c = self.trade_world()
        service.build_ui(c)  # the plan is known
        self.act(type="trade_done", buyPlace="Lorville", sellPlace="Area18", name="Agricium", side="buy")
        self.assertTrue(service.build_ui(c)["trade"]["suggestions"][0]["done"]["buy"])
        c.plan.stops.append(SimpleNamespace(place="Orison", local=None))  # same trade, another route
        self.assertFalse(service.build_ui(c)["trade"]["suggestions"][0]["done"]["buy"])

    def test_trade_done_refuses_bad_requests(self):
        self.assertEqual(self.act(type="trade_done", buyPlace="a", sellPlace="b", name="c", side="both")[0], 400)
        self.assertEqual(self.act(type="trade_done", buyPlace="a", sellPlace="b", side="buy")[0], 400)
        self.assertEqual(self.act(type="trade_done", buyPlace=5, sellPlace="b", name="c", side="buy")[0], 400)

    def test_trade_ticks_are_capped(self):
        self.controller._trade_done = {("a", "b", str(i), "buy") for i in range(service.MAX_TRADE_TICKS)}
        status, answer = self.act(type="trade_done", buyPlace="a", sellPlace="b", name="new", side="buy")
        self.assertEqual((status, answer["error"]), (400, "too many ticks"))
        # a tick that is already there can still be set again or removed
        self.controller._trade_done.add(("a", "b", "old", "buy"))
        self.controller._trade_done.discard(("a", "b", "0", "buy"))
        self.assertEqual(self.act(type="trade_done", buyPlace="a", sellPlace="b", name="old", side="buy")[0], 200)

    def test_listener_publishes_on_render(self):
        Controller.render(self.controller)  # the real one: refresh_live then notify
        self.assertIsNotNone(self.service.snapshot())

    def test_unknown_action(self):
        self.assertEqual(self.act(type="nope")[0], 400)
        self.assertEqual(self.act()[0], 400)

    def test_set_start(self):
        self.assertEqual(self.act(type="set_start", place="Area18"), (200, {"ok": True}))
        self.assertEqual(self.controller.cfg["start"], "Area18")
        self.assertEqual(self.act(type="set_start", place="")[0], 200)
        status, answer = self.act(type="set_start", place="Nowhere")
        self.assertEqual((status, answer["error"]), (400, "unknown place"))
        self.assertEqual(self.act(type="set_start", place=5)[0], 400)

    def test_set_ship(self):
        self.assertEqual(self.act(type="set_ship", ship="Hull C")[0], 200)
        self.assertEqual(self.controller.cfg["ship"], "Hull C")
        self.assertEqual(self.act(type="set_ship", ship="Death Star")[0], 400)
        self.assertEqual(self.act(type="set_ship", auto=True)[0], 200)
        self.assertTrue(self.controller.cfg["ship_auto"])
        self.assertEqual(self.controller.session.travel.ship, "Caterpillar")  # the last ship flown

    def test_hide_and_pickup_by_id(self):
        self.assertEqual(self.act(type="hide_contract", contract="m1")[0], 200)
        self.assertEqual(self.act(type="hide_contract", contract="nope")[0], 400)
        self.assertEqual(self.act(type="set_pickup", contract="m1", leg="m1:0", place="Area18")[0], 200)
        self.assertEqual(self.act(type="set_pickup", contract="m1", leg="m1:9", place="Area18")[0], 400)
        self.assertEqual(self.act(type="set_pickup", contract="m1", leg="m1:0", place="Nowhere")[0], 400)
        self.assertEqual(self.calls, [("hide", "m1"), ("pickup", "m1", "Area18")])

    def test_options(self):
        self.controller.refresh_prices = lambda: None
        self.assertEqual(self.act(type="set_option", key="trade", value=True)[0], 200)
        self.assertIs(self.controller.cfg["trade"], True)
        self.assertEqual(self.act(type="set_option", key="alpha", value=True)[0], 400)
        self.assertEqual(self.act(type="set_option", key="trade", value="yes")[0], 400)
        self.assertEqual(self.act(type="set_option", key="live_server", value=False)[0], 400)  # the API stays up

    def test_team_actions(self):
        status, answer = self.act(type="team_join", code="abcdefgh23")
        self.assertEqual(status, 400)  # no pseudo yet
        self.assertEqual(self.act(type="team_name", name="Alexis")[0], 200)
        self.assertEqual(self.act(type="team_join", code="abcdefgh23")[0], 200)
        self.assertEqual(self.controller.cfg["team"]["code"], "abcdefgh23")
        self.assertEqual(self.act(type="team_join", code="not a code")[0], 400)
        self.assertEqual(self.act(type="team_name", name="   ")[0], 400)
        self.assertEqual(self.act(type="team_leave")[0], 200)

    def test_team_create_needs_a_pseudo(self):
        self.assertEqual(self.act(type="team_create")[0], 400)

    def test_trade_budget(self):
        self.assertEqual(self.act(type="trade_budget", text="50k")[0], 200)
        self.assertEqual(self.controller.cfg["trade_budget"], 50000)
        self.assertEqual(self.act(type="trade_budget", text="lots")[0], 400)

    def test_a_bug_in_an_action_is_a_500_and_the_engine_lives(self):
        with mock.patch.dict(service.ACTIONS, {"boom": lambda c, r: 1 / 0}):
            status, answer = self.act(type="boom")
        self.assertEqual((status, answer["ok"]), (500, False))

    def test_timeout_when_the_engine_does_not_answer(self):
        self.controller.after = lambda ms, fn: None  # the engine thread is stuck
        with mock.patch.object(service, "ACTION_TIMEOUT_S", 0.05):
            self.assertEqual(self.act(type="set_start", place="")[0], 504)

    def test_an_action_that_timed_out_never_runs_later(self):
        queued = []
        self.controller.after = lambda ms, fn: queued.append(fn)  # the engine gets to it too late
        with mock.patch.object(service, "ACTION_TIMEOUT_S", 0.05):
            self.assertEqual(self.act(type="set_start", place="Area18")[0], 504)
        self.assertEqual(len(queued), 1)
        queued[0]()  # the engine finally runs it
        self.assertIsNone(self.controller.cfg.get("start"))

    def test_the_removed_actions_are_unknown(self):
        for kind in ("set_view", "open_map"):
            self.assertEqual(self.act(type=kind)[0], 400)


class ParentTest(unittest.TestCase):
    def test_this_process_is_alive(self):
        import os
        self.assertTrue(service.parent_alive(os.getpid()))

    def test_a_dead_process_is_gone(self):
        import subprocess
        import sys
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        self.assertFalse(service.parent_alive(p.pid))


if __name__ == "__main__":
    unittest.main()
