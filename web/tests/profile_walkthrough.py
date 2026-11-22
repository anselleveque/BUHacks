"""Walk through Round 30 (save my profile) in Chromium against the in-process mock.

From the repo root: python web/tests/profile_walkthrough.py
Covers sync and conflicts, the three-step "Create your profile" sheet with the show-once recovery
code, restore with every error including the rate limit, the five nudges with their cooldowns,
the completeness meter, focus, live regions, keyboard use and phone layouts (375x812, 390x844).
Requires Playwright and its Chromium browser. No socket is opened.
"""
import json
import re
import sys
from datetime import timedelta
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mock_browser import LAUNCH_ARGS, ORIGIN, MockSite, make_server  # noqa: E402

CODE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{5}(-[0-9A-HJKMNP-TV-Z]{5}){3}$")
SERVER_PROFILE = {"display_name": "Laptop Ana", "avatar": "🦉", "color": "moss", "bio": "",
                  "defaults": {"home_station_id": "place-kencl", "budget": 15}}
DAY = 24 * 60 * 60 * 1000


def start(site, path="/?now=2026-11-21T21:12:00"):
    site.page.goto(ORIGIN + path)
    expect(site.page.get_by_role("combobox", name="Start", exact=True)).to_be_enabled()
    return site.page


def open_profile(page):
    page.get_by_role("button", name="Profile", exact=True).click()
    expect(page.locator("#profileDialog")).to_be_visible()


def storage_text(page):
    return page.evaluate("JSON.stringify({...localStorage}) + JSON.stringify({...sessionStorage})")


def no_page_overflow(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "horizontal scroll"


def choose_start(page, station="kenm"):
    picker = page.get_by_role("combobox", name="Start", exact=True)
    picker.fill(station)
    page.locator("#stationList [role=option]").first.click()


def plan_night(page, station="kenm"):
    choose_start(page, station)
    page.locator("#spin").click()
    expect(page.locator("#selectedDetails")).to_be_visible(timeout=20000)
    expect(page.locator("#pinNight")).to_be_visible(timeout=20000)


def nudge_state(page, **values):
    page.evaluate("(values) => localStorage.setItem('midnight-munch-nudges', JSON.stringify(values))", values)


def new_session(page):
    page.evaluate("sessionStorage.clear()")
    page.reload()
    expect(page.get_by_role("combobox", name="Start", exact=True)).to_be_enabled()


def onboarding_with_code(browser, size, shots):
    site = MockSite(browser, *size)
    page = start(site)
    try:
        open_profile(page)
        expect(page.locator("#profileMeterLabel")).to_have_text("Profile 0 percent complete")
        expect(page.locator("#recoveryStatusLine")).to_have_text("No recovery code yet")
        create = page.get_by_role("button", name="Create your profile")
        create.focus()
        create.press("Enter")
        sheet = page.get_by_role("dialog", name="Create your profile")
        expect(sheet).to_be_visible()
        expect(page.locator("#onboardStep1Title")).to_be_focused()
        expect(page.locator("#onboardingProgress")).to_have_text("Step 1 of 3")
        expect(page.locator("#onboardingSheet").get_by_role("button", name="Back", exact=True)).to_be_hidden()
        page.locator("#onboardName").fill("Ana <b>&amp;")
        page.locator("#onboardingSheet").get_by_role("radio", name="Owl (Night)").check()
        page.screenshot(path=f"{shots}/onboard-1-{size[0]}.png")
        page.keyboard.press("Tab")
        page.locator("#onboardingSheet").get_by_role("button", name="Next", exact=True).click()
        expect(page.locator("#onboardStep2Title")).to_be_focused()
        station = page.locator("#onboardStation")
        station.fill("kenm")
        expect(station).to_have_attribute("aria-expanded", "true")
        expect(page.locator("#onboardStationAnnouncement")).to_have_text("1 station, use up and down")
        station.press("ArrowDown")
        expect(station).to_have_attribute("aria-activedescendant", "onboard-station-0")
        station.press("Enter")
        expect(station).to_have_value("Kenmore (Green)")
        expect(station).to_have_attribute("aria-expanded", "false")
        page.locator("#onboardBudget").select_option("15")
        page.locator("#onboardStay").select_option("60")
        page.screenshot(path=f"{shots}/onboard-2-{size[0]}.png")
        page.locator("#onboardingSheet").get_by_role("button", name="Next", exact=True).click()
        expect(page.locator("#onboardStep3Title")).to_be_focused()
        expect(page.locator("#recoveryExplain")).to_have_text(
            "This code is the only way to get your profile back on another phone. We can't recover it for you.")
        page.locator("#onboardingSheet").get_by_role("button", name="Save a recovery code").click()
        code_node = page.locator("#recoveryCode")
        expect(code_node).to_be_focused()
        code = code_node.text_content()
        assert CODE.match(code), code
        expect(page.locator("#recoveryStatus")).to_have_text("Your recovery code is ready. It is shown only once.")
        assert "monospace" in code_node.evaluate("(el) => getComputedStyle(el).fontFamily").lower()
        assert code_node.evaluate("(el) => parseFloat(getComputedStyle(el).fontSize)") >= 16
        assert code_node.bounding_box()["height"] < 60, "the code stays on one line"
        done = page.locator("#onboardingSheet").get_by_role("button", name="Done", exact=True)
        expect(done).to_be_disabled()
        expect(page.locator("#onboardingSheet").get_by_role("button", name="Skip for now")).to_be_hidden()
        # Escape cannot throw the only copy away before it is saved.
        page.keyboard.press("Escape")
        expect(sheet).to_be_visible()
        expect(page.locator("#savedRecoveryCode")).to_be_focused()
        page.locator("#onboardingSheet").get_by_role("button", name="Copy", exact=True).click()
        expect(page.locator("#recoveryStatus")).to_have_text("Copied.")
        assert page.evaluate("navigator.clipboard.readText()") == code
        with page.expect_download() as info:
            page.locator("#onboardingSheet").get_by_role("button", name="Download as text file").click()
        download = info.value
        assert download.suggested_filename == "midnight-munch-recovery-code.txt"
        assert code in Path(download.path()).read_text()
        page.evaluate("window.printCalls = 0; window.print = () => { window.printCalls++; window.printClass = "
                      "document.documentElement.classList.contains('printing-recovery'); }; 0")
        page.locator("#onboardingSheet").get_by_role("button", name="Print", exact=True).click()
        printed = page.evaluate("[window.printCalls, window.printClass]")
        assert printed == [1, True], printed
        page.screenshot(path=f"{shots}/onboard-3-{size[0]}.png")
        no_page_overflow(page)
        page.locator("#savedRecoveryCode").check()
        expect(done).to_be_enabled()
        done.click()
        expect(sheet).to_be_hidden()
        expect(create).to_be_focused()
        expect(page.locator("#profileToast")).to_have_text("Profile saved. Your recovery code is ready.")
        expect(page.locator("#recoveryStatusLine")).to_have_text("Recovery code saved on Nov 21")
        expect(page.locator("#profileMeterLabel")).to_have_text("Profile 100 percent complete")
        expect(page.locator("#profileNextAction")).to_be_hidden()
        expect(page.locator("#profileName")).to_have_text("Ana <b>&amp;")
        saved = site.server.community.profile(site.cookie)
        assert saved["profile"]["display_name"] == "Ana <b>&amp;" and saved["profile"]["avatar"] == "🦉"
        assert saved["profile"]["defaults"] == {"home_station_id": "place-kencl", "budget": 15, "stay_min": 60}
        assert saved["has_recovery_code"]
        assert code not in storage_text(page) and not any(code in line for line in site.console)
        assert page.locator("#profileDialog b").count() == 0
        # The sheet never shows the old code again.
        page.get_by_role("button", name="Make a new recovery code").click()
        expect(page.locator("#recoveryCode")).to_have_text("")
        expect(page.locator("#recoveryReplaceNote")).to_be_visible()
        page.locator("#onboardingSheet").get_by_role("button", name="Skip for now").click()
        expect(sheet).to_be_hidden()
        assert not site.errors, site.errors
        return code, site.cookie
    finally:
        site.close()


def onboarding_without_code(browser, size):
    site = MockSite(browser, *size)
    page = start(site)
    try:
        open_profile(page)
        page.get_by_role("button", name="Create your profile").click()
        page.locator("#onboardName").fill("Sam")
        page.locator("#onboardingSheet").get_by_role("button", name="Next", exact=True).click()
        page.locator("#onboardingSheet").get_by_role("button", name="Skip", exact=True).click()
        expect(page.locator("#onboardStep3Title")).to_be_focused()
        page.locator("#onboardingSheet").get_by_role("button", name="Back", exact=True).click()
        expect(page.locator("#onboardStep2Title")).to_be_focused()
        page.locator("#onboardingSheet").get_by_role("button", name="Next", exact=True).click()
        page.locator("#onboardingSheet").get_by_role("button", name="Skip for now").click()
        expect(page.locator("#onboardingSheet")).to_be_hidden()
        expect(page.locator("#recoveryStatusLine")).to_have_text("No recovery code yet")
        expect(page.locator("#profileMeterLabel")).to_have_text("Profile 40 percent complete")
        expect(page.locator("#profileNextAction")).to_have_text("Next: pick an avatar")
        assert site.server.community.profile(site.cookie)["profile"]["display_name"] == "Sam"
        assert [path for method, path, _ in site.requests if path.startswith("/api/recovery")] == []
        # Debounced sync: two quick edits send one save.
        before = len(site.api("PUT"))
        page.locator("#displayName").fill("Sam B")
        page.get_by_role("button", name="Save profile").click()
        page.locator("#bio").fill("Dumplings")
        page.get_by_role("button", name="Save profile").click()
        expect(page.locator("#syncStatus")).to_have_text("Saved on this server.")
        page.wait_for_timeout(1000)
        assert len(site.api("PUT")) == before + 1
        assert site.server.community.profile(site.cookie)["profile"]["bio"] == "Dumplings"
        assert not site.errors, site.errors
    finally:
        site.close()


def restore_flows(browser, size, code, owner_cookie, server):
    site = MockSite(browser, *size, server=server)
    page = start(site)
    try:
        open_profile(page)
        trigger = page.locator("#restoreFromPrivacy")
        trigger.click()
        dialog = page.get_by_role("dialog", name="Restore profile")
        field = dialog.get_by_label("Recovery code")
        expect(field).to_be_focused()
        submit = dialog.get_by_role("button", name="Restore", exact=True)
        submit.click()
        expect(page.locator("#restoreError")).to_have_text("Enter your recovery code.")
        expect(field).to_have_attribute("aria-invalid", "true")
        expect(field).to_be_focused()
        field.fill("ABC-123")
        field.press("Enter")
        expect(page.locator("#restoreError")).to_have_text("A recovery code has 20 letters and numbers. Check it and try again.")
        wrong = "7K4M2-9QRTX-B3HNV-W8Z6D" if code != "7K4M2-9QRTX-B3HNV-W8Z6D" else "7K4M2-9QRTX-B3HNV-W8Z6E"
        field.fill(wrong)
        submit.click()
        expect(page.locator("#restoreConfirmText")).to_have_text(
            "This will switch this browser to the restored profile. Your current anonymous stamps stay on this browser only.")
        switch = dialog.get_by_role("button", name="Switch to restored profile")
        expect(switch).to_be_focused()
        dialog.get_by_role("button", name="Cancel", exact=True).click()
        expect(field).to_be_focused()
        expect(page.locator("#restoreConfirm")).to_be_hidden()
        submit.click()
        switch.click()
        expect(page.locator("#restoreError")).to_have_text("That code didn't work.")
        expect(field).to_be_focused()
        assert site.cookie != owner_cookie
        server.community.restore_limit = 1
        submit.click()
        switch.click()
        expect(page.locator("#restoreError")).to_have_text("Too many tries. Wait a few minutes.")
        server.community.restore_limit = 10
        server.community.restore_failures.clear()
        page.screenshot(path=f"{SHOTS}/restore-error-{size[0]}.png")
        no_page_overflow(page)
        # Any case, spaces instead of dashes.
        field.fill(code.lower().replace("-", " "))
        submit.click()
        switch.click()
        expect(dialog).to_be_hidden()
        expect(trigger).to_be_focused()
        expect(page.locator("#profileToast")).to_have_text("Profile restored. Your stamps and points are back.")
        assert site.cookie == owner_cookie
        expect(page.locator("#profileName")).to_have_text("Ana <b>&amp;")
        expect(page.locator("#displayName")).to_have_value("Ana <b>&amp;")
        assert page.evaluate("JSON.parse(localStorage.getItem('midnight-munch-profile')).name") == "Ana <b>&amp;"
        expect(page.locator("#recoveryStatusLine")).to_have_text("Recovery code saved")
        assert code.replace("-", "") not in storage_text(page).replace("-", "").upper()
        # The restore link is also on the profile screen and the welcome nudge.
        expect(page.locator("#restoreFromProfile")).to_have_text("Have a recovery code? Restore profile")
        assert not site.errors, site.errors
    finally:
        site.close()


def consent_and_conflict(browser, size):
    server = make_server()
    site = MockSite(browser, *size, server=server)
    page = site.page
    try:
        # A profile that so far lives only in this browser.
        page.add_init_script("""if (!localStorage.getItem('midnight-munch-profile')) localStorage.setItem('midnight-munch-profile',
            JSON.stringify({name: 'Local Lu', avatar: '🐸', color: 'green', bio: '', defaults: {budget: '9'}}))""")
        start(site)
        open_profile(page)
        expect(page.locator("#syncConsent")).to_be_hidden()
        page.locator("#bio").fill("Late ramen")
        page.get_by_role("button", name="Save profile").click()
        consent = page.get_by_role("group", name="Save your profile on this server?")
        expect(consent).to_be_visible()
        page.wait_for_timeout(1000)
        assert site.api("PUT") == []
        consent.get_by_role("button", name="Only this browser").click()
        expect(page.locator("#syncStatus")).to_have_text("Saved on this browser only.")
        page.locator("#bio").fill("Late ramen!")
        page.get_by_role("button", name="Save profile").click()
        page.wait_for_timeout(1000)
        assert site.api("PUT") == []
        # The next action offers it again; agreeing uploads once.
        page.evaluate("localStorage.removeItem('midnight-munch-profile-sync')")
        page.reload()
        open_profile(page)
        page.locator("#bio").fill("Late ramen again")
        page.get_by_role("button", name="Save profile").click()
        consent.get_by_role("button", name="Save on this server").click()
        expect(page.locator("#syncStatus")).to_have_text("Saved on this server.")
        stored = server.community.profile(site.cookie)
        assert stored["profile"]["display_name"] == "Local Lu" and stored["profile"]["defaults"] == {"budget": 9}
        # Another device changes the profile; this browser's next save is stale (409): the newer copy wins.
        server.community.save_profile(site.cookie, SERVER_PROFILE, server.now_source() + timedelta(minutes=5))
        page.locator("#bio").fill("Edited on the phone")
        page.get_by_role("button", name="Save profile").click()
        expect(page.locator("#syncStatus")).to_have_text("Updated from your other device.")
        expect(page.locator("#profileToast")).to_have_text("Updated from your other device.")
        expect(page.locator("#displayName")).to_have_value("Laptop Ana")
        expect(page.locator("#profileName")).to_have_text("Laptop Ana")
        assert page.evaluate("JSON.parse(localStorage.getItem('midnight-munch-profile')).defaults") == {
            "station": "place-kencl", "budget": "15"}
        assert any(json.loads(body).get("if_updated_at") for _, _, body in site.api("PUT"))
        # Loaded on start: a newer server copy replaces the cached one.
        server.community.save_profile(site.cookie, dict(SERVER_PROFILE, display_name="Tablet Ana"),
                                      server.now_source() + timedelta(minutes=9))
        page.reload()
        expect(page.locator("#profileName")).to_have_text("Tablet Ana")
        expect(page.locator("#profileToast")).to_have_text("Updated from your other device.")
        # The write limit (429) keeps the local copy and saves it later.
        server.community.write_limit = 0
        open_profile(page)
        page.locator("#bio").fill("Too many saves")
        page.get_by_role("button", name="Save profile").click()
        expect(page.locator("#syncStatus")).to_have_text("Saved on this browser. Saving on the server in a moment.")
        server.community.write_limit = 20
        # Still marked unsaved: coming back online saves it.
        page.evaluate("window.dispatchEvent(new Event('online'))")
        expect(page.locator("#syncStatus")).to_have_text("Saved on this server.")
        assert server.community.profile(site.cookie)["profile"]["bio"] == "Too many saves"
        assert not site.errors, site.errors
    finally:
        site.close()


def cleared_storage_restore(browser, size):
    server = make_server()
    site = MockSite(browser, *size, server=server)
    page = site.page
    try:
        start(site)
        server.community.save_profile("11111111-1111-4111-8111-111111111111", SERVER_PROFILE, server.now_source())
        site.cookie = "11111111-1111-4111-8111-111111111111"
        page.reload()
        nudge = page.get_by_role("complementary", name="A saved profile is available.")
        expect(nudge).to_be_visible()
        expect(page.locator("#nudgeAnnouncement")).to_have_text("Suggestion: A saved profile is available.")
        assert page.locator("#profileNudge").evaluate("(el) => !el.contains(document.activeElement)")
        page.screenshot(path=f"{SHOTS}/nudge-found-{size[0]}.png")
        bar = page.locator("#planPrimary").bounding_box()
        card = page.locator("#profileNudge").bounding_box()
        assert card["y"] + card["height"] <= bar["y"] + 1, (card, bar)
        no_page_overflow(page)
        nudge.get_by_role("button", name="Restore", exact=True).click()
        expect(nudge).to_be_hidden()
        expect(page.locator("#profileName")).to_have_text("Laptop Ana")
        expect(page.locator("#profileToast")).to_have_text("Profile restored from this server.")
        # Resetting this browser's profile offers the server copy in the profile screen.
        open_profile(page)
        page.get_by_role("button", name="Reset this browser's profile").click()
        expect(page.locator("#profileName")).to_have_text("My profile")
        use = page.get_by_role("button", name="Use the profile saved on this server")
        expect(use).to_be_visible()
        use.click()
        expect(page.locator("#profileName")).to_have_text("Laptop Ana")
        expect(page.locator("#profileNextAction")).to_be_focused()
        assert not site.errors, site.errors
    finally:
        site.close()


def nudges(browser, size):
    server = make_server(pick_delay=1.2)
    site = MockSite(browser, *size, server=server)
    page = site.page
    nudge = page.locator("#profileNudge")
    try:
        start(site)
        expect(nudge).to_be_hidden()
        # (1) After the first successful plan.
        plan_night(page)
        expect(nudge).to_be_visible()
        expect(page.locator("#nudgeText")).to_have_text("Save your start and budget for next time.")
        expect(page.locator("#nudgeAnnouncement")).to_have_text("Suggestion: Save your start and budget for next time.")
        expect(page.locator("#nightHeading")).to_be_focused()
        expect(nudge.get_by_role("button", name="Have a recovery code? Restore profile")).to_be_visible()
        bar = page.locator("#nightPrimary").bounding_box()
        card = nudge.bounding_box()
        assert card["y"] + card["height"] <= bar["y"] + 1, (card, bar)
        page.screenshot(path=f"{SHOTS}/nudge-plan-{size[0]}.png")
        no_page_overflow(page)
        later = nudge.get_by_role("button", name="Not now", exact=True)
        later.focus()
        later.press("Enter")
        expect(nudge).to_be_hidden()
        expect(page.locator("#nightHeading")).to_be_focused()
        state = page.evaluate("JSON.parse(localStorage.getItem('midnight-munch-nudges'))")
        assert state["snoozed"]["plan"] > state["last"]
        # One per session: another trigger stays quiet even with the cooldown cleared.
        nudge_state(page, last=0, days=[])
        page.evaluate("account.visits.push({place_id: 1}); document.dispatchEvent(new Event('community-checkin'))")
        page.wait_for_timeout(300)
        expect(nudge).to_be_hidden()
        # One per 3 days: a new session within the cooldown stays quiet.
        nudge_state(page, last=page.evaluate("Date.now()") - DAY, days=[])
        new_session(page)
        page.evaluate("account.visits.push({place_id: 1}); document.dispatchEvent(new Event('community-checkin'))")
        page.wait_for_timeout(300)
        expect(nudge).to_be_hidden()
        # Snoozed nudges stay snoozed after the cooldown; (2) the stamp nudge shows instead.
        nudge_state(page, last=page.evaluate("Date.now()") - 4 * DAY, snoozed={"plan": page.evaluate("Date.now()") + DAY}, days=[])
        new_session(page)
        plan_night(page)
        page.wait_for_timeout(300)
        expect(nudge).to_be_hidden()
        page.evaluate("account.visits.push({place_id: 1}); document.dispatchEvent(new Event('community-checkin'))")
        expect(page.locator("#nudgeText")).to_have_text("You earned your first stamp.")
        nudge.get_by_role("button", name="Add a name").click()
        expect(page.locator("#onboardStep1Title")).to_be_focused()
        page.keyboard.press("Escape")
        expect(page.locator("#onboardingSheet")).to_be_hidden()
        expect(page.get_by_role("button", name="Profile", exact=True)).to_be_focused()
        assert page.evaluate("JSON.parse(localStorage.getItem('midnight-munch-nudges')).done.stamp") is True

        # Never during loading, with a dialog open, or while a pinned trip is followed.
        nudge_state(page, last=0, days=[])
        new_session(page)
        choose_start(page)
        page.locator("#lateButton").click()
        page.locator("#spin").click()
        expect(page.locator("#nightLoading")).to_be_visible()
        # Offered while loading: it waits (still loading, still hidden) and shows once the plan is ready.
        assert page.evaluate("""() => { document.dispatchEvent(new Event('night-planned'));
            return [byId('nightLoading').hidden, byId('profileNudge').hidden, pendingNudge]; }""") == [False, True, "plan"]
        expect(page.locator("#selectedDetails")).to_be_visible(timeout=20000)
        expect(page.locator("#nudgeText")).to_have_text("Save your start and budget for next time.")
        dont = nudge.get_by_role("button", name="Don't ask again")
        dont.focus()
        page.keyboard.press("Space")
        expect(nudge).to_be_hidden()
        nudge_state(page, last=0, stopped=True, days=["2026-01-01", "2026-01-02"])
        new_session(page)
        page.evaluate("document.dispatchEvent(new Event('night-planned'))")
        page.wait_for_timeout(300)
        expect(nudge).to_be_hidden()

        nudge_state(page, last=0, days=[])
        new_session(page)
        open_profile(page)
        page.evaluate("document.dispatchEvent(new Event('night-planned'))")
        page.wait_for_timeout(200)
        expect(nudge).to_be_hidden()
        page.locator("#profileDialog [data-close]").click()
        expect(page.locator("#nudgeText")).to_have_text("Save your start and budget for next time.")
        nudge.get_by_role("button", name="Not now", exact=True).click()

        nudge_state(page, last=0, days=[])
        new_session(page)
        plan_night(page)
        nudge.get_by_role("button", name="Not now", exact=True).click()
        nudge_state(page, last=0, days=[])
        page.evaluate("sessionStorage.clear(); memoryStore.clear()")
        page.locator("#pinNight").click()
        expect(page.locator("#pinnedScreen")).to_be_visible()
        page.evaluate("account.visits.push({place_id: 1}); document.dispatchEvent(new Event('community-checkin'))")
        page.wait_for_timeout(300)
        expect(nudge).to_be_hidden()
        page.locator("#unpinScreen").click()
        assert not site.errors, site.errors
    finally:
        site.close()


def welcome_and_protect(browser, size):
    server = make_server()
    site = MockSite(browser, *size, server=server)
    page = site.page
    nudge = page.locator("#profileNudge")
    try:
        start(site)
        # (4) Welcome back on the 3rd visit day.
        nudge_state(page, last=0, days=["2020-01-01", "2020-01-02"])
        new_session(page)
        expect(page.locator("#nudgeText")).to_have_text("Set your usual start station.")
        nudge.get_by_role("button", name="Set home station").click()
        expect(page.locator("#onboardStep2Title")).to_be_focused()
        page.locator("#onboardStation").fill("bu central")
        page.locator("#onboardStationList [role=option]").first.click()
        page.locator("#onboardingSheet").get_by_role("button", name="Next", exact=True).click()
        page.locator("#onboardingSheet").get_by_role("button", name="Skip for now").click()
        expect(page.locator("#onboardingSheet")).to_be_hidden()
        assert server.community.profile(site.cookie)["profile"]["defaults"]["home_station_id"] == "place-bucen"
        # (3) 20 points or 3 visits without a recovery code.
        server.community.visits[site.cookie] = {
            place: {"place_id": place, "name": f"Place {place}", "visited_at": "2026-11-21T20:00:00-05:00"}
            for place in (101, 102, 103)}
        nudge_state(page, last=0, days=[])
        new_session(page)
        expect(page.locator("#nudgeText")).to_have_text("Save a recovery code to switch phones.")
        nudge.get_by_role("button", name="Save a code").click()
        expect(page.locator("#onboardStep3Title")).to_be_focused()
        page.locator("#onboardingSheet").get_by_role("button", name="Save a recovery code").click()
        expect(page.locator("#recoveryCode")).to_have_text(re.compile(CODE.pattern))
        page.locator("#savedRecoveryCode").check()
        page.locator("#onboardingSheet").get_by_role("button", name="Done", exact=True).click()
        nudge_state(page, last=0, days=[])
        new_session(page)
        page.wait_for_timeout(500)
        expect(nudge).to_be_hidden()
        assert not site.errors, site.errors
    finally:
        site.close()


def storage_unavailable(browser, size):
    server = make_server()
    reporter = "22222222-2222-4222-8222-222222222222"
    server.community.save_profile(reporter, SERVER_PROFILE, server.now_source())
    site = MockSite(browser, *size, server=server)
    site.cookie = reporter
    page = site.page
    try:
        page.add_init_script("""Object.defineProperty(window, 'localStorage', {get() { throw new DOMException('denied', 'SecurityError'); }});""")
        page.goto(ORIGIN)
        expect(page.locator("#nudgeText")).to_have_text("A saved profile is available.", timeout=15000)
        page.get_by_role("button", name="Restore", exact=True).click()
        expect(page.locator("#profileName")).to_have_text("Laptop Ana")
        assert not site.errors, site.errors
    finally:
        site.close()


SHOTS = "/tmp"


def main(shots):
    global SHOTS
    SHOTS = shots
    with sync_playwright() as playwright:
        for size in [(375, 812), (390, 844)]:
            def run(step, *args):
                with playwright.chromium.launch(args=LAUNCH_ARGS) as browser:
                    return step(browser, size, *args)
            code, cookie = run(onboarding_with_code, shots)
            # Restore on a second browser against the same server.
            server = make_server()
            with playwright.chromium.launch(args=LAUNCH_ARGS) as browser:
                owner = MockSite(browser, *size, server=server)
                start(owner)
                owner.page.evaluate("""async () => {
                    const put = await fetch('/api/profile', {method: 'PUT', headers: {'Content-Type': 'application/json'},
                      body: JSON.stringify({display_name: 'Ana <b>&amp;', avatar: '🦉', color: 'moss', bio: '', defaults: {}})});
                    return put.status; }""")
                code = owner.page.evaluate("fetch('/api/recovery/create', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'}).then((r) => r.json()).then((d) => d.code)")
                cookie = owner.cookie
                owner.close()
            run(restore_flows, code, cookie, server)
            run(onboarding_without_code)
            run(consent_and_conflict)
            run(cleared_storage_restore)
            run(nudges)
            run(welcome_and_protect)
            run(storage_unavailable)
            print(f"Profile walkthrough passed at {size[0]}x{size[1]}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp")
