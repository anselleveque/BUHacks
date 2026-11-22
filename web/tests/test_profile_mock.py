"""The mock's saved profile and recovery code endpoints behave like foodapp's (Round 30)."""
import json
import sys
from http.client import HTTPResponse
from http.cookies import SimpleCookie
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mock_browser import HOST, MemorySocket, QuietHandler, make_server, mock  # noqa: E402

PROFILE = {"display_name": "Night Owl", "avatar": "🦉", "color": "moss", "bio": "Ramen after shows.",
           "defaults": {"home_station_id": "place-bucen", "budget": 12, "stay_min": 45, "walk_ok_min": 15,
                        "max_travel_min": None, "areas": ["Fenway"], "late_mode": "on", "count_ride_home": True,
                        "diets": ["vegan"], "cravings": ["spicy"], "theme": "dark"}}


class Client:
    def __init__(self, server, peer="127.0.0.1"):
        self.server = server
        self.peer = peer
        self.cookie = None

    def call(self, method, path, body=None, origin=True):
        data = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        headers = {"Host": HOST, "Content-Length": str(len(data))}
        if data:
            headers["Content-Type"] = "application/json"
        if origin:
            headers["Origin"] = f"http://{HOST}"
        if self.cookie:
            headers["Cookie"] = f"bite_reporter={self.cookie}"
        head = f"{method} {path} HTTP/1.0\r\n" + "".join(f"{key}: {value}\r\n" for key, value in headers.items())
        conn = MemorySocket(head.encode() + b"\r\n" + data)
        QuietHandler(conn, (self.peer, 1234), self.server)
        response = HTTPResponse(MemorySocket(conn.output.getvalue()))
        response.begin()
        for header in response.headers.get_all("Set-Cookie") or []:
            jar = SimpleCookie(header)
            if "bite_reporter" in jar:
                self.cookie = jar["bite_reporter"].value or None
        return response.status, json.loads(response.read())


@pytest.fixture
def server():
    return make_server()


def test_profile_round_trip_and_cookie(server):
    client = Client(server)
    assert client.call("GET", "/api/profile") == (200, {"exists": False, "profile": None, "created_at": None,
                                                        "updated_at": None, "has_recovery_code": False})
    status, saved = client.call("PUT", "/api/profile", PROFILE)
    assert status == 200 and saved["exists"] and saved["profile"] == PROFILE and client.cookie
    assert client.call("GET", "/api/profile")[1] == saved
    status, me = client.call("GET", "/api/me")
    assert me["profile_exists"] is True and me["has_recovery_code"] is False
    # POST is accepted for the same save.
    status, again = client.call("POST", "/api/profile", dict(PROFILE, bio="", if_updated_at=saved["updated_at"]))
    assert status == 200 and again["updated_at"] > saved["updated_at"] and again["profile"]["bio"] == ""


def test_stale_update_returns_409_with_the_current_profile(server):
    phone, laptop = Client(server), Client(server)
    _, first = phone.call("PUT", "/api/profile", PROFILE)
    laptop.cookie = phone.cookie
    _, second = laptop.call("PUT", "/api/profile", dict(PROFILE, display_name="Laptop", if_updated_at=first["updated_at"]))
    status, conflict = phone.call("PUT", "/api/profile", dict(PROFILE, display_name="Phone", if_updated_at=first["updated_at"]))
    assert status == 409 and conflict["error"] == "Your profile changed on another device."
    assert conflict["profile"]["display_name"] == "Laptop" and conflict["updated_at"] == second["updated_at"]


@pytest.mark.parametrize("change, message", [
    ({"defaults": {"favorite": 1}}, "Unknown setting."),
    ({"defaults": {"budget": 13}}, "Invalid setting."),
    ({"defaults": {"walk_ok_min": 45}}, "Invalid setting."),
    ({"defaults": {"areas": ["Mars"]}}, "Invalid setting."),
    ({"defaults": {"count_ride_home": "yes"}}, "Invalid setting."),
    ({"defaults": []}, "Defaults must be an object."),
    ({"display_name": "x" * 29}, "Keep the display name to 28 characters."),
    ({"bio": "x" * 141}, "Keep the bio to 140 characters."),
    ({"avatar": "🦖"}, "Choose an avatar."),
    ({"color": "teal"}, "Choose a color."),
])
def test_profile_validation(server, change, message):
    assert Client(server).call("PUT", "/api/profile", dict(PROFILE, **change)) == (400, {"error": message})


def test_control_characters_are_stripped_and_origin_is_required(server):
    client = Client(server)
    assert client.call("PUT", "/api/profile", PROFILE, origin=False)[0] == 403
    _, saved = client.call("PUT", "/api/profile", dict(PROFILE, display_name="Ana\u0000‏"))
    assert saved["profile"]["display_name"] == "Ana"


def test_write_rate_limit(server):
    server.community.write_limit = 2
    client = Client(server)
    assert client.call("PUT", "/api/profile", PROFILE)[0] == 200
    assert client.call("PUT", "/api/profile", PROFILE)[0] == 200
    status, body = client.call("PUT", "/api/profile", PROFILE)
    assert status == 429 and body["error"] == "Slow down." and body["retry_after"] >= 1


def test_recovery_code_create_restore_and_revoke(server):
    owner = Client(server)
    assert owner.call("POST", "/api/recovery/create", {})[0] == 401
    owner.call("PUT", "/api/profile", PROFILE)
    status, created = owner.call("POST", "/api/recovery/create", {})
    code = created["code"]
    assert status == 200 and len(code) == 23 and code.count("-") == 3
    assert set(code.replace("-", "")) <= set(mock.CODE_ALPHABET)
    assert code not in json.dumps(server.community.recovery)
    assert owner.call("GET", "/api/profile")[1]["has_recovery_code"] is True
    other = Client(server, peer="10.0.0.2")
    # Any case, spaces, no dashes, and Crockford's I/L/O aliases are accepted.
    typed = code.lower().replace("-", " ").replace("1", "l").replace("0", "o")
    status, restored = other.call("POST", "/api/recovery/restore", {"code": typed})
    assert status == 200 and restored["profile"] == PROFILE and "stats" in restored
    assert other.cookie == owner.cookie
    _, newer = owner.call("POST", "/api/recovery/create", {})
    assert Client(server).call("POST", "/api/recovery/restore", {"code": code})[0] == 400
    assert Client(server).call("POST", "/api/recovery/restore", {"code": newer["code"]})[0] == 200
    assert owner.call("POST", "/api/recovery/revoke", {}) == (200, {"ok": True})
    assert owner.call("GET", "/api/profile")[1]["has_recovery_code"] is False


def test_wrong_and_malformed_codes_match_then_rate_limit(server):
    client = Client(server)
    wrong = client.call("POST", "/api/recovery/restore", {"code": "7K4M2-9QRTX-B3HNV-W8Z6D"})
    malformed = client.call("POST", "/api/recovery/restore", {"code": "nope"})
    empty = client.call("POST", "/api/recovery/restore", b"not json")
    assert wrong == malformed == empty == (400, {"error": "That code didn't work."})
    for _ in range(7):
        client.call("POST", "/api/recovery/restore", {"code": "x"})
    assert client.call("POST", "/api/recovery/restore", {"code": "x"}) == (429, {"error": "Too many tries. Wait a few minutes."})
    assert Client(server, peer="10.0.0.9").call("POST", "/api/recovery/restore", {"code": "x"})[0] == 400


def test_delete_and_export_include_the_profile(server):
    client = Client(server)
    client.call("PUT", "/api/profile", PROFILE)
    client.call("POST", "/api/recovery/create", {})
    assert client.call("GET", "/api/me/export")[1]["profile"] == PROFILE
    assert client.call("POST", "/api/me/delete", {"confirm": "delete"})[0] == 200
    assert server.community.profiles == {} and server.community.recovery == {}
