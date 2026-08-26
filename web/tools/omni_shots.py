"""Render every Omni screen at desktop and phone widths, run DOM checks, and
save screenshots. Usage:

    OMNI_BASE=http://127.0.0.1:5055 OMNI_OUT=/tmp/omni_shots \
        ../.venv/bin/python tools/omni_shots.py [template_key]

Exits non-zero if any DOM check fails. The screenshots are meant to be looked
at; the checks catch what eyes miss (overflow, clipped rows, wrong first
control, em dashes).
"""
import json
import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("OMNI_BASE", "http://127.0.0.1:5055")
OUT = os.environ.get("OMNI_OUT", "/tmp/omni_shots")
TEMPLATE = sys.argv[1] if len(sys.argv) > 1 else "pi_law_firm"
SIZES = [("desk", 1280, 900, False), ("phone", 390, 844, True)]

CHECKS = """() => {
  const r = {};
  r.scrollWidth = document.documentElement.scrollWidth;
  r.innerWidth = window.innerWidth;
  r.noOverflow = document.documentElement.scrollWidth <= window.innerWidth + 1;
  r.emDash = document.body.innerText.includes('\\u2014');
  const fb = document.querySelector('.mh-filterbar');
  if (fb) { r.filterbarFits = fb.scrollWidth <= fb.clientWidth + 1;
            const btn = document.querySelector('.mh-filter-btn');
            r.filterBtnOnScreen = btn ? btn.getBoundingClientRect().right <= window.innerWidth : null; }
  const form = document.querySelector('form[data-compose-panel="reply"]');
  if (form) { const first = form.querySelector('input:not([type=hidden]), textarea, button, select');
              r.composerFirstIsAsk = !!(first && first.classList.contains('mh-ask-input'));
              const rb = document.getElementById('replyBody');
              const b = rb ? rb.getBoundingClientRect() : null;
              r.composerOnScreen = b ? (b.bottom <= window.innerHeight + 1 && b.top >= 0) : null;
              r.composerVisible = b ? (b.width > 0 && b.height > 0) : null; }
  const panel = document.querySelector('[data-field-count]');
  if (panel) { r.fieldCount = parseInt(panel.getAttribute('data-field-count'), 10);
               r.fieldRows = panel.querySelectorAll('.om-field').length; }
  const chips = document.querySelectorAll('.om-examples .mh-quick-chip');
  if (chips.length) r.chips = chips.length;
  const rows = document.querySelectorAll('.mh-thread-row');
  if (rows.length) { r.rows = rows.length;
    const stack = document.querySelector('.mh-thread-stack');
    r.rowsClipped = Array.from(rows).filter(x => x.scrollWidth > x.clientWidth + 1).length; }
  const wsEl = document.querySelector('.mh-workspace');
  if (wsEl) { const conv = document.querySelector('.mh-conversation'); const col = document.querySelector('.mh-thread-column');
              r.convVisible = conv ? getComputedStyle(conv).display !== 'none' : null;
              r.listVisible = col ? getComputedStyle(col).display !== 'none' : null; }
  return r;
}"""


def check(page, name, expect):
    got = page.evaluate(CHECKS)
    fails = []
    for k, v in expect.items():
        if got.get(k) != v:
            fails.append(f"{k}={got.get(k)!r} (want {v!r})")
    status = "ok " if not fails else "BAD"
    print(f"[{status}] {name}: {json.dumps(got)}")
    if fails:
        print("       fails:", "; ".join(fails))
    return not fails


def shot(page, name):
    path = os.path.join(OUT, name + ".png")
    page.screenshot(path=path)
    return path


def main():
    os.makedirs(OUT, exist_ok=True)
    ok = True
    with sync_playwright() as p:
        b = p.chromium.launch()
        for tag, w, h, mobile in SIZES:
            ctx = b.new_context(viewport={"width": w, "height": h}, device_scale_factor=2,
                                is_mobile=mobile, has_touch=mobile)
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

            page.goto(BASE + "/omni", wait_until="load")
            page.wait_for_selector(".om-examples .mh-quick-chip")
            ok &= check(page, f"landing/{tag}", {"noOverflow": True, "emDash": False, "chips": 10})
            shot(page, f"landing_{tag}")

            chip = page.locator(f'.om-examples [data-example="{TEMPLATE}"]')
            chip.click()
            page.wait_for_function("() => document.getElementById('omPrompt').value.length > 20")
            chip.click()
            page.wait_for_url("**/omni/w/**/build", timeout=20000)
            # Builder: the first question is the sources tiles; keep the defaults
            # and continue, then pick fields, then screenshot mid-interview.
            page.wait_for_selector(".om-msg.is-question [data-continue]", state="visible", timeout=15000)
            ok &= check(page, f"build-start/{tag}", {"noOverflow": True, "emDash": False})
            shot(page, f"build_start_{tag}")
            page.locator(".om-msg.is-question:last-of-type [data-continue]").first.click()
            page.wait_for_function("() => document.querySelectorAll('.om-msg.is-question').length >= 2", timeout=15000)
            page.wait_for_timeout(1300)
            q2 = page.locator(".om-msg.is-question").last
            if q2.locator("[data-continue]").count():
                q2.locator("[data-continue]").first.click()
            elif q2.locator(".om-chips .mh-quick-chip").count():
                q2.locator(".om-chips .mh-quick-chip").first.click()
            page.wait_for_function("() => document.querySelectorAll('.om-msg.is-question').length >= 3", timeout=15000)
            page.wait_for_timeout(1300)
            ok &= check(page, f"build-mid/{tag}", {"noOverflow": True, "emDash": False})
            shot(page, f"build_mid_{tag}")
            if mobile:
                page.locator('[data-build-tab="preview"]').click()
                page.wait_for_timeout(300)
                ok &= check(page, f"build-preview/{tag}", {"noOverflow": True})
                shot(page, f"build_preview_{tag}")
                page.locator('[data-build-tab="chat"]').click()
            page.locator("[data-skip]").click()
            page.wait_for_selector("[data-build]", state="visible", timeout=15000)
            page.wait_for_timeout(1200)
            ok &= check(page, f"build-summary/{tag}", {"noOverflow": True, "emDash": False})
            shot(page, f"build_summary_{tag}")
            page.locator("[data-build]").click()
            page.wait_for_url(lambda u: "/build" not in u and "/omni/w/" in u, timeout=30000)
            page.wait_for_selector(".mh-thread-row")
            ok &= check(page, f"inbox-list/{tag}",
                        {"noOverflow": True, "emDash": False, "rowsClipped": 0,
                         "filterBtnOnScreen": True, "listVisible": True})
            shot(page, f"inbox_list_{tag}")

            page.locator(".mh-thread-row").first.click()
            page.wait_for_selector("#replyBody")
            page.wait_for_timeout(300)
            expect = {"noOverflow": True, "emDash": False, "composerFirstIsAsk": True,
                      "composerVisible": True, "convVisible": True}
            if mobile:
                expect["listVisible"] = False
            ok &= check(page, f"inbox-thread/{tag}", expect)
            shot(page, f"inbox_thread_{tag}")
            if mobile:
                page.locator('[data-detail-tab="applicant"]').click()
                page.wait_for_timeout(200)
                ok &= check(page, f"inbox-details/{tag}", {"noOverflow": True})
                shot(page, f"inbox_details_{tag}")
                page.locator('[data-detail-tab="conversation"]').click()

            # Composer prompt line writes the draft into the reply box.
            page.locator("[data-ask-input]").fill("")
            page.locator("[data-ask-input]").press("Enter")
            page.wait_for_function("() => document.getElementById('replyBody').value.length > 20", timeout=15000)
            page.wait_for_timeout(1700)
            ok &= check(page, f"inbox-draft/{tag}", {"noOverflow": True, "emDash": False})
            shot(page, f"inbox_draft_{tag}")

            wid = page.url.split("/omni/w/")[1].split("?")[0].split("/")[0]

            # Agent action from the side panel: a proposal card appears in the thread.
            if not mobile:
                page.locator('.om-panel details:has(.om-actions) summary').click()
                page.locator('[data-agent-action="booking"]').click()
                page.wait_for_selector(".om-proposal", timeout=15000)
                page.wait_for_timeout(300)
                ok &= check(page, f"inbox-proposal/{tag}", {"noOverflow": True, "emDash": False})
                shot(page, f"inbox_proposal_{tag}")
                page.locator(".om-proposal [data-act='confirm']").first.click()
                page.wait_for_function("() => !document.querySelector('.om-proposal') || document.querySelector('.om-proposal.is-done')", timeout=15000)
                page.wait_for_timeout(1200)
                page.wait_for_selector("#replyBody")
                sent = page.locator(".mh-message.is-outbound").count()
                print(f"[{'ok ' if sent else 'BAD'}] agent-confirm/{tag}: outbound messages={sent}")
                ok &= bool(sent)

            # Setup change by prompt: proposal dock, then apply.
            fields_before = page.evaluate("() => parseInt((document.querySelector('[data-field-count]')||{getAttribute:()=>'0'}).getAttribute('data-field-count'), 10)")
            page.locator("#omSetupAsk").fill("add a field for whether a police report was filed")
            page.locator("#omSetupAsk").press("Enter")
            page.wait_for_selector(".om-change", timeout=15000)
            page.wait_for_timeout(300)
            ok &= check(page, f"setup-proposal/{tag}", {"noOverflow": True, "emDash": False})
            shot(page, f"setup_proposal_{tag}")
            page.locator('[data-change-act="apply"]').click()
            page.wait_for_selector(".flash-toast", timeout=20000)
            page.wait_for_selector("#replyBody", timeout=15000)
            page.wait_for_timeout(300)
            fields_after = page.evaluate("() => parseInt((document.querySelector('[data-field-count]')||{getAttribute:()=>'0'}).getAttribute('data-field-count'), 10)")
            print(f"[{'ok ' if fields_after == fields_before + 1 else 'BAD'}] setup-applied/{tag}: fields {fields_before} -> {fields_after}")
            ok &= fields_after == fields_before + 1
            shot(page, f"setup_applied_{tag}")

            for sub in ("connect", "rules", "setup"):
                page.goto(f"{BASE}/omni/w/{wid}/{sub}", wait_until="load")
                page.wait_for_timeout(200)
                ok &= check(page, f"{sub}/{tag}", {"noOverflow": True, "emDash": False})
                shot(page, f"{sub}_{tag}")
            if errors:
                print(f"[BAD] js errors at {tag}: {errors[:5]}")
                ok = False
            ctx.close()
        b.close()
    print("ALL OK" if ok else "FAILURES")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
