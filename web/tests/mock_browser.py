"""Serve the app from the in-process mock API inside Playwright, without a socket.

Shared by the browser walkthroughs in this folder. Every request (GET, POST and
PUT, with its body) goes through tests/mock_api.py's handler. The bite_reporter
cookie is kept in a small jar here, because fulfilled responses do not reach the
browser's own cookie store reliably.
"""
from datetime import datetime
from http.client import HTTPResponse
from http.cookies import SimpleCookie
import importlib.util
from io import BytesIO
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("mock_api", ROOT / "tests/mock_api.py")
mock = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mock)
HOST = "midnight-munch.test"
ORIGIN = f"https://{HOST}"  # A secure context, so the clipboard API exists.
LAUNCH_ARGS = ["--single-process", "--disable-gpu", "--no-zygote", "--disable-software-rasterizer"]


class MemorySocket:
    def __init__(self, request):
        self.input = BytesIO(request)
        self.output = BytesIO()

    def makefile(self, *_):
        return self.input

    def sendall(self, data):
        self.output.write(data)


class QuietHandler(mock.Handler):
    def log_message(self, *_):
        pass


def make_server(now=datetime(2026, 11, 21, 21, 12, tzinfo=mock.BOSTON), **overrides):
    args = dict(allow_now=True, slow=0, transit_down=False, routing_down=False, stale_nearby=False,
                station_search_fail=False, stations_unavailable=False, bus_mode="subway", fixed_departure=(),
                replan_failures=0, replan_delay=0, pick_delay=0)
    args.update(overrides)
    return SimpleNamespace(args=SimpleNamespace(**args), community=mock.MockCommunity(), community_lock=Lock(),
                           nearby_lock=Lock(), nearby_fail_once=False, pick_timeout_once=False,
                           now_source=lambda: now)


class MockSite:
    """One browser context wired to one mock server."""

    def __init__(self, browser, width, height, server=None, storage_state=None):
        self.server = server or make_server()
        self.cookie = None
        self.requests = []
        self.errors = []
        self.console = []
        options = dict(viewport={"width": width, "height": height}, has_touch=True, is_mobile=True,
                       reduced_motion="reduce", geolocation={"latitude": 42.34895, "longitude": -71.09517},
                       permissions=["geolocation", "clipboard-read", "clipboard-write"], accept_downloads=True)
        if storage_state:
            options["storage_state"] = storage_state
        self.context = browser.new_context(**options)
        self.page = self.context.new_page()
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on("console", lambda message: self.console.append(message.text))
        self.page.route("**/*", self.intercept)

    def intercept(self, route):
        request = route.request
        url = urlsplit(request.url)
        if url.netloc != HOST:
            route.abort()
            return
        path = url.path + (f"?{url.query}" if url.query else "")
        body = request.post_data_buffer or b""
        self.requests.append((request.method, path, body))
        headers = {"Host": HOST, "Content-Length": str(len(body))}
        sent = request.all_headers()
        if "content-type" in sent:
            headers["Content-Type"] = sent["content-type"]
        if "origin" in sent:
            # The mock checks Origin against http://Host; the page is served over https.
            headers["Origin"] = sent["origin"].replace("https://", "http://", 1)
        if self.cookie:
            headers["Cookie"] = f"bite_reporter={self.cookie}"
        head = f"{request.method} {path} HTTP/1.0\r\n" + "".join(f"{key}: {value}\r\n" for key, value in headers.items())
        conn = MemorySocket(head.encode() + b"\r\n" + body)
        QuietHandler(conn, ("127.0.0.1", 1234), self.server)
        response = HTTPResponse(MemorySocket(conn.output.getvalue()))
        response.begin()
        for header in response.headers.get_all("Set-Cookie") or []:
            jar = SimpleCookie(header)
            if "bite_reporter" in jar:
                value = jar["bite_reporter"].value
                self.cookie = value if value and jar["bite_reporter"]["max-age"] != "0" else None
        reply = {"content-type": response.getheader("Content-Type") or "application/octet-stream"}
        for name in ("Content-Disposition", "Retry-After"):
            if response.getheader(name):
                reply[name.lower()] = response.getheader(name)
        route.fulfill(status=response.status, headers=reply, body=response.read())

    def api(self, method):
        """Requests to the profile API, as (method, path, json body) tuples."""
        return [(verb, path, body) for verb, path, body in self.requests if verb == method]

    def close(self):
        self.context.close()
