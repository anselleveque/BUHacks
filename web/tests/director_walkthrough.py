"""Run from the repo root with python3 web/tests/director_walkthrough.py."""
import re
import sys
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from foodapp.demo.server import DemoServer  # noqa: E402


def open_streams(context, origin, count):
    page = context.new_page()
    page.goto(origin + "/demo/director.css")
    page.evaluate("""count => {
      window.streams = Array.from({ length: count }, () => new AbortController());
      window.readers = [];
      window.streamsReady = 0;
      for (const controller of window.streams) void (async () => {
        const response = await fetch("/demo/api/events", {
          credentials: "omit", cache: "no-store", signal: controller.signal
        });
        const reader = response.body.getReader();
        const first = await reader.read();
        if (!response.ok || first.done) return;
        window.readers.push(reader);
        window.streamsReady++;
      })().catch(() => {});
    }""", count)
    page.wait_for_function("count => window.streamsReady === count", arg=count)
    return page


def director_controls(browser, origin):
    page = browser.new_page()
    failed = True

    def state_request(route):
        if failed:
            route.abort()
        else:
            route.continue_()

    page.route("**/demo/api/state*", state_request)
    page.goto(origin + "/demo/")
    expect(page.locator("#connectionError")).to_have_text(
        f"Can't reach the demo server at {origin.removeprefix('http://')}"
    )
    expect(page.locator('[data-jump="5"]')).to_be_disabled()
    expect(page.locator("#connection")).to_have_text("Reconnecting...")
    failed = False

    expect(page.locator("#connection")).to_have_text("Live")
    expect(page.locator("#scenarios button")).to_have_count(9)
    clock = page.locator("#clockLabel")
    expect(clock).to_have_text(re.compile(r"11:30:00 PM"))
    page.locator('[data-jump="5"]').click()
    expect(clock).to_have_text(re.compile(r"11:35:00 PM"))
    page.locator("#playPause").click()
    expect(page.locator("#clockMode")).to_have_text("Running")
    page.locator("#playPause").click()
    expect(page.locator("#clockMode")).to_have_text("Frozen")
    frozen = clock.inner_text()
    page.wait_for_timeout(1250)
    expect(clock).to_have_text(frozen)

    page.locator("#timeInput").fill("23:40")
    page.get_by_role("button", name="Set", exact=True).click()
    expect(clock).to_have_text(re.compile(r"11:40:00 PM"))
    page.locator('[data-speed="20"]').click()
    expect(page.locator("#clockMode")).to_have_text("Running 20x")
    expect(clock).not_to_have_text(re.compile(r"11:40:00 PM"))
    page.locator('[data-scenario="4"]').click()
    expect(page.locator("#scenarioName")).to_have_text("4. No trains at 4 AM")
    with page.expect_response(lambda response: response.url.endswith("/demo/api/state")):
        page.locator("#resetScenario").click()
    expect(page.locator("#scenarios button")).to_have_count(9)
    page.close()


def connection_starvation(browser, origin):
    shared = browser.new_context()
    other = browser.new_context()
    # Chromium allows six HTTP/1 connections per pool. Keep five streams in the
    # clients' pool and three more on the same host in a separate browser context.
    shared_streams = open_streams(shared, origin, 5)
    open_streams(other, origin, 3)

    used_events = []
    panel = shared.new_page()
    app = shared.new_page()
    for page in (panel, app):
        page.on("request", lambda request: used_events.append(request.url)
                if request.url.endswith("/demo/api/events") else None)
    panel.goto(origin + "/demo/")
    expect(panel.locator("#connection")).to_have_text("Live", timeout=12000)
    expect(panel.locator("#scenarios button")).to_have_count(9)
    panel.locator("#timeInput").fill("23:55")
    panel.get_by_role("button", name="Set", exact=True).click()
    expect(panel.locator("#clockLabel")).to_have_text(re.compile(r"11:55:00 PM"))

    app.goto(origin + "/?director=1")
    mini = app.get_by_role("region", name="Demo controls")
    expect(mini.get_by_role("status")).to_have_text("Live", timeout=12000)
    expect(mini.locator(".time")).to_have_text(re.compile(r"11:55:00 PM"))
    app.evaluate("""() => window.addEventListener("midnight-demo:jump", event => {
      window.demoJump = event.detail;
    })""")
    panel.locator('[data-jump="5"]').click()
    expect(panel.locator("#clockLabel")).to_have_text(re.compile(r"12:00:00 AM"))
    expect(mini.locator(".time")).to_have_text(re.compile(r"12:00:00 AM"), timeout=12000)
    assert app.evaluate("window.demoJump.to - window.demoJump.from") == 300000
    assert not used_events, used_events

    # A sixth held stream saturates the pool. Verify bounded failures and
    # automatic recovery as soon as a connection is available again.
    last_stream = open_streams(shared, origin, 1)
    expect(panel.locator("#connection")).to_have_text("Reconnecting...", timeout=16000)
    last_stream.close()
    expect(panel.locator("#connection")).to_have_text("Live", timeout=12000)
    expect(mini.get_by_role("status")).to_have_text("Live", timeout=12000)
    assert shared_streams.evaluate("streams.every(stream => !stream.signal.aborted)")
    shared.close()
    other.close()


def main():
    with tempfile.TemporaryDirectory() as data_dir:
        server = DemoServer(("127.0.0.1", 0), ROOT / "data/foodapp.db", data_dir=data_dir, running=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            origin = f"http://127.0.0.1:{server.server_port}"
            with sync_playwright() as playwright, playwright.chromium.launch() as browser:
                director_controls(browser, origin)
                connection_starvation(browser, origin)
            print("2 Director walkthroughs passed")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    main()
