"""Capture real product screenshots for the /for-sites marketing page.

The marketing page shows actual product screens, not mockups. Re-run this after
any app UI change so the images on the site stay truthful:

    # with the app running in demo mode on :5000
    ../.venv/bin/python tools/capture_shots.py

Requires SITE_DEMO so the study-team pages render seeded demo data without a
login. Images land in static/shots/.
"""
from __future__ import annotations

import os
import pathlib
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("SHOT_BASE", "http://127.0.0.1:5000")
OUT = pathlib.Path(__file__).resolve().parent.parent / "static" / "shots"
# Narrow-ish desktop on purpose: the shots are shown scaled down on the
# marketing page, so less content per pixel keeps the app's text legible.
VIEWPORT = {"width": 1180, "height": 780}

# (filename, path, wait_selector) — study-team screens shown on the site.
SHOTS = [
    ("recruitment.png", "/app/dashboard", None),
    ("applicants.png", "/app/leads", None),
    ("balance.png", "/app/balance", None),
    ("home.png", "/app/home", None),
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=2,
                                  reduced_motion="reduce")
        page = ctx.new_page()
        for name, path, sel in SHOTS:
            url = f"{BASE}{path}"
            page.goto(url, wait_until="networkidle")
            if sel:
                page.wait_for_selector(sel, timeout=5000)
            page.wait_for_timeout(400)
            if "/login" in page.url:
                print(f"!! {path} redirected to login — is SITE_DEMO on?")
                continue
            page.screenshot(path=str(OUT / name))
            print(f"ok {name:18} <- {path}")

        # Applicant detail: follow the first applicant from the queue so the
        # screenshot is a real record, not a hand-picked id. The queue rows
        # navigate on click rather than being anchors.
        page.goto(f"{BASE}/app/leads", wait_until="networkidle")
        row = page.query_selector("tr.link")
        if row:
            row.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(400)
            page.screenshot(path=str(OUT / "applicant.png"))
            print(f"ok {'applicant.png':18} <- {page.url}")
        else:
            print("!! no applicant row found on /app/leads")

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
