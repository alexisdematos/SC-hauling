"""Where you are and your route, for the VerseTime map in the browser.

A small HTTP server on 127.0.0.1 only (nothing from the network reaches
it) answers GET /state with JSON. Only the VerseTime site (and localhost,
for its development) may read it from a page: other sites get no CORS
header, so the browser keeps the answer from them.
"""
import json
import re
import socket
import threading
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SITE = "https://ofs-versetime.netlify.app"
DEFAULT_PORT = 47823
_LOCAL_ORIGIN = re.compile(r"^http://(?:localhost|127\.0\.0\.1)(?::\d+)?$")


def allowed_origin(origin):
    return bool(origin) and (origin == SITE or bool(_LOCAL_ORIGIN.match(origin)))


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_state(ship, location, source, plan, now):
    """The /state answer: ship, where we are, and the stops of the route
    with their arrival time, jump and cargo (SC-map place names)."""
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
            "locationSource": source if location else None, "route": route}


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
        self._cors()
        self.end_headers()

    def do_GET(self):
        if not self._host_ok():
            return
        if urllib.parse.urlparse(self.path).path != "/state":
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
    def __init__(self, get_state):
        """get_state(): the /state dict, called on the server's threads."""
        self.get_state = get_state
        self._server = None

    @property
    def running(self):
        return self._server is not None

    def start(self, port):
        """Listen on 127.0.0.1:port. Raises OSError when the port is taken."""
        self.stop()
        server = _Server(("127.0.0.1", port), _Handler)
        server.get_state = self.get_state
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self._server = server

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


def map_url(place, port=None):
    """The VerseTime map on a place; with the live position when the server runs."""
    query = f"?live={port}" if port else ""
    anchor = "#" + urllib.parse.quote(place.replace(" ", "_")) if place else ""
    return f"{SITE}/{query}{anchor}"


def open_map(place, port=None):
    webbrowser.open(map_url(place, port))
