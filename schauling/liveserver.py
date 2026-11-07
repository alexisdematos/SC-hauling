"""Where you are and your route, for the VerseTime map in the browser.

A small HTTP server on 127.0.0.1 only (nothing from the network reaches
it) answers GET /state with JSON. Only the VerseTime site (site_url, and
the old Netlify address, where tabs may stay open; localhost for its development) may
read it from a page: other sites get no CORS
header, so the browser keeps the answer from them.
"""
import hmac
import json
import queue
import re
import socket
import threading
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .detours import via_stops

SITE = "https://versetime.ofs.workers.dev"  # default site_url
OLD_SITE = "https://ofs-versetime.netlify.app"  # before the move to Cloudflare: still allowed, tabs may stay open
_site = SITE
DEFAULT_PORT = 47823
_LOCAL_ORIGIN = re.compile(r"^http://(?:localhost|127\.0\.0\.1)(?::\d+)?$")
# The SC-overlay window (Tauri on Windows serves its pages from here): the only page that may call /ui, /events, /action
TAURI_ORIGINS = ("http://tauri.localhost", "https://tauri.localhost")
PROTECTED = ("/ui", "/events", "/action")
MAX_BODY = 16 * 1024
KEEPALIVE_S = 15


def set_site(url):
    """The VerseTime site to open and allow (site_url in config.json)."""
    global _site
    _site = (url or SITE).rstrip("/")


def site():
    return _site


def allowed_origin(origin):
    return bool(origin) and (origin in (_site, SITE, OLD_SITE) or bool(_LOCAL_ORIGIN.match(origin)))


def token_ok(given, expected):
    """Constant-time comparison; no token set means the protected routes are off."""
    if not expected or not given:
        return False
    return hmac.compare_digest(str(given).encode("utf-8"), str(expected).encode("utf-8"))


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def heading_via(heading):
    """The stops the game adds to get past a planet, in order (heading.via_stops of the detours the log shows)."""
    return via_stops(heading.get("detours") or [])


def build_state(ship, location, source, plan, now, team=None, contracts=None, team_member=None, heading=None,
                handle=None, aboard=None, shard=None):
    """The /state answer: ship, where we are, and the stops of the route
    with their arrival time, jump and cargo (SC-map place names). team: the
    code of the team we share with, so the map shows it too. contracts: see
    team.contracts_state. team_member: our member id, so the map knows which
    teammate we are (local only: team.TeamSharer leaves it out of the PUT).
    heading: the quantum destination picked last (session.heading); jumpAt
    is when the ship should start moving (pick + aligning + spool).
    handle: our in-game name; aboard: {"owner", "ship"} when we are in
    another player's ship (its owner's in-game name), so the team map puts
    us with them. shard: the game server id (Game.log), so the team knows who is on the same one."""
    route = []
    for stop in plan.stops if plan else []:
        route.append({
            "place": stop.place,
            "arrival": _iso(stop.arrival) if stop.arrival else None,
            "jump": " → ".join(stop.via) if stop.via else None,
            "actions": [{"type": "load" if a.kind == "pickup" else "unload",
                         "scu": a.leg.total or None,
                         "cargo": (a.leg.cargo or "").lstrip("~") or None} for a in stop.actions],
        })
    return {"version": 1, "updatedAt": _iso(now), "ship": ship, "location": location,
            "locationSource": source if location else None, "route": route, "team": team,
            "contracts": contracts or [], "teamMember": team_member if team else None,
            "handle": handle, "aboard": aboard, "shard": shard,
            "heading": {"from": heading["from"], "to": heading["to"],
                        "pickedAt": _iso(heading["picked_at"]) if heading.get("picked_at") else None,
                        "eta": _iso(heading["eta"]) if heading.get("eta") else None,
                        "jumpAt": _iso(heading["jump_at"]) if heading.get("jump_at") else None,
                        "via": heading_via(heading)} if heading else None}


class _Handler(BaseHTTPRequestHandler):
    def _host_ok(self):
        """DNS rebinding: a hostile domain pointed at 127.0.0.1 makes its
        page "same origin" and skips CORS. A request for us names us in Host."""
        port = self.server.server_address[1]
        if self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return True
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _protected_cors(self):
        origin = self.headers.get("Origin")
        if origin in TAURI_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Vary", "Origin")

    def _reply(self, status, body=None, protected=False):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else b""
        self.send_response(status)
        (self._protected_cors if protected else self._cors)()
        if body is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _api(self, path):
        """The service API when this request may use it, else None (the answer is already sent)."""
        api = self.server.api
        if api is None:
            self._reply(404, protected=True)
            return None
        auth = self.headers.get("Authorization") or ""
        given = auth[7:] if auth.startswith("Bearer ") else None
        if given is None and path == "/events":  # EventSource cannot set headers
            given = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("token") or [None])[0]
        if not token_ok(given, api.token):
            self._reply(401, {"error": "token"}, protected=True)
            return None
        return api

    def _cors(self):
        origin = self.headers.get("Origin")
        if allowed_origin(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            # Chrome asks before a public site reads a local address.
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Vary", "Origin")

    def do_OPTIONS(self):
        if not self._host_ok():
            return
        self.send_response(204)
        (self._protected_cors if urllib.parse.urlparse(self.path).path in PROTECTED else self._cors)()
        self.end_headers()

    def _read_body(self):
        """(length, bytes): the body is read before any answer, even a refusal: a client that is answered with
        its body unread gets a reset connection instead of the status. Past the limit only the start is read."""
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            return None, b""
        if length > MAX_BODY:
            self.close_connection = True
        return length, self.rfile.read(max(0, min(length, MAX_BODY + 1)))

    def do_POST(self):
        length, body = self._read_body()
        if not self._host_ok():
            return
        path = urllib.parse.urlparse(self.path).path
        if path != "/action":
            return self._reply(404)
        api = self._api(path)
        if api is None:
            return
        if length is None:
            return self._reply(411, {"error": "length"}, protected=True)
        if not 0 < length <= MAX_BODY:
            return self._reply(413, {"error": "size"}, protected=True)
        try:
            request = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._reply(400, {"error": "json"}, protected=True)
        if not isinstance(request, dict):
            return self._reply(400, {"error": "json"}, protected=True)
        status, answer = api.act(request)
        self._reply(status, answer, protected=True)

    def _events(self, api):
        """Server-Sent Events: the /ui answer now, then at each change."""
        subscription = api.subscribe()
        try:
            self.send_response(200)
            self._protected_cors()
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"retry: 2000\n\n")
            event = api.snapshot_event()
            while True:
                self.wfile.write(event if event else b": ping\n\n")
                self.wfile.flush()
                try:
                    event = subscription.get(timeout=KEEPALIVE_S)
                except queue.Empty:
                    event = None
        except (OSError, ValueError):  # the window closed the stream
            pass
        finally:
            api.unsubscribe(subscription)

    def do_GET(self):
        if not self._host_ok():
            return
        path = urllib.parse.urlparse(self.path).path
        if path in ("/ui", "/events"):
            api = self._api(path)
            if api is None:
                return
            if path == "/events":
                return self._events(api)
            ui = api.snapshot()
            if ui is None:  # nothing built yet (the first render comes right after the start)
                return self._reply(503, {"error": "starting"}, protected=True)
            return self._reply(200, ui, protected=True)
        if path != "/state":
            self.send_response(404)
            self._cors()
            self.end_headers()
            return
        body = json.dumps(self.server.get_state(), ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):  # no console in the exe
        pass


class _Server(ThreadingHTTPServer):
    # Windows lets a second program take a port bound with SO_REUSEADDR (the
    # http.server default): claim it alone, so "port in use" is reported.
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class LiveServer:
    def __init__(self, get_state, api=None):
        """get_state(): the /state dict, called on the server's threads. api: the service API behind /ui,
        /events and /action (token, snapshot(), snapshot_event(), subscribe(), unsubscribe(q), act(request)),
        or None: those routes answer 404."""
        self.get_state = get_state
        self.api = api
        self._server = None

    @property
    def running(self):
        return self._server is not None

    @property
    def port(self):
        return self._server.server_address[1] if self._server else None

    def start(self, port):
        """Listen on 127.0.0.1:port. Raises OSError when the port is taken."""
        self.stop()
        server = _Server(("127.0.0.1", port), _Handler)
        server.get_state = self.get_state
        server.api = self.api
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self._server = server

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


def map_url(place, port=None, team=None):
    """The VerseTime map on a place, or on the team; with the live position
    when the server runs."""
    query = f"?live={port}" if port else ""
    if team:
        anchor = f"#team={team}"
    else:
        anchor = "#" + urllib.parse.quote(place.replace(" ", "_")) if place else ""
    return f"{_site}/{query}{anchor}"


def open_map(place, port=None, team=None):
    webbrowser.open(map_url(place, port, team))
