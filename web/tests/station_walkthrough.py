"""Run the station picker in Chromium against the in-process mock.

From the repo root: python web/tests/station_walkthrough.py
Requires Playwright and its Chromium browser. Requests use the mock HTTP
handler without opening a localhost socket.
"""
from datetime import datetime
from http.client import HTTPResponse
import importlib.util
from io import BytesIO
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("mock_api", ROOT / "tests/mock_api.py")
mock = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mock)
ORIGIN = "https://midnight-munch.test"


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


def walkthrough(browser, width, height):
    args = SimpleNamespace(allow_now=True, slow=0, transit_down=False, routing_down=False,
                           stale_nearby=False, station_search_fail=False, stations_unavailable=False)
    server = SimpleNamespace(args=args, community=mock.MockCommunity(), community_lock=Lock(), nearby_lock=Lock(),
                             nearby_fail_once=False, now_source=lambda: datetime(2026, 11, 21, 20, tzinfo=mock.BOSTON))
    context = browser.new_context(viewport={"width": width, "height": height}, has_touch=True, is_mobile=True,
                                  reduced_motion="reduce", geolocation={"latitude": 42.34895, "longitude": -71.09517},
                                  permissions=["geolocation"])
    page = context.new_page()
    errors = []
    requested = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def intercept(route):
        url = urlsplit(route.request.url)
        if url.netloc != urlsplit(ORIGIN).netloc:
            route.abort()
            return
        path = url.path + (f"?{url.query}" if url.query else "")
        requested.append(path)
        conn = MemorySocket(f"GET {path} HTTP/1.0\r\nHost: localhost\r\n\r\n".encode())
        QuietHandler(conn, ("127.0.0.1", 1234), server)
        response = HTTPResponse(MemorySocket(conn.output.getvalue()))
        response.begin()
        route.fulfill(status=response.status, headers={"content-type": response.getheader("Content-Type")}, body=response.read())

    page.route("**/*", intercept)
    picker = page.get_by_role("combobox", name="Start", exact=True)
    options = page.get_by_role("option").and_(page.locator("#stationList [role=option]"))

    def search(text):
        picker.fill(text)
        expect(picker).to_have_attribute("aria-expanded", "true")
        return options

    def choose(text, name):
        search(text).filter(has_text=name).click()
        expect(picker).to_have_attribute("aria-expanded", "false")

    try:
        page.goto(ORIGIN)
        expect(picker).to_be_enabled()
        expect(picker).to_have_attribute("aria-controls", "stationList")
        expect(picker).to_have_attribute("aria-autocomplete", "list")
        for query, found in [
            ("kenm", "Kenmore"), ("KÉNM!", "Kenmore"), ("kenmroe", "Kenmore"),
            ("kenmre", "Kenmore"), ("kenmorx", "Kenmore"),
            ("bu", "Boston University Central"), ("bu central", "Boston University Central"),
            ("harvard-sq", "Harvard"), ("m.g.h.", "Charles/MGH"), ("mit", "Kendall/MIT"),
            ("southie", "Broadway"), ("orange", "Back Bay"),
        ]:
            expect(search(query).filter(has_text=found).first).to_be_visible()
        expect(search("kenm").first.locator("strong").first).to_have_text("Kenm")
        expect(search("central")).to_have_count(3)
        assert options.all_text_contents() == ["CentralRed", "Central AvenueRed", "Boston University CentralGreen"]
        expect(search("bu")).to_have_count(2)
        assert options.all_text_contents() == ["Boston University CentralGreen", "Boston University EastGreen"]
        expect(search("southie")).to_have_count(2)
        assert options.all_text_contents() == ["AndrewRed", "BroadwayRed"]
        expect(search("green")).to_have_count(8)
        assert all("Green" in name for name in options.all_text_contents())
        expect(search("red line")).to_have_count(8)
        assert all("Red" in name for name in options.all_text_contents())
        expect(page.locator("#stationAnnouncement")).to_have_text("8 stations, use up and down")
        picker.press("ArrowDown")
        first_id = options.first.get_attribute("id")
        expect(picker).to_have_attribute("aria-activedescendant", first_id)
        expect(options.first).to_have_attribute("aria-selected", "true")
        picker.press("ArrowDown")
        picker.press("End")
        expect(options.last).to_have_attribute("aria-selected", "true")
        picker.press("Home")
        picker.press("ArrowUp")
        expect(options.last).to_have_attribute("aria-selected", "true")
        picker.press("Escape")
        expect(picker).to_have_value("")
        search("central")
        picker.press("ArrowDown")
        picker.press("Enter")
        expect(picker).to_have_value("Central (Red)")
        search("kenm")
        picker.press("Enter")  # Only match, without first moving into the list.
        expect(picker).to_have_value("Kenmore (Green)")
        expect(page.locator("#travelChoices input").first).to_be_enabled()
        page.wait_for_function("locationPoint?.id === 'place-kencl' && locationPoint.lat === 42.348949")
        search("harvard")
        picker.press("ArrowDown")
        picker.press("Tab")
        expect(picker).to_have_value("Kenmore (Green)")
        expect(page.get_by_role("button", name="Clear start station")).to_be_focused()
        page.get_by_role("button", name="Clear start station").press("Enter")
        expect(picker).to_be_focused()
        expect(picker).to_have_value("")
        expect(page.locator("#travelChoices input").first).to_be_disabled()
        expect(search("kexm")).to_have_count(0)
        search("<img>")
        expect(page.locator("#stationNoMatch")).to_have_text("No station matches '<img>'. Try a line or a neighborhood.")
        assert page.locator("#stationNoMatch img").count() == 0
        # Simulate paste as an uncluttered replacement input.
        picker.press("ControlOrMeta+a")
        page.keyboard.insert_text("BU Central")
        expect(options).to_have_count(1)
        picker.press("Enter")
        expect(picker).to_have_value("Boston University Central (Green)")

        for query, name in [("andrew", "Andrew"), ("broadway", "Broadway"), ("harvard sq", "Harvard"),
                            ("mit", "Kendall/MIT"), ("mgh", "Charles/MGH")]:
            choose(query, name)
        page.get_by_role("button", name="Profile", exact=True).click()
        page.locator("#default-station").select_option("place-bucen")
        page.get_by_role("button", name="Use as start").click()
        expect(picker).to_have_value("Boston University Central (Green)")
        page.reload()
        expect(picker).to_have_value("Boston University Central (Green)")
        search("some uncommitted text")
        page.get_by_role("button", name="Use my location").click()
        expect(picker).to_have_value("")
        expect(page.locator("#locationStatus")).to_have_text("Using your device location.")
        picker.focus()
        expect(page.get_by_role("group", name="Home station").get_by_role("option")).to_have_count(1)
        expect(page.get_by_role("group", name="Recent starts").get_by_role("option")).to_have_count(5)
        expect(page.get_by_role("group", name="Nearest stations").get_by_role("option")).to_have_count(5)
        expect(search("green").first).to_contain_text("Kenmore")

        page.set_viewport_size({"width": width, "height": height - 300})
        expect(page.locator("#stationKeyboardLayout")).to_have_class("layout station-keyboard")
        box = picker.bounding_box()
        list_box = page.locator("#stationList").bounding_box()
        assert box["y"] >= 0 and box["y"] + box["height"] < height - 300
        assert list_box["y"] + list_box["height"] <= height - 300
        assert options.first.bounding_box()["height"] >= 48
        assert page.locator("#stationList").evaluate("(el) => el.scrollHeight > el.clientHeight")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert picker.evaluate("(el) => parseInt(getComputedStyle(el).fontSize)") >= 16
        options.first.tap()
        expect(picker).to_have_value("Kenmore (Green)")
        expect(picker).not_to_be_focused()
        assert not errors, errors
        page.set_viewport_size({"width": width, "height": height})

        args.station_search_fail = True
        page.reload()
        fallback = page.locator("#station")
        expect(fallback).to_be_visible()
        expect(fallback).to_be_enabled()
        assert "/data/stations.json?fallback=1" in requested
        fallback.select_option("place-kencl")
        page.wait_for_function("locationPoint?.id === 'place-kencl'")
        args.stations_unavailable = True
        page.reload()
        expect(fallback).to_be_enabled()
        expect(page.locator("#stationFallbackStatus")).to_contain_text("saved in this browser")
        page.evaluate("localStorage.removeItem('midnight-munch-station-catalog')")
        page.reload()
        expect(fallback).to_be_disabled()
        expect(page.get_by_role("button", name="Retry loading stations")).to_be_visible()
        args.stations_unavailable = args.station_search_fail = False
        page.get_by_role("button", name="Retry loading stations").click()
        expect(picker).to_be_enabled()
        assert not errors, errors
    finally:
        context.close()


if __name__ == "__main__":
    with sync_playwright() as playwright:
        for viewport in [(375, 812), (390, 844)]:
            with playwright.chromium.launch(args=[
                "--single-process", "--disable-gpu", "--no-zygote", "--disable-software-rasterizer"
            ]) as browser:
                walkthrough(browser, *viewport)
            print(f"Station picker passed at {viewport[0]}×{viewport[1]}")
