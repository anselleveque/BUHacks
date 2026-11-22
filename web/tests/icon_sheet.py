"""Render the live icon registry in Chromium at 16, 20 and 24 px.

Run from the repo root with Python and the existing Playwright setup.
An optional argument names the output PNG.
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from mock_browser import LAUNCH_ARGS, ORIGIN, MockSite

SCRATCH = Path(
    "/private/tmp/claude-501/-Users-user-Python-BUHacks/"
    "2dda37b2-0a93-4d20-91cd-306f741bc89c/scratchpad/T51"
)


def render(page, output):
    page.goto(ORIGIN + "/")
    page.wait_for_function("typeof makeIcon === 'function'")
    page.add_style_tag(content="""
        body { display: grid; grid-template-columns: repeat(2, 1fr); margin: 0; }
        section { padding: 20px; background: var(--canvas); color: var(--ink); }
        h1 { margin: 0 0 12px; }
        .sample { display: grid; align-items: center; min-height: 38px;
                  grid-template-columns: 110px repeat(3, 1fr); border-top: 1px solid var(--line); }
    """)
    page.evaluate("""() => {
        const sections = [];
        for (const theme of ["dark", "light"]) {
          const section = document.createElement("section");
          section.dataset.theme = theme;
          const heading = document.createElement("h1");
          heading.textContent = theme === "dark" ? "Dark" : "Light";
          section.append(heading);
          for (const name of ["Size", ...Object.keys(iconPaths)]) {
            const row = document.createElement("div");
            row.className = "sample";
            row.append(name);
            for (const size of [16, 20, 24]) {
              const sample = document.createElement("span");
              if (name === "Size") sample.textContent = size + " px";
              else {
                const icon = makeIcon(name);
                icon.style.width = icon.style.height = size + "px";
                sample.append(icon, " Aa");
              }
              row.append(sample);
            }
            section.append(row);
          }
          sections.push(section);
        }
        document.body.replaceChildren(...sections);
    }""")
    # The production theme tokens are scoped to the root. Copy those same
    # declarations to the light half of the sheet.
    page.evaluate("""() => {
        const keys = ["--canvas", "--ink", "--line"];
        const root = document.documentElement;
        root.dataset.theme = "light";
        const colors = getComputedStyle(root);
        const light = document.querySelector('section[data-theme="light"]');
        for (const key of keys) light.style.setProperty(key, colors.getPropertyValue(key));
        light.style.colorScheme = "light";
        root.dataset.theme = "dark";
    }""")
    output.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=output, full_page=True, animations="disabled")


if __name__ == "__main__":
    with sync_playwright() as playwright, playwright.chromium.launch(args=LAUNCH_ARGS) as browser:
        site = MockSite(browser, 860, 920)
        render(site.page, Path(sys.argv[1]) if len(sys.argv) > 1 else SCRATCH / "icon-sheet.png")
