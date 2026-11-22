import re
from datetime import datetime
from http.client import HTTPResponse
from io import BytesIO
from threading import Lock
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from mock_api import BOSTON, WEB, Handler, MockCommunity


def test_leaflet_html_uses_string_literals():
    # Keep HTML options literal across the application. Dynamic marker content
    # belongs in DOM nodes so that it never goes through an HTML parser.
    literal = re.compile(r"""(?:"[^"\n]*"|'[^'\n]*'|`[^`\n]*`)\s*[,}]""")
    for path in WEB.glob("*.js"):
        source = path.read_text()
        for option in re.finditer(r"""\bhtml\s*:|["']html["']\s*:""", source):
            value = source[option.end():].lstrip()
            match = literal.match(value)
            location = f"{path.name}:{source.count(chr(10), 0, option.start()) + 1}"
            assert "instanceof HTMLElement" not in value.splitlines()[0], location
            assert match and "${" not in match[0], location


class MockConnection:
    def __init__(self, request):
        self.request = BytesIO(request)
        self.response = BytesIO()

    def makefile(self, *_args):
        return self.request

    def sendall(self, data):
        self.response.write(data)


def route_mock(route, server):
    url = urlsplit(route.request.url)
    if url.netloc != "bite.test":
        route.abort()
        return
    target = url.path + (f"?{url.query}" if url.query else "")
    connection = MockConnection(f"GET {target} HTTP/1.0\r\nHost: bite.test\r\n\r\n".encode())
    Handler(connection, ("127.0.0.1", 1234), server)
    response = HTTPResponse(SimpleNamespace(makefile=lambda *_: BytesIO(connection.response.getvalue())))
    response.begin()
    body = response.read()
    if url.path == "/vendor/leaflet/leaflet.js":
        # The checkout uses newer Leaflet. Enforce the older DivIcon input
        # contract while exercising the normal rendering implementation.
        body += b"""
const createDivIcon = L.DivIcon.prototype.createIcon;
L.DivIcon.prototype.createIcon = function (oldIcon) {
  if (typeof this.options.html !== "string") throw new Error("Expected string icon HTML");
  return createDivIcon.call(this, oldIcon);
};
"""
    route.fulfill(status=response.status, content_type=response.getheader("Content-Type"), body=body)


def unique_ride_stops(*itineraries):
    return {
        (leg[end]["lat"], leg[end]["lng"])
        for itinerary in itineraries if itinerary
        for leg in itinerary["legs"] if leg["mode"] == "ride"
        for end in ("from", "to")
    }


@pytest.mark.parametrize("width,height", [(1280, 800), (375, 812)])
def test_mixed_route_marker_icons(width, height):
    playwright = pytest.importorskip("playwright.sync_api")
    now = datetime(2026, 11, 21, 20, tzinfo=BOSTON)
    server = SimpleNamespace(
        args=SimpleNamespace(allow_now=True, slow=0, transit_down=False, routing_down=False,
                             stale_nearby=False, bus_mode="mixed",
                             fixed_departure=("20:06", "20:21", "20:36")),
        now_source=lambda: now, community=MockCommunity(), community_lock=Lock(),
        nearby_lock=Lock(), nearby_fail_once=False,
    )
    with playwright.sync_playwright() as session, session.chromium.launch(
        args=["--single-process", "--disable-gpu", "--no-zygote", "--disable-software-rasterizer"]
    ) as browser:
        page = browser.new_page(viewport={"width": width, "height": height})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", lambda route: route_mock(route, server))
        page.clock.set_system_time(now)
        page.goto("http://bite.test/?now=20:00")
        playwright.expect(page.locator("#spin")).to_be_enabled()
        page.locator("#anytimeButton").click()
        page.locator("#station").select_option("place-harsq")
        page.locator("#budget").fill("24")
        page.locator("#spin").click()
        page.wait_for_function("selectedTrip && selectedHomeTrip")
        playwright.expect(page.locator("#pinNight")).to_be_visible()
        playwright.expect(page.locator("#pinNight")).to_be_enabled()
        outbound = page.evaluate("selectedTrip")
        assert {leg.get("vehicle", "subway") for leg in outbound["legs"] if leg["mode"] == "ride"} == {"subway", "bus"}
        assert any(leg["mode"] == "walk" for leg in outbound["legs"])
        count = len(unique_ride_stops(outbound))
        playwright.expect(page.locator(".leaflet-map-pane .pin.route-stop")).to_have_count(count)
        playwright.expect(page.locator(".leaflet-map-pane .pin.route-stop .map-marker-face > svg")).to_have_count(count)
        picked = page.locator(".leaflet-map-pane .pin.selected")
        playwright.expect(picked.locator(".map-marker-face.picked > svg.icon-star")).to_have_count(1)
        playwright.expect(picked).to_have_attribute("aria-label", re.compile("Your pick"))
        playwright.expect(picked).to_have_attribute("tabindex", "0")
        assert picked.evaluate("(node) => [node.offsetWidth, node.offsetHeight]") == [44, 44]
        assert picked.locator(".map-marker-face").evaluate("(node) => node.offsetWidth") == 26
        assert page.locator(".leaflet-map-pane .pin.user .map-marker-face:empty").count() == 1
        paths = page.locator(".pin.route-stop svg path").evaluate_all(
            "(paths) => paths.map((path) => path.getAttribute('d'))"
        )
        assert set(paths) == set(page.evaluate("[iconPaths.train, iconPaths.bus]"))
        assert not re.search(r"\[object|\b(?:undefined|NaN)\b", page.locator("body").text_content())

        page.locator("#pinNight").click()
        trip = page.evaluate('JSON.parse(localStorage.getItem("midnight-munch-pinned-trip"))')
        count = len(unique_ride_stops(trip["outbound"], trip["home"])) + 1
        playwright.expect(page.locator(".leaflet-map-pane .pin.pinned-marker")).to_have_count(count)
        playwright.expect(page.locator(".leaflet-map-pane .pin.pinned-marker .map-marker-face > svg")).to_have_count(count)
        restaurant = page.locator('.pinned-marker[title^="Pinned restaurant:"] svg path')
        arrival_vehicle = [leg.get("vehicle", "subway") for leg in outbound["legs"] if leg["mode"] == "ride"][-1]
        icon_name = "bus" if arrival_vehicle == "bus" else "train"
        playwright.expect(restaurant).to_have_attribute("d", page.evaluate(f"iconPaths.{icon_name}"))
        assert "[object" not in page.locator(".leaflet-map-pane").text_content()
        assert not re.search(r"\[object|\b(?:undefined|NaN)\b", page.locator("body").text_content())

        # Exercise the live place classification and stamp badge independently of the fixture clock.
        faces = page.evaluate("""() => {
            const place = { ...pickedPlace, has_menu: true, open_now: true, open_late: false };
            return [
                {},
                { open_late: true, stamped: true },
                { open_late: true, open_now: false },
                { has_menu: false }
            ].map((overrides) => {
                const pin = placeMarker({ ...place, ...overrides }).addTo(map);
                const face = pin.getElement().firstElementChild;
                const result = [face.classList[1], face.offsetWidth, face.textContent,
                    face.querySelector("svg") !== null, face.lastElementChild.classList[1]];
                pin.remove();
                return result;
            });
        }""")
        assert faces[:2] == [["open-place", 14, "", False, "available"], ["late", 20, "L", False, "earned"]]
        assert [face[:4] for face in faces[2:]] == [["closed-place", 14, "", False], ["no-menu", 14, "", False]]

        # The string fallback is inserted as text even when it looks like markup.
        assert page.evaluate("""() => {
            const symbol = '<img src=x onerror=alert(1)>';
            const pin = marker(locationPoint, "test-pin", symbol).addTo(map);
            const icon = pin.getElement();
            const safe = icon.textContent === symbol && !icon.querySelector("img");
            pin.remove();
            return safe;
        }""")
        assert not errors, errors
        page.close()
