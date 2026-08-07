"""Build every BridgeMD brand asset from one source-of-truth mark.

The mark is two rounded squares overlapping on the diagonal - the two sides
BridgeMD joins - with the overlap cut out of both. It is defined once here, in
a 512x512 box, and everything else (favicons, PWA icons, .ico, social card) is
generated from it, so the brand can never drift between the app, the marketing
site and the browser tab.

    cd matcher/web && ../.venv/bin/python tools/build_brand.py

Writes into static/: logo.svg, favicon-16/32.png, favicon.ico,
apple-touch-icon.png, icon-192/512.png, og-default.png.
"""
from __future__ import annotations

import pathlib
import struct
import sys

from playwright.sync_api import sync_playwright

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"

TEAL = "#0d6c6a"          # --accent, the deep tone
TINT = "#7ec9c3"          # the light tone
CHARCOAL = "#17191d"      # --ink
TILE_SCALE = 0.84         # breathing room when the mark sits inside a tile

# White at 46% over TEAL, pre-mixed to solid hex. Alpha in a presentation
# attribute is not portable across rasterisers, so the tile ships flat colours.
ON_TEAL = ("#ffffff", "#86b5b4")
# On the charcoal card the tones flip: the light square comes forward so the
# mark still reads teal rather than sinking into the background.
ON_CHARCOAL = ("#7ec9c3", "#3a7f7b")

# Both squares carry the same 22.5% corner radius as the tile they sit in. Each
# is drawn with the intersection appended as a second subpath and an even-odd
# fill, so the overlap is a real hole rather than a third colour - which keeps
# the notch correct on white, on the teal tile and on charcoal alike.
BACK = ("M142 96 H254 A46 46 0 0 1 300 142 V254 A46 46 0 0 1 254 300 "
        "H142 A46 46 0 0 1 96 254 V142 A46 46 0 0 1 142 96 Z")
FRONT = ("M258 212 H370 A46 46 0 0 1 416 258 V370 A46 46 0 0 1 370 416 "
         "H258 A46 46 0 0 1 212 370 V258 A46 46 0 0 1 258 212 Z")
LAP = ("M258 212 H300 V254 A46 46 0 0 1 254 300 H212 V258 "
       "A46 46 0 0 1 258 212 Z")


def mark(tones: tuple[str, str] = (TEAL, TINT), scale: float = 1.0) -> str:
    """The mark, as two even-odd paths in a 512 box: back square, front square,
    each with the overlap cut away."""
    a, b = tones
    zoom = (f"translate(256 256) scale({scale}) translate(-256 -256)"
            if scale != 1.0 else "")
    attr = f' transform="{zoom}"' if zoom else ""
    return (
        f'<g{attr} fill-rule="evenodd">\n'
        f'    <path fill="{b}" d="{BACK} {LAP}"/>\n'
        f'    <path fill="{a}" d="{FRONT} {LAP}"/>\n'
        "  </g>"
    )


def tile_svg(radius: int = 116, bg: str = TEAL) -> str:
    """The app-icon lockup: mark in white tones on a rounded teal tile."""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" '
        'role="img" aria-label="BridgeMD">\n'
        f'  <rect width="512" height="512" rx="{radius}" fill="{bg}"/>\n'
        f"  {mark(ON_TEAL, TILE_SCALE)}\n"
        "</svg>\n"
    )


def write_ico(pngs: list[pathlib.Path], out: pathlib.Path) -> None:
    """Pack PNGs into a multi-size .ico (PNG-compressed entries)."""
    blobs = [p.read_bytes() for p in pngs]
    sizes = [16, 32, 48][: len(blobs)]
    header = struct.pack("<HHH", 0, 1, len(blobs))
    offset = 6 + 16 * len(blobs)
    entries, body = b"", b""
    for size, blob in zip(sizes, blobs):
        entries += struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32,
                               len(blob), offset)
        offset += len(blob)
        body += blob
    out.write_bytes(header + entries + body)


def og_html() -> str:
    """Social card: same mark, same charcoal, so shares look like the product."""
    return f"""<!doctype html><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@600;800&display=swap" rel="stylesheet">
<style>
  html, body {{ margin: 0; width: 1536px; height: 1024px; }}
  body {{ background:
      radial-gradient(900px 620px at 78% -12%, rgba(13,108,106,.46), transparent 62%),
      {CHARCOAL};
    font-family: Manrope, "Helvetica Neue", Helvetica, Arial, sans-serif;
    display: flex; flex-direction: column; justify-content: center;
    padding: 0 118px; box-sizing: border-box; color: #fff; }}
  .lock {{ display: flex; align-items: center; gap: 30px; }}
  .lock b {{ font-size: 86px; font-weight: 800; letter-spacing: -.035em; }}
  h1 {{ font-size: 80px; font-weight: 800; letter-spacing: -.038em; line-height: 1.07;
    margin: 58px 0 0; max-width: 26ch; }}
  p {{ font-size: 34px; font-weight: 600; color: rgba(255,255,255,.66);
    margin: 30px 0 0; letter-spacing: -.01em; }}
  .chips {{ display: flex; gap: 14px; margin-top: 54px; }}
  .chips span {{ font-size: 24px; font-weight: 700; padding: 13px 24px;
    border-radius: 999px; border: 1px solid rgba(255,255,255,.2);
    background: rgba(255,255,255,.06); color: rgba(255,255,255,.82); }}
</style>
<div class="lock">
  <svg viewBox="0 0 512 512" width="112" height="112">{mark(ON_CHARCOAL)}</svg>
  <b>BridgeMD</b>
</div>
<h1>Clinical trials, matched and managed in one place.</h1>
<p>bridgemd.health</p>
<div class="chips">
  <span>Consent-first</span><span>De-identified by default</span><span>Human-in-the-loop AI</span>
</div>
"""


def main() -> int:
    (STATIC / "logo.svg").write_text(tile_svg())
    print("ok logo.svg")

    # Rounded tile for favicons/PWA; square bleed for iOS, which masks it itself.
    rounded = STATIC / "_tmp_rounded.svg"
    square = STATIC / "_tmp_square.svg"
    rounded.write_text(tile_svg())
    square.write_text(tile_svg(radius=0))
    og = STATIC / "_tmp_og.html"
    og.write_text(og_html())

    jobs = [
        (rounded, "favicon-16.png", 16), (rounded, "favicon-32.png", 32),
        (rounded, "_tmp-48.png", 48), (rounded, "icon-192.png", 192),
        (rounded, "icon-512.png", 512), (square, "apple-touch-icon.png", 180),
    ]
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            for src, name, size in jobs:
                pg = b.new_context(viewport={"width": size, "height": size},
                                   device_scale_factor=1).new_page()
                pg.goto(f"file://{src}")
                pg.wait_for_timeout(120)
                pg.screenshot(path=str(STATIC / name), omit_background=False)
                pg.close()
                print(f"ok {name} ({size}px)")

            pg = b.new_context(viewport={"width": 1536, "height": 1024},
                               device_scale_factor=1).new_page()
            pg.goto(f"file://{og}")
            pg.wait_for_timeout(1200)   # let the webfont land if online
            pg.screenshot(path=str(STATIC / "og-default.png"))
            pg.close()
            print("ok og-default.png (1536x1024)")
            b.close()

        write_ico([STATIC / "favicon-16.png", STATIC / "favicon-32.png",
                   STATIC / "_tmp-48.png"], STATIC / "favicon.ico")
        print("ok favicon.ico (16/32/48)")
    finally:
        for tmp in (rounded, square, og, STATIC / "_tmp-48.png"):
            tmp.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
