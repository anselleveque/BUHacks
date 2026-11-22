"""Exercise the in-app Director controls at two phone sizes against a real demo server.

Build data/foodapp.db first. Run from the repo root with:
    python3 web/tests/demo_dialog_walkthrough.py
"""
import re
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

from devpanel_walkthrough import check_sheet_layout, dialog, open_dev
from mock_browser import LAUNCH_ARGS

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from foodapp.demo.server import DemoServer  # noqa: E402


def walkthrough(browser, origin, size):
    context = browser.new_context(viewport=dict(zip(("width", "height"), size)), has_touch=True, is_mobile=True,
                                  reduced_motion="reduce")
    try:
        page = context.new_page()
        errors = []
        catalog_reads = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda req: catalog_reads.append(req.url)
                if req.method == "GET" and req.url == f"{origin}/demo/api/state" else None)
        # Keep external fonts and map tiles off the network while leaving the real demo API alone.
        page.route("**/*", lambda route: route.continue_()
                   if urlsplit(route.request.url).netloc == urlsplit(origin).netloc else route.abort())
        page.goto(origin)
        expect(page.get_by_role("combobox", name="Start", exact=True)).to_have_value(re.compile(r"^Boston University Central"), timeout=15000)
        open_dev(page)
        sheet = dialog(page)
        readout = sheet.locator("#devDemoTime")
        status = sheet.locator("#devStatus")
        scenario = sheet.get_by_role("combobox", name="Demo scenario")
        expect(scenario).to_be_enabled(timeout=10000)
        expect(readout).to_have_text(re.compile(r"^Sat 9:12:\d\d PM$"))
        check_sheet_layout(page, size)
        assert len(catalog_reads) == 1
        # Reopen before Chromium dispatches the queued close event. The old
        # event must not stop the reopened dialog's clock display.
        page.evaluate("""() => {
            const dialog = document.getElementById('devDialog');
            const closed = new Promise(resolve => dialog.addEventListener('close', () => resolve(), {once: true}));
            dialog.close();
            document.dispatchEvent(new KeyboardEvent('keydown', {
                code: 'KeyD', ctrlKey: true, altKey: true, shiftKey: true,
            }));
            return closed;
        }""")
        expect(sheet).to_be_visible()
        expect(page.locator("#devTitle")).to_be_focused()
        assert len(catalog_reads) == 1
        page.keyboard.press("Digit3")
        page.keyboard.press("KeyK")
        expect(scenario).to_have_value("1")
        expect(readout).to_have_text(re.compile(r"^Sat 9:12:"))

        sheet.get_by_role("button", name="Play", exact=True).click()
        expect(sheet.get_by_role("button", name="Freeze", exact=True)).to_be_enabled()
        initial = readout.text_content()
        expect(readout).not_to_have_text(initial, timeout=8000)
        page.evaluate("""() => window.addEventListener('midnight-demo:replan', (event) => {
            if (event.detail.reason === 'jump') window.sawDemoJumpReplan = true;
        })""")
        sheet.get_by_role("button", name="+5 min", exact=True).click()
        expect(page.locator("#nightClock")).to_contain_text("9:17", timeout=10000)
        page.wait_for_function("window.sawDemoJumpReplan", timeout=10000)
        sheet.get_by_role("button", name="Freeze", exact=True).click()
        expect(sheet.get_by_role("button", name="Play", exact=True)).to_be_enabled()
        page.wait_for_function("!window.midnightDemo.state.clock.running")
        frozen = readout.text_content()
        page.wait_for_timeout(3000)
        expect(readout).to_have_text(frozen)

        sheet.get_by_label("Set time", exact=True).fill("23:30")
        sheet.get_by_role("button", name="Set", exact=True).click()
        expect(readout).to_have_text("Sat 11:30:00 PM")
        expect(page.locator("#nightClock")).to_contain_text("11:30")
        sheet.get_by_role("button", name="20x", exact=True).click()
        expect(sheet.get_by_role("button", name="20x", exact=True)).to_have_attribute("aria-pressed", "true")
        page.wait_for_function("midnightDemo.state.clock.running && midnightDemo.now() > "
                               "Date.parse('2026-11-21T23:30:10-05:00')")
        sheet.get_by_role("button", name="Freeze", exact=True).click()
        expect(sheet.get_by_role("button", name="Play", exact=True)).to_be_enabled()

        scenario.select_option("3")
        expect(sheet).to_be_hidden(timeout=15000)
        expect(page.get_by_role("combobox", name="Start", exact=True)).to_have_value(re.compile(r"^Park Street"), timeout=15000)
        open_dev(page)
        expect(scenario.locator("option:checked")).to_contain_text("Last train home warning", timeout=10000)
        sheet.get_by_role("button", name="Night Owl", exact=True).click()
        expect(sheet).to_be_hidden(timeout=15000)
        page.wait_for_function("window.midnightDemo?.state?.persona.id === 'night-owl'", timeout=15000)
        open_dev(page)
        expect(sheet.get_by_role("button", name="Night Owl", exact=True)).to_have_attribute("aria-pressed", "true")
        expect(sheet.locator("#devDemoRecovery")).to_be_visible()
        alert = sheet.get_by_role("button", name=re.compile(r"Red Line shuttle", re.I))
        expect(alert).to_have_attribute("aria-pressed", "false")
        alert.click()
        expect(alert).to_have_attribute("aria-pressed", "true")

        scenario.select_option("2")
        expect(sheet).to_be_hidden(timeout=15000)
        page.wait_for_function("window.midnightDemo?.state?.scenario.id === 2", timeout=15000)
        open_dev(page)
        # Use the same boarding payload the app saves for a pinned trip.
        page.evaluate("""() => localStorage.setItem('midnight-munch-pinned-trip', JSON.stringify({
            outbound: {legs: [{mode: 'ride', depart_iso: '2026-11-21T21:16:00-05:00'}]}
        }))""")
        missed = sheet.get_by_role("button", name="Missed train", exact=True)
        missed.click()
        expect(status).to_have_text("Clock moved just past the 9:16 PM boarding.")
        missed.click()
        expect(status).to_have_text("That boarding time has already passed.")
        sheet.get_by_role("button", name="Second diner: price", exact=True).click()
        expect(status).to_have_text(re.compile(r"^(Second diner reported \$|Price \$)"))
        assert not errors, errors

        page.route("**/demo/api/state", lambda route: route.abort())
        page.reload()
        open_dev(page)
        expect(sheet.get_by_text("Can't reach the demo controls", exact=True)).to_be_visible(timeout=15000)
        expect(sheet.get_by_label("Motion preference")).to_be_enabled()
        expect(sheet.get_by_label("Simulate device location and check-in GPS")).to_be_enabled()
    finally:
        context.close()


def main():
    with TemporaryDirectory() as data:
        server = DemoServer(("127.0.0.1", 0), ROOT / "data/foodapp.db", data_dir=Path(data) / "demo", running=False)
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        try:
            # Each viewport starts at the same frozen scenario, with fresh user data.
            with sync_playwright() as playwright:
                for size in ((375, 812), (390, 844)):
                    server.director.reset("everything")
                    server.director.clock_action({"action": "freeze"})
                    browser = playwright.chromium.launch(args=LAUNCH_ARGS)
                    try:
                        walkthrough(browser, f"http://127.0.0.1:{server.server_port}", size)
                    finally:
                        browser.close()
                    print(f"Demo dialog walkthrough passed at {size[0]}x{size[1]}")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    main()
