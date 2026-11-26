"""Screenshots of the live system for the one-pager. Run: python -I site/capture.py (needs Playwright)."""

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "https://truckroll.siniaev.dev"
OUT = Path(__file__).parent / "truckroll" / "img"
WAIT_FOR_QUESTION_S = int(sys.argv[1]) if len(sys.argv) > 1 else 900


def ready(pg):
    pg.wait_for_function("document.querySelectorAll('#scene .station').length > 0", timeout=30000)
    pg.wait_for_timeout(1500)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        desk = b.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        desk.add_init_script("localStorage.setItem('lastVisit', new Date().toISOString())")
        pg = desk.new_page()
        pg.goto(URL); ready(pg)
        pg.screenshot(path=OUT / "app.png")
        pg.locator(".inc-row .line").first.click(force=True); pg.wait_for_timeout(600)
        pg.locator("#feed-box").screenshot(path=OUT / "incident.png")
        pg.locator(".inc-row .line").first.click(force=True)
        charging = pg.locator("#scene .station").nth(4)
        charging.click(force=True); pg.wait_for_timeout(800)
        pg.locator("#panel").screenshot(path=OUT / "station.png")
        pg.click("button[data-tab=controls]"); pg.wait_for_timeout(800)
        pg.locator("#panel").screenshot(path=OUT / "controls.png")

        pg.goto(f"{URL}/reports.html"); pg.wait_for_selector(".rp-row", state="attached"); pg.wait_for_timeout(800)
        rows = pg.locator(".rp-row")
        for n in range(rows.count()):
            if "auth" in rows.nth(n).inner_text().lower():
                rows.nth(n).click(force=True); break
        pg.wait_for_selector(".rp-timeline", state="attached"); pg.wait_for_timeout(800)
        pg.screenshot(path=OUT / "reports.png")

        away = b.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=2)
        away.add_init_script("localStorage.setItem('lastVisit', new Date(Date.now() - 8*3600e3).toISOString())")
        hp = away.new_page(); hp.goto(URL); hp.wait_for_selector(".handover-card", state="attached", timeout=40000)
        hp.wait_for_timeout(1000); hp.locator(".handover-card").screenshot(path=OUT / "handover.png")

        gp = desk.new_page(); gp.goto(f"{URL}/grafana/d/truckroll-fleet?kiosk&theme=dark"); gp.wait_for_timeout(9000)
        gp.screenshot(path=OUT / "grafana.png")

        phone = b.new_context(**p.devices["iPhone 13"])
        phone.add_init_script("localStorage.setItem('lastVisit', new Date().toISOString())")
        pp = phone.new_page(); pp.goto(URL); ready(pp); pp.screenshot(path=OUT / "phone.png")

        # A question waiting for an answer only exists for 45-90 s while a scenario runs: wait for one.
        pg.goto(URL); ready(pg)
        deadline = time.time() + WAIT_FOR_QUESTION_S
        while time.time() < deadline:
            if pg.locator("#questions .question").count():
                pg.wait_for_timeout(500)
                pg.screenshot(path=OUT / "question.png")
                pg.locator("#feed-box").screenshot(path=OUT / "question-feed.png")
                print("question captured")
                break
            pg.wait_for_timeout(3000)
        else:
            print("no question within the wait")
        b.close()
    print("done:", sorted(x.name for x in OUT.iterdir()))


if __name__ == "__main__":
    main()
