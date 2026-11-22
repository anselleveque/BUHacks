"""Walk through Round 32 (restaurant reports and developer controls) in Chromium against the in-process mock.

From the repo root: python web/tests/devpanel_walkthrough.py [screenshot folder]
Covers the hidden developer dialog (shortcut, ?dev=1, Escape, focus, live status, bottom sheet, 48 px targets),
simulated GPS for the start and check-in, the page-local ?now= clock, the motion override,
clearing report overrides, the saved-data reset with its confirmation, the --dev gate, and the "Restaurant is
closed" / "Fix restaurant timing" reports completed by a second diner (a second browser with its own cookie),
at 375x812 and 390x844. Requires Playwright and its Chromium browser. No socket is opened.
"""
import re
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mock_browser import LAUNCH_ARGS, ORIGIN, MockSite, make_server  # noqa: E402

START = "/?now=2026-11-21T21:12:00"
SHORTCUT = "Control+Alt+Shift+D"


def start(site, path=START):
    site.page.goto(ORIGIN + path)
    expect(site.page.get_by_role("combobox", name="Start", exact=True)).to_be_enabled()
    expect(site.page.locator("#spin")).to_be_enabled()
    return site.page


def no_page_overflow(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "horizontal scroll"


def dialog(page):
    return page.get_by_role("dialog", name="Developer controls")


def open_dev(page):
    page.keyboard.press(SHORTCUT)
    expect(dialog(page)).to_be_visible()
    expect(page.locator("#devTitle")).to_be_focused()


def close_dev(page):
    page.get_by_role("button", name="Close developer controls").click()
    expect(dialog(page)).to_be_hidden()


def check_sheet_layout(page, size):
    box = page.locator("#devDialog").bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= size[0] + 0.5, box
    assert abs(box["y"] + box["height"] - size[1]) <= 1, f"not a bottom sheet: {box}"
    small = page.evaluate("""() => [...document.querySelectorAll(`#devDialog button, #devDialog select,
        #devDialog input:not([type=checkbox]), #devDialog label.fare-switch`)]
        .filter((el) => el.offsetParent !== null)
        .map((el) => [el.id || el.textContent.trim().slice(0, 30), el.getBoundingClientRect().height])
        .filter(([, height]) => height < 47.5)""")
    assert not small, f"touch targets under 48 px: {small}"
    assert page.evaluate("[...document.querySelectorAll('#devDialog *')].every((el) => el.scrollWidth <= "
                         "el.closest('dialog').clientWidth + 1)"), "content wider than the sheet"


def plan(page, station="kenm"):
    picker = page.get_by_role("combobox", name="Start", exact=True)
    picker.fill(station)
    page.locator("#stationList [role=option]").first.click()
    page.locator("#spin").click()
    expect(page.locator("#selectedDetails")).to_be_visible(timeout=20000)
    expect(page.locator("#heroReport")).to_have_js_property("hidden", False, timeout=20000)
    not_now = page.get_by_role("button", name="Not now")
    if not_now.is_visible():
        not_now.click()
    return page.evaluate("[currentDish.place.id, currentDish.place.name]")


def open_report(page, choice):
    if not page.evaluate("byId('heroReport').closest('details').open"):
        page.locator("details.night-more > summary").click()
    page.locator("#heroReport").click()
    report = page.get_by_role("dialog", name="Report a problem")
    expect(report).to_be_visible()
    expect(page.locator("#reportMenuStatus")).to_have_text("")
    report.get_by_role("radio", name=choice).check()
    return report


def dev_dialog_basics(browser, size, shots):
    site = MockSite(browser, *size, server=make_server(dev=True))
    page = start(site)
    try:
        expect(dialog(page)).to_be_hidden()
        page.locator("#budget").focus()
        open_dev(page)
        expect(page.locator("#devServerNote")).to_be_hidden()
        page.screenshot(path=f"{shots}/devpanel-open-{size[0]}.png")
        check_sheet_layout(page, size)
        no_page_overflow(page)
        page.keyboard.press("Escape")
        expect(dialog(page)).to_be_hidden()
        expect(page.locator("#budget")).to_be_focused()
        open_dev(page)
        page.keyboard.press(SHORTCUT)  # the shortcut toggles it closed too
        expect(dialog(page)).to_be_hidden()

        # Location: apply as start, then "Use my location" and the failure results.
        open_dev(page)
        page.locator("#devGpsEnabled").check()
        expect(page.locator("#devStatus")).to_have_text("Simulated GPS is on.")
        page.locator("#devLatitude").fill("42.35001")
        page.locator("#devLongitude").fill("-71.12002")
        page.locator("#devAccuracy").fill("25")
        page.get_by_role("button", name="Apply as start").click()
        expect(page.locator("#devStatus")).to_have_text("Start set to 42.35001, -71.12002 (±25 m).")
        page.wait_for_function("locationPoint?.lat === 42.35001 && locationPoint?.lng === -71.12002")
        page.locator("#devLatitude").fill("123")
        page.get_by_role("button", name="Apply as start").click()
        expect(page.locator("#devStatus")).to_have_text(
            "Enter a valid latitude, longitude and accuracy. The start was not changed.")
        page.locator("#devLatitude").fill("42.35001")
        page.locator("#devGpsResult").select_option("denied")
        page.get_by_role("button", name="Apply as start").click()
        expect(page.locator("#devStatus")).to_have_text(
            "Simulated result: permission denied. The start was not changed.")
        page.screenshot(path=f"{shots}/devpanel-location-{size[0]}.png")
        close_dev(page)
        page.locator("#locate").click()
        expect(page.locator("#locationStatus")).to_have_text(
            "Location permission was denied. Choose a T station or try again.")
        open_dev(page)
        page.locator("#devGpsResult").select_option("unavailable")
        close_dev(page)
        page.locator("#locate").click()
        expect(page.locator("#locationStatus")).to_have_text(
            "Your device location couldn’t be found. Choose a T station or try again.")
        open_dev(page)
        page.locator("#devGpsResult").select_option("position")
        close_dev(page)
        page.locator("#locate").click()
        expect(page.locator("#locationStatus")).to_have_text("Using your simulated location.")

        # Motion override.
        open_dev(page)
        page.locator("#devMotion").select_option("full")
        expect(page.locator("html")).to_have_attribute("data-motion", "full")
        assert page.evaluate("reducedMotion") is False  # the context itself prefers reduced motion
        page.locator("#devMotion").select_option("reduce")
        expect(page.locator("#devStatus")).to_have_text("Motion: Reduce motion.")
        assert page.evaluate("reducedMotion") is True
        page.locator("#devMotion").select_option("system")
        expect(page.locator("html")).not_to_have_attribute("data-motion", re.compile(".*"))

        # Clock: ?now= reload, the dialog comes back with the result, then the real time.
        page.locator("#devClock").fill("2026-11-22T00:45")
        page.get_by_role("button", name="Apply time").click()
        page.wait_for_url(re.compile(r"now=2026-11-22T00%3A45%3A00"))
        expect(dialog(page)).to_be_visible()
        expect(page.locator("#devStatus")).to_have_text("Boston time set to 2026-11-22 00:45.")
        expect(page.locator("#demoTime")).to_have_text("Demo time")
        expect(page.locator("#nightClock")).to_have_text(re.compile(r"Sun 12:4[56] AM"))
        expect(page.locator("#devClock")).to_have_value(re.compile(r"2026-11-22T00:4[56]"))
        page.wait_for_timeout(300)
        assert any("now=2026-11-22T05%3A4" in path for _, path, _ in site.requests if "/api/places" in path)
        page.locator("#devClockSection").scroll_into_view_if_needed()
        page.screenshot(path=f"{shots}/devpanel-clock-{size[0]}.png")
        page.get_by_role("button", name="Use real time").click()
        page.wait_for_url(re.compile(r"^(?!.*now=).*$"))
        expect(page.locator("#devStatus")).to_have_text("Using the real time.")
        expect(page.locator("#demoTime")).to_be_hidden()
        expect(page.get_by_role("button", name="Use real time")).to_be_disabled()
        page.evaluate("setDevServer({dev: {enabled: true, allow_now: false}})")
        expect(page.locator("#devClockNote")).to_contain_text("ignores it for plans until it runs with --allow-now")
        close_dev(page)

        # ?dev=1 opens it on load.
        page.goto(ORIGIN + START + "&dev=1")
        expect(dialog(page)).to_be_visible()
        assert not site.errors, site.errors
    finally:
        site.close()


def reports_with_two_diners(browser, size, shots, other_browser):
    """Two diners: two browsers (Chromium runs single-process here), so two separate cookies."""
    server = make_server(dev=True)
    first, second = MockSite(browser, *size, server=server), MockSite(other_browser, *size, server=server)

    def listed():
        return [p["id"] for p in server.community.places({}, server.now_source(), None)["places"]]

    try:
        page = start(first)
        place_id, name = plan(page)
        # Timing: an invalid value shows the server's error; a valid one waits for a second diner.
        report = open_report(page, "Fix restaurant timing")
        expect(page.locator("#reportItemField")).to_be_hidden()
        expect(page.locator("#timingPlaceForm")).to_be_visible()
        expect(page.locator("#priceForm")).to_be_hidden()
        page.locator("#observedTiming").fill("<b>late</b>")
        report.get_by_role("button", name="Submit opening hours").click()
        expect(page.locator("#priceStatus")).to_have_text("Use plain text for the opening hours.")
        page.locator("#observedTiming").fill("Daily 5 PM–2 AM")
        report.get_by_role("button", name="Submit opening hours").click()
        expect(page.locator("#priceStatus")).to_have_text("Thanks. One more matching report updates the hours.")
        expect(page.locator("#reportPlaceDetails")).to_have_text("1 of 2 matching reports of new hours")
        page.screenshot(path=f"{shots}/report-timing-{size[0]}.png")
        # Closed: the first report waits for a second diner too.
        report.get_by_role("radio", name="Restaurant is closed").check()
        expect(page.locator("#closedPlaceForm")).to_be_visible()
        expect(page.locator("#timingPlaceForm")).to_be_hidden()
        report.get_by_role("button", name="Report restaurant closed").click()
        expect(page.locator("#priceStatus")).to_have_text("Thanks. One more report hides this restaurant.")
        expect(page.locator("#reportPlaceDetails")).to_contain_text("1 of 2 reports that it’s closed")
        page.screenshot(path=f"{shots}/report-closed-{size[0]}.png")
        no_page_overflow(page)
        report.get_by_role("button", name="Close report").click()
        expect(report).to_be_hidden()
        assert first.cookie and place_id in listed()

        other = start(second)
        assert plan(other) == [place_id, name]
        report = open_report(other, "Fix restaurant timing")
        expect(other.locator("#reportPlaceDetails")).to_contain_text("1 of 2 matching reports of new hours")
        other.locator("#observedTiming").fill("daily 5 pm–2 am")
        report.get_by_role("button", name="Submit opening hours").click()
        expect(other.locator("#priceStatus")).to_have_text("Hours updated after 2 matching reports.")
        expect(other.locator("#reportPlaceDetails")).to_contain_text("Hours from diners: daily 5 pm–2 am")
        assert server.community.place_state(name)["community_timing"] == "daily 5 pm–2 am"
        assert next(p for p in server.community.places({}, server.now_source(), None)["places"]
                    if p["id"] == place_id)["hours_text"] == "daily 5 pm–2 am"
        report.get_by_role("radio", name="Restaurant is closed").check()
        expect(other.locator("#reportPlaceDetails")).to_contain_text("1 of 2 reports that it’s closed")
        report.get_by_role("button", name="Report restaurant closed").click()
        expect(other.locator("#priceStatus")).to_have_text("Hidden after 2 reports that it’s closed.")
        assert second.cookie and second.cookie != first.cookie
        report.get_by_role("button", name="Close report").click()
        # The hero re-rolls away from the closed place and the place leaves every list.
        other.wait_for_function(f"!pickRequest && currentDish?.place.id !== {place_id}", timeout=20000)
        assert any("exclude=" in path for _, path, _ in second.requests if path.startswith("/api/pick"))
        other.locator("#browseTab").click()
        expect(other.locator("#browseView")).to_be_visible()
        expect(other.locator("#browseView")).not_to_contain_text(name, timeout=10000)
        other.screenshot(path=f"{shots}/report-closed-hidden-{size[0]}.png")
        assert place_id not in listed()

        # Developer controls clear the overrides; the closed place is listed again.
        open_dev(other)
        other.get_by_role("button", name="Clear all closed/timing overrides").click()
        expect(other.locator("#devStatus")).to_have_text("Cleared 4 restaurant reports.")
        assert server.community.place_reports == {} and place_id in listed()
        expect(other.locator("#browseView")).to_contain_text(name, timeout=10000)
        assert not first.errors and not second.errors, (first.errors, second.errors)
    finally:
        first.close()
        second.close()


def saved_data_reset(browser, size, shots):
    server = make_server(dev=True)
    site = MockSite(browser, *size, server=server)
    page = start(site)
    try:
        page.evaluate("localStorage.setItem('midnight-munch-walk-ok', '20'); localStorage.setItem('other-app', 'x')")
        assert page.evaluate("""fetch('/api/place-report', {method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({place_id: 101, action: 'closed'})}).then((response) => response.status)""") == 200
        open_dev(page)
        reset = page.get_by_role("button", name="Reset saved data…")
        reset.click()
        expect(page.locator("#devStatus")).to_have_text("Everything is set to keep. Uncheck what you want to delete.")
        page.locator("#devKeepCommunity").uncheck()
        reset.click()
        expect(page.locator("#devConfirm")).to_be_visible()
        expect(page.locator("#devConfirmText")).to_contain_text("every diner’s check-ins")
        expect(page.get_by_role("button", name="Cancel")).to_be_focused()
        page.screenshot(path=f"{shots}/devpanel-reset-confirm-{size[0]}.png")
        check_sheet_layout(page, size)
        page.get_by_role("button", name="Cancel").click()
        expect(page.locator("#devConfirm")).to_be_hidden()
        expect(page.locator("#devStatus")).to_have_text("Reset canceled.")
        assert server.community.place_reports
        page.locator("#devKeepBrowser").uncheck()
        reset.click()
        page.get_by_role("button", name="Yes, reset").click()
        expect(dialog(page)).to_be_visible(timeout=15000)
        expect(page.locator("#devStatus")).to_have_text("Saved data reset.")
        assert server.community.place_reports == {} and server.community.reports == {}
        assert page.evaluate("localStorage.getItem('midnight-munch-walk-ok')") is None
        assert page.evaluate("localStorage.getItem('other-app')") == "x"
        # A failure keeps the dialog open and shows the server's message.
        server.args.dev = False
        page.get_by_role("button", name="Clear all closed/timing overrides").click()
        expect(page.locator("#devStatus")).to_have_text("Start the server with --dev to use developer resets.")
        assert not site.errors, site.errors
    finally:
        site.close()


def dev_off(browser, size, shots):
    site = MockSite(browser, *size)  # the mock without --dev
    page = start(site)
    try:
        open_dev(page)
        expect(page.locator("#devServerNote")).to_have_text("Resets need the server started with --dev.")
        for name in ("Clear all closed/timing overrides", "Reset saved data…"):
            expect(page.get_by_role("button", name=name)).to_be_disabled()
        expect(page.locator("#devKeepCommunity")).to_be_disabled()
        expect(page.get_by_role("button", name="Apply as start")).to_be_enabled()
        page.screenshot(path=f"{shots}/devpanel-dev-off-{size[0]}.png")
        no_page_overflow(page)
        assert not site.errors, site.errors
    finally:
        site.close()


def main(shots):
    Path(shots).mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        for size in [(375, 812), (390, 844)]:
            for step in (dev_dialog_basics, saved_data_reset, dev_off):
                with playwright.chromium.launch(args=LAUNCH_ARGS) as browser:
                    step(browser, size, shots)
            with playwright.chromium.launch(args=LAUNCH_ARGS) as browser, \
                    playwright.chromium.launch(args=LAUNCH_ARGS) as other_browser:
                reports_with_two_diners(browser, size, shots, other_browser)
            print(f"Developer controls walkthrough passed at {size[0]}x{size[1]}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp")
