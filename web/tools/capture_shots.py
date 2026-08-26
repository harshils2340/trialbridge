"""Capture real product screenshots for the marketing pages.

The site shows actual product screens, not mockups, so re-run this after any app
UI change or the images on the site quietly become fiction.

    # with the app running in demo mode
    SHOT_BASE=http://127.0.0.1:5055 ../.venv/bin/python tools/capture_shots.py

Requires SITE_DEMO so the study-team pages render seeded demo data without a
login. Images land in static/shots/ at 2x for retina.

Two kinds of shot:

  full     the whole viewport - use when the point IS the layout (the inbox as
           a three-pane workspace)
  element  cropped to one region - use when the point is a single feature. A
           full-page shot scaled into a card on the marketing site renders the
           product's 12px UI text at about 5px, which reads as grey mush; a
           crop of the same region stays legible at the same card size.

Each shot carries the caption the marketing page should use with it, printed on
completion so the copy and the image are decided in the same place rather than
drifting apart.
"""
from __future__ import annotations

import os
import pathlib
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("SHOT_BASE", "http://127.0.0.1:5055").rstrip("/")
OUT = pathlib.Path(__file__).resolve().parent.parent / "static" / "shots"
# Roomy enough that the inbox keeps its applicant rail (it moves behind a tab
# below ~1600) and nothing is cropped by a narrow viewport.
VIEWPORT = {"width": 1728, "height": 1000}

# The patient portal is behind an unguessable link + password, so it can only be
# captured when credentials are supplied. Skipped otherwise.
PORTAL_TOKEN = os.environ.get("PORTAL_TOKEN", "")
PORTAL_PASSWORD = os.environ.get("PORTAL_PASSWORD", "")

# (filename, path, crop_selector | None, wait_selector | None, caption)
SHOTS = [
    ("inbox_hero.png", "/app/inbox", ".mh-conversation", ".mh-thread-row",
     "Every enquiry from every channel in one queue, email, Instagram and "
     "Google Ads, triaged before anyone opens a chart."),
    ("inbox_sources.png", "/app/inbox", ".mh-thread-column", ".mh-thread-row",
     "Ads, DMs and inbound email arrive side by side, each tagged with the "
     "study it belongs to and who owns the reply."),
    ("records.png", "/app/inbox?thread=28", ".mh-applicant-panel",
     "[data-record-pull], #records",
     "With the patient's authorisation, the chart is pulled straight from their "
     "health record and checked against the protocol's criteria."),
]


def _dismiss_overlays(page):
    """Demo tours and toasts are true to the product but noise in a still."""
    page.evaluate("""()=>{
      document.querySelectorAll('.toast-stack,.demo-tour,[data-demo-tour]')
        .forEach(e=>e.remove());}""")


# Every shot is captured at one aspect ratio. Element crops gave each feature a
# different shape (a thread column is tall and narrow, a composer is wide and
# short), and a grid mixing 0.37:1 with 5.1:1 reads as broken however sharp the
# individual images are. A fixed ratio, zoomed on the feature, keeps them a set.
SHOT_W, SHOT_H = 1200, 750          # 16:10, doubled by device_scale_factor


def _clip_around(page, selector):
    """A SHOT_W x SHOT_H window centred on `selector`, clamped to the viewport.

    Centring on the element is what makes the shot read as "zoomed in" on the
    feature while still showing enough surrounding product to be recognisable.
    """
    return page.evaluate("""([sel,w,h])=>{
      const e=document.querySelector(sel); if(!e) return null;
      const r=e.getBoundingClientRect();
      let x=Math.round(r.x+r.width/2-w/2), y=Math.round(r.y+r.height/2-h/2);
      x=Math.max(0,Math.min(x, Math.max(0,document.documentElement.clientWidth-w)));
      y=Math.max(0,Math.min(y, Math.max(0,document.documentElement.clientHeight-h)));
      return {x:x, y:y, width:w, height:h};}""",
      [selector, SHOT_W, SHOT_H])


def _shoot(page, name, crop_sel, caption):
    _dismiss_overlays(page)
    page.wait_for_timeout(350)
    clip = _clip_around(page, crop_sel) if crop_sel else {
        "x": 0, "y": 0, "width": SHOT_W, "height": SHOT_H}
    if clip is None:
        print(f"!! {name:20} crop target {crop_sel!r} not found - skipped")
        return None
    page.screenshot(path=str(OUT / name), clip=clip)
    print(f"ok {name:20} {SHOT_W}x{SHOT_H}@2x centred on {crop_sel or 'viewport'}")
    return caption


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    captions: list[tuple[str, str]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=2,
                                  reduced_motion="reduce")
        page = ctx.new_page()

        for name, path, crop_sel, wait_sel, caption in SHOTS:
            page.goto(f"{BASE}{path}", wait_until="load")
            if wait_sel:
                try:
                    page.wait_for_selector(wait_sel, timeout=8000)
                except Exception:
                    print(f"!! {name:20} never saw {wait_sel!r}")
                    continue
            if "/login" in page.url:
                print(f"!! {path} redirected to login - is SITE_DEMO on?")
                continue
            # The hero wants a thread open so the reading pane isn't empty.
            if name == "inbox_hero.png":
                row = page.query_selector(".mh-thread-row")
                if row:
                    row.click()
                    page.wait_for_load_state("load")
                    page.wait_for_timeout(500)
            got = _shoot(page, name, crop_sel, caption)
            if got:
                captions.append((name, got))

        # Bridget drafting a reply: the draft has to be ON SCREEN, so trigger it
        # and wait for the streamed text to settle before the shutter.
        page.goto(f"{BASE}/app/inbox", wait_until="load")
        row = page.query_selector(".mh-thread-row")
        if row:
            row.click()
            page.wait_for_load_state("load")
        # The draft is raised from the inline ask box and streams into the reply
        # textarea (there is no separate preview panel), so wait on the field
        # settling rather than on a panel that no longer exists.
        go = page.query_selector("[data-ask-go]")
        if go:
            go.click()
            try:
                page.wait_for_function(
                    "()=>{const t=document.getElementById('replyBody');"
                    " return t && !t.classList.contains('is-bridget-writing')"
                    " && t.value.trim().length>20;}", timeout=25000)
                cap = ("Bridget reads the conversation and drafts the reply. "
                       "A coordinator edits and sends, nothing goes out on "
                       "its own.")
                if _shoot(page, "bridget_draft.png", ".mh-composer", cap):
                    captions.append(("bridget_draft.png", cap))
            except Exception:
                print("!! bridget_draft.png  draft never settled")
        else:
            print("!! bridget_draft.png  no ask box on this thread")

        # Patient portal - the participant's own view of their trial.
        if PORTAL_TOKEN and PORTAL_PASSWORD:
            page.goto(f"{BASE}/portal/{PORTAL_TOKEN}", wait_until="load")
            pw = page.query_selector("input[type=password]")
            if pw:
                pw.fill(PORTAL_PASSWORD)
                page.click("button[type=submit]")
                page.wait_for_load_state("load")
                page.wait_for_timeout(600)
            cap = ("Participants follow their own trial the way they track a "
                   "delivery, what happened, what's next, and what to bring.")
            if _shoot(page, "portal.png", ".portal-visits, main", cap):
                captions.append(("portal.png", cap))
        else:
            print("-- portal.png  skipped (set PORTAL_TOKEN and PORTAL_PASSWORD)")

        browser.close()

    print(f"\n{len(captions)} shot(s) -> {OUT}\n")
    print("Captions to use with each image:")
    for name, cap in captions:
        print(f"\n  {name}\n    {cap}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
