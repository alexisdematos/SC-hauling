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
from schauling.routing import Action, build_actions, peak_load, plan
from schauling.titles import TitleTemplates
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
        # Other delivery missions (global.ini): packages, boxes, "go to".
        self.assertEqual(parse_pickup("Pick up Package #4821 from Area18."),
                         {"item": "Package #4821", "origin": "Area18"})
        self.assertEqual(parse_pickup("Pick Up Medical Supplies From Seraphim Station"),
                         {"item": "Medical Supplies", "origin": "Seraphim Station"})
        self.assertEqual(parse_pickup("Pick up the Medical Supplies from Lorville."),
                         {"item": "Medical Supplies", "origin": "Lorville"})
        self.assertEqual(parse_pickup("Pick Up from Bountiful Harvest"), {"item": None, "origin": "Bountiful Harvest"})
        self.assertEqual(parse_pickup("Go to Shubin SAL-5 to pick up cargo."), {"item": None, "origin": "Shubin SAL-5"})
        self.assertIsNone(parse_pickup("Pick up the shipment."))
        self.assertEqual(parse_delivery("Deliver Package #4821 to Area18.")["dest"], "Area18")

    def test_title_route(self):
        self.assertEqual(
            title_route("Ling Family Rookie Cargo Haul - Medium Scale - Levski > August Dunlow Spaceport "
                        "in Orison <EM4>[BP]</EM4> <EM4>[100 Rep]</EM4>"),
            ("Levski", "August Dunlow Spaceport in Orison"),
        )
        self.assertEqual(title_route("RSI Disc. Month: Important Supply Haul"), (None, None))


class TitleTemplatesTest(unittest.TestCase):
    TEMPLATES = TitleTemplates([
        "Covalex_HaulCargo_SingleToMulti_title=~mission(ReputationRank) Rank - ~mission(CargoGradeToken) Cargo Haul"
        " - ~mission(Location|Address) > ~mission(Destination|Address), ~mission(Destination1|Address),"
        " ~mission(Destination2|Address) <EM4>[BP?]</EM4>",
        "Foxwell_HaulCargo_MultiToSingle_title_01=Red Level Contract: Clear PAFs - ~mission(Location1|Address),"
        " ~mission(Location2|Address), ~mission(Location|Address) > ~mission(Destination|Address) <EM4>[BP]</EM4>",
        "Covalex_HaulCargo_LinearChain_title=~mission(ReputationRank) Rank  - ~mission(CargoGradeToken) Cargo Haul",
        "HaulCargo_obj_short_01=Collect Shipment",
    ])

    def test_route(self):
        self.assertEqual(
            self.TEMPLATES.route("Senior Rank - Medium Cargo Haul - Levski > Area18, Orison, Lorville "
                                 "<EM4>[BP?]</EM4>: "),
            (["Levski"], ["Area18", "Orison", "Lorville"]))
        # Fewer drop-offs than the template has: the empty ones go.
        self.assertEqual(self.TEMPLATES.route("Senior Rank - Medium Cargo Haul - Levski > Area18, ,  [BP?]"),
                         (["Levski"], ["Area18"]))
        # Places in Location, Location1… order, whatever the title order.
        self.assertEqual(self.TEMPLATES.route("Red Level Contract: Clear PAFs - Orbituary, Checkmate, Ruin Station"
                                              " > Megumi Refueling [BP]"),
                         (["Ruin Station", "Orbituary", "Checkmate"], ["Megumi Refueling"]))
        self.assertIsNone(self.TEMPLATES.route("Senior Rank  - Medium Cargo Haul"))
        self.assertIsNone(self.TEMPLATES.route("RSI Disc. Month: Important Supply Haul"))

    def test_tracker_uses_templates(self):
        t = Tracker(PLACES, titles=self.TEMPLATES)
        code = "HaulCargo_SingleToMulti3_Planetary_Small_Souv"
        t.feed(notification("m11", "", "Senior Rank - Medium Cargo Haul - Levski > Area18, Orison, Lorville [BP?]",
                            "Contract Accepted"))
        for phase in ("ab", "cd", "ef"):
            t.feed(marker("m11", f"dropoff_{phase}_0", code))
        [c] = t.active()
        self.assertEqual({t.pickup_of(c, l).name for l in c.open_legs()}, {"Levski"})
        self.assertEqual(t.dropoff_of(c, c.open_legs()[0]).name, "Area18 / Orison / Lorville")


class ContractPageTest(unittest.TestCase):
    # Windows OCR of a real contract page (overlay text below it included).
    LINES = ['PRIMARY OBJECTIVES', 'O Deliver 0/24 SCU of Distilled Spirits to Grim HEX within', "Yela's asteroid belt.",
             'O Pick up at Levski', 'O Deliver 0/12 SCU of Party Favors to Grim HEX within', "Yela's asteroid belt.",
             'o Pick up at Seraphim Statlon', 'O Deliver 0/3 SCU of Audio-Visual Equipment to', 'Grim HEX',
             'within Yela•s asteroid belt.', 'O Pick up at Levski', 'O Deliver 0/3 SCIJ of Souvenirs to ArealB on',
             'ArcCorp.', 'O Pick up at Levski', 'Hauling • I contract(s) • 42 SCU • cargo',
             'Levski', "—Grim HEX within Yela's asteroid belt : 24 SCU Distilled Spirits"]

    def test_parse(self):
        from schauling.ocr import parse_objectives
        self.assertEqual(parse_objectives(self.LINES), [
            {"item": "Distilled Spirits", "total": 24, "dest": "Grim HEX within Yela's asteroid belt", "origin": "Levski"},
            {"item": "Party Favors", "total": 12, "dest": "Grim HEX within Yela's asteroid belt",
             "origin": "Seraphim Statlon"},
            {"item": "Audio-Visual Equipment", "total": 3, "dest": "Grim HEX within Yela's asteroid belt",
             "origin": "Levski"},
            {"item": "Souvenirs", "total": 3, "dest": "ArealB on ArcCorp", "origin": "Levski"},
        ])
        self.assertEqual(parse_objectives(["Collect 2 Medical Supplies from Port Tressler"]), [])
        # In game the blue destination may come as a line of its own, elsewhere.
        self.assertEqual(parse_objectives(["O Deliver 0/8 SCU of Gold to", "o Pick up at Orbituary"]),
                         [{"item": "Gold", "total": 8, "dest": None, "origin": "Orbituary"}])
        # Full stop missed by the OCR on a one-line delivery: its pickup line says it is whole.
        self.assertEqual(parse_objectives(["O Deliver 0/12 SCU of Party Favors to Area18 on ArcCorp",
                                           "O Pick up at Levski"]),
                         [{"item": "Party Favors", "total": 12, "dest": "Area18 on ArcCorp", "origin": "Levski"}])

    def test_apply_to_contract(self):
        from schauling.ocr import parse_objectives
        t = Tracker(PLACES)
        code = "HaulCargo_Multi2ToSingle_Interstellar_Medium_Souv"
        # Cargo names from the log for the first two legs only (by name), none for the third.
        t.feed(notification("m12", "dropoff_ab_1", "0/12 Party Favors > Area18"))
        t.feed(notification("m12", "dropoff_ab_0", "0/24 Distilled Spirits > Area18"))
        t.feed(marker("m12", "dropoff_ab_2", code))
        n = t.apply_page("m12", parse_objectives(self.LINES))
        self.assertEqual(n, 4)  # the fourth cargo, unknown to the log, comes from the page
        [c] = t.active()
        got = {l.item: t.pickup_of(c, l) for l in c.open_legs()}
        self.assertEqual({k: (s.name, s.confidence) for k, s in got.items()}, {
            "Distilled Spirits": ("Levski", EXACT), "Party Favors": ("Seraphim Station", EXACT),
            "Audio-Visual Equipment": ("Levski", EXACT), "Souvenirs": ("Levski", EXACT)})
        self.assertEqual(t.apply_page("nope", []), 0)

    def test_untracked_contract_gets_every_cargo(self):
        # Accepted untracked: the log has the _0 markers only; code and page list 5 cargos.
        t = Tracker(PLACES, items={})
        code = "HaulCargo_AToB_Interstellar_Bulk_DistSp_Dia_FresFoo_Gol_Aphor"
        t.feed(marker("m13", "pickup_ba_0", code))
        t.feed(marker("m13", "dropoff_ba_0", code))
        [c] = t.active()
        self.assertEqual(len(c.open_legs()), 5)  # from the code, names guessed
        page = [{"item": i, "total": n, "dest": "Teasa Spaceport in Lorville", "origin": "Megumi Refueling"}
                for i, n in [("Distilled Spirits", 32), ("Diamond", 8), ("Fresh Food", 16), ("Gold", 8), ("Aphorite", 2)]]
        self.assertEqual(t.apply_page("m13", page), 5)
        legs = c.open_legs()
        self.assertEqual([(l.item, l.total) for l in legs],
                         [("Distilled Spirits", 32), ("Diamond", 8), ("Fresh Food", 16), ("Gold", 8), ("Aphorite", 2)])
        self.assertEqual({t.pickup_of(c, l).name for l in legs}, {"Megumi Refueling"})
        self.assertEqual({t.dropoff_of(c, l).name for l in legs}, {"Lorville"})
        # A Multi2ToSingle code names one cargo of four: the page adds the legs the log missed.
        code = "HaulCargo_Multi2ToSingle_Interstellar_Medium_Souv"
        for oid in ("pickup_cd_0", "pickup_cd_1", "dropoff_cd_0"):
            t.feed(marker("m14", oid, code))
        t.apply_page("m14", page[:3])
        self.assertEqual(sorted(l.item for l in t.contracts["m14"].legs.values()),
                         ["Diamond", "Distilled Spirits", "Fresh Food"])

    def test_match_page(self):
        t = Tracker(PLACES, items={})
        t.feed(marker("m15", "dropoff_ba_0", "HaulCargo_AToB_Interstellar_Bulk_DistSp_Dia_FresFoo_Gol_Aphor"))
        t.feed(notification("m16", "dropoff_cd_0", "0/12 Party Favors > Area18"))
        t.items.update({"Dia": "Diamond"})
        page = [{"item": "Diamond", "total": 8, "dest": "Teasa Spaceport in Lorville", "origin": "Orbituary"}]
        self.assertEqual(t.match_page(page), "m15")
        page = [{"item": "Party Favors", "total": 12, "dest": "Area18", "origin": "Levski"}]
        self.assertEqual(t.match_page(page), "m16")
        self.assertIsNone(t.match_page([{"item": "Iron", "total": 1, "dest": "Lorville", "origin": None}]))
        self.assertIsNone(t.match_page([]))

    def test_match_page_by_destination(self):
        # Two Ling contracts, same four cargos: tracked to Area18 (names in the log),
        # untracked Multi2ToSingle to Orison (no names). The Orison page is the second's.
        t = Tracker(PLACES, items={})
        for n, item in enumerate(["Distilled Spirits", "Party Favors", "Audio-Visual Equipment", "Souvenirs"]):
            t.feed(notification("m20", f"dropoff_ab_{n}", f"0/3 {item} > Area18"))
        t.feed(notification("m21", "", "Ling Family - Small Scale - Levski > Orison", "Contract Accepted"))
        for oid in ("pickup_cd_0", "pickup_cd_1", "dropoff_cd_0"):
            t.feed(marker("m21", oid, "HaulCargo_Multi2ToSingle_Interstellar_Medium_Souv"))
        page = [{"item": i, "total": 3, "dest": "August Dunlow Spaceport in Orison", "origin": o}
                for i, o in [("Distilled Spirits", "Levski"), ("Party Favors", "Port Tressler"),
                             ("Audio-Visual Equipment", None), ("Souvenirs", "Port Tressler")]]
        self.assertEqual(t.match_page(page), "m21")
        self.assertLess(t.page_fit("m20", page), 0.6)
        self.assertEqual(t.pages_for("m21", [page]), [page])

    def test_pages_of_another_contract_are_dropped(self):
        # Gaslight accepted untracked, then the player clicked on to a Ling contract.
        t = Tracker(PLACES, items={})
        t.feed(marker("m17", "dropoff_ba_0", "HaulCargo_AToB_Interstellar_Bulk_DistSp_Dia_FresFoo_Gol_Aphor"))
        gaslight = [{"item": i, "total": n, "dest": "Teasa Spaceport in Lorville", "origin": "Gaslight"}
                    for i, n in [("Distilled Spirits", 32), ("Diamond", 8), ("Fresh Food", 16), ("Gold", 8),
                                 ("Aphorite", 2)]]
        ling = [{"item": i, "total": n, "dest": "Teasa Spaceport in Lorville", "origin": o}
                for i, n, o in [("Distilled Spirits", 24, "Levski"), ("Party Favors", 12, "Seraphim Station"),
                                ("Audio-Visual Equipment", 3, "Seraphim Station"), ("Souvenirs", 3, "Seraphim Station")]]
        self.assertEqual(t.pages_for("m17", [gaslight[:4], ling, []]), [gaslight[:4]])
        # Untracked Multi2ToSingle: the code names one cargo of four, keep the first page only.
        code = "HaulCargo_Multi2ToSingle_Interstellar_Medium_Souv"
        for n in range(4):
            t.feed(marker("m18", f"dropoff_cd_{n}", code))
        self.assertEqual(t.pages_for("m18", [[], ling, gaslight]), [ling])

    def test_page_still_drawing(self):
        from schauling.ocr import merge_pages, parse_objectives
        # First screenshot while the page draws: a cut line, the next text glued to it.
        drawing = ["PRIMARY OBJECTIVES", "O Deliver 0/24 SCU of Distilled Spirits to Area18 on", "ArcCorp.",
                   "O Pick up at Levski", "O Deliver 0/3 SCU of Audio-Visual Equipm",
                   "x Ä 194,500 N/A Ling Family Hauling Guess the folks at Area18 on ArcCorp are planning",
                   "a party and need help acquiring everything in a timely manner. You'll need to make",
                   "multiple pick-ups for this one."]
        first = parse_objectives(drawing)
        self.assertEqual([e["item"] for e in first], ["Distilled Spirits"])
        done = [{"item": "Audio-Visual Equipment", "total": 3, "dest": "Area18 on ArcCorp", "origin": "Levski"}]
        cut = [{"item": "Audio-Visual Equipm", "total": 3, "dest": "Area18", "origin": None}]
        self.assertEqual([e["item"] for e in merge_pages([cut + first, done])],
                         ["Distilled Spirits", "Audio-Visual Equipment"])
        # The code lists one cargo per leg: no leg for a line it does not know.
        t = Tracker(PLACES, items={})
        code = "HaulCargo_AToB_Interstellar_Medium_DistSpri_PartyFav_ElecEq_Souv"
        t.feed(marker("m19", "dropoff_ab_0", code))
        for n, (item, scu) in enumerate([("Distilled Spirits", 24), ("Party Favors", 12),
                                         ("Audio-Visual Equipment", 3), ("Souvenirs", 3)]):
            t.feed(notification("m19", f"dropoff_ab_{n}", f"0/{scu} {item} > Area18"))
        t.apply_page("m19", done + [{"item": "Junk x 194,500", "total": 3, "dest": "Area18", "origin": "Levski"}])
        self.assertEqual(len(t.contracts["m19"].legs), 4)

    def test_page_fits_code_abbreviations(self):
        from schauling.tracker import _abbreviates
        # The game drops letters: "Spri" for Spirits.
        self.assertTrue(_abbreviates("DistSpri", "Distilled Spirits"))
        self.assertTrue(_abbreviates("FresFoo", "Fresh Food"))
        self.assertFalse(_abbreviates("Gol", "Diamond"))
        # Untracked AToB: no names in the log, only the code; ElecEq is learnt (Audio-Visual Equipment).
        t = Tracker(PLACES)
        t.feed(marker("m23", "dropoff_ab_0", "HaulCargo_AToB_Interstellar_Medium_DistSpri_PartyFav_ElecEq_Souv"))
        page = [{"item": i, "total": n, "dest": None, "origin": "Levski"}
                for i, n in [("Distilled Spirits", 24), ("Party Favors", 12), ("Audio-Visual Equipment", 3),
                             ("Souvenirs", 3)]]
        self.assertEqual(t.page_fit("m23", page), 1.0)
        self.assertEqual(t.match_page(page), "m23")

    def test_merge_pages(self):
        from schauling.ocr import merge_pages
        a = {"item": "Gold", "total": 8, "dest": "X", "origin": None}
        b = {"item": "Gold", "total": 8, "dest": "X", "origin": "Orbituary"}
        c = {"item": "Diamond", "total": 8, "dest": "X", "origin": "Orbituary"}
        d = {"item": "Fresh Food", "total": 16, "dest": "X", "origin": "Orbituary"}
        merged = merge_pages([[b], [a, c], []])
        self.assertEqual([(e["item"], e["origin"]) for e in merged], [("Gold", "Orbituary"), ("Diamond", "Orbituary")])
        self.assertEqual(len(merge_pages([[a, c], [d]])), 3)
        self.assertEqual(merge_pages([]), [])


class PatchCheckTest(unittest.TestCase):
    def test_log_lines_still_read(self):
        from schauling import patchcheck
        lines = fixture_lines()
        counts, _ = patchcheck.scan(lines)
        self.assertGreater(counts["objective marker"][0], 0)
        self.assertEqual(patchcheck.log_warnings(counts), [])
        # A patch renames a field of the marker lines: they show, but we read none.
        changed = [l.replace("objectiveId [", "objectiveGuid [") for l in lines]
        self.assertTrue(any("objective marker lines no longer read" in w
                            for w in patchcheck.log_warnings(patchcheck.scan(changed)[0])))
        # Or the markers go away while contracts are accepted.
        gone = [l for l in lines if "CreateMarker" not in l]
        self.assertTrue(any("no objective marker" in w for w in patchcheck.log_warnings(patchcheck.scan(gone)[0])))

    def test_global_ini_and_once_per_version(self):
        from schauling import patchcheck
        with tempfile.TemporaryDirectory() as d:
            ini = Path(d) / "global.ini"
            ini.write_text("Covalex_HaulCargo_AToB_title=Haul - ~mission(Location|Address) > ~mission(Destination|Address)\n"
                           "HaulCargo_obj_itemspecifics_01=~mission(item)\n", encoding="utf-8")
            self.assertEqual(patchcheck.ini_warnings(ini), [])
            ini.write_text("Covalex_HaulCargo_AToB_title=Haul - ~mission(CargoGradeToken)\n", encoding="utf-8")
            self.assertEqual(len(patchcheck.ini_warnings(ini)), 2)
        cfg = {}
        self.assertEqual(patchcheck.new_warnings(cfg, "4.10", ["a"]), ["a"])
        self.assertEqual(patchcheck.new_warnings(cfg, "4.10", ["a"]), [])  # shown once
        self.assertEqual(patchcheck.new_warnings(cfg, "4.11", ["a"]), ["a"])  # again after the next patch


class LiveServerTest(unittest.TestCase):
    def test_state_and_cors(self):
        import socket
        import urllib.request
        from schauling import liveserver
        from schauling.routing import Plan, PlanStop
        from types import SimpleNamespace as Leg
        leg = Leg(total=12, amount=0, cargo="~Party Favors")
        stop = PlanStop("Port Tressler", [Action("pickup", "Port Tressler", leg, "c")], 120, ["Nyx", "Pyro"],
                        1793270400.0)
        state = liveserver.build_state("Drake Caterpillar", "Lorville", "log", Plan([stop], [], 120), 1793270000.0)
        self.assertEqual(state["route"], [{"place": "Port Tressler", "arrival": "2026-10-29T10:40:00Z", "jump": "Nyx → Pyro",
                                           "actions": [{"type": "load", "scu": 12, "cargo": "Party Favors"}]}])
        with socket.socket() as s:  # a free port
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        server = liveserver.LiveServer(lambda: state)
        server.start(port)
        try:
            def get(origin, method="GET"):
                req = urllib.request.Request(f"http://127.0.0.1:{port}/state", method=method,
                                             headers={"Origin": origin} if origin else {})
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.headers, r.read()
            headers, body = get(liveserver.SITE)
            self.assertEqual(json.loads(body)["location"], "Lorville")
            self.assertEqual(headers["Access-Control-Allow-Origin"], liveserver.SITE)
            self.assertEqual(headers["Access-Control-Allow-Private-Network"], "true")
            self.assertEqual(get("http://localhost:8080")[0]["Access-Control-Allow-Origin"], "http://localhost:8080")
            self.assertIsNone(get("https://evil.example")[0]["Access-Control-Allow-Origin"])
            self.assertIn("GET", get(liveserver.SITE, "OPTIONS")[0]["Access-Control-Allow-Methods"])
            # DNS rebinding: a request naming another host gets nothing.
            import http.client
            for host, status in ((f"evil.example:{port}", 403), (f"127.0.0.1:{port}", 200),
                                 (f"localhost:{port}", 200), (None, 403)):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.putrequest("GET", "/state", skip_host=True)
                if host:
                    conn.putheader("Host", host)
                conn.putheader("Origin", liveserver.SITE)
                conn.endheaders()
                r = conn.getresponse()
                self.assertEqual(r.status, status, host)
                if status == 403:
                    self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))
                    self.assertEqual(r.read(), b"")
                conn.close()
            # A second server on the same port: an error to show, not a crash.
            with self.assertRaises(OSError):
                liveserver.LiveServer(lambda: state).start(port)
        finally:
            server.stop()
        self.assertEqual(liveserver.map_url("Port Tressler", 47823),
                         "https://ofs-versetime.netlify.app/?live=47823#Port_Tressler")
        self.assertEqual(liveserver.map_url("Lorville"), "https://ofs-versetime.netlify.app/#Lorville")
        self.assertEqual(liveserver.map_url("Lorville", 47823, "abcdefgh23"),
                         "https://ofs-versetime.netlify.app/?live=47823#team=abcdefgh23")
        self.assertIsNone(state["team"])
        self.assertEqual(liveserver.build_state("C2", None, "log", None, 0, "abcdefgh23")["team"], "abcdefgh23")
        self.assertIsNone(state["teamMember"])  # not in a team
        inside = liveserver.build_state("C2", None, "log", None, 0, "abcdefgh23", [], "m" * 16)
        self.assertEqual(inside["teamMember"], "m" * 16)
        self.assertIsNone(liveserver.build_state("C2", None, "log", None, 0, None, [], "m" * 16)["teamMember"])


class TeamTest(unittest.TestCase):
    def test_codes(self):
        from schauling import team
        self.assertEqual(team.parse_code(" ABCDEFGH23 "), "abcdefgh23")
        self.assertEqual(team.parse_code("https://ofs-versetime.netlify.app/#team=abcdefgh23"), "abcdefgh23")
        self.assertIsNone(team.parse_code("abcdefghi2"))  # no i, l, o in codes
        self.assertIsNone(team.parse_code("abc"))
        self.assertEqual(team.invite_link("abcdefgh23"), "https://ofs-versetime.netlify.app/#team=abcdefgh23")
        member, secret = team.new_member()
        self.assertRegex(member, r"^[a-z0-9]{16}$")
        self.assertGreaterEqual(len(secret), 32)
        self.assertEqual(team.clean_name("  Space   Trucker " + "x" * 30), ("Space Trucker " + "x" * 30)[:24])

    def test_sharer_pace(self):
        from schauling import team
        now = [100.0]
        sent = []

        class Api:
            fail = None

            def put(self, *args):
                if self.fail:
                    raise team.TeamError(self.fail, "nope")
                sent.append(args)
        api = Api()
        sharer = team.TeamSharer(api, clock=lambda: now[0])
        sharer._thread = True  # no thread: send_once by hand
        member = ("abcdefgh23", "m" * 16, "s" * 64, "Me")
        self.assertIsNone(sharer.due(now[0]))  # not in a team: nothing to send
        sharer.set(member, {"location": "Lorville", "updatedAt": "1"})
        self.assertEqual(sharer.due(now[0]), 0)
        self.assertTrue(sharer.send_once())
        self.assertEqual(sent[-1][3:], ("Me", {"location": "Lorville", "updatedAt": "1"}))
        sharer.set(member, {"location": "Lorville", "updatedAt": "1", "teamMember": "m" * 16})
        sharer.send_once()
        self.assertNotIn("teamMember", sent[-1][4])  # local only: not in the PUT
        sharer.set(member, {"location": "Lorville", "updatedAt": "2"})  # same place: heartbeat only
        self.assertEqual(sharer.due(now[0]), team.HEARTBEAT_S)
        sharer.set(member, {"location": "Area18", "updatedAt": "3"})  # changed: 5 s after the last send
        self.assertEqual(sharer.due(now[0] + 2), team.MIN_GAP_S - 2)
        now[0] += 5
        api.fail = 429
        self.assertFalse(sharer.send_once())
        self.assertEqual(sharer.error, "nope")
        self.assertEqual(sharer.due(now[0]), team.RETRY_S)  # then the last state again
        api.fail = None
        now[0] += team.RETRY_S
        self.assertTrue(sharer.send_once())
        self.assertIsNone(sharer.error)
        self.assertEqual(sent[-1][4]["location"], "Area18")
        api.fail = 404  # team gone: stop trying
        sharer.set(member, {"location": "Orison"})
        now[0] += 60
        sharer.send_once()
        self.assertEqual(sharer.due(now[0] + 3600), float("inf"))
        sharer.set(None, None)  # left the team
        self.assertIsNone(sharer.due(now[0]))

    def test_contracts_in_state(self):
        from schauling import team
        lines = [l for l in fixture_lines() if "774fcb71" in l]
        cut = next(i for i, l in enumerate(lines) if "MISSION_OBJECTIVE_STATE_COMPLETED" in l)
        t = Tracker(PLACES)
        for line in lines[: cut + 1]:
            t.feed(line)
        [c] = team.contracts_state(t)
        self.assertEqual(c["title"], "RSI Disc. Month: Important Supply Haul")
        self.assertEqual(c["cargos"][0]["cargo"], "Ship Decoy Countermeasures")
        self.assertEqual(c["cargos"][0]["to"], "Baijini Point")
        self.assertIn(c["cargos"][0]["sure"], ("", "~", "?"))
        self.assertEqual(set(c["cargos"][0]), {"cargo", "scu", "from", "to", "sure"})

    def test_sure(self):
        from schauling import team
        from schauling.tracker import UNKNOWN, Stop
        a, b = Stop("Levski", None, EXACT), Stop("Lorville", None, EXACT)
        self.assertEqual(team._sure(a, b), "")
        self.assertEqual(team._sure(a, Stop("Lorville", None, GUESS)), "~")
        self.assertEqual(team._sure(a, Stop("Lorville", None, UNKNOWN)), "?")
        self.assertEqual(team._sure(Stop("? (not in SC-map)", None, EXACT), b), "?")

    def test_state_size(self):
        from schauling import team
        cargo = {"cargo": "Distilled Spirits", "scu": 24, "from": "Levski", "to": "Area18", "sure": ""}

        def contract(i):
            return {"id": f"id{i}", "title": "A long title " * 10, "tracked": False, "cargos": [cargo] * 4}
        small = {"route": [], "contracts": [contract(i) for i in range(5)]}
        self.assertIs(team.fit_state(small), small)  # under 16 KB: as it is
        self.assertLess(len(json.dumps(small)), team.MAX_STATE)
        big = {"route": [], "contracts": [contract(i) for i in range(60)]}
        cut = team.fit_state(big)
        size = len(json.dumps(cut, ensure_ascii=False).encode("utf-8"))
        self.assertLessEqual(size, team.MAX_STATE)
        self.assertTrue(all(c["title"] is None for c in cut["contracts"]))  # titles go first
        self.assertEqual(cut["contracts"][-1]["id"], "id59")  # then the oldest contracts
        self.assertLessEqual(len(cut["contracts"]), team.MAX_CONTRACTS)
        many = {"contracts": [{"id": f"id{i}", "cargos": []} for i in range(40)]}
        self.assertEqual([c["id"] for c in team.fit_state(many)["contracts"]][:1], ["id10"])  # the 30 newest
        self.assertEqual(len(big["contracts"]), 60)  # the original stays whole
        titles_only = {"contracts": [contract(i) for i in range(30)]}
        self.assertEqual(len(team.fit_state(titles_only)["contracts"]), 30)  # dropping titles was enough

    def test_crossings(self):
        from schauling import team
        mine = {"route": [{"place": "Area18", "arrival": "2026-10-29T20:00:00Z", "actions": [{"type": "unload"}]},
                          {"place": "Levski", "arrival": "2026-10-29T20:30:00Z", "actions": [{"type": "load"}]}],
                "contracts": [{"id": "m1"}, {"id": "m2"}]}
        bob = {"id": "b", "name": "Bob", "state": {
            "ship": "C2", "route": [{"place": "Area18", "arrival": "2026-10-29T20:12:00Z",
                                     "actions": [{"type": "unload", "scu": 8}]},
                                    {"place": "Orison", "arrival": None, "actions": []}],
            "contracts": [{"id": "m2", "title": "Shared haul", "cargos": [{"cargo": "Gold"}]},
                          {"id": "b1", "title": "Bob's haul", "tracked": True,
                           "cargos": [{"cargo": "Gold", "scu": 8, "from": "Lorville", "to": None, "sure": "~"}]}]}}
        [line] = team.crossings(mine, bob)
        self.assertTrue(line.startswith("Bob unloads at Area18 too (+12m) at "), line)
        self.assertEqual(team.shared_ids(mine, bob), {"m2"})
        lines = team.contract_lines(bob, team.shared_ids(mine, bob))
        self.assertEqual(lines[0], "Shared haul   (shared with you)")  # not repeated
        self.assertEqual(lines[1:], ["▶ Bob's haul", "  ~Lorville → ~? : 8 SCU Gold"])
        self.assertTrue(team.member_line(bob).startswith("Bob · C2 · next Area18 "))
        self.assertTrue(team.member_line(bob).endswith("· 2 contract(s)"))
        self.assertEqual(team.crossings(mine, {"name": "Al", "state": None}), [])

    def test_api_requests(self):
        from schauling import team
        calls = []

        def request(method, url, body=None, secret=None):
            calls.append((method, url, body, secret))
            return {"team": "abcdefgh23"}
        api = team.TeamApi("http://localhost:8787/", request)
        self.assertEqual(api.create(), "abcdefgh23")
        api.put("abcdefgh23", "m1", "s1", "Me", {"route": []})
        api.leave("abcdefgh23", "m1", "s1")
        self.assertEqual(calls, [
            ("POST", "http://localhost:8787/teams", None, None),
            ("PUT", "http://localhost:8787/teams/abcdefgh23/members/m1", {"name": "Me", "state": {"route": []}}, "s1"),
            ("DELETE", "http://localhost:8787/teams/abcdefgh23/members/m1", None, "s1")])


class PlacesTest(unittest.TestCase):
    def test_resolve(self):
        self.assertEqual(PLACES.resolve("Teasa Spaceport in Lorville").name, "Lorville")
        self.assertEqual(PLACES.resolve("August Dunlow Spaceport").name, "Orison")
        self.assertEqual(PLACES.resolve("Area18 on ArcCorp").name, "Area18")
        grim = Places([Place("Grim HEX", "Space station", "Yela", "Stanton", 0, 0, 0)], {})
        self.assertEqual(grim.resolve("Grim HEX within Yela's asteroid belt").name, "Grim HEX")
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

    def test_single_to_multi(self):
        # One pickup for every cargo; the title lists the drop-offs.
        t = Tracker(PLACES)
        code = "HaulCargo_SingleToMulti_Planetary_Small_Souv_PartyFav"
        t.feed(notification("m9", "", "Covalex - Levski > Area18, Orison", "Contract Accepted"))
        t.feed(marker("m9", "pickup_ab_0", code))
        t.feed(marker("m9", "pickup_cd_0", code))
        t.feed(notification("m9", "dropoff_ab_0", "Deliver 0/4 Souvenirs to Area18"))
        t.feed(marker("m9", "dropoff_cd_0", code))
        [c] = t.active()
        legs = c.open_legs()
        self.assertEqual([(t.pickup_of(c, l).name, t.pickup_of(c, l).confidence) for l in legs],
                         [("Levski", EXACT), ("Levski", EXACT)])
        self.assertEqual([(t.dropoff_of(c, l).name, t.dropoff_of(c, l).confidence) for l in legs],
                         [("Area18", EXACT), ("Orison", GUESS)])

    def test_region_link(self):
        # Pyro direct haul: A to B for every cargo, and no cargo named "RegionLink".
        t = Tracker(PLACES)
        code = "RedWind_HaulCargo_CFP_RegionLink_Medium"
        self.assertEqual(cargo_tokens(code), [])
        t.feed(notification("m10", "", "Red Wind - Megumi Refueling > Stanton Gateway (Pyro)", "Contract Accepted"))
        t.feed(marker("m10", "pickup_ab_0", code))
        t.feed(marker("m10", "pickup_cd_0", code))
        t.feed(notification("m10", "dropoff_ab_0", "Deliver 0/8 Scrap to Stanton Gateway (Pyro)"))
        t.feed(marker("m10", "dropoff_cd_0", code))
        [c] = t.active()
        for leg in c.open_legs():
            self.assertEqual(t.pickup_of(c, leg).name, "Megumi Refueling")
            self.assertEqual(t.dropoff_of(c, leg).name, "Stanton Gateway (Pyro)")

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

    def test_pages_read_survive_a_restart(self):
        from schauling.session import Session
        data = Path(__file__).resolve().parent.parent.parent / "SC-map" / "data"
        if not (data / "locations.csv").is_file():
            self.skipTest("SC-map data not next to this project")
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "Game.log"
            log.write_text(marker("m22", "dropoff_ab_0", "HaulCargo_Multi2ToSingle_Interstellar_Medium_Souv") + "\n"
                           + marker("m22", "pickup_ab_1", "HaulCargo_Multi2ToSingle_Interstellar_Medium_Souv") + "\n",
                           encoding="utf-8")
            page = [{"item": i, "total": 3, "dest": "Area18", "origin": o}
                    for i, o in [("Distilled Spirits", "Levski"), ("Party Favors", "Port Tressler"),
                                 ("Souvenirs", "Port Tressler")]]
            pages, manual = {"gone": page}, {}
            s = Session(log, data, "RSI Zeus Mk II ES", manual=manual, pages=pages)
            s.keep_page("m22", page)
            # Overlay restarted: the log alone gives two legs, the kept page three.
            s = Session(log, data, "RSI Zeus Mk II ES", manual=manual, pages=pages)
            self.assertEqual(sorted(l.item for l in s.tracker.contracts["m22"].legs.values()),
                             ["Distilled Spirits", "Party Favors", "Souvenirs"])
            self.assertNotIn("gone", pages)  # contracts no longer in the log are forgotten

    def test_quantum_route_gives_start(self):
        from schauling.session import Session
        data = Path(__file__).resolve().parent.parent.parent / "SC-map" / "data"
        if not (data / "locations.csv").is_file():
            self.skipTest("SC-map data not next to this project")
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "Game.log"
            log.write_text("", encoding="utf-8")
            s = Session(log, data, "RSI Zeus Mk II ES")
            with open(log, "a", encoding="utf-8") as f:
                f.write("<2026-10-28T16:31:50.102Z> [Notice] <Calculate Route> [ItemNavigation][CL][11712] | NOT AUTH"
                        " | RSI_Zeus_ES_905331888284[905331888284]|CSCItemNavigation::CalculateRoute|Projected Start"
                        " Location is Levski for route to destination rs_ext_nyx-castra_jp1 [Team_CGP4][QuantumTravel]\n")
            s.update()
            self.assertTrue(s.place_changed)
            self.assertEqual(s.here, "Levski")
            # A gateway name alone: the system comes from the destination code.
            with open(log, "a", encoding="utf-8") as f:
                f.write("<t> [Notice] <Calculate Route> | X_1[1]|CSCItemNavigation::CalculateRoute|Projected Start"
                        " Location is Stanton Gateway for route to destination rs_ext_nyx_levski [QuantumTravel]\n")
            s.update()
            self.assertEqual(s.here, "Stanton Gateway (Nyx)")


class DataSyncTest(unittest.TestCase):
    def fake_api(self, version, calls, listed=()):
        files = {name: f"{name} v{version}".encode() for name in datasync.FILES + list(listed)}

        def get(url, timeout):
            calls.append(url.rsplit("/", 1)[-1])
            if url.endswith("/data/version"):
                return json.dumps({"version": version, "files": {n: {} for n in files}}).encode()
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

    def test_optional_file_when_listed(self):
        with tempfile.TemporaryDirectory() as d:
            calls = []
            datasync.sync(d, get=self.fake_api("v1", calls))
            self.assertNotIn("ships.json", calls)  # an API without it
            datasync.sync(d, get=self.fake_api("v2", calls, listed=["ships.json"]))
            self.assertEqual((Path(d) / "ships.json").read_text(), "ships.json vv2")

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

    def test_peak_load(self):
        from types import SimpleNamespace as Leg
        from schauling.report import route_lines
        from schauling.routing import Plan, PlanStop
        aboard = Leg(total=10, amount=4, picked=True, cargo="Gold")  # 6 SCU left to deliver
        a = Leg(total=24, amount=0, picked=False, cargo="Souvenirs")
        b = Leg(total=12, amount=0, picked=False, cargo="Party Favors")
        stops = [PlanStop("A", [Action("pickup", "A", a, "c")]),
                 PlanStop("B", [Action("dropoff", "B", aboard, "c"), Action("pickup", "B", b, "c")]),
                 PlanStop("C", [Action("dropoff", "C", a, "c"), Action("dropoff", "C", b, "c")])]
        p = Plan(stops, [], 0)
        self.assertEqual(peak_load(p), 6 + 24 - 6 + 12)
        self.assertIn("Max aboard: 36 / 46 SCU", route_lines(p, 46))
        self.assertIn("Too much: up to 36 SCU aboard, the ship holds 32", route_lines(p, 32))
        self.assertFalse(any("aboard" in line for line in route_lines(p)))
        # Local time on arrival, ☾ at night; nothing for a place in space.
        stops[1].seconds, stops[1].local = 90, ("21:31", "Night", False)
        stops[2].seconds, stops[2].local = 60, ("06:14", "Morning", True)
        lines = route_lines(p)
        self.assertIn("2. B  (+1m30)  · 21:31 night ☾", lines)
        self.assertIn("3. C  (+1m00)  · 06:14 morning", lines)

    def test_local_times(self):
        from schauling.api import local_times
        answers = {"Lorville": {"localTime": "16:17", "phaseText": "Afternoon", "isDay": True},
                   "Megumi Refueling": {"localTime": None, "reason": "In space: no local day cycle."}}
        asked = []

        def get(url):
            asked.append(url)
            if "Area18" in url:
                raise OSError("down")
            return answers[next(n for n in answers if n.replace(" ", "+") in url)]

        got = local_times([("Lorville", 1000.7), ("Megumi Refueling", 2000), ("Area18", 3000)], get=get)
        self.assertEqual(got, [("16:17", "Afternoon", True), None, None])
        self.assertTrue(any("at=1000" in u for u in asked))

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
        self.assertEqual(tt.systems_between("Levski", "Lorville"), ["Nyx", "Pyro", "Stanton"])
        self.assertEqual(TravelTimes(PLACES, fetch=lambda a, b: (100, None)).systems_between("Levski", "Lorville"),
                         ["Nyx", "Stanton"])
        self.assertIsNone(tt.systems_between("Lorville", "Area18"))

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
