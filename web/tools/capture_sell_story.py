"""Capture sell-story assets from the real product UI.

    PORT=5001 SITE_DEMO=1 NO_LOGIN=1 ../.venv/bin/python web/app.py
    SHOT_BASE=http://127.0.0.1:5001 ../.venv/bin/python tools/capture_sell_story.py

Writes to static/shots/:

  sell_01_intake.png        Intake channels (also sell_02_accounts.png)
  sell_02_applicants.png    Scored applicant list
  sell_03_blast.png         Send a blast modal
  sell_gif_04_bridget.gif   Tell Bridget what to say → reply drafts in
"""
from __future__ import annotations

import io
import os
import pathlib
import re
import shutil
import sys

from PIL import Image
from playwright.sync_api import sync_playwright

BASE = os.environ.get("SHOT_BASE", "http://127.0.0.1:5001").rstrip("/")
OUT = pathlib.Path(__file__).resolve().parent.parent / "static" / "shots"
VIEWPORT = {"width": 1600, "height": 960}
# Narrow enough that the conversation card matches the SMALL landing slot
# (448x392) at a 1:1 pixel scale: a 520px-wide card had to shrink to fit the
# slot, which turned the draft into 9px text. Tall enough that the whole
# message + composer scene fits: a clip clamps at the viewport edge, and a
# too-small window shipped GIF frames cut mid-sentence.
BRIDGET_VIEWPORT = {"width": 470, "height": 900}
BRIDGET_CARD_W = BRIDGET_VIEWPORT["width"] - 36     # workspace padding 18px each side
SCALE = 2
CARD_SHOT_BG = "#f5f7fb"
# Modal-capture viewports are TALLER than any card so nothing is ever clipped by
# the window (.ui-modal-card scrolls internally at max-height:calc(100vh-40px),
# which used to chop the bottom of the card out of the capture).
# 660 not 640: the account rows stack their "Connected" pill under the copy at
# <=640px, and a stacked row is what left the intake card squat and padded.
INTAKE_SHOT = {"width": 660, "height": 1000}
BLAST_SHOT = {"width": 640, "height": 1200}
# Landing feature-card slots are 640x392 (big) / 448x392 (small) with
# object-fit:cover, so exports MUST match those aspects exactly at 2x or the
# page crops the shot.
CARD_EXPORT = (1280, 784)        # big card slot, 640x392 @2x
CARD_EXPORT_SMALL = (896, 784)   # small card slot, 448x392 @2x
# Breathing room around the product card as a fraction of the export, x and y.
# Intake uses (0, 0): the card should edge-fill the slot; gray bands read as dead
# space on the landing grid (see sell_01_intake in #features).
CANVAS_MARGIN = (0.03, 0.04)
INTAKE_MARGIN = (0, 0)
INTAKE_COVER_MAX_SCALE = 1.18  # fill short cards without obvious blur


def _dismiss(page):
    page.evaluate("""()=>{
      document.querySelectorAll('.toast-stack,.demo-tour,[data-demo-tour]')
        .forEach(e=>e.remove());
    }""")


def _save_png(page, name: str, clip=None):
    path = OUT / name
    path.write_bytes(page.screenshot(type="png", clip=clip, animations="disabled"))
    kb = path.stat().st_size // 1024
    print(f"ok {name} ({kb}KB)")
    return path


def _clip_el(page, selector: str, pad: int = 12):
    return page.evaluate("""([sel, pad])=>{
      const el = document.querySelector(sel);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return {
        x: Math.max(0, Math.floor(r.x - pad)),
        y: Math.max(0, Math.floor(r.y - pad)),
        width: Math.min(Math.ceil(r.width + pad * 2), window.innerWidth),
        height: Math.min(Math.ceil(r.height + pad * 2), window.innerHeight),
      };
    }""", [selector, pad])


def _frame_png(page, clip=None, *, animations: str = "disabled") -> Image.Image:
    return Image.open(io.BytesIO(
        page.screenshot(type="png", clip=clip, animations=animations)))


def _canvas_box(export_size: tuple[int, int], margin: tuple[float, float] | None = None):
    w, h = export_size
    mx_f, my_f = margin if margin is not None else CANVAS_MARGIN
    mx, my = int(w * mx_f), int(h * my_f)
    return mx, my, w - mx * 2, h - my * 2


def _fit_scale(im: Image.Image, export_size: tuple[int, int],
               margin: tuple[float, float] | None = None) -> float:
    """Largest scale at which `im` fits the canvas box, never upscaled.

    Capped at 1.0: every capture is already at device_scale_factor 2, so
    stretching it past that only blurs the UI text it is meant to show off.
    """
    _, _, avail_w, avail_h = _canvas_box(export_size, margin)
    return min(avail_w / im.width, avail_h / im.height, 1.0)


def _cover_scale(im: Image.Image, export_size: tuple[int, int],
                 margin: tuple[float, float] | None = None,
                 max_scale: float = 99.0) -> float:
    """Scale so `im` covers the canvas box (CSS background-size: cover)."""
    _, _, avail_w, avail_h = _canvas_box(export_size, margin)
    return min(max(avail_w / im.width, avail_h / im.height), max_scale)


def _composite_on_canvas(im: Image.Image, export_size: tuple[int, int], *,
                         scale: float | None = None,
                         anchor: str = "center",
                         fill: str = "contain",
                         margin: tuple[float, float] | None = None,
                         max_scale: float = 1.0) -> Image.Image:
    im = im.convert("RGBA")
    margin_x, margin_y, avail_w, avail_h = _canvas_box(export_size, margin)
    if scale is None:
        scale = (_cover_scale(im, export_size, margin, max_scale=max_scale)
                 if fill == "cover"
                 else _fit_scale(im, export_size, margin))
    new_w = max(1, int(im.width * scale))
    new_h = max(1, int(im.height * scale))
    if (new_w, new_h) != im.size:
        im = im.resize((new_w, new_h), Image.Resampling.LANCZOS)
    if fill == "cover":
        # Crop the scaled card to the export rect; top-anchor keeps the header visible.
        left = max(0, (new_w - avail_w) // 2)
        top = 0 if anchor == "top" else max(0, (new_h - avail_h) // 2)
        crop = im.crop((left, top, left + avail_w, top + avail_h))
        canvas = Image.new("RGB", export_size, CARD_SHOT_BG)
        canvas.paste(crop.convert("RGB"), (margin_x, margin_y))
        return canvas
    canvas = Image.new("RGB", export_size, CARD_SHOT_BG)
    x = margin_x + (avail_w - new_w) // 2
    # Stills centre vertically so a card shorter than the slot floats mid-card
    # rather than leaving one deep band at the bottom. Animation frames anchor
    # to the top so the card's top edge (and the message above the composer)
    # stays put while the composer below it grows and shrinks.
    y = margin_y if anchor == "top" else margin_y + (avail_h - new_h) // 2
    canvas.paste(im, (x, y), im)
    return canvas


def _save_gif(frames: list[Image.Image], name: str, durations: list[int],
              export_size: tuple[int, int] = CARD_EXPORT):
    path = OUT / name
    if not frames:
        raise RuntimeError(f"no frames for {name}")
    # One scale for the whole clip, chosen by the tallest frame. Fitting each
    # frame on its own made the card zoom in and out as the ask box came and
    # went, which read as a glitch rather than a draft arriving.
    scale = min(_fit_scale(im, export_size) for im in frames)
    out: list[Image.Image] = []
    for im in frames:
        out.append(_composite_on_canvas(im, export_size, scale=scale, anchor="top")
                   .convert("P", palette=Image.Palette.ADAPTIVE, colors=256))
    out[0].save(
        path, save_all=True, append_images=out[1:],
        duration=durations, loop=0, optimize=True,
    )
    kb = path.stat().st_size // 1024
    print(f"ok {name} ({len(out)} frames, {kb}KB)")


def _snap(page, frames, durs, ms, clip_fn, *, animations: str = "allow"):
    clip = clip_fn(page)
    if clip:
        frames.append(_frame_png(page, clip, animations=animations))
        durs.append(ms)


def _bridget_clip(page, pad: int = 10):
    """The whole conversation card (message + composer) plus its shadow."""
    return page.evaluate("""(pad) => {
      const conv = document.querySelector('.mh-conversation');
      if (!conv) return null;
      const r = conv.getBoundingClientRect();
      if (r.width < 80) return null;
      return {
        x: Math.max(0, Math.floor(r.left - pad)),
        y: Math.max(0, Math.floor(r.top - pad)),
        width: Math.min(Math.ceil(r.width + pad * 2), window.innerWidth),
        height: Math.min(Math.ceil(r.height + pad * 2), window.innerHeight),
      };
    }""", pad)


def _prep_bridget_shot(page):
    """Wide conversation card; hiding the thread list must not collapse width."""
    page.evaluate("""([bg, cardW]) => {
      document.body.classList.add('sell-bridget-shot');
      let s = document.getElementById('sellBridgetShotStyle');
      if (!s) {
        s = document.createElement('style');
        s.id = 'sellBridgetShotStyle';
        document.head.appendChild(s);
      }
      s.textContent = `
        body.sell-bridget-shot { background: ${bg} !important; margin: 0; overflow: hidden; }
        body.sell-bridget-shot .appnav, body.sell-bridget-shot .appside,
        body.sell-bridget-shot .apptop { display: none !important; }
        body.sell-bridget-shot .mh-sources,
        body.sell-bridget-shot .mh-thread-column,
        body.sell-bridget-shot .mh-applicant-panel,
        body.sell-bridget-shot #copilot,
        body.sell-bridget-shot .mh-conversation-head,
        body.sell-bridget-shot .mh-subject-bar,
        body.sell-bridget-shot .mh-clinical-card,
        body.sell-bridget-shot .mh-link-applicant,
        body.sell-bridget-shot .mh-detail-tabs { display: none !important; }
        body.sell-bridget-shot .appbody { margin: 0 !important; padding: 0 !important; }
        body.sell-bridget-shot .mh-workspace {
          display: block !important; height: auto !important; max-height: none !important;
          padding: 14px 18px 0 !important; background: ${bg} !important;
          overflow: visible !important;
        }
        body.sell-bridget-shot .mh-conversation {
          display: flex !important; flex-direction: column !important;
          width: 100% !important; max-width: ${cardW}px !important; min-width: 0 !important;
          margin: 0 auto !important; min-height: 0 !important;
          /* Content-sized: the app stretches this pane to the viewport, and a
             clip taken through a 900px blank card has no bottom edge. */
          height: auto !important; max-height: none !important; flex: 0 0 auto !important;
          background: var(--surface) !important;
          border: 1px solid rgba(18,87,176,.10) !important; border-radius: 16px !important;
          box-shadow: 0 12px 32px rgba(16,24,40,.08) !important;
          overflow: hidden !important;
        }
        body.sell-bridget-shot .mh-detail-layout { display: block !important; min-height: 0 !important;
          height: auto !important; max-height: none !important; }
        body.sell-bridget-shot .mh-dialogue {
          display: flex !important; flex-direction: column !important; min-height: 0 !important;
        }
        body.sell-bridget-shot .mh-message-list {
          flex: 0 0 auto !important; min-height: 0 !important; max-height: none !important;
          overflow: visible !important; padding-bottom: 6px !important;
        }
        body.sell-bridget-shot .mh-composer { margin-bottom: 0 !important; flex: 0 0 auto !important; }
        /* The footer stays so the clip ends on a draft with its Send button
           under it - the reply is ready to go, not just written. Only the
           delivery hint is dropped: it is empty in demo mode anyway. */
        body.sell-bridget-shot .mh-compose-foot { display: flex !important; margin-top: 10px !important; }
        body.sell-bridget-shot .mh-compose-foot > span { display: none !important; }
        /* The product folds the Bridget row away while it writes; the clip
           keeps it so the instruction sits above the draft as it streams in,
           and the card holds one height for every frame. */
        body.sell-bridget-shot .mh-ask-wrap:has(#replyBody.is-bridget-writing) .mh-ask { display: flex !important; }
        /* Fixed height: the draft is three lines here, and a box that grows
           as text streams in would push the card's bottom edge every frame. */
        body.sell-bridget-shot .mh-compose-panel textarea { min-height: 96px !important; max-height: none !important; }
      `;
      document.querySelector('.mh-detail-layout')?.classList.remove('has-applicant');
      const msgs = [...document.querySelectorAll('.mh-message-list .mh-message')];
      msgs.slice(0, -1).forEach(m => m.remove());
      const list = document.querySelector('.mh-message-list');
      if (list) list.scrollTop = 0;
      const reply = document.querySelector('#replyBody');
      if (reply) {
        reply.value = '';
        reply.classList.remove('is-bridget-writing');
        reply.dispatchEvent(new Event('input', { bubbles: true }));
      }
      const ask = document.querySelector('[data-ask-input]');
      if (ask) ask.value = '';
      const askBox = document.querySelector('.mh-ask[data-bridget-draft]');
      if (askBox) {
        askBox.classList.remove('is-busy', 'is-error');
        askBox.style.display = '';
      }
      // Chips stay hidden: the instruction is typed into the Bridget row,
      // and without the chip row the ask state and the draft state are the
      // same height, so the card never jumps between frames.
      const suggest = document.querySelector('.mh-ask-suggest');
      if (suggest) suggest.hidden = true;
    }""", [CARD_SHOT_BG, BRIDGET_CARD_W])


def _trim_shot_margins(im: Image.Image, bg=(245, 247, 251), tol: int = 10) -> Image.Image:
    """Drop empty bands around a card screenshot before compositing."""
    im = im.convert("RGB")
    w, h = im.size
    px = im.load()

    def bg_at(x: int, y: int) -> bool:
        r, g, b = px[x, y]
        return (abs(r - bg[0]) <= tol and abs(g - bg[1]) <= tol
                and abs(b - bg[2]) <= tol)

    top = bottom = left = right = None
    for y in range(h):
        if any(not bg_at(x, y) for x in range(0, w, 3)):
            top = y
            break
    for y in range(h - 1, -1, -1):
        if any(not bg_at(x, y) for x in range(0, w, 3)):
            bottom = y + 1
            break
    for x in range(w):
        if any(not bg_at(x, y) for y in range(0, h, 3)):
            left = x
            break
    for x in range(w - 1, -1, -1):
        if any(not bg_at(x, y) for y in range(0, h, 3)):
            right = x + 1
            break
    if None in (top, bottom, left, right):
        return im
    return im.crop((left, top, right, bottom))


def _save_card_fill_png(page, card_selector: str, name: str, export_size: tuple[int, int],
                        *, fill: str = "contain", margin: tuple[float, float] | None = None,
                        max_scale: float = 1.0):
    """Place the modal card on a landing-matched canvas so it fills the slot."""
    card = page.locator(card_selector)
    card.wait_for(state="visible")
    card_im = Image.open(io.BytesIO(card.screenshot(type="png")))
    card_im = _trim_shot_margins(card_im)
    canvas = _composite_on_canvas(
        card_im, export_size, fill=fill, anchor="top",
        margin=margin, max_scale=max_scale)
    path = OUT / name
    canvas.save(path, optimize=True)
    kb = path.stat().st_size // 1024
    print(f"ok {name} ({kb}KB)")
    return path


def _normalize_card_png(path: pathlib.Path, size: tuple[int, int]):
    im = Image.open(path).convert("RGB")
    im = im.resize(size, Image.Resampling.LANCZOS)
    im.save(path, optimize=True)


def _floating_modal_shot(page, modal_id: str, *, hide_head: bool = False, card_max: str = "100%"):
    """Card fills the Framer image slot; background matches the landing band."""
    page.evaluate("""([modalId, hideHead, cardMax, bg]) => {
      document.body.classList.add('sell-modal-shot');
      let s = document.getElementById('sellModalShotStyle');
      if (!s) {
        s = document.createElement('style');
        s.id = 'sellModalShotStyle';
        document.head.appendChild(s);
      }
      s.textContent = `
        body.sell-modal-shot { background: ${bg} !important; margin: 0; overflow: hidden; }
        body.sell-modal-shot .appnav, body.sell-modal-shot .appside,
        body.sell-modal-shot .apptop, body.sell-modal-shot .layout { display: none !important; }
        body.sell-modal-shot ${modalId} {
          position: fixed !important; inset: 0 !important; z-index: 99999 !important;
          display: flex !important; align-items: flex-start !important;
          justify-content: center !important; background: ${bg} !important;
          padding: 0 !important; margin: 0 !important; border: 0 !important;
          box-shadow: none !important; overflow: hidden !important;
        }
        body.sell-modal-shot ${modalId} .ui-modal-backdrop { display: none !important; }
        body.sell-modal-shot ${modalId} .ui-modal-card {
          margin: 0 !important; width: 100% !important; max-width: ${cardMax} !important;
          max-height: none !important; overflow: visible !important;
          box-shadow: 0 12px 32px rgba(16,24,40,.08) !important;
          border: 1px solid rgba(18,87,176,.10) !important; border-radius: 16px !important;
        }
        ${hideHead ? `body.sell-modal-shot ${modalId} .ui-modal-head { display: none !important; }` : ''}
      `;
      document.querySelectorAll('.mh-sell-lead').forEach(el => el.remove());
    }""", [modal_id, hide_head, card_max, CARD_SHOT_BG])


def _gray_canvas(page):
    """Hide app chrome so the focused component floats on gray like a modal."""
    # Same bg as the composite canvas: an element screenshot includes the page
    # behind its rounded corners, and any other color shows as corner notches.
    page.evaluate("""(bg)=>{
      document.body.classList.add('sell-capture');
      if (!document.getElementById('sellCaptureStyle')) {
        const s = document.createElement('style');
        s.id = 'sellCaptureStyle';
        s.textContent = `
          body.sell-capture { background: ${bg} !important; }
          body.sell-capture .appnav,
          body.sell-capture .appside,
          body.sell-capture .apptop { display: none !important; }
          body.sell-capture .appbody {
            margin: 0 !important; padding: 40px 32px !important;
            min-height: 100vh; box-sizing: border-box;
            display: flex; align-items: flex-start; justify-content: center;
          }
          body.sell-capture .layout { min-height: auto; }
        `;
        document.head.appendChild(s);
      }
    }""", CARD_SHOT_BG)


def capture_intake_png(page):
    """Intake channels modal with real channel logos."""
    page.goto(f"{BASE}/app/inbox", wait_until="load")
    page.wait_for_selector(".mh-thread-row, .mh-conversation", timeout=10000)
    _dismiss(page)
    page.locator('[data-ui-open="sourceModal"]').first.click()
    page.wait_for_selector("#sourceModal .ui-modal-card", timeout=5000)
    page.wait_for_timeout(300)

    page.evaluate("""()=>{
      const modal = document.querySelector('#sourceModal');
      if (!modal) return;
      const title = modal.querySelector('.ui-modal-head h3');
      if (title) {
        const ico = title.querySelector('.ico');
        title.textContent = '';
        if (ico) title.appendChild(ico);
        title.appendChild(document.createTextNode(' Intake channels'));
      }
      const body = modal.querySelector('.ui-modal-body');
      if (!body) return;

      body.querySelectorAll('.mh-gmail-connect, .mh-instagram-connect, .mh-connect-divider, #sourceForm, .mh-modal-form')
        .forEach(e => e.remove());

      const list = body.querySelector('.mh-account-list');
      if (!list) return;
      const head = list.querySelector('header');
      if (head) {
        head.innerHTML = '<b>Connected for intake</b><span>All channels live</span>';
      }

      const stories = [
        { key: 'email', match: (ch, label) => ch === 'email' || label.includes('gmail') || label.includes('inbox') || label.includes('newsletter') },
        { key: 'instagram', match: (ch, label) => ch === 'instagram' || label.includes('instagram') },
        { key: 'google_ads', match: (ch, label) => (ch === 'google_ads' || label.includes('ads') || label.includes('google ads')) && !label.includes('facebook') },
        { key: 'facebook', match: (ch, label) => label.includes('facebook') },
        { key: 'referrals', match: (ch, label) => label.includes('referral') || label.includes('physician') || ch === 'referral' },
      ];
      const picked = new Map();
      const rows = Array.from(list.querySelectorAll('.mh-account-row'));
      rows.forEach(row => {
        const ic = row.querySelector('.mh-channel-ic');
        let ch = '';
        if (ic) {
          const m = (ic.className || '').match(/is-([a-z_]+)/);
          if (m) ch = m[1] === 'google' ? 'google_ads' : m[1];
        }
        const label = (row.querySelector('b')?.textContent || '').toLowerCase();
        for (const s of stories) {
          if (!picked.has(s.key) && s.match(ch, label)) {
            picked.set(s.key, row);
            return;
          }
        }
        row.remove();
      });
      rows.forEach(row => {
        if (![...picked.values()].includes(row)) row.remove();
      });
      stories.forEach(s => {
        const row = picked.get(s.key);
        if (row) list.appendChild(row);
      });

      const nice = {
        email: ['Gmail', 'Site recruitment inbox'],
        instagram: ['Instagram', 'DMs from ads & profile'],
        google_ads: ['Google Ads', 'Lead form inquiries'],
        facebook: ['Facebook', 'Page messages & leads'],
        referrals: ['Referrals', 'Physician & site handoffs'],
      };
      stories.forEach(s => {
        const row = picked.get(s.key);
        if (!row) return;
        const copy = row.querySelector('.mh-account-copy');
        if (copy && nice[s.key]) {
          copy.innerHTML = `<b>${nice[s.key][0]}</b><small>${nice[s.key][1]}</small>`;
        }
        const actions = row.querySelector('.mh-account-actions');
        if (actions) {
          actions.innerHTML = '<span class="mh-sell-connected">Connected</span>';
        }
        if (s.key === 'instagram') {
          const ic = row.querySelector('.mh-channel-ic');
          if (ic) {
            ic.className = 'mh-channel-ic is-instagram';
            ic.innerHTML = '<img class="brand-ico" src="https://logos-api.apistemic.com/domain:instagram.com" alt="" width="24" height="24" style="width:24px;height:24px;border-radius:6px;object-fit:cover">';
          }
        }
        if (s.key === 'facebook') {
          const ic = row.querySelector('.mh-channel-ic');
          if (ic) {
            ic.className = 'mh-channel-ic is-facebook';
            ic.innerHTML = '<svg class="brand-ico" viewBox="0 0 24 24" aria-hidden="true"><rect width="24" height="24" rx="6" fill="#0866FF"/><path fill="#fff" d="M15.35 12.5l.42-2.63h-2.52V8.16c0-.72.35-1.42 1.48-1.42h1.15V4.5s-1.04-.18-2.04-.18c-2.08 0-3.44 1.26-3.44 3.54v2.01H8.05v2.63h2.35V19h2.85v-6.5z"/></svg>';
          }
        }
      });

      // Fifth row when the demo has no referral account wired yet.
      if (!picked.has('referrals') && picked.get('facebook')) {
        const ref = picked.get('facebook').cloneNode(true);
        ref.querySelector('.mh-account-copy').innerHTML = '<b>Referrals</b><small>Physician & site handoffs</small>';
        const ic = ref.querySelector('.mh-channel-ic');
        if (ic) {
          ic.className = 'mh-channel-ic is-email';
          ic.innerHTML = '<svg class="brand-ico" viewBox="0 0 24 24" aria-hidden="true"><rect width="24" height="24" rx="6" fill="#1257b0"/><path fill="#fff" d="M12 6.2a4.2 4.2 0 1 0 0 8.4 4.2 4.2 0 0 0 0-8.4Zm0 6.8a2.6 2.6 0 1 1 0-5.2 2.6 2.6 0 0 1 0 5.2Zm5.8-6.3a1 1 0 1 0-1.7-1 5.8 5.8 0 0 1-8.2 0 1 1 0 1 0-1.4 1.4 7.8 7.8 0 0 0 11 0 1 1 0 0 0-.7-1.4Z"/></svg>';
        }
        ref.querySelector('.mh-account-actions').innerHTML = '<span class="mh-sell-connected">Connected</span>';
        list.appendChild(ref);
      }

      const style = document.createElement('style');
      style.textContent = `
        /* One frame, not a card inside a card: the modal's padding and the
           list's own border drew two nested boxes, and the inner one is the
           whole picture. */
        #sourceModal .ui-modal-card { padding: 0 !important; overflow: hidden !important;
          width: 640px !important; max-width: 640px !important;
          height: 392px !important; display: flex !important; flex-direction: column !important; }
        #sourceModal .ui-modal-body { padding: 0 !important; margin: 0 !important; flex: 1 !important;
          display: flex !important; flex-direction: column !important; min-height: 0 !important; }
        #sourceModal .mh-account-list { margin: 0 !important; border: 0 !important; border-radius: 0 !important;
          flex: 1 !important; display: flex !important; flex-direction: column !important; min-height: 0 !important; }
        #sourceModal .mh-account-list > header { padding: 18px 22px; flex: 0 0 auto; }
        #sourceModal .mh-account-list > header b { font-size: 16px; }
        #sourceModal .mh-account-list > header span { font-size: 13px; }
        /* Name, description and status on one line, so the card reads as a
           list of channels rather than a stack of pills. Tall rows so the card
           fills the landing slot vertically instead of floating with gray bands. */
        #sourceModal .mh-account-row { flex: 1 1 0 !important; flex-wrap: nowrap !important;
          align-items: center !important; gap: 16px !important; padding: 0 22px !important;
          min-height: 0 !important; }
        #sourceModal .mh-account-copy b { font-size: 16px; }
        #sourceModal .mh-account-copy small { font-size: 13.5px; }
        #sourceModal .mh-channel-ic { width: 34px !important; height: 34px !important; }
        #sourceModal .mh-account-actions { width: auto !important; padding-left: 0 !important;
          margin-left: auto; flex: 0 0 auto; }
        #sourceModal .mh-sell-connected {
          display: inline-flex; align-items: center; gap: 6px;
          padding: 5px 10px; border-radius: 999px;
          background: #e8f5ee; color: #1b6b45;
          font-size: 12.5px; font-weight: 650; letter-spacing: .01em;
        }
        #sourceModal .mh-sell-connected::before {
          content: ""; width: 7px; height: 7px; border-radius: 50%;
          background: #22a06b;
        }
      `;
      modal.appendChild(style);
    }""")
    page.set_viewport_size(INTAKE_SHOT)
    _floating_modal_shot(page, "#sourceModal", hide_head=True)
    page.wait_for_timeout(300)
    try:
        page.wait_for_function(
            """() => {
              const img = document.querySelector('#sourceModal .is-instagram img');
              return img && img.complete && img.naturalWidth > 0;
            }""",
            timeout=5000,
        )
    except Exception:
        page.wait_for_timeout(800)

    _save_card_fill_png(
        page, "#sourceModal .ui-modal-card", "sell_01_intake.png", CARD_EXPORT,
        fill="contain", margin=INTAKE_MARGIN, max_scale=1.0)
    shutil.copy2(OUT / "sell_01_intake.png", OUT / "sell_02_accounts.png")


def capture_applicants_png(page):
    """Real applicant table with product badges and avatars."""
    page.goto(f"{BASE}/app/leads", wait_until="load")
    page.wait_for_selector("#queueTable tbody tr", timeout=10000)
    _dismiss(page)
    _gray_canvas(page)

    page.evaluate("""()=>{
      document.querySelectorAll('.app-head .ui-btn, .app-filter, #queueEmpty, .app-chips, .appdock, #copilot')
        .forEach(e => e.remove());
      // Drop the Source / Last-activity columns for the card shot: with them the
      // table is wider than any readable card and the right edge gets clipped.
      document.querySelectorAll(
        '#queueTable th:nth-child(5), #queueTable td:nth-child(5),' +
        '#queueTable th:nth-child(6), #queueTable td:nth-child(6)'
      ).forEach(e => e.remove());
      const head = document.querySelector('.app-head');
      if (head) {
        const h1 = head.querySelector('h1');
        if (h1) h1.textContent = 'Applicants';
        head.querySelectorAll('p, .app-sub').forEach(e => {
          e.textContent = 'Scored and sorted so coordinators know who to call first.';
        });
      }
      document.querySelectorAll('.sel-col').forEach(c => c.style.display = 'none');
      const rows = [...document.querySelectorAll('#queueTable tbody tr')];
      rows.slice(5).forEach(r => r.remove());
      const ths = document.querySelectorAll('#queueTable thead .sel-col');
      ths.forEach(th => th.style.display = 'none');
      const card = document.querySelector('[data-demo-tour-target="applicants"]');
      if (card) {
        card.style.width = 'min(760px, 100%)';
        card.style.boxShadow = '0 22px 56px rgba(16,24,40,.10)';
        card.style.borderRadius = '16px';
      }
    }""")
    page.wait_for_timeout(250)

    # Element screenshot (not a hand-computed clip: that broke once and shipped a
    # sliver of the page), composited onto the BIG landing slot's exact aspect.
    _save_card_fill_png(page, '[data-demo-tour-target="applicants"]',
                        "sell_02_applicants.png", CARD_EXPORT)


def capture_blast_png(page):
    """Real blast modal from the applicants page."""
    page.goto(f"{BASE}/app/leads", wait_until="load")
    page.wait_for_selector("#queueTable tbody tr", timeout=10000)
    _dismiss(page)
    page.locator('[data-ui-open="broadcastModal"]').first.click()
    page.wait_for_selector("#broadcastModal:not([hidden])", timeout=5000)

    nct = page.locator("#blastNct")
    if nct.count() and nct.locator("option").count() > 1:
        nct.select_option(index=1)
    page.locator("#blastBody").fill(
        "Hi, screening slots opened this week. Reply with a morning or "
        "afternoon that works and we'll book you in."
    )
    try:
        page.wait_for_selector(".blast-count.is-ready", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(400)

    page.evaluate("""()=>{
      const modal = document.querySelector('#broadcastModal');
      if (!modal) return;
      const title = modal.querySelector('.ui-modal-head h3');
      if (title) {
        const ico = title.querySelector('.ico');
        title.textContent = '';
        if (ico) title.appendChild(ico);
        title.appendChild(document.createTextNode(' Reach applicants'));
      }
      modal.querySelectorAll('.blast-actions, input[name="task"], .ui-file').forEach(e => e.remove());
      modal.querySelectorAll('.field').forEach(f => {
        const lbl = (f.querySelector('label')?.textContent || '').toLowerCase();
        if (lbl.includes('to-do') || lbl.includes('attach') || lbl.includes('document')) f.remove();
      });
      // Keep the footer's Send button (the whole point of the card); drop Cancel.
      modal.querySelectorAll('.blast-note, .blast-foot [data-ui-close]').forEach(e => e.remove());
      const send = modal.querySelector('#blastSend');
      if (send && !/\\d/.test(send.textContent || '')) {
        const n = modal.querySelector('.blast-count-n')?.textContent?.match(/\\d+/)?.[0] || '9';
        send.innerHTML = send.innerHTML.replace(/Send blast/, 'Send to ' + n);
      }
      const style = document.createElement('style');
      style.textContent = `
        #broadcastModal .ui-modal-card { width: min(480px, 100%); }
        #broadcastModal .blast-note { font-size: 12px; }
        #broadcastModal #blastBody { min-height: 96px; max-height: 96px; }
      `;
      modal.appendChild(style);
    }""")
    page.set_viewport_size(BLAST_SHOT)
    _floating_modal_shot(page, "#broadcastModal")
    page.wait_for_timeout(300)

    _save_card_fill_png(page, "#broadcastModal .ui-modal-card", "sell_03_blast.png",
                        CARD_EXPORT_SMALL)


def capture_bridget_gif(page):
    """Smooth GIF: chip click → single-surface draft streams in."""
    page.goto(f"{BASE}/app/inbox?thread=82&status=open", wait_until="load")
    page.wait_for_selector("[data-ask-input]", timeout=10000)
    _dismiss(page)
    _prep_bridget_shot(page)
    page.wait_for_timeout(450)

    frames: list[Image.Image] = []
    durs: list[int] = []
    # One fixed clip for every frame: the scene gets shorter once the Bridget
    # ask box gives way to the draft, and re-measuring per frame made the card
    # jump around inside the gif.
    clip0 = _bridget_clip(page)
    snap = lambda ms: _snap(page, frames, durs, ms, lambda _p: clip0)

    # "Say what you want written": the instruction is typed into the Bridget
    # row, not picked from a chip, because typing IS the feature the card sells.
    # Short enough to sit whole in the Bridget row at this card width (about
    # 33 characters before the row clips it), and worded to land on the
    # "set up a call" rung of Bridget's demo ladder, whose three-line reply
    # fits the box. Off-ladder wording falls back to a five-line reply that
    # scrolls the opening line out of view.
    instruction = "Set up a call and ask for times"
    ask_input = page.locator("[data-ask-input]")
    ask_input.wait_for(state="visible")

    # Ask Bridget up front, the same request the row's send button makes, and
    # replay the answer under the clip's control. Letting the product write it
    # live raced the shutter: its stream had already put "Hi" on screen (and
    # cleared the instruction) before the scripted stream reset the box, so the
    # clip flashed a half-written reply, then an empty one, then the reply.
    draft = page.evaluate("""async (instruction) => {
      const askBox = document.querySelector('.mh-ask[data-bridget-draft]');
      const meta = document.querySelector('meta[name="csrf-token"]');
      const r = await fetch(askBox.getAttribute('data-bridget-draft'), {
        method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'Accept': 'application/json',
                  'X-CSRF-Token': meta ? meta.getAttribute('content') : ''},
        body: JSON.stringify({instruction}),
      });
      const data = await r.json();
      if (!r.ok || !data.draft) throw new Error('no draft: ' + JSON.stringify(data));
      return data.draft.trim();
    }""", instruction)

    snap(1100)
    snap(600)

    ask_input.click()
    typed = ""
    for chunk in re.findall(r".{1,4}", instruction):
        typed += chunk
        page.evaluate("""(text) => {
          const i = document.querySelector('[data-ask-input]');
          i.value = text;
          i.dispatchEvent(new Event('input', { bubbles: true }));
        }""", typed)
        snap(70)
    snap(600)

    # Sent: the row goes busy exactly as the product's click handler makes it.
    page.evaluate("""()=>{
      document.querySelector('.mh-ask').classList.add('is-busy');
      const go = document.querySelector('[data-ask-go]');
      if (go) go.disabled = true;
      document.querySelector('[data-ask-input]').blur();
    }""")
    snap(320)
    snap(320)

    page.evaluate("""()=>{
      const t = document.querySelector('#replyBody');
      if (t) {
        t.value = '';
        t.classList.add('is-bridget-writing');
        t.style.resize = 'none';
        t.style.animation = 'none';
        t.style.boxShadow = 'none';
      }
    }""")
    snap(220)

    partial = ""
    # Two words a frame: one word a frame doubled the file for a difference
    # the eye reads as the same streaming.
    for chunk in re.findall(r"\S+\s*(?:\S+\s*)?", draft):
        partial += chunk
        page.evaluate("""(text) => {
          const t = document.querySelector('#replyBody');
          t.value = text;
          // Grow rather than scroll: the point of the clip is reading the
          // whole reply, so the box is never allowed to hide a line of it.
          t.style.height = Math.max(96, t.scrollHeight) + 'px';
        }""", partial)
        snap(72)

    page.evaluate("""()=>{
      const t = document.querySelector('#replyBody');
      t.classList.remove('is-bridget-writing');
      t.style.resize = '';
      document.querySelector('.mh-ask').classList.remove('is-busy');
      const go = document.querySelector('[data-ask-go]');
      if (go) go.disabled = false;
    }""")
    snap(2400)
    snap(1000)

    _save_gif(frames, "sell_gif_04_bridget.gif", durs, CARD_EXPORT_SMALL)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=SCALE,
            reduced_motion="reduce",
        )
        page = ctx.new_page()
        page.goto(f"{BASE}/app/inbox", wait_until="load")
        if "/login" in page.url:
            print("!! redirected to login, start with SITE_DEMO=1 NO_LOGIN=1")
            return 1

        capture_intake_png(page)
        capture_applicants_png(page)
        capture_blast_png(page)

        gif_page = browser.new_context(
            viewport=BRIDGET_VIEWPORT,
            device_scale_factor=SCALE,
            reduced_motion="no-preference",
            color_scheme="light",
        ).new_page()
        capture_bridget_gif(gif_page)
        browser.close()

    print("\nCaptions:")
    print("  sell_01_intake.png         Gmail, Instagram, ads, connected in one place")
    print("  sell_02_applicants.png     Every applicant scored and sorted")
    print("  sell_03_blast.png          Email or text blast from one list")
    print("  sell_gif_04_bridget.gif    Tell Bridget what to say → reply drafts in")
    return 0


if __name__ == "__main__":
    sys.exit(main())
