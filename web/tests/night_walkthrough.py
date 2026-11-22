"""Check every number and label on the "Your night" screen in Chromium against the in-process mock.

From the repo root: python web/tests/night_walkthrough.py [screenshot folder]
Route chips name every vehicle in order, chip fares are one way, the hero fare equals the
outbound fare plus the chosen way home (with the breakdown under More), the return tile follows
the return vehicle, and nothing is cut off at 320, 375, 390 and 430 px.
"""
import re
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mock_browser import LAUNCH_ARGS, ORIGIN, MockSite, make_server  # noqa: E402

MONEY = re.compile(r"\$(\d+\.\d\d)")


def plan(site, station):
    page = site.page
    page.goto(ORIGIN + "/?now=2026-11-21T21:12:00")
    picker = page.get_by_role("combobox", name="Start", exact=True)
    expect(picker).to_be_enabled()
    picker.fill(station)
    page.locator("#stationList [role=option]").first.click()
    page.locator("#spin").click()
    expect(page.locator("#pinNight")).to_be_visible(timeout=20000)
    expect(page.locator("#homeModes")).to_be_visible()
    return page


def dollars(text):
    return [float(value) for value in MONEY.findall(text)]


def check_layout(page, width):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "horizontal scroll"
    clipped = page.evaluate("""() => [...document.querySelectorAll(
        '#result h2, #result p, #result strong, .time-tile strong, .time-tile small, .tile-label, #tripTabs small, .route-chip-time')]
        .filter((node) => node.offsetParent && node.scrollWidth > node.clientWidth + 1).map((node) => node.id || node.className)""")
    assert not clipped, (width, clipped)
    for chip in page.locator("#tripTabs > button").all():
        names = chip.locator(".route-chip-vehicle")
        assert names.count() >= 1
        for index in range(names.count()):
            assert names.nth(index).bounding_box()["width"] >= 8, "every vehicle keeps a visible piece"
        assert chip.evaluate("(el) => el.scrollWidth <= el.clientWidth + 1")


def mixed_trip(browser, width, height, shots):
    site = MockSite(browser, width, height, make_server(bus_mode="mixed"))
    try:
        page = plan(site, "harvard")
        chips = page.locator("#tripTabs > button")
        expect(chips.first.locator(".route-chip-vehicles")).to_have_text("Red→Bus 66")
        expect(chips.first).to_have_attribute("aria-label", re.compile(r"^Red → Bus 66 · \d+:\d\d PM, \d+ min · \$2\.40 one way$"))
        for chip in chips.all():
            assert "round trip" not in chip.inner_text()
        expect(page.locator("#lastRideTitle")).to_have_text("Last T home")
        hero = page.locator("#shortFare").text_content()
        breakdown = page.locator("#fareBreakdown").text_content()
        assert hero == "Fares $4.80" and breakdown == "Fares: out $2.40 · home $2.40", (hero, breakdown)
        dish, total = dollars(page.locator("#price").text_content())[0], dollars(page.locator("#dishTotal").text_content())[0]
        assert round(dish + 4.80, 2) == total
        expect(page.locator("#dishTravel")).to_have_text(re.compile(r"^\d+ min there by Red → Bus 66$"))
        check_layout(page, width)
        page.screenshot(path=f"{shots}/night-mixed-{width}.png")
        # Walking home: one fare, the tile follows the walk, the mode button and the hero agree.
        page.locator("#homeModes button", has_text="Walk home").click()
        expect(page.locator("#lastRideTitle")).to_have_text("Walk home")
        expect(page.locator("#shortFare")).to_have_text("Fare $2.40")
        expect(page.locator("#fareBreakdown")).to_have_text("Fares: out $2.40 · home free")
        expect(page.locator("#lastTrainDetail")).to_have_text(re.compile(r"^Free · home about \d+:\d\d (AM|PM)$"))
        assert dollars(page.locator("#dishTotal").text_content())[0] == round(dish + 2.40, 2)
        check_layout(page, width)
        page.screenshot(path=f"{shots}/night-walk-home-{width}.png")
        # Green B then a bus out, a bus home: the hero is $4.10 and nothing says $4.80.
        page.evaluate("""() => {
            const bus = (line) => ({ mode: 'ride', vehicle: 'bus', line, depart_iso: selectedTrip.summary.depart_iso,
              arrive_iso: selectedTrip.summary.arrive_iso, from: {}, to: {} });
            selectedTrip = { ...selectedTrip, fare: 2.40, legs: [{ mode: 'ride', line: 'Green Line B',
              depart_iso: selectedTrip.summary.depart_iso, arrive_iso: selectedTrip.summary.arrive_iso, from: {}, to: {} }, bus('57')] };
            selectedHomeTrip = { ...selectedHomeTrip, fare: 1.70, legs: [bus('57')] };
            homeModeOverride = false;
            renderDishPrice(currentDish);
            const chip = document.createElement('button');
            chip.id = 'longChip';
            byId('tripTabs').append(chip);
            fillRouteChip(chip, selectedTrip, '9:17 PM', '24 min · $2.40 one way');
            const longer = document.createElement('button');
            longer.id = 'longerChip';
            byId('tripTabs').append(longer);
            fillRouteChip(longer, { legs: [...selectedTrip.legs, bus('66')] }, '9:17 PM', '31 min · $2.40 one way');
        }""")
        expect(page.locator("#shortFare")).to_have_text("Fares $4.10")
        expect(page.locator("#fareBreakdown")).to_have_text("Fares: out $2.40 · home $1.70")
        assert dollars(page.locator("#dishTotal").text_content())[0] == round(dish + 4.10, 2)
        expect(page.locator("#longChip .route-chip-vehicles")).to_have_text("Green B→Bus 57")
        expect(page.locator("#longChip")).to_have_attribute("aria-label", "Green B → Bus 57 · 9:17 PM, 24 min · $2.40 one way")
        expect(page.locator("#longerChip .route-chip-vehicles")).to_have_text("T→Bus 57→Bus 66")
        page.locator("#longerChip").scroll_into_view_if_needed()
        check_layout(page, width)
        assert not site.errors, site.errors
    finally:
        site.close()


def subway_only(browser, width, height, shots):
    site = MockSite(browser, width, height, make_server(bus_mode="subway"))
    try:
        page = plan(site, "kenm")
        expect(page.locator("#tripTabs > button").first.locator(".route-chip-vehicles")).to_have_text("T")
        expect(page.locator("#tripTabs > button").first.locator("small")).to_have_text(re.compile(r"^\d+ min · \$2\.40 one way$"))
        expect(page.locator("#lastRideTitle")).to_have_text("Last T home")
        expect(page.locator("#shortFare")).to_have_text("Fares $4.80")
        check_layout(page, width)
        page.screenshot(path=f"{shots}/night-subway-{width}.png")
        assert not site.errors, site.errors
    finally:
        site.close()


def bus_only(browser, width, height, shots):
    site = MockSite(browser, width, height, make_server(bus_mode="bus"))
    try:
        page = plan(site, "park street")
        expect(page.locator("#tripTabs > button").first.locator(".route-chip-vehicles")).to_have_text("Bus 66")
        expect(page.locator("#lastRideTitle")).to_have_text("Last bus home")
        expect(page.locator("#shortFare")).to_have_text("Fares $3.40")
        expect(page.locator("#fareBreakdown")).to_have_text("Fares: out $1.70 · home $1.70")
        expect(page.locator("#homeModes button", has_text="Ride home")).to_contain_text("$1.70")
        check_layout(page, width)
        page.screenshot(path=f"{shots}/night-bus-{width}.png")
        assert not site.errors, site.errors
    finally:
        site.close()


if __name__ == "__main__":
    shots = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
    with sync_playwright() as playwright:
        for width, height in [(320, 700), (375, 812), (390, 844), (430, 932)]:
            for step in (mixed_trip, subway_only, bus_only):
                # With --single-process, closing a context ends the browser: one browser per check.
                with playwright.chromium.launch(args=LAUNCH_ARGS) as browser:
                    step(browser, width, height, shots)
            print(f"Night screen passed at {width}x{height}")
