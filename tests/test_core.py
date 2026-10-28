import json
import tempfile
import unittest
from pathlib import Path

from schauling import datasync, keys
from schauling.api import JUMP_SECONDS, TYPICAL_HOP_SECONDS, TravelTimes
from schauling.logparser import parse_delivery, parse_line, parse_pickup, title_route
from schauling.locations import location_code, place_for_code
from schauling.picker import matches
from schauling.places import START_RADIUS_KM, Place, Places, parse_coordinates
from schauling.ships import boarded_ship, piloted_class, ship_for_class
from schauling.routing import Action, build_actions, plan
from schauling.tracker import EXACT, GUESS, Tracker, cargo_tokens, courier_hint

FIXTURE = Path(__file__).parent / "fixtures" / "game_sample.log"

BODIES = {"Delamar": (-9641669.472, -11490738.599, -93.643)}

PLACES = Places([
    Place("Baijini Point", "Space station", "ArcCorp", "Stanton", 0, 0, 0),
    Place("Lorville", "Landing zone", "Hurston", "Stanton", -328.989, -752.435, 572.120),
    Place("Orison", "Landing zone", "Crusader", "Stanton", 5295.517, -863.194, 5282.237),
    Place("Area18", "Landing zone", "ArcCorp", "Stanton", -747.409, -116.734, -262.094),
    Place("Levski", "Landing zone", "Delamar", "Nyx", 0, 0, 0),
    Place("Seraphim Station", "Space station", "Crusader", "Stanton", 0, 0, 0),
] + [
    # Gateways as SC-map names them: "<other system> Gateway (<this system>)".
    Place(f"{to} Gateway ({here})", "Space station", f"{here}-{to} JP", here, 0, 0, 0)
    for here, to in [("Stanton", "Pyro"), ("Stanton", "Nyx"), ("Pyro", "Stanton"),
                     ("Pyro", "Nyx"), ("Nyx", "Pyro"), ("Nyx", "Stanton")]
] + [
    Place("Megumi Refueling", "Space station", "PYR6 L5", "Pyro", 0, 0, 0),
], BODIES)


def fixture_lines():
    return FIXTURE.read_text(encoding="utf-8").splitlines()


class LogParserTest(unittest.TestCase):
    def test_notification(self):
        ev = parse_line(
            '<2026-10-28T12:43:34.715Z> [Notice] <SHUDEvent_OnNotification> Added notification '
            '"New Objective: 0/24 Distilled Spirits > <EM4>Stanton System</EM4>: " [4] to queue. '
            "New queue size: 2, MissionId: [m1], ObjectiveId: [dropoff_ab-cd_0]"
        )
        self.assertEqual(ev["type"], "objective")
        self.assertEqual(ev["mission"], "m1")
        self.assertEqual(ev["objective"], "dropoff_ab-cd_0")
        self.assertEqual(ev["text"], "0/24 Distilled Spirits > <EM4>Stanton System</EM4>")

    def test_title_keeps_colons(self):
        ev = parse_line(
            '<t> [Notice] <SHUDEvent_OnNotification> Added notification "Contract Accepted:  '
            'RSI Disc. Month: Important Supply Haul: " [5] to queue. New queue size: 1, '
            "MissionId: [m1], ObjectiveId: []"
        )
        self.assertEqual(ev["type"], "accepted")
        self.assertEqual(ev["text"], "RSI Disc. Month: Important Supply Haul")
        self.assertIsNone(ev["objective"])

    def test_marker_events(self):
        add = parse_line(
            "<t> [Notice] <CObjectiveMarkerComponent::AddToPlayerDataBank> MissionObjectiveMarker_2809[2809] "
            "- Added to DataBank of Player: P[1] - ZonePos: x: 5296737.8, y: -872575.6, z: 5280417.2, "
            "missionId[m1], objectiveId[dropoff_ab_0]"
        )
        self.assertEqual((add["type"], add["marker"]), ("position", "2809"))
        self.assertAlmostEqual(add["pos"][0], 5296737.8)
        rem = parse_line(
            "<t> [Notice] <CObjectiveMarkerComponent::RemoveFromPlayerDataBank> MissionObjectiveMarker_2809[2809] "
            "- Removed from DataBank of Player: P[1], missionId[m1], objectiveId[dropoff_ab_0]"
        )
        self.assertEqual((rem["type"], rem["marker"]), ("marker_removed", "2809"))

    def test_end_mission(self):
        ev = parse_line("<t> [Notice] <EndMission> Ending mission for player. MissionId[m1] Player[P] "
                        "PlayerId[1] CompletionType[Abandon] Reason[Player left]")
        self.assertEqual((ev["type"], ev["state"]), ("ended", "ABANDON"))

    def test_other_lines(self):
        self.assertIsNone(parse_line("<t> [Notice] <Something> unrelated"))
        self.assertIsNone(parse_line(
            '<t> [Notice] <SHUDEvent_OnNotification> Added notification "Entered Monitored Space: " '
            "[0] to queue. New queue size: 1, MissionId: [00000000-0000-0000-0000-000000000000], ObjectiveId: []"
        ))

    def test_delivery_forms(self):
        cases = {
            "Deliver 0/128 SCU of Ship Decoy Countermeasures to Baijini Point":
                ("Ship Decoy Countermeasures", 128, "Baijini Point"),
            "Deliver 0/3 Souvenirs to Teasa Spaceport": ("Souvenirs", 3, "Teasa Spaceport"),
            "0/24 Distilled Spirits > <EM4>Stanton System</EM4>": ("Distilled Spirits", 24, "Stanton System"),
            "Levski > Stanton System : 0/12 SCU Party Favors": ("Party Favors", 12, "Stanton System"),
        }
        for text, (item, total, dest) in cases.items():
            d = parse_delivery(text)
            self.assertEqual((d["item"], d["total"], d["dest"]), (item, total, dest), text)
        self.assertIsNone(parse_delivery("Need 0/1 of Wikelo Favor"))
        d = parse_delivery("Deliver Constellation Mk V Cntr. Gold Box To RSI Event Platform")
        self.assertEqual((d["item"], d["total"], d["dest"]),
                         ("Constellation Mk V Cntr. Gold Box", 0, "RSI Event Platform"))
        self.assertEqual(parse_pickup("Collect Constellation Mk V Cntr. Gold Box From RSI Event Platform"),
                         {"item": "Constellation Mk V Cntr. Gold Box", "origin": "RSI Event Platform"})

    def test_title_route(self):
        self.assertEqual(
            title_route("Ling Family Rookie Cargo Haul - Medium Scale - Levski > August Dunlow Spaceport "
                        "in Orison <EM4>[BP]</EM4> <EM4>[100 Rep]</EM4>"),
            ("Levski", "August Dunlow Spaceport in Orison"),
        )
        self.assertEqual(title_route("RSI Disc. Month: Important Supply Haul"), (None, None))


class PlacesTest(unittest.TestCase):
    def test_resolve(self):
        self.assertEqual(PLACES.resolve("Teasa Spaceport in Lorville").name, "Lorville")
        self.assertEqual(PLACES.resolve("August Dunlow Spaceport").name, "Orison")
        self.assertEqual(PLACES.resolve("Area18 on ArcCorp").name, "Area18")
        self.assertEqual(PLACES.resolve("Megumi Refueling at the L5 Lagrange of Pyro VI").name, "Megumi Refueling")
        self.assertIsNone(PLACES.resolve("Stanton System"))
        self.assertIsNone(PLACES.resolve(None))
        # Jump points: the gateway on this side.
        self.assertEqual(PLACES.resolve("Stanton - Pyro Jump Point").name, "Pyro Gateway (Stanton)")
        self.assertEqual(PLACES.resolve("Nyx - Pyro Jump Point").name, "Pyro Gateway (Nyx)")

    def test_match_position(self):
        # Orison marker: metres in the Crusader frame, 9 km from SC-map's point.
        self.assertEqual(PLACES.match_position(5296737.8, -872575.6, 5280417.2).name, "Orison")
        # Inside a station: its own frame, which we cannot name.
        self.assertIsNone(PLACES.match_position(1084.6, 1298.2, -1556.1))
        # Far from every known place.
        self.assertIsNone(PLACES.match_position(9e6, 9e6, 9e6))

    def test_showlocation(self):
        pos = parse_coordinates("Coordinates: x:-9641672902.905437 y:-11490734473.539658 z:-93642.5508")
        self.assertEqual(pos, (-9641672902.905437, -11490734473.539658, -93642.5508))
        self.assertIsNone(parse_coordinates("hello"))
        # In quantum, 40 000 km from Delamar: still the nearest start.
        place, km = PLACES.nearest(-9641672902.9 + 4e7, -11490734473.5, -93642.6, START_RADIUS_KM)
        self.assertEqual(place.name, "Levski")
        self.assertAlmostEqual(km, 40000, delta=10)
        # Inside a station: its own frame, nothing to say.
        self.assertEqual(PLACES.nearest(-14.0, -13.0, 0.0, START_RADIUS_KM), (None, None))

    def test_match_system_frame(self):
        # Levski marker written in the Nyx system frame, 5 km from Delamar's centre.
        self.assertEqual(PLACES.match_position(-9641672902.9, -11490734473.5, -93642.6).name, "Levski")
        # A point in space far from any body: no match.
        self.assertIsNone(PLACES.match_position(-18930668553.9, -2609269001.4, -1249452.1))


class TrackerTest(unittest.TestCase):
    def replay(self, lines):
        t = Tracker(PLACES)
        for line in lines:
            t.feed(line)
        return t

    def test_completed_contract_goes_away(self):
        lines = fixture_lines()
        cut = next(i for i, l in enumerate(lines) if "pickup_1f934d6b-bc1b-48ee-b574-4ed09af4aa4c_0 - state "
                   "MISSION_OBJECTIVE_STATE_COMPLETED" in l)
        t = self.replay(l for l in lines[: cut + 1] if "774fcb71" in l)
        [c] = t.active()
        self.assertEqual(c.title, "RSI Disc. Month: Important Supply Haul")
        legs = c.open_legs()
        self.assertEqual([l.item for l in legs],
                         ["Ship Decoy Countermeasures", "Ship Ammunition - Size 4", "Medical Supplies"])
        self.assertEqual([l.picked for l in legs], [True, False, False])
        drop = t.dropoff_of(c, legs[0])
        self.assertEqual((drop.name, drop.confidence), ("Baijini Point", EXACT))

        t = self.replay(l for l in lines if "774fcb71" in l)
        self.assertEqual(t.active(), [])

    def test_untracked_stays_until_hidden(self):
        lines = [l for l in fixture_lines() if "40eaf018" in l]
        # The log ends with every marker gone (untracked, or abandoned): still shown.
        t = self.replay(lines)
        [c] = t.active()
        self.assertFalse(c.tracked)
        # AToB: the one pickup of the title is every cargo's.
        for leg in c.open_legs():
            pickup = t.pickup_of(c, leg)
            self.assertEqual((pickup.name, pickup.confidence), ("Levski", EXACT))
            self.assertEqual(t.dropoff_of(c, leg).name, "Orison")
        # The player hides it once abandoned.
        t.hide(c)
        self.assertEqual(t.active(), [])

    def test_manual_pickup_wins(self):
        t = self.replay([l for l in fixture_lines() if "40eaf018" in l])
        [c] = t.active()
        second = c.open_legs()[1]
        t.set_pickup(c, second, "Seraphim Station")
        self.assertEqual(t.pickup_of(c, second).name, "Seraphim Station")
        actions, unplaced = build_actions(t)
        self.assertIn(("pickup", "Seraphim Station"), [(a.kind, a.place) for a in actions])
        self.assertEqual(unplaced, [])

    def test_multi_pickup_unknown_leg(self):
        # Multi2ToSingle: no title pickup list, no marker on the second leg.
        t = Tracker(PLACES)
        code = "HaulCargo_Multi2ToSingle_Interstellar_Medium_Souv"
        t.feed(notification("m7", "", "Ling Family - Small Scale - Levski > Area18 on ArcCorp", "Contract Accepted"))
        t.feed(marker("m7", "pickup_ab_0", code))
        t.feed(marker("m7", "pickup_cd_0", code))
        [c] = t.active()
        # One phase per pickup: the title's Levski may be either leg's.
        self.assertEqual([t.pickup_of(c, l).name for l in c.open_legs()], ["?", "?"])

    def test_multi2_later_cargos_from_last_pickup(self):
        # One phase: pickup_0 Levski (Nyx frame), pickup_1 Seraphim (Crusader frame), 4 cargos.
        t = Tracker(PLACES)
        code = "HaulCargo_Multi2ToSingle_Interstellar_Medium_Souv"
        t.feed(marker("m8", "pickup_ab_0", code, (-9641672902.9, -11490734473.5, -93642.6)))
        t.feed(marker("m8", "pickup_ab_1", code, (5295517.0, -863194.0, 5282237.0)))
        for n in range(4):
            t.feed(marker("m8", f"dropoff_ab_{n}", code))
        [c] = t.active()
        stops = [t.pickup_of(c, l) for l in c.open_legs()]
        self.assertEqual([(s.name, s.confidence) for s in stops],
                         [("Levski", EXACT), ("Orison", EXACT), ("Orison", GUESS), ("Orison", GUESS)])

    def test_end_mission_abandon(self):
        t = self.replay([l for l in fixture_lines() if "40eaf018" in l] + [
            "<2026-10-28T14:01:11.495Z> [Notice] <EndMission> Ending mission for player. "
            "MissionId[40eaf018-b9fe-4c1a-b45c-5a2ae1252837] Player[P] PlayerId[1] "
            "CompletionType[Abandon] Reason[Player left]"])
        self.assertEqual(t.active(), [])


def notification(mission, objective, text, kind="New Objective"):
    return (f'<2026-10-28T13:00:00.000Z> [Notice] <SHUDEvent_OnNotification> Added notification "{kind}: '
            f'{text}: " [1] to queue. New queue size: 1, MissionId: [{mission}], ObjectiveId: [{objective}]')


def marker(mission, objective, code, pos=(0, 0, 0)):
    return (f"<2026-10-28T13:00:00.000Z> [Notice] <CLocalMissionPhaseMarker::CreateMarker> Creating objective "
            f"marker: missionId [{mission}], generator name [G], contract [{code}], contractDefinitionId[x], "
            f"objectiveId [{objective}], markerEntityId [1], zoneHostId [1], "
            f"position [x: {pos[0]}, y: {pos[1]}, z: {pos[2]}]")


class CargoNamesTest(unittest.TestCase):
    def test_tokens(self):
        self.assertEqual(cargo_tokens("HaulCargo_AToB_Interstellar_Medium_DistSpri_PartyFav_ElecEq_Souv"),
                         ["DistSpri", "PartyFav", "ElecEq", "Souv"])
        self.assertEqual(cargo_tokens("HaulCargo_Multi2ToSingle_Interstellar_Medium_Souv"), ["Souv"])
        self.assertEqual(cargo_tokens("Iasi_HaulCargo_AtoB_Supply_SecuritySupplies"), ["SecuritySupplies"])
        self.assertEqual(cargo_tokens("FTL_Courier_Nyx_MicroSatellite_Rank0"), [])

    def test_courier_hint(self):
        self.assertEqual(courier_hint("FTL_Courier_Nyx_MicroSatellite_Rank0"), ("Nyx", "Micro Satellite"))
        self.assertEqual(courier_hint("Iasi_2H_Box_Courier_2"), (None, None))

    def test_learn_then_guess(self):
        t = Tracker(PLACES, items={})
        # A contract that names its cargo teaches the tokens...
        code = "HaulCargo_AToB_Local_Small_GoldOre_Beryl"
        for n, item in enumerate(["Gold Ore", "Beryl"]):
            t.feed(marker("m1", f"dropoff_ab_{n}", code))
            t.feed(notification("m1", f"dropoff_ab_{n}", f"Deliver 0/8 SCU of {item} to Lorville"))
        t.active()
        self.assertEqual(t.items["GoldOre"], "Gold Ore")
        # ...that an untracked contract (no objective text) then uses.
        t.feed(marker("m2", "pickup_cd_0", "HaulCargo_AToB_Local_Small_Beryl_GoldOre"))
        t.feed(marker("m2", "pickup_cd_1", "HaulCargo_AToB_Local_Small_Beryl_GoldOre"))
        c = t.contracts["m2"]
        t.active()
        self.assertEqual([l.cargo for l in c.open_legs()], ["~Beryl", "~Gold Ore"])
        # One token for several legs says nothing about each leg.
        t.feed(marker("m3", "pickup_ab_0", "HaulCargo_Multi2ToSingle_Local_Small_GoldOre"))
        t.feed(marker("m3", "pickup_ef_0", "HaulCargo_Multi2ToSingle_Local_Small_GoldOre"))
        t.active()
        self.assertEqual([l.cargo for l in t.contracts["m3"].open_legs()], [None, None])

    def test_single_destination_shared(self):
        t = Tracker(PLACES)
        code = "HaulCargo_Multi2ToSingle_Interstellar_Medium_Souv"
        t.feed(marker("m4", "pickup_ab_0", code))
        t.feed(marker("m4", "pickup_cd_0", code))
        # Only the first leg has a delivery marker, in the Hurston frame (Lorville).
        t.feed(marker("m4", "dropoff_ab_0", code, (-328668.0, -756979.7, 566539.6)))
        [c] = t.active()
        stops = [t.dropoff_of(c, l) for l in c.open_legs()]
        self.assertEqual([(s.name, s.confidence) for s in stops], [("Lorville", EXACT)] * 2)

    def test_single_destination_from_title(self):
        t = Tracker(PLACES)
        code = "HaulCargo_AToB_Interstellar_Medium_DistSpri_PartyFav"
        t.feed(notification("m5", "", "Ling Family Cargo Haul - Levski > Area18 on ArcCorp", "Contract Accepted"))
        for n in range(2):
            t.feed(marker("m5", f"dropoff_ab_{n}", code))
            t.feed(notification("m5", f"dropoff_ab_{n}", "0/3 Souvenirs > Stanton System"))
        [c] = t.active()
        self.assertEqual({(t.dropoff_of(c, l).name, t.dropoff_of(c, l).confidence) for l in c.open_legs()},
                         {("Area18", EXACT)})

    def test_title_lists_every_pickup(self):
        t = Tracker(PLACES)
        code = "HaulCargo_MultiToSingle_Interstellar_Small_Souv_DistSpri_ProFoo_Stim"
        t.feed(notification("m6", "", "Ling Family Rookie Cargo Haul - Small Scale - Levski on Delamar, "
                            "Seraphim Station above Crusader, Area18 on ArcCorp > Teasa Spaceport in Lorville "
                            "<EM4>[BP]</EM4>", "Contract Accepted"))
        for phase in ("ab", "cd", "ef"):
            t.feed(marker("m6", f"pickup_{phase}_0", code))
        [c] = t.active()
        legs = c.open_legs()
        # Nothing known per leg: the three title pickups are candidates for each.
        self.assertEqual(t.pickup_of(c, legs[0]).name, "Levski / Seraphim Station / Area18")
        # Two legs placed: the last one follows.
        t.set_pickup(c, legs[0], "Seraphim Station")
        t.set_pickup(c, legs[1], "Levski")
        stop = t.pickup_of(c, legs[2])
        self.assertEqual((stop.name, stop.confidence), ("Area18", EXACT))

    def test_courier_shows_system(self):
        t = Tracker(PLACES)
        code = "FTL_Courier_Nyx_MicroSatellite_Rank0"
        t.feed(marker("m3", "pickup_ab_0", code, (-14, -13, 0)))
        t.feed(marker("m3", "dropoff_ab_0", code))
        [c] = t.active()
        [leg] = c.open_legs()
        self.assertEqual(leg.cargo, "~Micro Satellite")
        self.assertEqual(t.pickup_of(c, leg).name, "? (Nyx)")
        self.assertEqual(t.dropoff_of(c, leg).name, "? (Nyx)")


class SessionTest(unittest.TestCase):
    def test_boarded_flag_only_for_new_lines(self):
        from schauling.session import Session
        data = Path(__file__).resolve().parent.parent.parent / "SC-map" / "data"
        if not (data / "locations.csv").is_file():
            self.skipTest("SC-map data not next to this project")
        board = ('<2026-10-28T14:47:15.533Z> [Notice] <SHUDEvent_OnNotification> Added notification '
                 "\"You have joined channel 'Drake Caterpillar : Player'. \" [51] to queue.\n")
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "Game.log"
            log.write_text(board, encoding="utf-8")
            s = Session(log, data, "Anvil F7A Hornet Mk II")
            # Replaying the log at start is not "boarding now", but sets the ship.
            self.assertFalse(s.boarded)
            self.assertEqual(s.last_ship, "Drake Caterpillar")
            with open(log, "a", encoding="utf-8") as f:
                f.write(board.replace("Drake Caterpillar", "RSI Zeus Mk II ES"))
            s.update()
            self.assertTrue(s.boarded)
            self.assertTrue(s.ship_changed)
            self.assertEqual(s.last_ship, "RSI Zeus Mk II ES")


class DataSyncTest(unittest.TestCase):
    def fake_api(self, version, calls):
        files = {name: f"{name} v{version}".encode() for name in datasync.FILES}

        def get(url, timeout):
            calls.append(url.rsplit("/", 1)[-1])
            if url.endswith("/data/version"):
                return json.dumps({"version": version}).encode()
            return files[url.rsplit("/", 1)[-1]]

        return get

    def test_downloads_only_when_the_version_changes(self):
        with tempfile.TemporaryDirectory() as d:
            calls = []
            folder, status = datasync.sync(d, get=self.fake_api("v1", calls))
            self.assertEqual(status, "updated to v1")
            self.assertEqual((Path(d) / "locations.csv").read_text(), "locations.csv vv1")
            calls.clear()
            _, status = datasync.sync(d, get=self.fake_api("v1", calls))
            self.assertEqual((status, calls), ("up to date", ["version"]))
            calls.clear()
            _, status = datasync.sync(d, get=self.fake_api("v2", calls))
            self.assertEqual(status, "updated to v2")
            self.assertEqual(sorted(calls), sorted(["version"] + datasync.FILES))

    def test_offline_keeps_the_last_copy(self):
        def down(url, timeout):
            raise OSError("no network")

        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(datasync.sync(d, get=down), (None, "offline: no network"))
            datasync.sync(d, get=self.fake_api("v1", []))
            folder, status = datasync.sync(d, get=down)
            self.assertEqual(folder, Path(d))
            self.assertTrue(status.startswith("offline"))

    def test_cut_download_keeps_the_old_version(self):
        with tempfile.TemporaryDirectory() as d:
            datasync.sync(d, get=self.fake_api("v1", []))
            ok = self.fake_api("v2", [])

            def cut(url, timeout):
                if url.endswith("quantum.json"):
                    raise OSError("cut")
                return ok(url, timeout)

            _, status = datasync.sync(d, get=cut)
            self.assertTrue(status.startswith("download failed"))
            self.assertEqual(datasync.local_version(d), "v1")
            self.assertEqual((Path(d) / "locations.csv").read_text(), "locations.csv vv1")


class LocationCodeTest(unittest.TestCase):
    PLACES = Places([
        Place("Levski", "Landing zone", "Delamar", "Nyx", 0, 0, 0),
        Place("New Babbage", "Landing zone", "microTech", "Stanton", 1, 1, 1),
        Place("Baijini Point", "Space station", "ArcCorp", "Stanton", 1, 1, 1),
        Place("HUR-L2 Faithful Dream Station", "Space station", "HUR-L2", "Stanton", 1, 1, 1),
        Place("HUR-L1 Green Glade Station", "Space station", "HUR-L1", "Stanton", 1, 1, 1),
        Place("HDMS-Edmond", "Outpost", "Hurston", "Stanton", 1, 1, 1),
        Place("Covalex Distribution Centre S1DC06", "Distribution center", "Hurston", "Stanton", 1, 1, 1),
        Place("Shubin Mining Facility SAL-2", "Outpost", "Lyria", "Stanton", 1, 1, 1),
        Place("Pyro Gateway (Nyx)", "Space station", "Nyx-Pyro JP", "Nyx", 1, 1, 1),
    ])

    def test_line(self):
        line = ("<2026-10-28T14:45:34.577Z> [Notice] <RequestLocationInventory> Player[P] requested "
                "inventory for Location[Nyx_Levski] [Team_CoreGameplayFeatures][Inventory]")
        self.assertEqual(location_code(line), "Nyx_Levski")

    def test_codes(self):
        cases = {"Nyx_Levski": "Levski", "Stanton4_NewBabbage": "New Babbage", "RR_ARC_LEO": "Baijini Point",
                 "RR_HUR_L2": "HUR-L2 Faithful Dream Station", "Stanton1_HurdynMining_HDMSEdmond": "HDMS-Edmond",
                 "Stanton1_DistributionCentre_Covalex_S1DC06": "Covalex Distribution Centre S1DC06",
                 "Stanton3a_Shubin_SAL2": "Shubin Mining Facility SAL-2", "RR_JP_NyxPyro": "Pyro Gateway (Nyx)",
                 "TheCollectorAsteroid_Stanton4": None}
        for code, name in cases.items():
            p = place_for_code(code, self.PLACES)
            self.assertEqual(p.name if p else None, name, code)


class WaitQuietTest(unittest.TestCase):
    def run_wait(self, busy_until, timeout=15.0):
        now = [0.0]
        ok = keys.wait_quiet(1.0, timeout, pressing=lambda: now[0] < busy_until,
                             clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s))
        return ok, now[0]

    def test_waits_for_keys_released(self):
        # Walking (Z held) for 3 s: typing starts 1 s after the key is let go.
        ok, t = self.run_wait(busy_until=3.0)
        self.assertTrue(ok)
        self.assertAlmostEqual(t, 4.0, delta=0.1)

    def test_gives_up_when_never_quiet(self):
        ok, t = self.run_wait(busy_until=99.0, timeout=15.0)
        self.assertFalse(ok)


class ShipsAndPickerTest(unittest.TestCase):
    NAMES = ["Anvil F7A Hornet Mk I", "Anvil F7A Hornet Mk II", "Drake Caterpillar", "Drake Caterpillar Pirate",
             "RSI Constellation Mk IV Phoenix", "RSI Constellation Mk IV Phoenix Emerald",
             "RSI Constellation Mk V Centaurus"]

    def test_piloted_class(self):
        line = ("<2026-10-28T13:00:00.000Z> [Notice] <Vehicle Control Flow> CVehicleMovementBase::ClearDriver: "
                "Local client node [1] releasing control token for 'RSI_Constellation_Phoenix_GS_9001' [9001]")
        self.assertEqual(piloted_class(line), "RSI_Constellation_Phoenix_GS")
        self.assertIsNone(piloted_class("<t> [Notice] <Something> else"))

    def test_boarded_ship(self):
        line = ('<2026-10-28T14:47:15.533Z> [Notice] <SHUDEvent_OnNotification> Added notification '
                "\"You have joined channel 'RSI Constellation Mk V Centaurus : Player'. \" [51] to queue.")
        self.assertEqual(boarded_ship(line, set(self.NAMES)), "RSI Constellation Mk V Centaurus")
        # Not a ship we know (no quantum drive), or another channel: nothing.
        self.assertIsNone(boarded_ship(line.replace("RSI Constellation Mk V Centaurus", "RSI Skylark"),
                                       set(self.NAMES)))

    def test_ship_for_class(self):
        cases = {"RSI_Constellation_Phoenix_GS": "RSI Constellation Mk IV Phoenix",
                 "RSI_Constellation_Mk5_Centaurus": "RSI Constellation Mk V Centaurus",
                 "ANVL_Hornet_F7A_Mk2": "Anvil F7A Hornet Mk II",
                 "DRAK_Caterpillar": "Drake Caterpillar",
                 "RSI_Skylark": None}
        for cls, name in cases.items():
            self.assertEqual(ship_for_class(cls, self.NAMES), name, cls)

    def test_picker_matches(self):
        items = [(n, n) for n in ["Levski", "NB Int. Spaceport", "HUR-L1 Green Glade Station", "Lorville",
                                  "Area18", "Everus Harbor"]]
        self.assertEqual([v for _, v in matches("lev", items)], ["Levski"])
        self.assertEqual([v for _, v in matches("hur l1", items)], ["HUR-L1 Green Glade Station"])
        self.assertEqual([v for _, v in matches("glade", items)], ["HUR-L1 Green Glade Station"])
        self.assertEqual([v for _, v in matches("ARÉA", items)], ["Area18"])
        self.assertEqual(len(matches("", items)), len(items))


class RoutingTest(unittest.TestCase):
    def test_pickup_before_delivery_and_shortest(self):
        # A-B-C on a line; cargo 1 goes C→A, cargo 2 goes A→B.
        pos = {"A": 0, "B": 1, "C": 2}
        travel = lambda a, b: abs(pos[a] - pos[b]) * 100
        acts = [Action("pickup", "C", "l1", "c"), Action("dropoff", "A", "l1", "c", after=0),
                Action("pickup", "A", "l2", "c"), Action("dropoff", "B", "l2", "c", after=2)]
        stops, total = plan(acts, travel, start="B")
        order = [s.place for s in stops]
        self.assertEqual(order, ["C", "A", "B"])
        self.assertEqual(total, 100 + 200 + 100)

    def test_many_actions_few_places_stays_exact(self):
        # Greedy would go to the nearest first (B) and pay for it later.
        cost = {frozenset("AB"): 1000, frozenset("AR"): 1050, frozenset("BR"): 1000,
                frozenset("BG"): 200, frozenset("GR"): 1050, frozenset("GL"): 180,
                frozenset("RL"): 1050, frozenset("BL"): 300, frozenset("AG"): 1100, frozenset("AL"): 1100}
        travel = lambda a, b: 0 if a == b else cost[frozenset(a + b)]
        acts = []
        for n in range(8):  # eight cargos A→G and B→G
            acts += [Action("pickup", "A" if n < 5 else "B", n, "c"), Action("dropoff", "G", n, "c", after=len(acts))]
        for n in range(5):  # five cargos R→L
            acts += [Action("pickup", "R", n, "d"), Action("dropoff", "L", n, "d", after=len(acts))]
        self.assertGreater(len(acts), 12)
        stops, total = plan(acts, travel, start="A")
        self.assertEqual([s.place for s in stops], ["A", "R", "B", "G", "L"])
        self.assertEqual(total, 1050 + 1000 + 200 + 180)

    def test_delivery_at_start_waits_for_pickup(self):
        travel = lambda a, b: 0 if a == b else 60
        acts = [Action("pickup", "X", "l", "c"), Action("dropoff", "S", "l", "c", after=0)]
        stops, _ = plan(acts, travel, start="S")
        self.assertEqual([s.place for s in stops], ["X", "S"])

    def test_pickup_and_delivery_at_same_stop(self):
        travel = lambda a, b: 0 if a == b else 60
        acts = [Action("pickup", "X", "l1", "c"), Action("dropoff", "Y", "l1", "c", after=0),
                Action("pickup", "Y", "l2", "c"), Action("dropoff", "X", "l2", "c", after=2)]
        stops, total = plan(acts, travel)
        self.assertEqual([s.place for s in stops], ["X", "Y", "X"])
        self.assertEqual(total, 120)


class TravelTimesTest(unittest.TestCase):
    def test_cross_system_uses_gateways(self):
        calls = []

        def fetch(a, b):
            calls.append((a, b))
            return 100, None

        tt = TravelTimes(PLACES, fetch=fetch)
        # Nyx to Stanton: direct jump (800) beats going through Pyro (1500).
        self.assertEqual(tt.seconds("Levski", "Lorville"), 100 + JUMP_SECONDS + 100)
        self.assertIn(("Levski", "Stanton Gateway (Nyx)"), calls)
        self.assertIn(("Nyx Gateway (Stanton)", "Lorville"), calls)

    def test_two_jumps_when_no_direct_link(self):
        no_link = Places([p for p in PLACES.places
                          if p.name not in ("Stanton Gateway (Nyx)", "Nyx Gateway (Stanton)")], BODIES)
        tt = TravelTimes(no_link, fetch=lambda a, b: (100, None))
        self.assertEqual(tt.seconds("Levski", "Lorville"), 100 + JUMP_SECONDS + 100 + JUMP_SECONDS + 100)

    def test_failures_not_cached_on_disk(self):
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / "cache.json"
            tt = TravelTimes(PLACES, fetch=lambda a, b: (None, "down") if b == "Area18" else (50, None),
                             cache_file=cache)
            tt.prefetch(["Lorville", "Area18"])
            saved = json.loads(cache.read_text(encoding="utf-8"))
            self.assertEqual(list(saved.values()), [50])


if __name__ == "__main__":
    unittest.main()
