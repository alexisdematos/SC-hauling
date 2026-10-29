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
python -m schauling --check         # after a patch: are the log lines and titles still read?
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
| Move | drag the header line |
| Click the game through the overlay | anywhere but on a text: the empty parts let left clicks through (menu option). Right-click still opens the menu anywhere on the overlay; on an empty part the game gets that right click too (Windows lets all clicks through a part of a window, or none) |
| Menu (cargo / route view, start location, ship, transparency, quit) | right-click |
| Set the pickup of a cargo | click its line |

Start, pickup and ship open a search list: type a few letters (`lev`, `hur l1`, `phoenix`), arrows, Enter.

**Ship**: by default the ship you last boarded, read from `Game.log`. Boarding joins the ship's comms channel, named as SC-map names ships (`You have joined channel 'RSI Zeus Mk II ES : Player'`); leaving the pilot seat logs its entity class as a fallback. Snubs and ground vehicles have no quantum drive and are skipped. A ship picked in the list holds until you board a ship again: boarding always wins.
| Start the route where you are | type `/showlocation` in the game chat: the overlay reads the coordinates it copies to the clipboard |

**Cargo names come at accept time only.** The game logs the objectives (cargo, SCU, destination) when you accept a contract, and only if that contract is the tracked one. Tracking it later logs nothing. Other contracts show `cargo (not in log)`.

**Abandoned contracts** go away with the log's `EndMission … CompletionType[Abandon]` line. If one stays, click its title to hide it. Untracking only takes the markers out of the player's DataBank: the contract stays. `▶` marks the tracked contract.

**Start from the log**: when the local inventory of a place loads around you, the game logs `RequestLocationInventory … Location[Nyx_Levski]`. The overlay maps these codes to SC-map places (`Stanton1_Lorville`, `RR_ARC_LEO` = Baijini Point, `RR_HUR_L2` = HUR-L2 Faithful Dream Station, `Stanton1_HurdynMining_HDMSEdmond` = HDMS-Edmond, `RR_JP_NyxPyro` = Pyro Gateway (Nyx)…) and starts the route there. Picking a quantum destination logs it too: `CalculateRoute|Projected Start Location is Levski for route to destination …` (not when you only switch to quantum mode: nothing is logged until a destination is picked). "Stanton Gateway" is in Pyro and in Nyx: the destination code, else the last place, says which. The newest of these, `/showlocation` and your own pick wins. `r_DisplayInfo 3` shows the zone on screen only, not in the log.

**Auto `/showlocation`** (menu, off by default): when you board a ship with hauling contracts open, the overlay waits 2 s, then presses Enter (opens the chat), types `/showlocation` and presses Enter, at a human pace. It has to be the chat: in the console (`²`) the command copies nothing. Not on accept: contracts are accepted in the mobiGlas, where the chat does not open, and the log does not say when the mobiGlas closes; boarding does show in the log. It waits until no key has been held for 1 s (up to 15 s), so walking or running does not mix with the typing; if you press a key while it types, it closes the chat with Escape without sending, and tries again at the next boarding. It does so only when `StarCitizen.exe` owns the foreground window, and at most once every 5 minutes (a manual `/showlocation` counts). This is automated input into the game: Windows flags these key presses as injected, whatever the speed, and anti-cheat software can see that flag. Turn it on at your own risk.

`/showlocation` gives the position in the frame of the zone you are in, like the markers. Inside a station or a city that frame has no name: set the start by hand there.

**Contract page (OCR)** (menu, on by default): the log never says where each cargo is picked up, the contract page does. When the log says `Contract Accepted` and the game is in front, the page you accepted from is on screen: a thread that watches the end of the log every 100 ms takes 5 screenshots of the game window from 1 to 3 s after it (the objectives type themselves out for about 1.5 s) (it leaves the overlay out of them) and reads each one at once (the right half of the window, where the objectives are) with the OCR built into Windows 10/11 (through one PowerShell process kept open: nothing to install, about 0.15 s a screenshot in 4K, English used when Windows has it). Each page read is applied as it comes, so the cargos fill in within a second when the page is on screen. Each `Deliver 0/12 SCU of Party Favors to …` / `Pick up at Seraphim Station` pair goes to the cargo with the same name and SCU; the pickup counts as set by hand, and fills in cargo names the log did not give. OCR misreads (`Statlon`, `HUR-LI`) are matched to the closest SC-map name. Cargos the log missed (an untracked contract logs the `_0` objective only) are added: from the contract code first (`…_DistSp_Dia_FresFoo_Gol_Aphor` gives 5 cargos), then from the page, which is right when they disagree. The screenshots are merged, so a line the OCR missed on one is taken from another, but only those that show this contract: if you click on to another contract right away, a screenshot whose cargos are not this contract's (from its code or the log, at least 60 %) is dropped. A screenshot fits a contract when its destinations are the contract's (Ling contracts all carry the same four cargos), then when its cargos are the ones the code or the log names; with too few names known (an untracked `Multi2ToSingle`), the destinations alone decide. The menu read uses the same test to find the contract. Each read is written to `%APPDATA%\SC-hauling\ocr-debug.log` (what each screenshot showed, its raw objective lines, which contract got it). What the OCR read is kept in `config.json` and applied again when the overlay restarts: the log alone does not bring it back. The whole window is read, so any resolution or aspect ratio works (ultrawide included, up to 10,000 px wide). Reading pixels sends nothing to the game. Pages you open later are not read on their own (the log does not say when the mobiGlas opens): open the contract page, then right-click the overlay → *Read the contract page now*. The overlay captures the game window under it and finds the contract from the cargos it reads. The screenshot is what the screen shows, so no other window may cover the game.

**After a game patch**: at start, the overlay checks that the log lines it reads (contract accepted, objectives, markers, mission end, local inventory, quantum start, ship channel) are still read in the current `Game.log` and the last backup, that objective markers still come with accepted contracts, and that `global.ini` still has contract title templates naming their places and the `HaulCargo_obj_itemspecifics` texts. A change shows once per game version (`FileVersion` at the top of `Game.log`) on the status line: `Patch 4.11.0…? log: objective marker lines no longer read (0 of 14)`. `python -m schauling --check` prints the whole count.

**Live map** (menu, on by default): a small server on `127.0.0.1:47823` only (`live_port` in `config.json`, or `--live-port`) answers `GET /state` with the ship, where you are (`locationSource`: `log`, `showlocation` or `manual`) and the route: each stop with its arrival time, jump and cargo to load or unload, with SC-map place names. Only the VerseTime site (`https://ofs-versetime.netlify.app`, and `http://localhost` for its development) gets the CORS header that lets a page read it; nothing from the network reaches it, and a request whose `Host` is not `127.0.0.1:<port>` or `localhost:<port>` gets a 403 (against DNS rebinding, where a hostile domain points at 127.0.0.1). *Open the map (VerseTime)* in the menu opens the site on where you are, following the live position (`?live=47823#Lorville`), or only `#Lorville` when the server is off. A port already in use shows on the status line.

**Team** (menu, off until you create or join a team): your teammates see you on the VerseTime map. *Create a team* gets a code from the VerseTime API and copies the invite link (`https://ofs-versetime.netlify.app/#team=<code>`); *Join a team…* takes a code or that link; *Pseudo…* sets the name the team sees (1 to 24 characters, asked the first time). `GET /state` then carries `"team": "<code>"` (else `null`) and `"teamMember"`, your member id (else `null`), so the map knows which member is you; `teamMember` is not sent to the API, and the secret never leaves `config.json`, and *Open the map* opens `?live=47823#team=<code>`. The status line says `Team <code> · sharing on`, or why the last send failed.

**Team view** (right-click → *Cargo / Route / Team*, only in a team): the others in the team, read every 15 s while the view shows. One line each: pseudo, ship, next stop and its time, number of contracts; click it to see their contracts (title, then each cargo from → to, with the same `~` / `?` marks). Under each line, what you have in common: `Bob unloads at Area18 too (+12m) at 21:12, you 21:00`. A contract you both have (same id) shows its title only, `(shared with you)`, and your Cargo view adds `· shared with Bob` to its title. None of this changes your route. Not checked in game yet: does a contract shared in game keep the same id in both players' `Game.log`? If not, set `SHARE_BY_ID = False` in `schauling/team.py`.

`GET /state` also lists your open contracts: `"contracts": [{"id", "title", "tracked", "cargos": [{"cargo", "scu", "from", "to", "sure"}]}]` (SC-map place names, `null` when unknown; `sure` is `""`, `~` or `?`). The API takes 16 KB and 30 contracts at most: over that, titles go first, then the oldest contracts.

What goes on the Internet, only while you are in a team: your pseudo and the same JSON as `GET /state` (ship, where you are, the route with its stops, arrival times and cargo, your open contracts). It goes to the VerseTime API on Cloudflare (`https://versetime-api.ofs.workers.dev`): on each change of position or route (at most every 5 s), and every 60 s without change. Anyone with the team code can read it. The API forgets you 2 h after your last send, and the team 30 days after its last activity. *Leave the team* stops sending at once, asks the API to remove you, and forgets your member id and secret (kept in `config.json` while in the team: the secret proves the next sends are yours). `team_api` in `config.json` points at another API (`http://localhost:8787` for tests).

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
- **`AToB` contracts** have one pickup in the game data: the title's (or any cargo's) is every cargo's. **`Multi2ToSingle`**: `pickup_0`, `pickup_1` are the two places, `dropoff_0…3` the cargos. The first cargo comes from the first place, the others from the last one (seen in game on three contracts, so marked `~`). **`SingleToMulti`** and **`RegionLink`** (Pyro direct hauls) have one pickup too; a `SingleToMulti` title lists the drop-offs, and a cargo whose drop-off is not logged gets the one the other cargos leave (`~`: stock titles list two drop-offs only). `LinearChain` and `RoundDelivery` give no place in the title: not handled yet.
- **Other delivery missions** (packages, boxes): pickup texts "Pick up X from Y" and "Go to Y to pick up…" give the origin. Wrecks, caves and ground sites are not in SC-map: `?`.
- **Jump points**: "Nyx - Pyro Jump Point" is the Pyro Gateway on the Nyx side; "Stanton - Pyro Jump Point" is Pyro Gateway (Stanton).
- **No objective text** (`Multi2ToSingle` contracts, FTL couriers): the cargo comes from the contract code, marked `~`. `HaulCargo_…_DistSpri_PartyFav_ElecEq_Souv` lists the cargos in leg order; each time a contract names its cargos, we learn the abbreviations (kept in `config.json`). Couriers give the system and the package (`FTL_Courier_Nyx_MicroSatellite_Rank0`), and their markers sit inside stations: places show `? (Nyx)`.
- **Contract title**: read with the title templates of the `global.ini` the game reads (`LIVE\data\Localization\english\global.ini`, with Smart Citizen's and your `user.ini` changes). Each `~mission(Location…)` / `~mission(Destination…)` of a `*HaulCargo*_title` becomes a capture, so any title format works and places come in `Location`, `Location1`… order, whatever the order in the title. Without a match, the `… - A, B > C, D` shape. Idea from [Starlogger](https://github.com/chrisrico/Starlogger).

Contracts never carry over to the next game session in the logs, so only the current `Game.log` is read.

### Better delivery names

With `~mission(Destination|ListAll)` the game often writes "Stanton System" as the destination. With `|Address` it writes the station. In Smart Citizen, set:

```ini
HaulCargo_obj_itemspecifics_01=~mission(amount)/~mission(total) ~mission(item) > <EM4>~mission(Destination|Address)</EM4>
```

See `smartcitizen/user.ini.example` for the full set.

## Limits

- Jump points: a trip between systems goes gateway to gateway, through up to 3 jumps, and takes the cheapest chain (Nyx → Stanton direct, or through Pyro). The route view names it on the stop: `Megumi Refueling (+21m01, jump Nyx → Pyro)`. The jump itself counts 5 min, as seen in game.
- Travel times are straight-line quantum estimates (see the SC-map README).
- Local time on arrival: each stop shows the in-game local time and phase from the API's `/time` (`5. Lorville (+2m55) · 21:31 night ☾`), to know if it will be day. Arrival = now + quantum times + 5 min at each stop (approach, landing, cargo, take-off), worked out when the route is planned. Places in space or on a body that does not turn (Levski) have none.
- Ship capacity: the route view ends with the most SCU aboard at once along the route (unload first, then load, at each stop) next to the ship's cargo SCU, in red when it does not fit. The route itself does not take capacity into account, and container sizes are not checked.

## SC-map data

At start, SC-hauling asks the API for `/data/version`. When it changed, it downloads `locations.csv`, `bodies.csv`, `quantum.json` and, when the API lists it, `ships.json` (cargo SCU of each ship) into `%APPDATA%\SC-hauling\data`. That copy has the very names the API knows, so the routes it asks for always resolve. Offline, it uses the last copy, then the data bundled in the exe. `--scmap PATH` uses a folder as it is, without checking.

## Build

```sh
python -m venv .venv-build
.venv-build\Scripts\python -m pip install pyinstaller
.venv-build\Scripts\python build.py
```

It copies `locations.csv`, `bodies.csv`, `quantum.json` and `ships.json` (when there) from `../SC-map/data` (or `--scmap PATH`) into the exe, and writes `dist/SC-hauling.exe` and `dist/SC-hauling-<version>-win64.zip`. Rebuild after the SC-map data changes. The version is in `schauling/__init__.py`.

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
