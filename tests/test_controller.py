"""The engine without a window (schauling/controller.py): no tkinter, actions, timers, stale team tags."""
import subprocess
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from schauling import controller as controller_module
from schauling.controller import AUTO, Controller


def fake_session(**extra):
    leg = SimpleNamespace(from_team="Helmarok")
    contract = SimpleNamespace(id="c1", title="Haul", code="C1", legs={"a": leg})
    tracker = SimpleNamespace(contracts={"c1": contract}, hide=lambda c: None, set_pickup=lambda *a: None)
    session = SimpleNamespace(tracker=tracker, hidden=set(), manual={}, start=None, last_ship=None,
                              travel=SimpleNamespace(ship="Cutlass Black"), ships=["Cutlass Black"])
    session.__dict__.update(extra)
    return session, contract, leg


class ControllerTest(unittest.TestCase):
    def make(self, cfg=None, **extra):
        self.session, self.contract, self.leg = fake_session(**extra)
        patcher = mock.patch.object(controller_module.config, "save")
        self.saved = patcher.start()
        self.addCleanup(patcher.stop)
        c = Controller(self.session, cfg if cfg is not None else {})
        c.renders = 0
        c.plans = 0

        def render():
            c.renders += 1
        c.render = render
        c.request_plan = lambda: setattr(c, "plans", c.plans + 1)
        return c

    def test_no_tkinter(self):
        code = "import sys, schauling.controller; assert 'tkinter' not in sys.modules, 'tkinter imported'"
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_start_by_hand(self):
        c = self.make()
        c.apply_start("Lorville")
        self.assertEqual((c.cfg["start"], self.session.start, c._start_source), ("Lorville", "Lorville", "manual"))
        self.assertEqual(c.plans, 1)
        c.apply_start("")  # anywhere
        self.assertEqual((c.cfg["start"], self.session.start), (None, None))

    def test_ship_by_hand_then_auto(self):
        c = self.make()
        c.choose_ship("Hull C")
        self.assertEqual((c.cfg["ship"], c.cfg["ship_auto"], self.session.travel.ship), ("Hull C", False, "Hull C"))
        self.session.last_ship = "Caterpillar"
        c.choose_ship(AUTO)
        self.assertEqual((c.cfg["ship_auto"], self.session.travel.ship), (True, "Caterpillar"))

    def test_trade_budget_text(self):
        c = self.make()
        for text, budget in (("50k", 50000), ("1 200 000", 1200000), ("1,200,000", 1200000), ("2.5k", 2500)):  # commas are thousands separators
            self.assertTrue(c.set_trade_budget(text), text)
            self.assertEqual(c.cfg["trade_budget"], budget)
        c.cfg["trade_budget"] = 7
        for text in ("abc", "0", "-5", "", None):
            self.assertFalse(c.set_trade_budget(text), text)
        self.assertEqual(c.cfg["trade_budget"], 7)

    def test_team_name_and_join(self):
        c = self.make()
        self.assertFalse(c.set_team_name("   "))
        self.assertFalse(c.join_team("not a code"))
        self.assertEqual(c.status, "Not a team code or invite link")
        self.assertFalse(c.join_team("abcdefgh23"))  # no pseudo yet
        self.assertIn("pseudo", c.status)
        self.assertTrue(c.set_team_name("Alexis"))
        self.assertTrue(c.join_team("abcdefgh23"))
        self.assertEqual(c.cfg["team"]["code"], "abcdefgh23")
        self.assertEqual(c._team_code(), "abcdefgh23")

    def test_create_team_needs_a_pseudo(self):
        c = self.make()
        c.create_team()
        self.assertIn("pseudo", c.status)

    def test_options(self):
        c = self.make()
        with self.assertRaises(KeyError):
            c.set_option("alpha", True)
        c.refresh_prices = lambda: None
        c.set_option("trade", True)
        self.assertIs(c.cfg["trade"], True)
        self.assertTrue(self.saved.called)

    def test_invite_link_without_clipboard_goes_to_the_status(self):
        c = self.make({"team": {"code": "abcdefgh23", "member": "m", "secret": "s"}})
        c.copy_invite()
        self.assertIn("abcdefgh23", c.status)
        self.assertNotIn("copied", c.status)
        c.copy_text = lambda text: True
        c.copy_invite()
        self.assertIn("copied", c.status)

    def test_timers_run_when_due(self):
        c = self.make()
        ran = []
        c.after(0, lambda: ran.append("now"))
        c.after(60_000, lambda: ran.append("later"))
        time.sleep(0.01)
        c._run_timers()
        self.assertEqual(ran, ["now"])

    def test_a_failing_timer_does_not_stop_the_loop(self):
        c = self.make()
        errors, ran = [], []
        c.report_error = errors.append
        c.after(0, lambda: 1 / 0)
        c.after(0, lambda: ran.append("next"))
        time.sleep(0.01)
        with mock.patch("traceback.print_exc"):
            c._run_timers()
        self.assertEqual(ran, ["next"])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ZeroDivisionError)

    def test_a_failing_listener_shows_in_the_status(self):
        c = Controller(SimpleNamespace(), {})
        c.listeners.append(lambda: 1 / 0)
        with mock.patch("traceback.print_exc"):
            c.notify()
        self.assertIn("Error:", c.status)

    def test_the_service_may_ignore_the_game_closing(self):
        c = self.make()
        c.quit_with_game = False
        c._game_seen = True
        c._game_gone_at = time.monotonic() - 3600
        quit_calls = []
        c.quit = lambda: quit_calls.append(1)
        with mock.patch.object(controller_module.windows, "available", return_value=True), \
                mock.patch.object(controller_module.windows, "game_running", return_value=False):
            c.watch_game()
        self.assertEqual(quit_calls, [])
        c.quit_with_game = True
        c._game_seen = True
        c._game_gone_at = time.monotonic() - 3600
        with mock.patch.object(controller_module.windows, "available", return_value=True), \
                mock.patch.object(controller_module.windows, "game_running", return_value=False):
            c.watch_game()
        self.assertEqual(quit_calls, [1])

    def test_quit_stops_the_loop(self):
        c = self.make()
        c.live = SimpleNamespace(stop=lambda: None)
        c.team = SimpleNamespace(set=lambda *a: None)
        c.quit()
        self.assertFalse(c._running)

    def test_notify_survives_a_bad_listener(self):
        c = Controller(SimpleNamespace(), {})
        seen = []
        c.listeners += [lambda: 1 / 0, lambda: seen.append(1)]
        with mock.patch("traceback.print_exc"):
            c.notify()
        self.assertEqual(seen, [1])

    def test_stale_team_tag_goes_when_the_teammate_drops_the_contract(self):
        c = self.make()
        c._members, c._members_error = [{"name": "Helmarok", "state": {"contracts": []}}], None
        self.assertEqual(c._drop_stale_team_tags(), 1)
        self.assertIsNone(self.leg.from_team)
        self.assertIn("Helmarok no longer has", c.status)

    def test_team_tag_stays_while_the_teammate_has_it(self):
        c = self.make()
        c._members, c._members_error = [{"name": "Helmarok", "state": {"contracts": [{"id": "c1"}]}}], None
        self.assertEqual(c._drop_stale_team_tags(), 0)
        self.assertEqual(self.leg.from_team, "Helmarok")

    def test_team_tag_stays_for_a_member_who_does_not_publish_contracts(self):
        c = self.make()
        for state in ({}, {"contracts": None}):
            c._members, c._members_error = [{"name": "Helmarok", "state": state}], None
            self.assertEqual(c._drop_stale_team_tags(), 0)
        self.assertEqual(self.leg.from_team, "Helmarok")

    def test_team_tag_stays_with_two_members_of_the_same_pseudo(self):
        c = self.make()
        c._members, c._members_error = [{"name": "Helmarok", "state": {"contracts": []}},
                                        {"name": "Helmarok", "state": {"contracts": [{"id": "c1"}]}}], None
        self.assertEqual(c._drop_stale_team_tags(), 0)

    def test_team_tag_stays_when_the_last_read_failed(self):
        c = self.make()
        c._members, c._members_error = [{"name": "Helmarok", "state": {"contracts": []}}], "offline"
        self.assertEqual(c._drop_stale_team_tags(), 0)
        c._members, c._members_error = None, None
        self.assertEqual(c._drop_stale_team_tags(), 0)


if __name__ == "__main__":
    unittest.main()
