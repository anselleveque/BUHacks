#!/usr/bin/env python3
"""Browser-rendered menu fetcher (fallback for JavaScript-rendered pages).

Usage:
    python3 scripts/fetch_menu_js.py <slug>=<url> [<slug>=<url> ...]

For each page it loads the URL in headless Chromium, waits for the network to
settle, scrolls to trigger lazy loading, then saves:
    data/raw/<slug>.txt   visible page text
    data/raw/<slug>.png   full-page screenshot (to eyeball results)
and prints a JSON summary (status, text length, priced lines, samples).

It does not try to evade bot protection. If a site blocks the browser (403,
CAPTCHA), the result is reported as blocked and the place should fall back to
tier-based or hand-entered prices.
"""
import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

OUT = Path(__file__).resolve().parent.parent / "data" / "raw"
PRICE = re.compile(r"\$\s?\d{1,3}(?:\.\d{2})?")
BLOCK_HINTS = ("access denied", "forbidden", "captcha", "verify you are human",
               "unusual traffic", "request blocked")


def priced_lines(text: str):
    """Return lines containing a price, paired with the preceding line as a name guess."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    out = []
    for i, line in enumerate(lines):
        if PRICE.search(line):
            name = line if len(PRICE.sub("", line).strip()) > 2 else (lines[i - 1] if i else "")
            out.append({"name": name[:80], "line": line[:120]})
    return out


def dismiss_popups(page):
    for label in ("Dismiss", "Accept", "Accept all", "Got it", "Close", "No thanks"):
        try:
            page.get_by_role("button", name=label, exact=False).first.click(timeout=800)
        except Exception:
            pass


def fetch(browser, slug: str, url: str) -> dict:
    ctx = browser.new_context(viewport={"width": 1280, "height": 1800}, locale="en-US")
    page = ctx.new_page()
    result = {"slug": slug, "url": url, "status": None, "blocked": False,
              "text_len": 0, "priced_count": 0, "samples": [], "error": None}
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
        result["status"] = resp.status if resp else None
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass
        dismiss_popups(page)
        for _ in range(6):  # scroll to trigger lazy-loaded sections
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(400)
        text = page.inner_text("body")
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / f"{slug}.txt").write_text(text, encoding="utf-8")
        page.screenshot(path=str(OUT / f"{slug}.png"), full_page=True)
        items = priced_lines(text)
        result.update(
            text_len=len(text),
            priced_count=len(items),
            samples=items[:3],
            blocked=(result["status"] in (401, 403, 429))
            or any(h in text.lower()[:2000] for h in BLOCK_HINTS),
        )
    except Exception as e:  # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        ctx.close()
    return result


def main(argv):
    pairs = [a.split("=", 1) for a in argv if "=" in a]
    if not pairs:
        print(__doc__)
        return 1
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for slug, url in pairs:
            results.append(fetch(browser, slug, url))
        browser.close()
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
