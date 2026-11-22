"""Walk through developer GPS and two stamps at both mobile sizes.

Run from the repo root. Defaults to the in-process mock. To audit the HTTP
server, pass --url http://127.0.0.1:8899 and start a fresh demo in scenario 9
before each run. Use --width to run just one viewport against fresh demo data.
"""
import argparse
import re

from playwright.sync_api import expect, sync_playwright

from devpanel_walkthrough import close_dev, open_dev
from mock_browser import LAUNCH_ARGS, ORIGIN, MockSite


def browse(page):
    page.locator("#browseTab").click()
    expect(page.locator("#placeList .restaurant").first).to_be_visible()
    page.wait_for_function("!byId('mapStatus').classList.contains('skeleton')")


def check_gps(page, place, origin):
    expect(page.locator("#devGpsEnabled")).to_be_checked()
    expect(page.locator("#devLatitude")).to_have_value(str(place["lat"]))
    expect(page.locator("#devLongitude")).to_have_value(str(place["lng"]))
    expect(page.locator("#devAccuracy")).to_have_value("10")
    expect(page.locator("#devGpsResult :checked")).to_have_text("Position available")
    expect(page.locator("#devStatus")).to_have_text(
        f"Moved to {place['name']}. Log restaurant will accept check-in.")
    assert page.evaluate("[station.value, locationPoint]") == origin


def select_place(page, place):
    page.locator(f'#placeList button[data-place-id="{place["id"]}"]').click()
    expect(page.locator("#selectedName")).to_contain_text(place["name"])
    expect(page.locator("#nightLoading")).to_be_hidden(timeout=20000)
    more = page.locator("details.night-more")
    if not more.evaluate("(details) => details.open"):
        more.locator(":scope > summary").click()
    expect(page.locator("#logVisit")).to_be_enabled()


def check_in(page, place, visit_points, total_points):
    with page.expect_response(lambda response: response.url.endswith("/api/visit")) as response:
        page.get_by_role("button", name="Log restaurant", exact=True).click()
    visit = response.value
    assert visit.status == 201, visit.status
    payload = {"place_id": place["id"], "lat": place["lat"], "lng": place["lng"], "accuracy_m": 10}
    assert visit.request.post_data_json == payload
    expect(page.locator("#profileScore")).to_have_text(f"{visit_points} pts")
    expect(page.locator("#selectedName .stamp")).to_have_text("Stamp available")

    page.locator("#logFood").click()
    options = page.locator("#foodItems select > option").filter(has_text=re.compile(r" · "))
    expect(options.first).to_be_attached()
    page.locator("#foodItems select").select_option(options.first.get_attribute("value"))
    page.locator("#saveFood").click()
    expect(page.locator("#foodStatus")).to_have_text("Food saved. Stamp earned.")
    page.get_by_role("button", name="Close food log").click()
    not_now = page.get_by_role("button", name="Not now")
    if not_now.is_visible():
        not_now.click()
    expect(page.locator("#profileScore")).to_have_text(f"{total_points} pts")
    expect(page.locator("#selectedName .stamp")).to_have_text("Stamp earned")
    expect(page.locator("#logVisit")).to_be_disabled()
    expect(page.locator("#logStatus")).to_have_text("You already checked in here. Stamp earned.")

    # A stale second request must not create another visit or award points.
    duplicate = page.evaluate("""async (payload) => {
      const response = await fetch("/api/visit", {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)
      });
      return [response.status, (await response.json()).points.total];
    }""", payload)
    assert duplicate == [200, total_points], duplicate


def verify_stamp(page, place, count, points):
    page.locator("#profileOpen").click()
    expect(page.locator("#stampList li")).to_have_count(count)
    expect(page.locator("#stampList li").filter(has_text=place["name"])).to_contain_text("Stamp earned")
    expect(page.locator("#levelSummary")).to_contain_text(f"{points} pts")
    page.locator("#profileDialog [data-close]").click()

    browse(page)
    row = page.locator(f'#placeList button[data-place-id="{place["id"]}"] .stamp')
    expect(row).to_have_text("Stamp earned")
    page.locator("#openMap").click()
    pin = page.locator(".leaflet-marker-icon").filter(
        has=page.locator(".map-stamp-dot.earned"))
    expect(pin.and_(page.get_by_label(re.compile(re.escape(place["name"]) + r", .*Stamp earned")))).to_be_attached()
    page.locator("#backFromMap").click()


def walkthrough(page, url, *, mock=False):
    page.goto(url)
    if mock:
        start = page.get_by_role("combobox", name="Start", exact=True)
        start.fill("kenm")
        page.locator("#stationList [role=option]").first.click()
    else:
        page.wait_for_function("window.midnightDemo?.state && station.value && locationPoint")
    browse(page)
    origin = page.evaluate("[station.value, locationPoint]")
    assert origin[0]
    places = page.evaluate("nearbyPlaces.filter(place => hasCoordinates(place) && place.has_menu !== false)")
    first, second = places[:2]

    open_dev(page)
    picker = page.get_by_role("combobox", name="Restaurant", exact=True)
    expect(picker.locator("option").first).to_have_text("Choose a restaurant")
    names = picker.locator("option").all_text_contents()[1:]
    assert names == page.evaluate("(names) => names.sort((a,b) => a.localeCompare(b))", names)
    picker.select_option(str(first["id"]))
    check_gps(page, first, origin)
    close_dev(page)

    select_place(page, first)
    open_dev(page)
    expect(page.locator("#devRestaurant")).to_be_hidden()
    page.locator("#devGpsResult").select_option("denied")
    page.get_by_role("button", name="Move me to the restaurant").click()
    check_gps(page, first, origin)
    close_dev(page)
    check_in(page, first, 10, 30)
    verify_stamp(page, first, 1, 30)

    select_place(page, second)
    open_dev(page)
    page.get_by_role("button", name="Move me to the restaurant").click()
    check_gps(page, second, origin)
    close_dev(page)
    check_in(page, second, 40, 60)
    verify_stamp(page, second, 2, 60)

    open_dev(page)
    page.get_by_role("button", name="Back to my real location").click()
    expect(page.locator("#devGpsEnabled")).not_to_be_checked()
    expect(page.locator("#devStatus")).to_have_text("Using your real location again.")
    assert page.evaluate("window.midnightMunchDev.position()") is None
    assert page.evaluate("[station.value, locationPoint]") == origin
    close_dev(page)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url")
    parser.add_argument("--width", type=int, choices=[375, 390])
    args = parser.parse_args()
    with sync_playwright() as playwright:
        for width, height in [(375, 812), (390, 844)]:
            if args.width and width != args.width:
                continue
            with playwright.chromium.launch(args=LAUNCH_ARGS) as browser:
                if args.url:
                    page = browser.new_page(viewport={"width": width, "height": height},
                                            has_touch=True, is_mobile=True, reduced_motion="reduce")
                    walkthrough(page, args.url)
                else:
                    site = MockSite(browser, width, height)
                    try:
                        walkthrough(site.page, ORIGIN, mock=True)
                        assert not site.errors, site.errors
                    finally:
                        site.close()
            print(f"Developer GPS and stamps passed at {width}x{height}")


if __name__ == "__main__":
    main()
