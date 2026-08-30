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

ACCENT = "#1257b0"        # --accent, one BridgeMD blue everywhere - app and site
TINT = "#7fb8ee"          # the light tone (--mark-b)
CHARCOAL = "#17191d"      # --ink
TILE_SCALE = 0.84         # breathing room when the mark sits inside a tile

# White at 46% over ACCENT, pre-mixed to solid hex. Alpha in a presentation
# attribute is not portable across rasterisers, so the tile ships flat colours.
ON_ACCENT = ("#ffffff", "#8ab4e6")
# On the charcoal card the tones flip: the light square comes forward so the
# mark still reads blue rather than sinking into the background.
ON_CHARCOAL = ("#7fb8ee", "#0f4a94")

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


def mark(tones: tuple[str, str] = (ACCENT, TINT), scale: float = 1.0) -> str:
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


def tile_svg(radius: int = 116, bg: str = ACCENT,
             tones: tuple[str, str] = ON_ACCENT) -> str:
    """The app-icon lockup: mark in white tones on a rounded coloured tile."""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" '
        'role="img" aria-label="BridgeMD">\n'
        f'  <rect width="512" height="512" rx="{radius}" fill="{bg}"/>\n'
        f"  {mark(tones, TILE_SCALE)}\n"
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


# Figtree files shipped with the landing page — same face LinkedIn will
# see as the site, so the card does not fall back to Arial.
_FIGTREE = {
    400: "8221d5aa15__Xmz-HUzqDCFdgfMsYiV_F7wfS-Bs_d_QF5bwkEU4HTy.woff2",
    500: "22a0ab5a22__Xmz-HUzqDCFdgfMsYiV_F7wfS-Bs_dNQF5bwkEU4HTy.woff2",
    600: "7bde881678__Xmz-HUzqDCFdgfMsYiV_F7wfS-Bs_ehR15bwkEU4HTy.woff2",
    700: "acab8c9edc__Xmz-HUzqDCFdgfMsYiV_F7wfS-Bs_eYR15bwkEU4HTy.woff2",
}
_ASSETS = pathlib.Path(__file__).resolve().parent.parent / "landing" / "assets"


def og_html() -> str:
    """1200×630 share card: Figtree, the mark, one readable thread."""
    faces = "\n".join(
        f'@font-face{{font-family:Figtree;font-weight:{w};'
        f'src:url("{(_ASSETS / name).as_uri()}")}}'
        for w, name in _FIGTREE.items()
    )
    logo = tile_svg()
    return f"""<!doctype html><meta charset="utf-8">
<style>
{faces}
html,body{{margin:0;width:1200px;height:630px}}
body{{
  background:#eef2f7;
  font-family:Figtree,ui-sans-serif,system-ui,sans-serif;
  color:#17191d;
  display:flex;align-items:center;
  padding:56px 56px 56px 64px;box-sizing:border-box;gap:48px;
}}
.brand{{flex:0 0 390px}}
.brand svg{{display:block;width:56px;height:56px;border-radius:14px}}
h1{{font-size:52px;font-weight:700;letter-spacing:-.04em;line-height:1;
  margin:28px 0 0}}
.line{{font-size:23px;font-weight:500;color:#3d444c;letter-spacing:-.02em;
  line-height:1.25;margin:16px 0 0;max-width:14.5em}}
.url{{font-size:15px;font-weight:600;color:#1257b0;letter-spacing:-.01em;
  margin:28px 0 0}}
.window{{
  flex:1;align-self:center;background:#fff;border:1px solid #dde3ea;
  border-radius:16px;box-shadow:0 18px 40px rgba(23,25,29,.10);
  overflow:hidden;display:flex;flex-direction:column;min-width:0;
}}
.head{{padding:20px 22px 16px;border-bottom:1px solid #e8ebef}}
.head b{{display:block;font-size:18px;font-weight:700;letter-spacing:-.02em}}
.head span{{display:block;margin-top:4px;font-size:13px;font-weight:500;color:#5c646e}}
.msg{{margin:20px 22px 16px;background:#f4f6f8;border-radius:12px;padding:14px 16px;
  font-size:16px;font-weight:400;line-height:1.4;color:#17191d}}
.msg small{{display:block;font-size:12px;font-weight:600;color:#5c646e;margin-bottom:6px}}
.tabs{{display:flex;gap:16px;padding:0 22px;font-size:13px;font-weight:600}}
.tabs b{{color:#1257b0;border-bottom:2px solid #1257b0;padding-bottom:8px}}
.tabs span{{color:#5c646e;padding-bottom:8px}}
.prompt{{margin:14px 16px 16px;background:#e8f1fc;border-radius:10px;
  padding:12px 14px;font-size:14px;font-weight:600;color:#1257b0;
  display:flex;align-items:center;gap:8px}}
.prompt svg{{flex:0 0 16px;width:16px;height:16px;stroke:#1257b0;fill:none;
  stroke-width:1.8;stroke-linejoin:round}}
</style>
<div class="brand">
  {logo}
  <h1>BridgeMD</h1>
  <p class="line">One shared inbox for clinical research sites.</p>
  <p class="url">bridgemd.health</p>
</div>
<div class="window">
  <div class="head">
    <b>Extension study timing</b>
    <span>Victor Hale · Gmail</span>
  </div>
  <div class="msg">
    <small>Victor Hale</small>
    I am finishing the main study next month. When does the extension normally begin?
  </div>
  <div class="tabs"><b>Reply</b><span>Internal note</span></div>
  <div class="prompt">
    <svg viewBox="0 0 24 24"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/></svg>
    Tell Bridget what to say — offer a screening call next week
  </div>
</div>
"""


def manifest_json(theme: str, suffix: str) -> str:
    """PWA manifest for the one BridgeMD palette."""
    return (
        "{\n"
        '  "name": "BridgeMD",\n'
        '  "short_name": "BridgeMD",\n'
        '  "description": "Find recruiting clinical trials near you.",\n'
        '  "start_url": "/",\n'
        '  "scope": "/",\n'
        '  "display": "standalone",\n'
        '  "background_color": "#f6f6fb",\n'
        f'  "theme_color": "{theme}",\n'
        '  "icons": [\n'
        f'    {{ "src": "/static/icon{suffix}-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any" }},\n'
        f'    {{ "src": "/static/icon{suffix}-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any" }},\n'
        f'    {{ "src": "/static/icon{suffix}-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable" }}\n'
        "  ]\n"
        "}\n"
    )


# One palette, one brand: the app and the patient-facing site share the same
# blue everywhere, so there is only one icon set to generate.
PALETTES = [
    ("", ACCENT, ON_ACCENT, ACCENT),
]


def main() -> int:
    tmps: list[pathlib.Path] = []
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()

            for suffix, bg, tones, theme in PALETTES:
                (STATIC / f"logo{suffix}.svg").write_text(
                    tile_svg(bg=bg, tones=tones))
                print(f"ok logo{suffix}.svg")

                # Rounded tile for favicons/PWA; square bleed for iOS (it masks
                # its own corners).
                rounded = STATIC / f"_tmp_rounded{suffix}.svg"
                square = STATIC / f"_tmp_square{suffix}.svg"
                rounded.write_text(tile_svg(bg=bg, tones=tones))
                square.write_text(tile_svg(radius=0, bg=bg, tones=tones))
                tmp48 = STATIC / f"_tmp-48{suffix}.png"
                tmps += [rounded, square, tmp48]

                jobs = [
                    (rounded, f"favicon{suffix}-16.png", 16),
                    (rounded, f"favicon{suffix}-32.png", 32),
                    (rounded, tmp48.name, 48),
                    (rounded, f"icon{suffix}-192.png", 192),
                    (rounded, f"icon{suffix}-512.png", 512),
                    (square, f"apple-touch-icon{suffix}.png", 180),
                ]
                for src, name, size in jobs:
                    pg = b.new_context(viewport={"width": size, "height": size},
                                       device_scale_factor=1).new_page()
                    pg.goto(f"file://{src}")
                    pg.wait_for_timeout(120)
                    pg.screenshot(path=str(STATIC / name), omit_background=False)
                    pg.close()
                    print(f"ok {name} ({size}px)")

                write_ico([STATIC / f"favicon{suffix}-16.png",
                           STATIC / f"favicon{suffix}-32.png", tmp48],
                          STATIC / f"favicon{suffix}.ico")
                print(f"ok favicon{suffix}.ico (16/32/48)")

                man = "site.webmanifest" if not suffix else f"site{suffix}.webmanifest"
                (STATIC / man).write_text(manifest_json(theme, suffix))
                print(f"ok {man}")

            # Social card is 1200×630 (Open Graph). Render at 2× and
            # downscale so Figtree stays sharp when LinkedIn shows it small.
            from PIL import Image
            og = STATIC / "_tmp_og.html"
            og.write_text(og_html())
            tmps.append(og)
            raw = STATIC / "_tmp_og.png"
            tmps.append(raw)
            pg = b.new_context(viewport={"width": 1200, "height": 630},
                               device_scale_factor=2).new_page()
            pg.goto(f"file://{og}")
            pg.wait_for_timeout(200)
            pg.screenshot(path=str(raw))
            pg.close()
            Image.open(raw).resize((1200, 630), Image.Resampling.LANCZOS).save(
                STATIC / "og-default.png")
            print("ok og-default.png (1200x630)")
            b.close()
    finally:
        for tmp in tmps:
            tmp.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
