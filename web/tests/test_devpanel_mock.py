"""The mock's restaurant reports and developer resets behave like foodapp's (Round 32)."""
import json
import sys
from http.client import HTTPResponse
from http.cookies import SimpleCookie
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mock_browser import HOST, MemorySocket, QuietHandler, make_server, mock  # noqa: E402

FULL = {"confirm": True, "keep_community": True, "keep_photos": True, "keep_browser": True}
PROFILE = {"display_name": "Ana", "avatar": "🦉", "color": "moss", "bio": "", "defaults": {}}
DEV_OFF = {"error": "Start the server with --dev to use developer resets."}


class Diner:
    """One browser: its own bite_reporter cookie."""

    def __init__(self, server):
        self.server = server
        self.cookie = None

    def call(self, method, path, body=None, *, content_type="application/json", origin=f"http://{HOST}"):
        data = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        headers = {"Host": HOST, "Content-Length": str(len(data))}
        if data:
            headers["Content-Type"] = content_type
        if origin:
            headers["Origin"] = origin
        if self.cookie:
            headers["Cookie"] = f"bite_reporter={self.cookie}"
        head = f"{method} {path} HTTP/1.0\r\n" + "".join(f"{key}: {value}\r\n" for key, value in headers.items())
        conn = MemorySocket(head.encode() + b"\r\n" + data)
        QuietHandler(conn, ("127.0.0.1", 1234), self.server)
        response = HTTPResponse(MemorySocket(conn.output.getvalue()))
        response.begin()
        for header in response.headers.get_all("Set-Cookie") or []:
            jar = SimpleCookie(header)
            if "bite_reporter" in jar:
                self.cookie = jar["bite_reporter"].value or None
        return response.status, json.loads(response.read())

    def report(self, action, place_id=101, value=None):
        body = {"place_id": place_id, "action": action}
        if value is not None:
            body["value"] = value
        return self.call("POST", "/api/place-report", body)

    def place_ids(self, query="menus=all"):
        return [place["id"] for place in self.call("GET", "/api/places?" + query)[1]["places"]]


@pytest.fixture
def server():
    return make_server()


def test_two_diners_close_a_place_everywhere(server):
    a, b = Diner(server), Diner(server)
    assert 101 in a.place_ids()
    status, first = a.report("closed")
    assert status == 200 and (first["closed"], first["closed_votes"], first["needed"]) == (False, 1, 2) and a.cookie
    assert a.report("closed")[1]["closed_votes"] == 1  # one vote per diner
    place = next(row for row in a.call("GET", "/api/places?menus=all")[1]["places"] if row["id"] == 101)
    assert (place["community_closed"], place["community_closed_votes"], place["community_report_needed"]) == (False, 1, 2)
    status, second = b.report("closed")
    assert second["closed"] is True
    assert 101 not in a.place_ids()
    for seed in range(15):
        dish = a.call("GET", f"/api/pick?budget=36&seed={seed}")[1]["dish"]
        assert dish is None or dish["place"]["id"] != 101
    status, specific = a.call("GET", "/api/pick?budget=12&place_id=101")
    assert specific["dish"] is None and specific["message"].endswith("was reported closed by diners.")
    menu = a.call("GET", "/api/menu?place_id=101")[1]
    assert (menu["community_closed"], menu["community_closed_votes"]) == (True, 2)
    Diner(server).report("open")
    assert 101 not in a.place_ids()
    Diner(server).report("open")
    assert 101 in a.place_ids()


def test_timing_consensus_and_validation(server):
    a, b = Diner(server), Diner(server)
    a.report("timing", value="Daily 11 AM - 2 AM")
    assert next(p for p in a.call("GET", "/api/places?menus=all")[1]["places"] if p["id"] == 101)["community_timing_votes"] == 1
    a.report("timing", place_id=102, value="Daily 11 AM - 2 AM")
    status, accepted = b.report("timing", place_id=102, value=" daily 11 am -  2 am")
    assert (accepted["timing"], accepted["timing_votes"]) == ("daily 11 am - 2 am", 2)
    place = next(p for p in a.call("GET", "/api/places?menus=all")[1]["places"] if p["id"] == 102)
    assert place["hours_text"] == place["community_timing"] == "daily 11 am - 2 am"
    assert a.call("GET", "/api/menu?place_id=102")[1]["community_timing"] == "daily 11 am - 2 am"
    for body, status, error in (
        ({"place_id": 999, "action": "closed"}, 404, "Restaurant not found."),
        ({"place_id": "101", "action": "closed"}, 404, "Restaurant not found."),
        ({"place_id": 101, "action": "gone"}, 400, "Choose closed, open, or timing."),
        ({"place_id": 101, "action": "timing", "value": "x"}, 400, mock.PLACE_TIMING_ERROR),
        ({"place_id": 101, "action": "timing", "value": "<b>late</b>"}, 400, "Use plain text for the opening hours."),
    ):
        assert a.call("POST", "/api/place-report", body) == (status, {"error": error})
    assert a.call("POST", "/api/place-report", {"place_id": 101, "action": "closed"}, origin="http://evil.test") == (
        403, {"error": "Invalid request."})


def test_dev_endpoints_are_gated_like_foodapp(server):
    diner = Diner(server)
    assert "dev" not in diner.call("GET", "/api/options")[1]
    assert diner.call("POST", "/api/dev-reset", FULL) == (403, DEV_OFF)
    assert diner.call("POST", "/api/place-report-reset", {"action": "all"}) == (403, DEV_OFF)
    server.args.dev = True
    assert diner.call("GET", "/api/options")[1]["dev"] == {"enabled": True, "allow_now": True}
    for path in ("/api/dev-reset", "/api/place-report-reset"):
        assert diner.call("POST", path, FULL, origin="http://evil.test") == (403, {"error": "Invalid request."})
        assert diner.call("POST", path, FULL, content_type="text/plain") == (
            415, {"error": "Send the reset as application/json."})
    assert diner.call("POST", "/api/dev-reset", dict(FULL, confirm="yes")) == (400, {"error": "Confirm the reset."})
    assert diner.call("POST", "/api/dev-reset", dict(FULL, keep_photos=0))[0] == 400
    assert diner.call("POST", "/api/place-report-reset", {"action": "x"}) == (
        400, {"error": "Choose all, closed, or timing."})
    assert diner.call("POST", "/api/place-report-reset", {"place_id": 999}) == (404, {"error": "Restaurant not found."})
    assert diner.call("POST", "/api/dev-reset", FULL) == (200, {"ok": True, "deleted": {}})


def test_reset_keep_rules_match_foodapp(server):
    server.args.dev = True
    me, other = Diner(server), Diner(server)
    for diner in (me, other):
        diner.call("PUT", "/api/profile", PROFILE)
        diner.call("POST", "/api/recovery/create", {})
        diner.report("closed")
    community = server.community
    community.photos["p1"] = (me.cookie, "image/png", b"x")
    community.logs[me.cookie] = [{"place_id": 101, "name": "Commonwealth Bowl", "photo_url": "/api/visit-photo/p1",
                                  "created_at": "2026-11-21T21:00:00-05:00", "items": []}]
    status, result = me.call("POST", "/api/dev-reset", dict(FULL, keep_photos=False))
    assert result["deleted"] == {"photo_links": 1, "photos": 1}
    assert community.logs[me.cookie][0]["photo_url"] is None
    status, result = me.call("POST", "/api/dev-reset", dict(FULL, keep_browser=False))
    assert result["deleted"] == {"profiles": 1, "recovery": 1}
    assert me.cookie not in community.profiles and other.cookie in community.profiles
    me.call("PUT", "/api/profile", PROFILE)
    status, result = me.call("POST", "/api/dev-reset", dict(FULL, keep_community=False))
    assert result["deleted"]["place_reports"] == 2 and result["deleted"]["profiles"] == 1
    assert list(community.profiles) == [me.cookie] and community.place_reports == {} and community.reports == {}
    assert 101 in me.place_ids()
    me.report("closed")
    status, reset = me.call("POST", "/api/place-report-reset", {"place_id": 101, "action": "closed"})
    assert reset == {"ok": True, "deleted": 1}
