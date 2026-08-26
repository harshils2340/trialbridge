"""Record the BridgeMD product demo as an MP4, driven through the real app.

Nothing here is a mockup: Playwright opens the real screens, clicks the real
controls and waits on the real responses, so the film goes stale the same way
the product changes - re-run it after a UI change rather than editing a video.

    # scratch server + scratch database (never the one on 5001)
    PORT=5055 SITE_DEMO=1 NO_LOGIN=1 DB_PATH=/tmp/demo.db ../.venv/bin/python web/app.py
    SHOT_BASE=http://127.0.0.1:5055 ../.venv/bin/python tools/capture_demo_video.py

Two passes. The capture pass drives the app and writes one PNG per meaningful
moment plus a storyboard describing how long each moment holds and where the
frame should be zoomed. The render pass expands that storyboard to 30fps with
eased zooms, crossfades between scenes and a burnt-in caption, then pipes raw
frames into ffmpeg.

Keeping them separate matters: re-timing the film (--target) or re-wording a
caption is a render-only change that takes seconds, while a capture run has to
wait on the app.
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import os
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

BASE = os.environ.get("SHOT_BASE", "http://127.0.0.1:5055").rstrip("/")
ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT_MP4 = ROOT / "static" / "shots" / "demo.mp4"

# One viewport for every scene. Mixing viewports mid-film means mixing source
# resolutions, and the zoom maths stops being comparable between scenes.
VIEWPORT = {"width": 1680, "height": 945}
SCALE = 2
OUT_W, OUT_H = 1920, 1080
FPS = 30
FADE_MS = 380

INK = (23, 25, 29)
INK_SOFT = (61, 68, 76)
PAPER = (248, 249, 250)

SF = "/System/Library/Fonts/SFNS.ttf"

# Zoom levels, named after what they frame. Picked so the subject fills the
# frame instead of floating in a field of empty product chrome - the first cut
# framed the composer at 1.34 and half of every frame was blank conversation.
#
# The capture is 2x and the output crop is 1:1 with the source at z=1.75, so
# these are read as: below 1.75 downsamples (very sharp), above it upsamples a
# 2x screenshot and only starts to soften past about 2.6.
Z_WIDE = 1.0        # the whole workspace
Z_PAGE = 1.55       # a centred content column (the patient portal)
Z_COMPOSER = 1.90   # reply box + the message above it
Z_COLUMN = 2.85     # the thread list, close enough to read each source badge
Z_MODAL = 2.15      # a dialog, filling most of the frame
Z_RAIL = 2.55       # Bridget's column
Z_BAR = 3.00        # a one-line status bar


def _font(size: int, weight: str = "Semibold"):
    f = ImageFont.truetype(SF, size)
    try:
        f.set_variation_by_name(weight)
    except Exception:
        pass
    return f


# --- storyboard ------------------------------------------------------------- #

CENTER = (0.5, 0.5)


def _lerp(a, b, t):
    return a + (b - a) * t


def _lerp2(a, b, t):
    return (_lerp(a[0], b[0], t), _lerp(a[1], b[1], t))


def crop_box(vw, vh, z, fx, fy):
    """The 16:9 window this zoom/focus asks for, in CSS pixels.

    Capture and render share this so the recorder can screenshot exactly the
    region the renderer is going to use - which is where the smoothness comes
    from. A full-frame screenshot at 2x costs ~157ms (6.4fps, visibly juddery
    once stretched to 30fps); the same frame clipped to a 2x zoom costs ~42ms,
    and to a 2.6x zoom ~33ms, which is real-time capture."""
    bw = min(vw, vh * OUT_W / OUT_H)
    bh = bw * OUT_H / OUT_W
    cw, ch = bw / z, bh / z
    x = min(max(fx * vw - cw / 2, 0.0), vw - cw)
    y = min(max(fy * vh - ch / 2, 0.0), vh - ch)
    return (x, y, cw, ch)


def _union(a, b, pad=0.06, vw=0, vh=0):
    """Smallest box covering both, padded - what to capture when the camera is
    moving across a single shot."""
    x0 = min(a[0], b[0]); y0 = min(a[1], b[1])
    x1 = max(a[0] + a[2], b[0] + b[2]); y1 = max(a[1] + a[3], b[1] + b[3])
    px, py = (x1 - x0) * pad, (y1 - y0) * pad
    x0, y0 = max(0.0, x0 - px), max(0.0, y0 - py)
    x1, y1 = min(float(vw), x1 + px), min(float(vh), y1 + py)
    return (x0, y0, x1 - x0, y1 - y0)


def _ease(t):
    """Cubic in-out. Linear zooms read as a machine panning; eased ones read as
    a camera operator, which is the whole difference between the two."""
    return 4 * t * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2


class Rec:
    """Collects shots and beats while the capture pass drives the browser.

    Every shot is clipped to the region the renderer will actually use, so a
    zoomed beat captures a fraction of the pixels and the capture rate rises
    from ~6fps to ~25-30fps. The clip rectangle travels with the beat so the
    renderer can still pan and zoom inside it."""

    def __init__(self, work: pathlib.Path):
        self.work = work
        self.n = 0
        self.scenes: list[dict] = []
        self.cur: dict | None = None
        self.vw = VIEWPORT["width"]
        self.vh = VIEWPORT["height"]

    def scene(self, caption: str):
        self.cur = {"caption": caption, "beats": []}
        self.scenes.append(self.cur)
        return self.cur

    def shot(self, page, src=None, fast=False) -> tuple:
        self.n += 1
        if src is None:
            src = (0.0, 0.0, float(self.vw), float(self.vh))
        clip = {"x": float(src[0]), "y": float(src[1]),
                "width": max(8.0, float(src[2])), "height": max(8.0, float(src[3]))}
        # JPEG only for the wide shots, where the clip cannot save anything and
        # PNG would drop the capture to 6fps. Zoomed shots stay lossless.
        if fast:
            name = f"s{self.n:04d}.jpg"
            data = page.screenshot(type="jpeg", quality=93, clip=clip)
        else:
            name = f"s{self.n:04d}.png"
            data = page.screenshot(type="png", clip=clip)
        (self.work / name).write_bytes(data)
        return name, clip

    def beat(self, shot, ms, z=(1.0, 1.0), f=CENTER, f2=None, hold=True):
        name, clip = shot
        f2 = f if f2 is None else f2
        self.cur["beats"].append({
            "img": name, "ms": int(ms), "hold": bool(hold),
            "z0": z[0], "z1": z[1],
            "fx0": f[0], "fy0": f[1], "fx1": f2[0], "fy1": f2[1],
            "src": [clip["x"], clip["y"], clip["width"], clip["height"]],
            "vw": self.vw, "vh": self.vh,
        })

    def _span(self, z, f, f2):
        """The clip that covers a beat's whole camera move."""
        a = crop_box(self.vw, self.vh, z[0], f[0], f[1])
        b = crop_box(self.vw, self.vh, z[1], f2[0], f2[1])
        return _union(a, b, vw=self.vw, vh=self.vh)

    # -- capture verbs -------------------------------------------------------- #

    def hold(self, page, ms, z=(1.0, 1.0), f=CENTER, f2=None):
        """One frame held (optionally with a slow push) - the reading beats.

        Rendered per output frame, so these were always smooth; clipping them
        just makes them sharper, because the pixels are no longer downscaled
        from a full-viewport grab."""
        f2 = f if f2 is None else f2
        src = self._span(z, f, f2)
        self.beat(self.shot(page, src), ms, z=z, f=f, f2=f2)

    def roll(self, page, ms, n=None, z=(1.0, 1.0), f=CENTER, f2=None, fps=None):
        """Real page motion, captured at the frame rate the output will play at.

        n is derived from the duration rather than passed, so a scroll is
        sampled densely enough that no output frame is a repeat. Callers may
        still pass n, but the floor keeps them honest."""
        f2 = f if f2 is None else f2
        fps = fps or FPS
        want = max(2, int(round(ms / 1000 * fps)))
        n = max(want, n or 0)
        step = ms / n
        wide = max(z[0], z[1]) < 1.25
        for i in range(n):
            a, b = i / n, (i + 1) / n
            za, zb = _lerp(z[0], z[1], a), _lerp(z[0], z[1], b)
            fa, fb = _lerp2(f, f2, a), _lerp2(f, f2, b)
            src = self._span((za, zb), fa, fb)
            page.wait_for_timeout(max(0, step - (14 if wide else 34)))
            self.beat(self.shot(page, src, fast=wide), step, hold=False,
                      z=(za, zb), f=fa, f2=fb)

    def typing(self, page, sel, text, total_ms=1200, z=(1.0, 1.0), f=CENTER):
        """Type into a field over `total_ms`, at the output frame rate.

        The chunk size falls out of the duration rather than being guessed, so
        a typed line is as smooth as any other motion instead of arriving in
        four-character jumps."""
        el = page.locator(sel).first
        el.click()
        want = max(4, int(round(total_ms / 1000 * FPS)))
        chunk = max(1, math.ceil(len(text) / want))
        frames = math.ceil(len(text) / chunk)
        step = total_ms / max(1, frames)
        src = self._span(z, f, f)
        typed = ""
        for i, ch in enumerate(text):
            typed += ch
            if (i + 1) % chunk == 0 or i == len(text) - 1:
                el.fill(typed)
                self.beat(self.shot(page, src), step, hold=False, z=z, f=f)
        el.fill(text)

    def dump(self, path: pathlib.Path):
        path.write_text(json.dumps({
            "fps": FPS, "out": [OUT_W, OUT_H], "fade_ms": FADE_MS,
            "scenes": self.scenes,
        }, indent=1))


# --- browser helpers -------------------------------------------------------- #

# A cursor the screenshots can actually see. Playwright's real pointer leaves no
# trace in a screenshot, and without one a silent demo is a series of states
# with no visible cause; this shows where each click lands.
CURSOR_JS = """
(() => {
  const boot = () => {
    if (!document.body || document.getElementById('__demoCursor')) return;
    const dot = document.createElement('div');
    dot.id = '__demoCursor';
    dot.style.cssText = 'position:fixed;left:-200px;top:-200px;width:20px;height:20px;'
      + 'border-radius:50%;background:rgba(17,25,40,.70);border:2px solid #fff;'
      + 'box-shadow:0 2px 10px rgba(0,0,0,.32);z-index:2147483647;pointer-events:none;'
      + 'transform:translate(-50%,-50%);'
      + 'transition:left .42s cubic-bezier(.4,0,.2,1),top .42s cubic-bezier(.4,0,.2,1);';
    document.body.appendChild(dot);
    const ring = document.createElement('div');
    ring.id = '__demoRing';
    ring.style.cssText = 'position:fixed;left:-200px;top:-200px;width:20px;height:20px;'
      + 'border-radius:50%;border:2px solid rgba(18,87,176,.9);z-index:2147483646;'
      + 'pointer-events:none;transform:translate(-50%,-50%) scale(1);opacity:0;';
    document.body.appendChild(ring);
    window.__demoMove = (x, y, snap) => {
      dot.style.transition = snap ? 'none'
        : 'left .42s cubic-bezier(.4,0,.2,1),top .42s cubic-bezier(.4,0,.2,1)';
      dot.style.left = x + 'px'; dot.style.top = y + 'px';
    };
    window.__demoTap = () => {
      ring.style.left = dot.style.left; ring.style.top = dot.style.top;
      ring.style.transition = 'none';
      ring.style.transform = 'translate(-50%,-50%) scale(1)';
      ring.style.opacity = '.85';
      requestAnimationFrame(() => {
        ring.style.transition = 'transform .5s ease-out,opacity .5s ease-out';
        ring.style.transform = 'translate(-50%,-50%) scale(2.8)';
        ring.style.opacity = '0';
      });
    };
  };
  if (document.body) boot();
  else document.addEventListener('DOMContentLoaded', boot);
})();
"""

SCROLLER_JS = """
([sel, dy, ms]) => {
  const el = document.querySelector(sel);
  if (!el) return false;
  let box = el;
  while (box && box !== document.body) {
    const s = getComputedStyle(box);
    if (/(auto|scroll)/.test(s.overflowY) && box.scrollHeight > box.clientHeight + 4) break;
    box = box.parentElement;
  }
  if (!box || box === document.body) box = document.scrollingElement;
  const from = box.scrollTop, to = from + dy, t0 = performance.now();
  const step = (now) => {
    const t = Math.min(1, (now - t0) / ms);
    const e = t < .5 ? 4*t*t*t : 1 - Math.pow(-2*t+2, 3)/2;
    box.scrollTop = from + (to - from) * e;
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
  return true;
}
"""


def mask_host(page):
    """Show the product domain instead of the scratch server's address.

    The only cosmetic edit in the film. Tokens, passwords, names, counts and
    every other value on screen are the real ones this run produced; what gets
    swapped is 127.0.0.1:5055, which is an artefact of where the demo happens
    to be running and nothing a viewer should have to read past."""
    page.evaluate("""([base, host])=>{
      const swap = t => t.split(base).join(host);
      const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      const hits = []; let n;
      while ((n = w.nextNode())) if (n.nodeValue.includes(base)) hits.push(n);
      hits.forEach(n => { n.nodeValue = swap(n.nodeValue); });
      document.querySelectorAll('input,textarea').forEach(i => {
        if (i.value && i.value.includes(base)) i.value = swap(i.value);
      });
    }""", [BASE, DEMO_HOST])


def clean(page):
    page.evaluate("""()=>{document.querySelectorAll(
      '.toast-stack,.demo-tour,[data-demo-tour],.flash,.flash-stack')
      .forEach(e=>e.remove());}""")


def focus(page, sel, fallback=CENTER):
    """Normalised viewport position of an element - where the camera looks."""
    box = page.evaluate("""(sel)=>{
      const e=document.querySelector(sel); if(!e) return null;
      const r=e.getBoundingClientRect();
      if (!r.width && !r.height) return null;
      return {x:(r.x+r.width/2)/window.innerWidth,
              y:(r.y+r.height/2)/window.innerHeight};}""", sel)
    if not box:
        return fallback
    return (min(max(box["x"], 0.0), 1.0), min(max(box["y"], 0.0), 1.0))


def focus_last(page, sel, fallback=CENTER):
    """Like focus(), but on the LAST match - the note we just posted, not the
    first one that was already in the thread."""
    box = page.evaluate("""(sel)=>{
      const all=document.querySelectorAll(sel); if(!all.length) return null;
      const r=all[all.length-1].getBoundingClientRect();
      if (!r.width && !r.height) return null;
      return {x:(r.x+r.width/2)/window.innerWidth,
              y:(r.y+r.height/2)/window.innerHeight};}""", sel)
    if not box:
        return fallback
    return (min(max(box["x"], 0.0), 1.0), min(max(box["y"], 0.0), 1.0))


def point(page, sel, snap=False):
    """Send the visible cursor to an element without clicking it yet.

    Takes a selector or an already-built locator, because some targets ("the
    third study in the menu") are only expressible as the latter."""
    loc = page.locator(sel).first if isinstance(sel, str) else sel
    box = loc.bounding_box()
    if not box:
        return False
    page.evaluate("([x,y,s])=>window.__demoMove&&window.__demoMove(x,y,s)",
                  [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, snap])
    return True


def tap(page):
    page.evaluate("()=>window.__demoTap&&window.__demoTap()")


def scroll(page, sel, dy, ms):
    page.evaluate(SCROLLER_JS, [sel, dy, ms])


def reach(rec, page, sel, ms=460, z=(1.0, 1.0), f=CENTER, f2=None):
    """Cursor travels to a control, on camera, then taps it."""
    if point(page, sel):
        rec.roll(page, ms, n=5, z=z, f=f, f2=f2)
    tap(page)


# --- scenes ----------------------------------------------------------------- #

def open_inbox(page, url="/app/inbox"):
    page.goto(f"{BASE}{url}", wait_until="load")
    page.wait_for_selector(".mh-thread-row, .mh-conversation", timeout=20000)
    clean(page)
    page.wait_for_timeout(500)


def sc_workspace(rec, page):
    """Open on the whole workspace, wide, with Bridget already in it.

    The first cut opened at 1.55x on one conversation and pulled back, so for
    the first two seconds you could not see the product at all. An establishing
    shot has to establish: full frame first, and only then a slow push in."""
    rec.scene("Every study, every channel, one workspace")
    # /app/home redirects here - for a study team this inbox is the dashboard.
    open_inbox(page, "/app/inbox?thread=102&status=open")
    clean(page)
    page.wait_for_timeout(800)
    rec.hold(page, 1700, z=(Z_WIDE, Z_WIDE))
    rec.hold(page, 1500, z=(Z_WIDE, 1.10), f=CENTER, f2=(0.45, 0.5))


def sc_sources(rec, page):
    """The recruitment channels, connected, in the real modal."""
    rec.scene("Connect every recruitment source once")
    btn = '[data-ui-open="sourceModal"]'
    reach(rec, page, btn, ms=460)
    page.locator(btn).first.click()
    page.wait_for_selector("#sourceModal .ui-modal-card", timeout=8000)
    clean(page)
    modal = focus(page, "#sourceModal .ui-modal-card")
    top = (modal[0], 0.34)
    rec.roll(page, 420, n=5, z=(Z_WIDE, 1.85), f=CENTER, f2=top)
    rec.hold(page, 1000, z=(1.85, Z_MODAL), f=top)
    scroll(page, "#sourceModal .ui-modal-body", 320, 1500)
    rec.roll(page, 1400, z=(Z_MODAL, Z_MODAL), f=top, f2=(modal[0], 0.5))
    rec.hold(page, 900, z=(Z_MODAL, 2.22), f=(modal[0], 0.5))


def sc_channels(rec, page):
    """Same queue, mixed origins - the badge on each row is the point."""
    rec.scene("Email, Instagram and ad leads land in one queue")
    page.keyboard.press("Escape")
    page.wait_for_timeout(450)
    clean(page)
    col = focus(page, ".mh-thread-column")
    rec.hold(page, 600, z=(Z_COLUMN, Z_COLUMN), f=col)
    scroll(page, ".mh-thread-row", 1500, 2650)
    rec.roll(page, 2500, z=(Z_COLUMN, Z_COLUMN), f=col)
    rec.hold(page, 700, z=(Z_COLUMN, 2.95), f=col)


def sc_studies(rec, page):
    """One switch, and the queue is scoped to that trial."""
    rec.scene("Switch trial - the inbox scopes to it")
    sw = "#studySwitch summary, .appswitch"
    reach(rec, page, sw, ms=420)
    page.locator(sw).first.click()
    page.wait_for_timeout(420)
    clean(page)
    menu = focus(page, "#studySwitch [role=menu], #studySwitch")
    rec.roll(page, 380, n=4, z=(Z_WIDE, 1.75), f=CENTER, f2=menu)
    rec.hold(page, 800, z=(1.75, 1.80), f=menu)
    link = page.locator("#studySwitch a[href*='nct=NCT']").nth(2)
    point(page, link)
    rec.roll(page, 360, n=4, z=(1.80, 1.80), f=menu)
    tap(page)
    link.click()
    page.wait_for_load_state("load")
    page.wait_for_selector(".mh-thread-row, .mh-empty, .mh-conversation", timeout=15000)
    clean(page)
    page.wait_for_timeout(500)
    # Land wide: the proof is the whole queue changing, not one row.
    rec.roll(page, 420, n=5, z=(1.80, 1.72), f=menu, f2=(0.27, 0.26))
    rec.hold(page, 1400, z=(1.72, 1.78), f=(0.27, 0.26))


def sc_mention(rec, page):
    """Pull a teammate into the thread instead of forwarding it out of it."""
    rec.scene("Tag a teammate - no forwarding, no CC")
    page.locator(".mh-thread-row").first.click()
    page.wait_for_load_state("load")
    clean(page)
    page.wait_for_timeout(600)
    note_tab = ".mh-composer [role=tab]:has-text('Internal note'), " \
               ".mh-composer button:has-text('Internal note'), " \
               "label:has-text('Internal note')"
    comp = focus(page, ".mh-composer")
    reach(rec, page, note_tab, ms=420, z=(Z_COMPOSER, Z_COMPOSER), f=comp)
    page.locator(note_tab).first.click()
    page.wait_for_timeout(350)
    ta = "textarea[data-mentions]"
    rec.hold(page, 300, z=(Z_COMPOSER, Z_COMPOSER), f=comp)
    rec.typing(page, ta, "@Kar", total_ms=520, z=(Z_COMPOSER, Z_COMPOSER), f=comp)
    page.wait_for_timeout(500)
    rec.hold(page, 800, z=(Z_COMPOSER, Z_COMPOSER), f=comp)
    pick = page.locator(".mention-opt").first
    if pick.count():
        pick.click()
    else:
        page.keyboard.press("Enter")
    page.wait_for_timeout(350)
    cur = page.locator(ta).first.input_value()
    rec.typing(page, ta, cur + " can you countersign Linda's consent today?",
               total_ms=1000, z=(Z_COMPOSER, Z_COMPOSER), f=comp)
    add = ".mh-composer button:has-text('Add note'), button:has-text('Add note')"
    reach(rec, page, add, ms=380, z=(Z_COMPOSER, Z_COMPOSER), f=comp)
    page.locator(add).first.click()
    page.wait_for_load_state("load")
    clean(page)
    page.wait_for_timeout(700)
    # Land on the note itself, not the pane it happens to sit in.
    note = focus_last(page, ".mh-internal-note", fallback=focus(page, ".mh-dialogue"))
    rec.roll(page, 380, n=4, z=(Z_COMPOSER, 2.05), f=comp, f2=note)
    rec.hold(page, 1400, z=(2.05, 2.0), f=note)


def sc_compose(rec, page):
    """AI, first use: say what you want said, the draft lands in the reply box."""
    rec.scene("Say what you want written - Bridget writes it")
    # A thread with a real back-and-forth: a one-message thread leaves the
    # reading pane empty and the shot is mostly white.
    page.goto(f"{BASE}/app/inbox?thread=102&status=open", wait_until="load")
    page.wait_for_selector("[data-ask-input]", timeout=15000)
    clean(page)
    page.wait_for_timeout(600)
    comp = focus(page, ".mh-composer")
    rec.hold(page, 500, z=(Z_COMPOSER, Z_COMPOSER), f=comp)
    rec.typing(page, "[data-ask-input]",
               "Offer a screening call next week and ask which mornings work",
               total_ms=1250, z=(Z_COMPOSER, Z_COMPOSER), f=comp)
    reach(rec, page, "[data-ask-go]", ms=320, z=(Z_COMPOSER, Z_COMPOSER), f=comp)
    page.locator("[data-ask-go]").first.click()
    rec.roll(page, 2200, n=12, z=(Z_COMPOSER, Z_COMPOSER), f=comp)
    try:
        page.wait_for_function(
            """()=>{const t=document.getElementById('replyBody');
              return t && !t.classList.contains('is-bridget-writing')
                && t.value.trim().length>20;}""", timeout=25000)
    except Exception:
        print("!! compose: draft never settled")
    page.wait_for_timeout(250)
    reply = focus(page, "#replyBody", fallback=comp)
    rec.hold(page, 1700, z=(Z_COMPOSER, 2.0), f=comp, f2=reply)


def sc_bridget(rec, page):
    """AI, second use: one question across every study, ending in a real action."""
    rec.scene("Ask across every study - Bridget proposes, you confirm")
    dock = focus(page, "#copilot")
    reach(rec, page, "#copilotInput", ms=420, z=(1.05, Z_RAIL), f=CENTER, f2=dock)
    rec.typing(page, "#copilotInput", "Who is stuck in screening and hasn't booked?",
               total_ms=1150, z=(Z_RAIL, Z_RAIL), f=dock)
    page.keyboard.press("Enter")
    rec.roll(page, 1900, n=10, z=(Z_RAIL, Z_RAIL), f=dock)
    try:
        page.wait_for_function(
            """()=>{const l=document.getElementById('copilotLog');
              return l && l.querySelector('.copilot-confirm');}""", timeout=30000)
    except Exception:
        print("!! bridget: no confirm card")
    page.wait_for_timeout(500)
    log = focus(page, "#copilotLog", fallback=dock)
    rec.hold(page, 1500, z=(Z_RAIL, Z_RAIL), f=log)
    card = focus(page, ".copilot-confirm", fallback=log)
    rec.hold(page, 1600, z=(Z_RAIL, 3.05), f=log, f2=card)


def sc_handoff(rec, page):
    """Going away without dropping anyone: the queue moves, then hands back."""
    rec.scene("Going away? Your whole queue moves to a teammate")
    open_inbox(page, "/app/scope?nct=&next=/app/inbox")
    btn = '[data-ui-open="coverageModal"]'
    reach(rec, page, btn, ms=420)
    page.locator(btn).first.click()
    page.wait_for_selector("#coverageModal .ui-modal-card", timeout=8000)
    clean(page)
    modal = focus(page, "#coverageModal .ui-modal-card")
    rec.roll(page, 400, n=5, z=(Z_WIDE, 1.95), f=CENTER, f2=modal)
    rec.hold(page, 1300, z=(1.95, Z_MODAL), f=modal)
    sel = "#coverOwner"
    if page.locator(f"{sel} option").count() > 1:
        point(page, sel)
        tap(page)
        page.locator(sel).select_option(index=1)
    page.wait_for_timeout(300)
    rec.hold(page, 900, z=(Z_MODAL, Z_MODAL), f=modal)
    page.locator("#awayUntil").fill("2026-09-08")
    rec.hold(page, 500, z=(Z_MODAL, Z_MODAL), f=modal)
    rec.typing(page, "#handoffNote",
               "Linda's week 4 visit is Wednesday - consent needs countersigning.",
               total_ms=1150, z=(Z_MODAL, Z_MODAL), f=modal)
    go = "#coverageModal form[action*='away'] button[type=submit]"
    reach(rec, page, go, ms=380, z=(Z_MODAL, Z_MODAL), f=modal)
    page.locator(go).first.click()
    page.wait_for_load_state("load")
    clean(page)
    page.wait_for_timeout(700)
    bar = focus(page, ".away-bar")
    rec.roll(page, 420, n=5, z=(Z_MODAL, Z_BAR), f=modal, f2=bar)
    rec.hold(page, 1400, z=(Z_BAR, 3.15), f=bar)


def sc_catchup(rec, page):
    """Coming back is a recap, not a scroll."""
    rec.scene("Come back to a recap, not a backlog")
    end = ".away-bar form button, .away-bar button"
    if not page.locator(end).count():
        rec.scenes.pop()
        return
    bar = focus(page, ".away-bar")
    reach(rec, page, end, ms=380, z=(Z_BAR, Z_BAR), f=bar)
    page.locator(end).first.click()
    page.wait_for_load_state("load")
    clean(page)
    page.wait_for_timeout(700)
    if not page.locator(".away-recap").count():
        print("!! catchup: no recap bar rendered")
        rec.scenes.pop()
        return
    rc = focus(page, ".away-recap")
    rec.roll(page, 420, n=5, z=(1.9, Z_BAR), f=bar, f2=rc)
    rec.hold(page, 1800, z=(Z_BAR, 3.15), f=rc)


def sc_blast(rec, page):
    """One message, a filtered group, each in their own thread."""
    rec.scene("Email or text a whole group in one go")
    page.goto(f"{BASE}/app/leads", wait_until="load")
    page.wait_for_selector("#queueTable tbody tr, [data-ui-open='broadcastModal']",
                           timeout=15000)
    clean(page)
    page.wait_for_timeout(400)
    btn = '[data-ui-open="broadcastModal"]'
    reach(rec, page, btn, ms=420)
    page.locator(btn).first.click()
    page.wait_for_selector("#broadcastModal:not([hidden])", timeout=8000)
    clean(page)
    modal = focus(page, "#broadcastModal .ui-modal-card, #broadcastModal")
    rec.roll(page, 400, n=5, z=(Z_WIDE, 1.95), f=CENTER, f2=modal)
    point(page, "#blastNct")
    tap(page)
    # The study with the largest accepted audience, so the send button reads as
    # a real send rather than a demo of three people.
    page.locator("#blastNct").select_option(index=BLAST_STUDY)
    page.wait_for_timeout(400)
    try:
        page.wait_for_selector(".blast-count.is-ready", timeout=8000)
    except Exception:
        pass
    rec.roll(page, 800, n=6, z=(1.95, Z_MODAL), f=modal)
    rec.hold(page, 700, z=(Z_MODAL, Z_MODAL), f=modal)
    rec.typing(page, "#blastBody",
               "Screening slots opened this week - reply with a morning that works.",
               total_ms=1150, z=(Z_MODAL, Z_MODAL), f=modal)
    who = focus(page, ".blast-count", fallback=modal)
    top = (modal[0], max(0.16, (who[1] + focus(page, "#blastBody",
                                               fallback=modal)[1]) / 2))
    rec.hold(page, 1200, z=(Z_MODAL, 2.25), f=top)
    reach(rec, page, "#blastSend", ms=460, z=(2.25, 1.12), f=top, f2=modal)
    rec.hold(page, 1300, z=(1.12, 1.07), f=modal)


def sc_portal_link(rec, page, lead_id):
    """The coordinator hands over a private page, in one click."""
    rec.scene("Send each patient their own private page")
    page.goto(f"{BASE}/app/applicant/{lead_id}", wait_until="load")
    page.wait_for_selector("#portal", timeout=15000)
    clean(page)
    page.locator("#portal").first.scroll_into_view_if_needed()
    page.wait_for_timeout(500)
    issue = "#portal form[action$='/portal'] button[type=submit]"
    card = focus(page, "#portal")
    if page.locator(issue).count():
        reach(rec, page, issue, ms=420, z=(1.7, 1.7), f=card)
        page.locator(issue).first.click()
        page.wait_for_load_state("load")
        clean(page)
        page.locator("#portal").first.scroll_into_view_if_needed()
        page.wait_for_timeout(600)
    fields = page.locator("#portal .portal-copy input").all()
    link = fields[0].input_value() if fields else ""
    pw = fields[1].input_value() if len(fields) > 1 else ""
    mask_host(page)
    card = focus(page, "#portal")
    rec.roll(page, 420, n=5, z=(1.55, 2.0), f=card)
    copy = "#portal .portal-copy [data-copy-btn]"
    if page.locator(copy).count():
        reach(rec, page, copy, ms=400, z=(2.0, 2.0), f=card)
    rec.hold(page, 1500, z=(2.0, 1.95), f=card)
    return link, pw


def sc_portal(rec, portal):
    """The patient's side: where they are, what is next, what to bring."""
    rec.scene("They follow it like a delivery - what is next, what to bring")
    clean(portal)
    mask_host(portal)
    portal.wait_for_timeout(500)
    head = focus(portal, ".portal-status", fallback=(0.5, 0.25))
    track = focus(portal, ".portal-track", fallback=head)
    rec.hold(portal, 1000, z=(Z_PAGE, Z_PAGE), f=head)
    rec.hold(portal, 1200, z=(Z_PAGE, 1.85), f=head, f2=track)
    scroll(portal, ".portal-grid", 210, 950)
    portal.wait_for_timeout(50)
    grid = focus(portal, ".portal-grid", fallback=CENTER)
    appts = focus(portal, ".portal-visits")
    rec.roll(portal, 1050, n=7, z=(1.85, 1.66), f=track, f2=(grid[0], appts[1]))
    rec.hold(portal, 1900, z=(1.66, 1.74), f=(grid[0], appts[1]))


def sc_portal_reply(rec, portal):
    """Their answers come back into the same thread the site already reads."""
    rec.scene("Their replies land back in your inbox")
    portal.locator(".portal-thread").first.scroll_into_view_if_needed()
    portal.wait_for_timeout(400)
    mask_host(portal)
    msgs = focus(portal, ".portal-thread")
    scroll(portal, ".portal-thread", 200, 800)
    rec.roll(portal, 900, n=6, z=(1.74, 2.0), f=msgs)
    msgs = focus(portal, ".portal-thread")
    rec.hold(portal, 1700, z=(2.0, 2.1), f=msgs)


# --- capture pass ----------------------------------------------------------- #

PORTAL_PW = "Bridge-demo-2026"
# Adjunctive Seltorexant has the largest accepted audience in the demo data.
BLAST_STUDY = int(os.environ.get("BLAST_STUDY", "3"))
DEMO_HOST = os.environ.get("DEMO_HOST", "https://app.bridgemd.com")
PORTAL_LEAD = int(os.environ.get("PORTAL_LEAD", "8"))


def capture(work: pathlib.Path):
    rec = Rec(work)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=SCALE,
                                  reduced_motion="no-preference")
        page = ctx.new_page()
        page.add_init_script(
            "try{localStorage.setItem('bm_bridget_pane','1')}catch(e){}")
        page.add_init_script(CURSOR_JS)

        page.goto(f"{BASE}/app/inbox", wait_until="load")
        if "/login" in page.url:
            print("!! redirected to login - start the app with SITE_DEMO=1 NO_LOGIN=1")
            return None

        # One broken selector should cost one scene, not the whole run - a
        # capture pass is minutes of waiting on the app.
        for fn in (sc_workspace, sc_sources, sc_channels, sc_studies,
                   sc_mention, sc_compose, sc_bridget, sc_handoff,
                   sc_catchup, sc_blast):
            before = len(rec.scenes)
            try:
                fn(rec, page)
                n = len(rec.scenes[-1]["beats"]) if len(rec.scenes) > before else 0
                print(f"ok {fn.__name__:16} {n} beats")
            except Exception as e:
                del rec.scenes[before:]
                rec.cur = rec.scenes[-1] if rec.scenes else None
                print(f"!! {fn.__name__:16} {type(e).__name__}: {str(e)[:160]}")

        try:
            link, pw = sc_portal_link(rec, page, PORTAL_LEAD)
            print(f"ok sc_portal_link   {link or '(no link)'}")
        except Exception as e:
            link, pw = "", ""
            print(f"!! sc_portal_link   {type(e).__name__}: {str(e)[:160]}")

        # The patient signs in off camera: the film is about the tracker, not
        # about watching someone type a one-time password.
        pctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=SCALE)
        portal = pctx.new_page()
        portal.add_init_script(CURSOR_JS)
        ok = False
        if link:
            portal.goto(link.replace("http://localhost", "http://127.0.0.1"),
                        wait_until="load")
            if portal.locator("input[type=password]").count():
                portal.fill("input[type=password]", pw)
                portal.locator("button[type=submit]").first.click()
                portal.wait_for_load_state("load")
            if portal.locator("input[name=confirm]").count():
                portal.fill("input[name=password]", PORTAL_PW)
                portal.fill("input[name=confirm]", PORTAL_PW)
                portal.locator("button[type=submit]").first.click()
                portal.wait_for_load_state("load")
            ok = portal.locator(".portal-track").count() > 0
        if ok:
            sc_portal(rec, portal)
            sc_portal_reply(rec, portal)
            print("ok sc_portal        captured")
        else:
            print("!! portal never opened - those two scenes are missing")

        browser.close()

    rec.dump(work / "storyboard.json")
    return work / "storyboard.json"


# --- render pass ------------------------------------------------------------ #

class Frames:
    """Turns the storyboard into 30fps frame descriptors, then pixels."""

    def __init__(self, work, board):
        self.work = work
        self.board = board
        self.cache: dict[str, Image.Image] = {}

    def load(self, name):
        img = self.cache.get(name)
        if img is None:
            img = Image.open(self.work / name).convert("RGB")
            if len(self.cache) > 3:
                self.cache.clear()
            self.cache[name] = img
        return img

    def pixels(self, d):
        img = self.load(d["img"])
        iw, ih = img.size
        sx, sy, sw, sh = d["src"]
        want = crop_box(d["vw"], d["vh"], d["z"], d["fx"], d["fy"])
        # CSS pixels -> pixels of the (possibly clipped) image we captured.
        k = iw / sw
        x0 = (want[0] - sx) * k
        y0 = (want[1] - sy) * k
        cw, ch = want[2] * k, want[3] * k
        x0 = min(max(x0, 0.0), max(0.0, iw - cw))
        y0 = min(max(y0, 0.0), max(0.0, ih - ch))
        box = (int(round(x0)), int(round(y0)),
               int(round(min(x0 + cw, iw))), int(round(min(y0 + ch, ih))))
        out = img.crop(box)
        if out.size != (OUT_W, OUT_H):
            out = out.resize((OUT_W, OUT_H), Image.Resampling.LANCZOS)
        return caption(out, d.get("cap", ""), d.get("cap_a", 0.0))


CAP_FONT = None


def caption(img, text, alpha):
    global CAP_FONT
    if not text or alpha <= 0.02:
        return img
    if CAP_FONT is None:
        CAP_FONT = _font(37, "Semibold")
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    tw = d.textlength(text, font=CAP_FONT)
    px, py, th = 34, 22, 44
    x0, y0 = 76, OUT_H - 78 - (th + py * 2)
    a = max(0.0, min(1.0, alpha))
    d.rounded_rectangle([x0, y0, x0 + tw + px * 2, y0 + th + py * 2], radius=16,
                        fill=(13, 20, 32, int(226 * a)))
    d.text((x0 + px, y0 + py + 2), text, font=CAP_FONT,
           fill=(255, 255, 255, int(255 * a)))
    return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")


def endcard():
    img = Image.new("RGB", (OUT_W, OUT_H), PAPER)
    d = ImageDraw.Draw(img)
    big, small = _font(84, "Bold"), _font(34, "Regular")
    t1 = "BridgeMD"
    t2 = "Recruitment, coordination and retention in one place."
    w1 = d.textlength(t1, font=big)
    w2 = d.textlength(t2, font=small)
    d.text(((OUT_W - w1) / 2, OUT_H / 2 - 96), t1, font=big, fill=INK)
    d.text(((OUT_W - w2) / 2, OUT_H / 2 + 28), t2, font=small, fill=INK_SOFT)
    return img


def pace(board, target_s):
    """Compress to a target runtime by shortening the reading holds only.

    Motion beats keep their timing: a scroll or a typed line replayed at half
    duration reads as a glitch, while a hold that is 300ms shorter just reads
    as a tighter edit."""
    if not target_s:
        return
    fade_total = FADE_MS * max(0, len(board["scenes"]) - 1)
    total = sum(b["ms"] for s in board["scenes"] for b in s["beats"]) - fade_total
    want = target_s * 1000
    if total <= want:
        return
    holds = [b for s in board["scenes"] for b in s["beats"] if b["hold"]]
    hold_ms = sum(b["ms"] for b in holds)
    floor = 260
    floor_ms = sum(min(b["ms"], floor) for b in holds)
    need = total - want
    give = hold_ms - floor_ms
    if give <= 0:
        print(f"!! cannot reach {target_s}s - holds are already at the floor")
        return
    k = max(0.0, 1 - need / give)
    for b in holds:
        f = min(b["ms"], floor)
        b["ms"] = int(f + (b["ms"] - f) * k)
    print(f"pace {total/1000:.1f}s -> ~{target_s}s (holds scaled {k:.2f})")


def expand(board):
    """Beats -> per-frame descriptors, scene by scene, captions faded in/out.

    Each scene gets a release on the way out (the camera eases back) and a
    settle on the way in (it eases forward to its mark). Both are exactly the
    crossfade's length, so they cost nothing in runtime and the dissolve lands
    between two frames that are already moving - which is most of the
    difference between a cut that reads as smooth and one that reads as two
    stills crossfading."""
    fps = board["fps"]
    seam = max(1, round(board["fade_ms"] * fps / 1000))
    scenes = []
    for sc in board["scenes"]:
        fr = []
        for b in sc["beats"]:
            n = max(1, round(b["ms"] * fps / 1000))
            for i in range(n):
                t = i / (n - 1) if n > 1 else 1.0
                e = _ease(t)
                fr.append({"img": b["img"], "src": b["src"],
                           "vw": b["vw"], "vh": b["vh"],
                           "z": _lerp(b["z0"], b["z1"], e),
                           "fx": _lerp(b["fx0"], b["fx1"], e),
                           "fy": _lerp(b["fy0"], b["fy1"], e),
                           "cap": sc["caption"]})
        if not fr:
            continue
        # Reshape the frames the scene already has - appending seams instead
        # would add a seam's length per scene boundary to the runtime, since
        # the crossfade only consumes one seam per cut, not two.
        m = min(seam, len(fr) // 3)
        if m >= 2:
            for k in range(m):                       # release on the way out
                t = _ease((k + 1) / m)
                d = fr[len(fr) - m + k]
                d["z"] = d["z"] * (1 - 0.09 * t)
            for k in range(m):                       # settle on the way in
                t = _ease(k / m)
                fr[k]["z"] = fr[k]["z"] * (1 + 0.07 * (1 - t))
        ramp = min(10, max(1, len(fr) // 4))
        for i, d in enumerate(fr):
            d["cap_a"] = min(1.0, min((i + 1) / ramp, (len(fr) - i) / ramp))
        scenes.append(fr)
    return scenes


def sequence(scenes, fade_frames):
    """Splice scenes with crossfades. Each transition consumes N frames from the
    outgoing scene and N from the incoming one, so the film does not grow a
    dead beat at every cut."""
    seq: list[tuple] = []
    for i, fr in enumerate(scenes):
        if not fr:
            continue
        if not seq:
            seq.extend(("f", d) for d in fr)
            continue
        n = min(fade_frames, len(seq), len(fr) - 1)
        if n <= 0:
            seq.extend(("f", d) for d in fr)
            continue
        tail = [seq.pop()[1] for _ in range(n)][::-1]
        for k in range(n):
            # Smoothstep rather than linear: a linear alpha ramp spends too
            # long at 50/50, which is where a dissolve looks like a ghost.
            seq.append(("x", tail[k], fr[k], _ease((k + 1) / (n + 1))))
        seq.extend(("f", d) for d in fr[n:])
    return seq


def render(work: pathlib.Path, board_path: pathlib.Path, out: pathlib.Path,
           target_s: float | None):
    import imageio_ffmpeg

    board = json.loads(board_path.read_text())
    pace(board, target_s)
    scenes = expand(board)

    end = endcard()
    end_frames = int(1.6 * board["fps"])
    scenes.append([{"img": "__end__", "z": 1.0, "fx": .5, "fy": .5,
                    "cap": "", "cap_a": 0.0} for _ in range(end_frames)])

    fade_frames = max(1, round(board["fade_ms"] * board["fps"] / 1000))
    seq = sequence(scenes, fade_frames)
    secs = len(seq) / board["fps"]
    print(f"render {len(seq)} frames = {secs:.1f}s at {board['fps']}fps")

    F = Frames(work, board)

    def px(d):
        if d["img"] == "__end__":
            return end
        return F.pixels(d)

    exe = imageio_ffmpeg.get_ffmpeg_exe()
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{OUT_W}x{OUT_H}", "-r", str(board["fps"]), "-i", "-",
           "-an", "-c:v", "libx264", "-preset", "slow", "-crf", "19",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for i, item in enumerate(seq):
            if item[0] == "f":
                frame = px(item[1])
            else:
                frame = Image.blend(px(item[1]), px(item[2]), item[3])
            proc.stdin.write(frame.tobytes())
            if i % 150 == 0:
                print(f"  {i}/{len(seq)}", flush=True)
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read().decode()[-1500:]
    if proc.wait() != 0:
        print(err)
        return 1
    kb = out.stat().st_size // 1024
    print(f"\nok {out}  ({secs:.1f}s, {kb}KB, {OUT_W}x{OUT_H}@{board['fps']}fps)")
    return 0


def smooth(src: pathlib.Path, dst: pathlib.Path, fps: int = 60):
    """Motion-compensated interpolation to `fps`.

    Only worth running once real capture is already near the output rate -
    asking it to invent eight frames out of nine (which is what 6fps capture
    would need) tears the sharp edges UI is made of. From ~30fps it just
    removes the last of the unevenness."""
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [exe, "-y", "-i", str(src), "-an",
           "-vf", f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:"
                  "me_mode=bidir:vsbmc=1",
           "-c:v", "libx264", "-preset", "slow", "-crf", "19",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(dst)]
    print(f"smoothing -> {fps}fps (this is the slow part)")
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if r.returncode != 0:
        print(r.stderr.decode()[-1200:])
        return 1
    kb = dst.stat().st_size // 1024
    print(f"ok {dst}  ({kb}KB, {OUT_W}x{OUT_H}@{fps}fps)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--work", default="", help="frame/storyboard directory")
    ap.add_argument("--out", default=str(OUT_MP4))
    ap.add_argument("--target", type=float, default=None,
                    help="squeeze reading holds to hit this runtime, seconds")
    ap.add_argument("--render-only", action="store_true",
                    help="re-time or re-caption without re-driving the app")
    ap.add_argument("--smooth", type=int, default=0, metavar="FPS",
                    help="extra motion-interpolated pass at this rate, e.g. 60")
    args = ap.parse_args()

    work = pathlib.Path(args.work) if args.work else (
        pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / "bridgemd_demo_frames")
    work.mkdir(parents=True, exist_ok=True)
    board = work / "storyboard.json"

    if not args.render_only:
        board = capture(work)
        if board is None:
            return 1
    if not board.exists():
        print(f"!! no storyboard at {board} - run without --render-only first")
        return 1
    out = pathlib.Path(args.out)
    rc = render(work, board, out, args.target)
    if rc == 0 and args.smooth:
        rc = smooth(out, out.with_name(out.stem + f"_{args.smooth}fps.mp4"),
                    args.smooth)
    return rc


if __name__ == "__main__":
    sys.exit(main())
