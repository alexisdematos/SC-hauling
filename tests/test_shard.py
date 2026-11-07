import unittest
from types import SimpleNamespace

from schauling.session import Session
from schauling.ships import shard_change

JOIN = ("<2026-10-31T16:17:21.712Z> [Notice] <Join PU> address[104.155.89.51] port[64326] "
        "shard[pub_euw1b_12881860_150] locationId[562954248454145] [Team_GameServices][GIM][Matchmaking]")
UPDATE = ("<2026-10-31T16:17:21.991Z> [Notice] <Update Shard Id> New Shard Id: pub_euw1b_12881860_150. "
          "Old Shard Id [Team_OnlineTech][Telemetry][Services]")
LEAVE = ("<2026-10-31T17:02:00.000Z> [Notice] <Update Shard Id> New Shard Id: . "
         "Old Shard Id pub_euw1b_12881860_150 [Team_OnlineTech][Telemetry][Services]")
OTHER = "<2026-10-31T17:05:00.000Z> [Notice] <Update Shard Id> New Shard Id: pub_use1b_5551234_22. Old Shard Id [x]"


class ShardLine(unittest.TestCase):
    def test_real_lines(self):
        self.assertEqual(shard_change(JOIN), "pub_euw1b_12881860_150")
        self.assertEqual(shard_change(UPDATE), "pub_euw1b_12881860_150")
        self.assertEqual(shard_change(OTHER), "pub_use1b_5551234_22")

    def test_leaving_a_server_is_an_empty_id(self):
        self.assertEqual(shard_change(LEAVE), "")

    def test_other_lines_say_nothing(self):
        self.assertIsNone(shard_change("<2026-10-31T16:17:22Z> [Notice] <Something> shard talk"))
        self.assertIsNone(shard_change("<x> <Join PU> address[1.2.3.4] port[1]"))
        self.assertIsNone(shard_change(""))


class SessionShard(unittest.TestCase):
    def see(self, session, line):
        return Session._see_shard(session, line)

    def test_follows_the_server_and_reports_changes_only(self):
        s = SimpleNamespace(shard=None)
        self.assertTrue(self.see(s, JOIN))
        self.assertEqual(s.shard, "pub_euw1b_12881860_150")
        self.assertFalse(self.see(s, UPDATE))  # the same id again: no change
        self.assertTrue(self.see(s, LEAVE))
        self.assertIsNone(s.shard)
        self.assertTrue(self.see(s, OTHER))
        self.assertEqual(s.shard, "pub_use1b_5551234_22")
        self.assertFalse(self.see(s, "an unrelated line"))
        self.assertEqual(s.shard, "pub_use1b_5551234_22")


if __name__ == "__main__":
    unittest.main()
