# SC Hauling

Star Citizen hauling contracts read from `Game.log`, origin first, with the best order of stops and an always-on-top overlay.

```
Ling Family Rookie Cargo Haul - Small Scale - Levski > Riker Memorial Spaceport in Area18
  ~Levski → Area18 : 24 SCU Distilled Spirits
  Port Tressler → ~Area18 : 12 SCU Party Favors

1. Port Tressler  (+4m07)
     ↑ load 12 SCU Party Favors
2. Levski  (+15m58)
     ↑ load 24 SCU Distilled Spirits
3. Area18  (+15m58)
     ↓ unload 24 SCU Distilled Spirits
     ↓ unload 12 SCU Party Favors
Total quantum: ~36m03
```

- Reads only the log file the game writes. No memory reading, no injection. Nothing is sent to the game, unless you turn on auto `/showlocation`.
- Travel times come from the [VerseTime API](https://versetime-api.ofs.workers.dev) (SC-map), for your ship.
- Python 3.10+, standard library only.

## Install

Download `SC-hauling-<version>-win64.zip` from the releases, unzip it anywhere and run `SC-hauling.exe`. Nothing else to install: it bundles Python and the SC-map data. If it cannot find your `Game.log`, it asks for it (`StarCitizen\LIVE\Game.log`).

Windows may warn about an unknown publisher the first time (the exe is not signed): *More info* → *Run anyway*.

## Run from source

```sh
python -m schauling                 # overlay
python -m schauling --once          # print contracts and route, then exit
python -m schauling --watch         # print again on each change
```

Or double-click `SC-hauling.bat`.

| Option | Default |
|---|---|
| `--log PATH` | `Game.log` in the usual install folders |
| `--ship NAME` | `Drake Caterpillar` |
| `--from PLACE` | none: the route starts at the first stop |
| `--scmap PATH` | `../SC-map/data` |

Settings, the overlay position and the pickups you set by hand are kept in `%APPDATA%\SC-hauling\config.json`. Travel times are cached in `travel-cache.json` next to it.

## Overlay

Run the game in **borderless window** mode, or the overlay stays behind it.

While the game runs, the overlay shows only when the game (or the overlay itself) is in front, and it quits 10 s after `StarCitizen.exe` closes. Before the game starts it stays visible. Both are menu options.

| Action | How |
|---|---|
| Move | drag the window |
| Menu (cargo / route view, start location, ship, transparency, quit) | right-click |
| Set the pickup of a cargo | click its line |

Start, pickup and ship open a search list: type a few letters (`lev`, `hur l1`, `phoenix`), arrows, Enter.

**Ship**: by default the ship you last boarded, read from `Game.log`. Boarding joins the ship's comms channel, named as SC-map names ships (`You have joined channel 'RSI Zeus Mk II ES : Player'`); leaving the pilot seat logs its entity class as a fallback. Snubs and ground vehicles have no quantum drive and are skipped. A ship picked in the list holds until you board a ship again: boarding always wins.
| Start the route where you are | type `/showlocation` in the game chat: the overlay reads the coordinates it copies to the clipboard |

**Cargo names come at accept time only.** The game logs the objectives (cargo, SCU, destination) when you accept a contract, and only if that contract is the tracked one. Tracking it later logs nothing. Other contracts show `cargo (not in log)`.

**Abandoned contracts** go away with the log's `EndMission … CompletionType[Abandon]` line. If one stays, click its title to hide it. Untracking only takes the markers out of the player's DataBank: the contract stays. `▶` marks the tracked contract.

**Start from the log**: when the local inventory of a place loads around you, the game logs `RequestLocationInventory … Location[Nyx_Levski]`. The overlay maps these codes to SC-map places (`Stanton1_Lorville`, `RR_ARC_LEO` = Baijini Point, `RR_HUR_L2` = HUR-L2 Faithful Dream Station, `Stanton1_HurdynMining_HDMSEdmond` = HDMS-Edmond, `RR_JP_NyxPyro` = Pyro Gateway (Nyx)…) and starts the route there. The newest of this, `/showlocation` and your own pick wins. `r_DisplayInfo 3` shows the zone on screen only, not in the log.

**Auto `/showlocation`** (menu, off by default): when you board a ship with hauling contracts open, the overlay waits 2 s, then presses Enter (opens the chat), types `/showlocation` and presses Enter, at a human pace. It has to be the chat: in the console (`²`) the command copies nothing. Not on accept: contracts are accepted in the mobiGlas, where the chat does not open, and the log does not say when the mobiGlas closes; boarding does show in the log. It waits until no key has been held for 1 s (up to 15 s), so walking or running does not mix with the typing; if you press a key while it types, it closes the chat with Escape without sending, and tries again at the next boarding. It does so only when `StarCitizen.exe` owns the foreground window, and at most once every 5 minutes (a manual `/showlocation` counts). This is automated input into the game: Windows flags these key presses as injected, whatever the speed, and anti-cheat software can see that flag. Turn it on at your own risk.

`/showlocation` gives the position in the frame of the zone you are in, like the markers. Inside a station or a city that frame has no name: set the start by hand there.

## How sure is a place

| Mark | Meaning |
|---|---|
| none | from the objective text, or from the marker position |
| `~` | from the contract title: right for the first cargo, may be wrong for the others |
| `?` | unknown: click the line to set it |

What the game logs:

- **Deliveries**: one `New Objective` line per cargo, with its text from `global.ini` (item, amount, destination).
- **Pickups**: no text at all. Only the marker position, in metres, in the frame of the zone the player is in. We match it with the SC-map places, within 15 km:
  - system frame (far from the origin): stations and places at a body's centre, such as Levski;
  - planet or moon frame: every place on or around it, such as Seraphim Station or an HDMS outpost;
  - station or city frame (close to the origin): unnamed, we cannot tell which station.
- **`AToB` contracts** have one pickup in the game data: the title's (or any cargo's) is every cargo's. **`Multi2ToSingle`**: `pickup_0`, `pickup_1` are the two places, `dropoff_0…3` the cargos. The first cargo comes from the first place, the others from the last one (seen in game on three contracts, so marked `~`).
- **Jump points**: "Nyx - Pyro Jump Point" is the Pyro Gateway on the Nyx side; "Stanton - Pyro Jump Point" is Pyro Gateway (Stanton).
- **No objective text** (`Multi2ToSingle` contracts, FTL couriers): the cargo comes from the contract code, marked `~`. `HaulCargo_…_DistSpri_PartyFav_ElecEq_Souv` lists the cargos in leg order; each time a contract names its cargos, we learn the abbreviations (kept in `config.json`). Couriers give the system and the package (`FTL_Courier_Nyx_MicroSatellite_Rank0`), and their markers sit inside stations: places show `? (Nyx)`.
- **Contract title**: with Smart Citizen route titles (`… - Levski > Teasa Spaceport in Lorville`), the mission's first pickup.

Contracts never carry over to the next game session in the logs, so only the current `Game.log` is read.

### Better delivery names

With `~mission(Destination|ListAll)` the game often writes "Stanton System" as the destination. With `|Address` it writes the station. In Smart Citizen, set:

```ini
HaulCargo_obj_itemspecifics_01=~mission(amount)/~mission(total) ~mission(item) > <EM4>~mission(Destination|Address)</EM4>
```

See `smartcitizen/user.ini.example` for the full set.

## Limits

- Jump points: a trip between systems goes gateway to gateway, through up to 3 jumps, and takes the cheapest chain (Nyx → Stanton direct, or through Pyro). The jump itself counts 10 min, a rough figure.
- Travel times are straight-line quantum estimates (see the SC-map README).
- Container sizes and ship capacity are not checked.

## Build

```sh
python -m venv .venv-build
.venv-build\Scripts\python -m pip install pyinstaller
.venv-build\Scripts\python build.py
```

It copies `locations.csv`, `bodies.csv` and `quantum.json` from `../SC-map/data` (or `--scmap PATH`) into the exe, and writes `dist/SC-hauling.exe` and `dist/SC-hauling-<version>-win64.zip`. Rebuild after the SC-map data changes. The version is in `schauling/__init__.py`.

## Tests

```sh
python -m unittest discover -s tests -t .
```

`tests/fixtures/game_sample.log` holds two real contracts, with player names and ids replaced.

## License and credits

GPL-3.0, see `LICENSE`.

- Places, bodies and quantum drives: [SC-map / VerseTime](https://versetime-api.ofs.workers.dev) data, from the [Murphy Exploration Group](https://docs.google.com/spreadsheets/d/1aGJ0_49ve1NKf0GvSteSt3-a4jSxnj2snHmTDwKTBgs) and [scunpacked-data](https://github.com/StarCitizenWiki/scunpacked-data)
- Original VerseTime project: [dydrmr/VerseTime](https://github.com/dydrmr/VerseTime) (GPL-3.0)
- Not affiliated with Cloud Imperium Games. Star Citizen is a trademark of Cloud Imperium Rights LLC.
