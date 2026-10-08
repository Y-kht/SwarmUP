# The web server of the window, and the pages it serves (see Handler).
import hmac
import json
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from interface_views import FormError, USER_ERRORS, catalog


# The pages of the window: src/user-interface.
INTERFACE_FOLDER = Path(__file__).resolve().parents[2] / "user-interface"
ASSETS = {"style.css": "text/css; charset=utf-8", "views.css": "text/css; charset=utf-8", "icons.js": "text/javascript; charset=utf-8",
          "page_tools.js": "text/javascript; charset=utf-8",
          "page_frame.js": "text/javascript; charset=utf-8",
          "step_agents.js": "text/javascript; charset=utf-8",
          "step_folders.js": "text/javascript; charset=utf-8",
          "step_models.js": "text/javascript; charset=utf-8",
          "step_teamwork.js": "text/javascript; charset=utf-8",
          "live_swarm.js": "text/javascript; charset=utf-8",
          "missions.js": "text/javascript; charset=utf-8",
          "app.js": "text/javascript; charset=utf-8",
          "logo.svg": "image/svg+xml"}
HOST = "127.0.0.1"
TOKEN_HEADER = "X-SwarmUP-Token"
TAB_HEADER = "X-SwarmUP-Tab"
TOKEN_PLACEHOLDER = "__SWARMUP_TOKEN__"
SECURITY_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"}

BODY_LIMIT = 2000000


# ==============
# The web server. It only answers this computer, checks the name it is called with (so a website cannot reach it through a DNS trick),
# and every action needs the token of the session, which only the page it served knows.
# ==============
class Handler(BaseHTTPRequestHandler):
    server_version = "SwarmUP"

    def log_message(self, format, *args):
        pass

    def send(self, status, body, contentType):
        self.send_response(status)
        self.send_header("Content-Type", contentType)
        self.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def sendJson(self, status, data):
        self.send(status, json.dumps(data, default=str, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def allowed(self):
        return self.headers.get("Host", "") in self.server.hosts

    def authorized(self):
        return hmac.compare_digest(self.headers.get(TOKEN_HEADER, ""), self.server.session.token)

    def do_GET(self):
        if not self.allowed():
            return self.sendJson(403, {"ok": False, "error": "Forbidden."})
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            page = (INTERFACE_FOLDER / "index.html").read_text(encoding="utf-8").replace(TOKEN_PLACEHOLDER, self.server.session.token)
            return self.send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        if url.path.startswith("/assets/") and url.path[len("/assets/"):] in ASSETS:
            name = url.path[len("/assets/"):]
            return self.send(200, (INTERFACE_FOLDER / name).read_bytes(), ASSETS[name])
        if url.path.startswith("/api/") and not self.authorized():
            return self.sendJson(403, {"ok": False, "error": "This page is not allowed to use SwarmUP. Open the address the program printed."})
        if url.path == "/api/catalog":
            return self.sendJson(200, catalog())
        if url.path == "/api/poll":
            query = parse_qs(url.query)
            version, feed = int(query.get("version", ["-1"])[0]), int(query.get("feed", ["0"])[0])
            return self.sendJson(200, self.server.session.poll(version, feed, self.headers.get(TAB_HEADER)))
        self.sendJson(404, {"ok": False, "error": "Not found."})

    def do_POST(self):
        if not self.allowed() or not self.authorized():
            return self.sendJson(403, {"ok": False, "error": "This page is not allowed to use SwarmUP. Open the address the program printed."})
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        if not url.path.startswith("/api/") or length > BODY_LIMIT:
            return self.sendJson(404, {"ok": False, "error": "Not found."})
        # The session is the desk of the window (session_desk.py), or a single Session: both take the tab the page shows.
        session, tab = self.server.session, self.headers.get(TAB_HEADER)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            self.sendJson(200, session.act(url.path[len("/api/"):], payload, tab))
        except FormError as error:
            self.sendJson(400, {"ok": False, "error": str(error), "errors": error.errors, "state": self.describe(tab)})
        except USER_ERRORS as error:
            self.sendJson(400, {"ok": False, "error": str(error), "state": self.describe(tab)})
        except Exception as error:
            traceback.print_exc()
            self.sendJson(500, {"ok": False, "error": f"Something went wrong: {type(error).__name__}: {error}", "state": self.describe(tab)})

    def describe(self, tab):
        session = self.server.session
        return session.describe(tab) if hasattr(session, "tabs") else session.describe()


def makeServer(session, port=0):
    server = ThreadingHTTPServer((HOST, port), Handler)
    server.daemon_threads = True
    server.session = session
    server.hosts = {f"{HOST}:{server.server_port}", f"localhost:{server.server_port}"}
    return server
