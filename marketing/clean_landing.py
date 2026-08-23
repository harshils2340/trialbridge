"""Rebuild the BridgeMD marketing landing from the scraped Framer mirror.

This whole pipeline lives in `matcher/marketing/` (inside the repo):
  - clean_landing.py   this build script
  - marketing_test/    the scraped Framer mirror (build INPUT, source of truth)
  - product_shots/     product screenshots injected into the page
  - scrape_framer.py   re-scrapes the Framer site into marketing_test/
It writes the shipped page to `matcher/web/landing/` (committed build OUTPUT that
the Flask app serves at `/`).

Run with the project venv, which has Pillow (used for the raster hue-shift). Paths
resolve relative to THIS file, so it works from any cwd:

    matcher/.venv/bin/python matcher/marketing/clean_landing.py

Do NOT use the default `python3` here: the fbcode python has no pip, and a bare
system python is missing Pillow, so both crash with `ModuleNotFoundError: PIL`.
Pillow is a build-only dep (only this file imports it), so it is intentionally not
in matcher/web/requirements.txt, which is the web app's deploy manifest.
"""
import re, shutil, os, json, time, urllib.request

# Resolve inputs/outputs relative to this script, not the caller's cwd, so the build
# is reproducible from anywhere. Inputs (marketing_test/, product_shots/) sit next to
# this file; the output landing dir is matcher/web/landing/.
HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

SRC = "marketing_test"
# Same default (and same override) as web/app.py's CAL_LINK, so the static landing
# and the Jinja pages can never point at two different booking links.
CAL = os.environ.get("CAL_LINK", "https://cal.com/harshil-shah-7tkvs7/30min").strip()
FINDER_URL = "/find-trial"
# "Product" opens the actual app. /app/home is login-gated and redirects to the inbox.
APP_HOME = "/app/home"
# Framer's CDN image base. It appears in both the JS chunks and the SSR HTML; both get
# repointed at our local assets so hydration can't reset <img> src to the remote original.
CDN_IMG = "https://framerusercontent.com/images/"
# Real brand logos via apistemic's free logo API (Clearbit alternative, no key).
# Used nominatively to show which channels flow in; the hand-drawn SVG for each
# stays behind it as a fallback so a failed/blocked request never renders blank.
# Free tier requires a visible attribution link (rendered under the diagram).
# https://logos.apistemic.com/
LOGO_API = "https://logos-api.apistemic.com/domain:"

# Hero copy: clear, direct BridgeMD value prop. No em dashes, no AI slop.
EYEBROW = "One inbox for every recruitment channel"
H1_L1 = "Every Patient Inquiry"
H1_L2 = "In One Shared Inbox"
SUBHEAD = ("The first AI patient inbox for clinical trials. Every message from every "
           "source, centralized in one place.")

# Word-split headings (anchored by their now-new data-framer-name prefix) whose SSR
# visible text must be rewritten to the plain new copy.
_SPLIT_FIX = [
    ("Everything your study team needs in one place",
     "Everything your study team needs in one place"),
    ("One inbox for every recruitment channel",
     "One inbox for every recruitment channel, with pre-screening, routing, and a "
     "shared workspace, so your team can spend its time enrolling patients."),
    ("See how BridgeMD brings every recruitment channel",
     "See how BridgeMD brings every recruitment channel into one shared inbox for "
     "your study team."),
    # Stats band ("Numbers That Drive Success" in the template). Cards 1 and 4 are
    # capability facts (true by construction: 6+ channels in one inbox, 100% audit-
    # logged). Cards 2 and 3 (~5 hrs saved/week, +30% same-day replies) are EFFICIENCY
    # LEVERS shown as ILLUSTRATIVE targets -- BridgeMD has no measured pilot data yet,
    # so the subhead explicitly labels them illustrative. Do NOT present these as
    # measured results, and swap in real pilot numbers (and drop the "illustrative"
    # wording) once you have them (compliance.mdc + enrollment-velocity.mdc claims).
    ("Numbers That Drive Success", "Time back, faster replies"),
    ("See the numbers! Our platform boosts",
     "One shared inbox for every channel gives coordinators time back and speeds up "
     "responses. Figures below are illustrative until your own pilot data replaces them."),
]

# Stats band card values. Same compliance note as above: capability/scope facts only.
# Keyed on the visible TEXT node (>x<) since Framer gave every card the same
# data-framer-name, so only the text content distinguishes them (and this updates
# every SSR breakpoint variant at once). The number span keeps its blue gradient.
STATS_TEXT = [
    (">98%<", ">6+<"),
    (">10x<", ">~5 hrs<"),
    (">170M+<", ">+30%<"),
    (">320%<", ">100%<"),
    (">Deliverability<", ">Channels in one inbox<"),
    (">Efficiency<", ">Saved per coordinator / week<"),
    (">Leads<", ">More inquiries answered same-day<"),
    (">Conversions<", ">Audit-logged<"),
]

# Embed-the-finder section (our own original block; injected before the CTA). Plain
# HTML/CSS so it renders identically whether or not Framer's React hydrates.
# Matches the native Framer card sections: Figtree font, white cards on a light
# band, a centered eyebrow+heading+subhead, then a 3-up grid of cards each with a
# round icon chip. (The old bespoke two-column gradient block read as off-brand
# "AI slop" -- wrong font, wrong layout.)
EMBED_CSS = (
    '#bmd-embed{font-family:"Figtree",system-ui,-apple-system,sans-serif;'
    # Match Framer's Figtree stylistic sets (single-story a/g, etc.) so this
    # section's glyphs are identical to the rest of the page, not a "different font".
    'font-feature-settings:"cv09" 1,"cv03" 1,"cv04" 1,"cv11" 1,"blwf" 1;'
    "background:#f5f7fb;padding:110px 24px}"
    "#bmd-embed .bmd-embed-wrap{max-width:1120px;margin:0 auto}"
    "#bmd-embed .bmd-embed-head{text-align:center;max-width:660px;margin:0 auto 54px}"
    "#bmd-embed .pill{display:inline-block;padding:7px 15px;border-radius:999px;"
    "background:#fff;border:1px solid #dce6f5;color:#1257b0;font-size:13px;"
    "font-weight:700;margin-bottom:18px}"
    "#bmd-embed h2{font-size:clamp(30px,3.6vw,46px);line-height:1.08;margin:0 0 16px;"
    "letter-spacing:-.03em;color:#12122b;font-weight:800}"
    "#bmd-embed .lead{font-size:18px;line-height:1.55;color:#516079;margin:0}"
    "#bmd-embed .bmd-embed-grid{display:grid;grid-template-columns:repeat(3,1fr);"
    "gap:24px}"
    "#bmd-embed .bmd-embed-card{background:#fff;border:1px solid rgba(18,87,176,.10);"
    "border-radius:20px;padding:32px 28px;box-shadow:0 2px 10px rgba(16,16,40,.04)}"
    "#bmd-embed .bmd-embed-card .ic{display:inline-flex;align-items:center;"
    "justify-content:center;width:52px;height:52px;border-radius:50%;"
    "background:rgba(18,87,176,.08);color:#1257b0;margin-bottom:20px}"
    "#bmd-embed .bmd-embed-card .ic svg{width:24px;height:24px}"
    "#bmd-embed .bmd-embed-card h3{margin:0 0 10px;font-size:20px;font-weight:700;"
    "letter-spacing:-.01em;color:#12122b}"
    "#bmd-embed .bmd-embed-card p{margin:0;color:#516079;font-size:15.5px;"
    "line-height:1.6}"
    "#bmd-embed .bmd-embed-cta{display:flex;justify-content:center;gap:14px;"
    "margin-top:44px;flex-wrap:wrap}"
    "#bmd-embed .btn-primary{display:inline-flex;align-items:center;gap:8px;border:0;"
    "cursor:pointer;font-family:inherit;color:#fff;font-weight:700;font-size:16px;"
    "padding:15px 28px;border-radius:14px;"
    "background:linear-gradient(180deg,#3f7fce,#1257b0);"
    "box-shadow:0 12px 24px -10px rgba(18,87,176,.6)}"
    "#bmd-embed .btn-ghost{display:inline-flex;align-items:center;gap:8px;"
    "background:#fff;color:#1257b0;text-decoration:none;border:1px solid #dce6f5;"
    "font-weight:700;font-size:16px;padding:15px 28px;border-radius:14px}"
    "@media(max-width:860px){#bmd-embed .bmd-embed-grid{grid-template-columns:1fr}}"
    # --- "Inside" mockup: browser window + partner site + embedded finder widget ---
    "#bmd-embed .bmd-embed-frame{max-width:940px;margin:0 auto 50px;background:#fff;"
    "border:1px solid rgba(18,87,176,.14);border-radius:16px;"
    "box-shadow:0 34px 80px -34px rgba(18,40,90,.4);overflow:hidden}"
    "#bmd-embed .bmd-bw-bar{display:flex;align-items:center;gap:14px;padding:11px 16px;"
    "background:#eef2f8;border-bottom:1px solid #e2e8f2}"
    "#bmd-embed .bmd-bw-dots{display:inline-flex;gap:7px;flex:0 0 auto}"
    "#bmd-embed .bmd-bw-dots i{width:11px;height:11px;border-radius:50%;background:#cdd6e4}"
    "#bmd-embed .bmd-bw-url{flex:1;display:inline-flex;align-items:center;gap:7px;"
    "background:#fff;border:1px solid #e2e8f2;border-radius:8px;padding:6px 12px;"
    "font-size:12.5px;color:#64748b}"
    "#bmd-embed .bmd-bw-url svg{width:13px;height:13px;color:#94a3b8;flex:0 0 auto}"
    "#bmd-embed .bmd-host-nav{display:flex;align-items:center;justify-content:space-between;"
    "gap:16px;padding:16px 26px;border-bottom:1px solid #eef2f6}"
    "#bmd-embed .bmd-host-brand{display:inline-flex;align-items:center;gap:9px;"
    "font-weight:800;color:#0f2a3a;font-size:16px;letter-spacing:-.01em}"
    "#bmd-embed .bmd-host-brand svg{width:22px;height:22px;color:#0e7c86;flex:0 0 auto}"
    "#bmd-embed .bmd-host-links{display:inline-flex;gap:20px;font-size:14px;"
    "font-weight:600}"
    "#bmd-embed .bmd-host-links a{color:#64748b;text-decoration:none}"
    "#bmd-embed .bmd-host-links a.on{color:#0e7c86}"
    "#bmd-embed .bmd-host-hero{padding:26px 26px 4px}"
    "#bmd-embed .bmd-host-hero h4{margin:0 0 6px;font-size:22px;font-weight:800;"
    "color:#12122b;letter-spacing:-.02em}"
    "#bmd-embed .bmd-host-hero p{margin:0;color:#64748b;font-size:14px}"
    "#bmd-embed .bmd-widget{position:relative;margin:20px 26px 30px;"
    "border:1px solid rgba(18,87,176,.18);border-radius:14px;padding:20px 18px;"
    "background:linear-gradient(180deg,#f7faff,#fff)}"
    "#bmd-embed .bmd-widget-tag{position:absolute;top:-12px;left:18px;"
    "display:inline-flex;align-items:center;gap:6px;background:#fff;"
    "border:1px solid rgba(18,87,176,.2);color:#1257b0;font-size:11.5px;font-weight:700;"
    "padding:3px 10px 3px 8px;border-radius:999px}"
    "#bmd-embed .bmd-widget-tag svg{width:14px;height:14px}"
    "#bmd-embed .bmd-finder-search{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}"
    "#bmd-embed .bmd-finder-search .fld{flex:1 1 180px;display:inline-flex;"
    "align-items:center;gap:8px;background:#fff;border:1px solid #dbe4f0;"
    "border-radius:10px;padding:10px 12px;font-size:13px;color:#64748b}"
    "#bmd-embed .bmd-finder-search .fld svg{width:15px;height:15px;color:#1257b0;"
    "flex:0 0 auto}"
    "#bmd-embed .bmd-finder-search .fld b{color:#12122b;font-weight:700}"
    "#bmd-embed .bmd-finder-search .fbtn{display:inline-flex;align-items:center;"
    "color:#fff;font-weight:700;font-size:13.5px;padding:10px 22px;border-radius:10px;"
    "cursor:pointer;transition:filter .15s ease,transform .15s ease;"
    "background:linear-gradient(180deg,#3f7fce,#1257b0)}"
    "#bmd-embed .bmd-finder-search .fbtn:hover{filter:brightness(1.06)}"
    "#bmd-embed .bmd-finder-search .fbtn:active{transform:translateY(1px)}"
    "#bmd-embed .bmd-finder-meta{font-size:12.5px;color:#64748b;margin:0 2px 12px;"
    "font-weight:600}"
    "#bmd-embed .bmd-tcard{display:flex;gap:14px;justify-content:space-between;"
    "align-items:flex-start;background:#fff;border:1px solid #e6ecf5;border-radius:12px;"
    "padding:14px 16px;margin-bottom:10px}"
    "#bmd-embed .bmd-tcard:last-child{margin-bottom:0}"
    "#bmd-embed .bmd-tc-title{font-weight:700;color:#12122b;font-size:14.5px;"
    "line-height:1.3;margin-bottom:5px}"
    "#bmd-embed .bmd-tc-meta{font-size:12px;color:#64748b;display:flex;flex-wrap:wrap;"
    "gap:6px;align-items:center;margin-bottom:6px}"
    "#bmd-embed .bmd-tc-meta .mono{font-family:ui-monospace,Menlo,monospace;color:#516079}"
    "#bmd-embed .bmd-tc-why{font-size:12.5px;color:#516079;line-height:1.45}"
    "#bmd-embed .bmd-tc-side{display:flex;flex-direction:column;align-items:flex-end;"
    "gap:8px;flex:0 0 auto}"
    "#bmd-embed .bmd-badge{display:inline-flex;align-items:center;font-size:11px;"
    "font-weight:700;padding:3px 9px;border-radius:999px;white-space:nowrap}"
    "#bmd-embed .bmd-badge.ok{background:#e7f6ee;color:#15803d}"
    "#bmd-embed .bmd-badge.info{background:#eaf1fb;color:#1257b0}"
    "#bmd-embed .bmd-tc-apply{display:inline-flex;background:#1257b0;color:#fff;"
    "font-weight:700;font-size:12.5px;padding:7px 16px;border-radius:9px;cursor:pointer;"
    "transition:filter .15s ease,transform .15s ease}"
    "#bmd-embed .bmd-tc-apply:hover{filter:brightness(1.08)}"
    "#bmd-embed .bmd-tc-apply:active{transform:translateY(1px)}"
    "@media(max-width:640px){#bmd-embed{padding:72px 16px 84px}"
    "#bmd-embed .bmd-host-links{display:none}"
    "#bmd-embed .bmd-tcard{flex-direction:column}"
    "#bmd-embed .bmd-tc-side{flex-direction:row;align-items:center;"
    "justify-content:space-between;width:100%}"
    "#bmd-embed .bmd-finder-search{flex-direction:column}"
    "#bmd-embed .bmd-finder-search .fbtn,#bmd-embed .bmd-tc-apply{"
    "width:100%;min-height:44px;justify-content:center}}"
    # --- Stage: browser mockup + floating "auto-routed to a person" card ---
    "#bmd-embed .bmd-embed-stage{position:relative;max-width:1000px;margin:0 auto 64px}"
    "#bmd-embed .bmd-embed-stage .bmd-embed-frame{margin:0 auto}"
    "#bmd-embed .bmd-route-card{position:absolute;right:-6px;bottom:-36px;width:322px;"
    "background:#fff;border:1px solid rgba(18,87,176,.16);border-radius:16px;"
    "box-shadow:0 26px 64px -24px rgba(18,40,90,.55);padding:15px 16px 14px}"
    "#bmd-embed .bmd-rc-head{display:flex;align-items:center;gap:8px;margin-bottom:11px}"
    "#bmd-embed .bmd-rc-mark{display:inline-flex}"
    "#bmd-embed .bmd-rc-mark svg{width:20px;height:20px}"
    "#bmd-embed .bmd-rc-title{font-weight:800;color:#12122b;font-size:14px;"
    "letter-spacing:-.01em}"
    "#bmd-embed .bmd-rc-count{margin-left:auto;background:#eaf1fb;color:#1257b0;"
    "font-size:11px;font-weight:700;padding:2px 9px;border-radius:999px}"
    "#bmd-embed .bmd-rc-row{display:flex;gap:10px;align-items:flex-start;"
    "padding:11px 12px;background:#f7faff;border:1px solid #e6ecf5;border-radius:11px}"
    "#bmd-embed .bmd-rc-dot{width:8px;height:8px;border-radius:50%;background:#1257b0;"
    "margin-top:5px;flex:0 0 auto}"
    "#bmd-embed .bmd-rc-name{font-weight:700;color:#12122b;font-size:13px;line-height:1.3}"
    "#bmd-embed .bmd-rc-sub{color:#64748b;font-size:11.5px;margin-top:2px}"
    "#bmd-embed .bmd-rc-route{display:flex;gap:9px;align-items:flex-start;"
    "margin:12px 4px 0;font-size:12.5px;color:#516079;line-height:1.4}"
    "#bmd-embed .bmd-rc-route svg{width:17px;height:17px;color:#15803d;flex:0 0 auto;"
    "margin-top:1px}"
    "#bmd-embed .bmd-rc-route b{color:#12122b;font-weight:700}"
    "#bmd-embed .bmd-rc-note{margin:10px 4px 0;font-size:11.5px;color:#94a3b8;"
    "line-height:1.45}"
    "@media(max-width:980px){#bmd-embed .bmd-embed-stage{margin-bottom:50px}"
    "#bmd-embed .bmd-route-card{position:static;width:auto;max-width:420px;"
    "right:auto;bottom:auto;margin:18px auto 0}}"
)

_SVG = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">%s</svg>')
_IC_SNIPPET = _SVG % '<path d="M8 8l-4 4 4 4"/><path d="M16 8l4 4-4 4"/>'
_IC_STUDIES = _SVG % ('<rect x="3.5" y="4.5" width="17" height="4" rx="1.2"/>'
                      '<rect x="3.5" y="10.5" width="17" height="4" rx="1.2"/>'
                      '<rect x="3.5" y="16.5" width="11" height="3" rx="1.2"/>')
_IC_INBOX = _SVG % ('<path d="M3.5 12.5V6a2 2 0 0 1 2-2h13a2 2 0 0 1 2 2v6.5"/>'
                    '<path d="M3.5 12.5H8l1.5 2.5h5L16 12.5h4.5V18a2 2 0 0 1-2 2h-13'
                    'a2 2 0 0 1-2-2z"/>')
# Icons + BridgeMD mark used by the "Inside" mockup (a partner site with the finder
# embedded). _SVG is the shared stroke wrapper defined above.
_IC_LOCK2 = _SVG % ('<rect x="5" y="10.5" width="14" height="9" rx="2"/>'
                    '<path d="M8 10.5V7.5a4 4 0 0 1 8 0v3"/>')
_IC_CROSS = _SVG % ('<rect x="4.5" y="4.5" width="15" height="15" rx="4"/>'
                    '<path d="M12 8.5v7"/><path d="M8.5 12h7"/>')
_IC_SEARCH2 = _SVG % '<circle cx="11" cy="11" r="7"/><path d="M20.5 20.5l-4-4"/>'
_IC_PIN = _SVG % ('<path d="M12 21s7-5.5 7-11a7 7 0 1 0-14 0c0 5.5 7 11 7 11z"/>'
                  '<circle cx="12" cy="10" r="2.5"/>')
_IC_ROUTE = _SVG % ('<path d="M5 5v6a4 4 0 0 0 4 4h8"/><path d="M14 11l4 4-4 4"/>')
_EMB_MARK = (
    '<svg viewBox="0 0 512 512" fill-rule="evenodd" aria-hidden="true">'
    '<path fill="#7fb3e6" d="M142 96H254A46 46 0 0 1 300 142V254A46 46 0 0 1 254 300H142'
    'A46 46 0 0 1 96 254V142A46 46 0 0 1 142 96ZM258 212H300V254A46 46 0 0 1 254 300H212'
    'V258A46 46 0 0 1 258 212Z"/>'
    '<path fill="#1257b0" d="M258 212H370A46 46 0 0 1 416 258V370A46 46 0 0 1 370 416H258'
    'A46 46 0 0 1 212 370V258A46 46 0 0 1 258 212ZM258 212H300V254A46 46 0 0 1 254 300H212'
    'V258A46 46 0 0 1 258 212Z"/></svg>')

# The embedded-finder mockup: a browser window framing a partner hospital site, with
# the BridgeMD trial finder ("indeed side") dropped in as a widget. Static HTML so it
# renders without Framer's runtime. Copy is illustrative (a demo site + sample trials),
# not a real customer or recruitment claim.
EMBED_MOCK = (
    '<div class="bmd-embed-frame">'
    '<div class="bmd-bw-bar"><span class="bmd-bw-dots"><i></i><i></i><i></i></span>'
    f'<span class="bmd-bw-url">{_IC_LOCK2}stmarysresearch.org/find-a-trial</span></div>'
    '<div class="bmd-bw-body">'
    f'<div class="bmd-host-nav"><span class="bmd-host-brand">{_IC_CROSS}'
    'St. Mary&rsquo;s Research</span>'
    '<span class="bmd-host-links"><a>Care</a><a>Conditions</a>'
    '<a class="on">Find a trial</a><a>Contact</a></span></div>'
    '<div class="bmd-host-hero"><h4>Find a clinical trial at St. Mary&rsquo;s</h4>'
    '<p>Search our active studies and see if you may qualify.</p></div>'
    '<div class="bmd-widget">'
    f'<span class="bmd-widget-tag">{_EMB_MARK}Powered by BridgeMD</span>'
    '<div class="bmd-finder-search">'
    f'<span class="fld">{_IC_SEARCH2}Condition&nbsp;<b>Lupus nephritis</b></span>'
    f'<span class="fld">{_IC_PIN}Near&nbsp;<b>Toronto, ON</b></span>'
    f'<span class="fbtn" data-bmd-go="{FINDER_URL}" role="button" tabindex="0">'
    'Search</span></div>'
    '<div class="bmd-finder-meta">12 recruiting trials &middot; ranked by fit, then '
    'distance</div>'
    '<div class="bmd-tcard"><div class="bmd-tc-main">'
    '<div class="bmd-tc-title">A Study of an Investigational Therapy in Adults With '
    'Active Lupus Nephritis</div>'
    '<div class="bmd-tc-meta"><span class="mono">NCT03XXXXXX</span> &middot; Phase&nbsp;3 '
    '&middot; <span class="bmd-badge ok">Recruiting</span> &middot; 6&nbsp;km away</div>'
    '<div class="bmd-tc-why">Matches diagnosis (class&nbsp;IV), eGFR in range, on '
    'standard background therapy.</div></div>'
    '<div class="bmd-tc-side"><span class="bmd-badge ok">Likely eligible</span>'
    f'<span class="bmd-tc-apply" data-bmd-go="{FINDER_URL}" role="button" tabindex="0">'
    'Apply</span></div></div>'
    '<div class="bmd-tcard"><div class="bmd-tc-main">'
    '<div class="bmd-tc-title">Long-Term Safety Registry for Lupus Nephritis</div>'
    '<div class="bmd-tc-meta"><span class="mono">NCT04XXXXXX</span> &middot; '
    '<span class="bmd-badge info">Registry</span> &middot; '
    '<span class="bmd-badge ok">Recruiting</span> &middot; 14&nbsp;km away</div>'
    '<div class="bmd-tc-why">Open to your diagnosis; confirm current medications with '
    'the study team.</div></div>'
    '<div class="bmd-tc-side"><span class="bmd-badge info">Possible</span>'
    f'<span class="bmd-tc-apply" data-bmd-go="{FINDER_URL}" role="button" tabindex="0">'
    'Apply</span></div></div>'
    '</div></div></div>'
)

# Companion card that floats over the browser mockup: shows what happens the moment
# a patient hits "Apply" -- the inquiry lands in the shared inbox and is auto-routed
# to the right coordinator, no email/forwarding. Illustrative demo data (no real
# patient, no recruitment/payment claim). This is the Operational Efficiency story:
# cuts triage/cycle time, redirecting coordinator time into screening/enrolling.
ROUTE_CARD = (
    '<div class="bmd-route-card" aria-hidden="true">'
    f'<div class="bmd-rc-head"><span class="bmd-rc-mark">{_EMB_MARK}</span>'
    '<span class="bmd-rc-title">Shared inbox</span>'
    '<span class="bmd-rc-count">1 new</span></div>'
    '<div class="bmd-rc-row"><span class="bmd-rc-dot"></span>'
    '<div class="bmd-rc-main"><div class="bmd-rc-name">New inquiry &middot; Lupus '
    'nephritis</div>'
    '<div class="bmd-rc-sub">via St. Mary&rsquo;s finder &middot; just now</div></div></div>'
    f'<div class="bmd-rc-route">{_IC_ROUTE}<span>Auto-routed to <b>Sarah&nbsp;T.</b>, '
    'Lupus&nbsp;Nephritis coordinator</span></div>'
    '<div class="bmd-rc-note">On the right person&rsquo;s desk in seconds. No email, '
    'no forwarding.</div>'
    '</div>'
)

EMBED_HTML = (
    '<section id="bmd-embed"><div class="bmd-embed-wrap">'
    '<div class="bmd-embed-head">'
    '<span class="pill">BridgeMD Embed</span>'
    '<h2>Two ways to run the trial finder</h2>'
    '<p class="lead">Use it as a standalone site your team can share anywhere, or embed '
    'it straight into your hospital, clinic, or partner website, tailored to the '
    'studies you run. Either way, people search your trials without leaving that site, '
    'and every inquiry lands in your shared inbox, sorted and routed to your team.</p>'
    '</div>'
    + '<div class="bmd-embed-stage">' + EMBED_MOCK + ROUTE_CARD + '</div>' +
    '<div class="bmd-embed-grid">'
    f'<div class="bmd-embed-card"><span class="ic">{_IC_SNIPPET}</span>'
    '<h3>One snippet to embed</h3>'
    '<p>Drop it into your site. No developer project, no new login.</p></div>'
    f'<div class="bmd-embed-card"><span class="ic">{_IC_STUDIES}</span>'
    '<h3>Your studies first</h3>'
    '<p>Shows your active studies first, then relevant recruiting trials nearby.</p>'
    '</div>'
    f'<div class="bmd-embed-card"><span class="ic">{_IC_INBOX}</span>'
    '<h3>Every search is a lead</h3>'
    '<p>Each search becomes an inquiry in your shared inbox, sorted and routed.</p>'
    '</div>'
    '</div>'
    '<div class="bmd-embed-cta">'
    # data-bmd-go is picked up by the injected pointerdown handler (Framer eats the
    # normal click inside #main, so we navigate on pointerdown at window-capture).
    f'<button type="button" class="btn-primary" data-bmd-go="{FINDER_URL}">'
    'Open the trial finder</button>'
    f'<a class="btn-ghost" href="{CAL}" target="_blank" rel="noopener">'
    'Talk to us about embedding</a>'
    '</div>'
    '</div>'
    '</section>'
)

# ---------------------------------------------------------------------------
# FAQ section (our own original block; injected just before the CTA).
# The template's FAQ is (a) stale Saify cold-email copy and (b) a Framer/JS
# accordion that is dead now that we strip the runtime. So we render our own
# using native <details>/<summary>, zero JS, works everywhere. Content is
# capability/scope facts only (compliance.mdc + product-identity.mdc): Bridget
# drafts + human sends, no-training + de-identified + audit-logged, free for
# physicians / flat license, and "runs the inbox, not eligibility".
FAQ_ITEMS = [
    ("What is BridgeMD?",
     "A shared inbox for clinical research sites. It pulls every patient inquiry "
     "from email, web forms, ads, social, ClinicalTrials.gov, and physician "
     "referrals into one place, organized by study, so your team can respond fast "
     "and never lose an inquiry."),
    ("How does Bridget, the AI assistant, work?",
     "Bridget summarizes a conversation thread and drafts a reply for your staff to "
     "review and send. It never sends on its own. A human always decides."),
    ("Do you train AI on our patient data?",
     "No. Inquiry and patient data is not used to train any model, it runs on a "
     "no-training endpoint, and it is de-identified. Every action is audit-logged."),
    ("Which channels does it connect?",
     "Email, website forms, Instagram, Facebook, Google Ads, ClinicalTrials.gov, and "
     "physician referrals, all sorted by study in one shared inbox."),
    ("Does it decide who is eligible?",
     "No. BridgeMD runs the inbox: it captures, sorts, and routes inquiries so your "
     "team can respond and move suitable people toward screening. It does not decide "
     "eligibility or run the trial."),
]

FAQ_HTML = (
    '<section id="bmd-faq"><div class="bmd-faq-wrap">'
    '<div class="bmd-faq-head"><span class="pill">FAQ</span>'
    '<h2>Questions, answered</h2>'
    '<p class="lead">What BridgeMD does, how Bridget works, and how your data is '
    'handled.</p></div>'
    + ''.join(
        f'<details{" open" if i == 0 else ""}><summary>{q}</summary>'
        f'<div class="a">{a}</div></details>'
        for i, (q, a) in enumerate(FAQ_ITEMS))
    + '</div></section>'
)

# ---------------------------------------------------------------------------
# Trust / compliance strip (our own block; injected right below the hero, where
# the template's "logoipsum" customer strip used to sit). This is an ADVERTISING
# claim about security posture, so wording is deliberate (compliance.mdc: ads
# must be truthful). What's shown:
#   - "HIPAA compliant / PHIPA / PIPEDA": a direct compliance claim. Only ship this
#     wording while it is TRUE (safeguards + signed BAAs in place). If that is not
#     verified, revert to "Built for HIPAA" (design-intent framing) -- ads must be
#     truthful (compliance.mdc / FTC).
#   - practice facts true by construction: de-identified, no-training AI,
#     audit-logged, encrypted in transit & at rest.
#   - "SOC 2 in progress": SOC 2 IS a third-party audit/report. Do NOT upgrade
#     this to a plain "SOC 2" badge unless a real report exists -- buyers verify
#     it in vendor review. Remove this chip entirely if the audit isn't underway.
_SHIELD = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
           'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
           '<path d="M12 3l7 3v5c0 4.5-3 7.6-7 9-4-1.4-7-4.5-7-9V6l7-3z"/>'
           '<path d="M9 12l2 2 4-4"/></svg>')
_GLOBE = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
          'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
          '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/>'
          '<path d="M12 3c2.5 2.5 3.8 5.7 3.8 9S14.5 18.5 12 21c-2.5-2.5-3.8-5.7'
          '-3.8-9S9.5 5.5 12 3z"/></svg>')
_EYE = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M3 3l18 18"/><path d="M10.6 6.1A9.6 9.6 0 0 1 12 6c5 0 9 4.5 9 6'
        'a12 12 0 0 1-2.2 3M6.3 6.3A12 12 0 0 0 3 12c0 1.5 4 6 9 6 1 0 2-.2 2.9-.5"/>'
        '<path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/></svg>')
_SPARK = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
          'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
          '<path d="M12 3l1.8 4.7L18.5 9l-4.7 1.8L12 15.5l-1.8-4.7L5.5 9l4.7-1.3z"/>'
          '<path d="M18.5 15l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/></svg>')
_DOC = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/>'
        '<path d="M14 3v5h5"/><path d="M9 15l2 2 4-4"/></svg>')
_LOCK = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
         'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
         '<rect x="4.5" y="10.5" width="15" height="9.5" rx="2"/>'
         '<path d="M8 10.5V7a4 4 0 0 1 8 0v3.5"/></svg>')

TRUST_ITEMS = [
    (_SHIELD, "HIPAA compliant"),
    (_GLOBE, "PHIPA / PIPEDA"),
    (_EYE, "Data de-identified"),
    (_SPARK, "No-training AI"),
    (_DOC, "Audit-logged"),
    (_LOCK, "Encrypted at rest &amp; in transit"),
]

TRUST_HTML = (
    '<section id="bmd-trust"><div class="bmd-trust-wrap">'
    '<p class="bmd-trust-head">Built for healthcare-grade privacy and security</p>'
    '<div class="bmd-trust-row">'
    + ''.join(
        f'<span class="bmd-trust-item"><span class="bmd-trust-ic">{icon}</span>'
        f'<b>{label}</b></span>'
        for icon, label in TRUST_ITEMS)
    + '</div></div></section>'
)

# ---------------------------------------------------------------------------
# "Every channel -> one inbox" section. Shows the real source logos flowing into a
# single shared inbox, so a site instantly gets that BridgeMD unifies intake across
# Instagram, Facebook, Gmail, Outlook, ClinicalTrials.gov, REDCap and web forms.
# Logos are the exact brand marks from templates/_icons.html (simpleicons/CC0),
# used nominatively to show interoperability. Only list a source we actually intend
# to support (product-identity.mdc: describe capability, not fiction).
# The mark colours are hardcoded here (the app's --mark-* CSS vars aren't loaded on
# the Framer landing).
_L_IG = ('<svg viewBox="0 0 24 24" aria-hidden="true"><defs><radialGradient id="bmdig"'
         ' cx="0.3" cy="1" r="1.1"><stop offset="0" stop-color="#FED576"/>'
         '<stop offset=".28" stop-color="#F47133"/><stop offset=".6" stop-color="#BC3081"/>'
         '<stop offset="1" stop-color="#4C63D2"/></radialGradient></defs>'
         '<rect width="24" height="24" rx="6" fill="url(#bmdig)"/>'
         '<rect x="5.5" y="5.5" width="13" height="13" rx="4" fill="none" stroke="#fff" stroke-width="1.6"/>'
         '<circle cx="12" cy="12" r="3.1" fill="none" stroke="#fff" stroke-width="1.6"/>'
         '<circle cx="16.1" cy="7.9" r="1.05" fill="#fff"/></svg>')
_L_FB = ('<svg viewBox="0 0 24 24" aria-hidden="true"><rect width="24" height="24" rx="6" fill="#0866FF"/>'
         '<path fill="#fff" d="M15.35 12.5l.42-2.63h-2.52V8.16c0-.72.35-1.42 1.48-1.42h1.15V4.5s-1.04-.18-2.04-.18'
         'c-2.08 0-3.44 1.26-3.44 3.54v2.01H8.05v2.63h2.35V19h2.85v-6.5z"/></svg>')
# Official Gmail mark: the multicolour "M" envelope on a clean white app tile (the
# universally recognised icon), drawn ourselves so we control it fully (no apistemic
# hotlink, no attribution needed, no mismatched art). 48x48 canonical paths scaled
# into the 24x24 tile with padding.
_L_GMAIL = ('<svg viewBox="0 0 24 24" aria-hidden="true">'
            '<rect width="24" height="24" rx="6" fill="#fff"/>'
            '<g transform="translate(3.4 4.6) scale(0.358)">'
            '<path fill="#4caf50" d="M45,16.2l-5,2.75l-5,4.75L35,40h7c1.657,0,3-1.343,3-3V16.2z"/>'
            '<path fill="#1e88e5" d="M3,16.2l3.614,1.71L13,23.7V40H6c-1.657,0-3-1.343-3-3V16.2z"/>'
            '<polygon fill="#e53935" points="35,11.2 24,19.45 13,11.2 12,17 13,23.7 24,31.95 35,23.7 36,17"/>'
            '<path fill="#c62828" d="M3,12.298V16.2l10,7.5V11.2L9.876,8.859C9.132,8.301,8.228,8,7.298,8'
            'C4.924,8,3,9.924,3,12.298z"/>'
            '<path fill="#fbc02d" d="M45,12.298V16.2l-10,7.5V11.2l3.124-2.341C38.868,8.301,39.772,8,40.702,8'
            'C43.076,8,45,9.924,45,12.298z"/>'
            '</g></svg>')
# Official-style Outlook mark: the light-blue envelope with the dark-blue "O" badge in
# front, on a clean white app tile (matches the recognisable icon). Drawn ourselves so
# there is no apistemic hotlink (outlook.com there returns the generic Microsoft mark).
_L_OUTLOOK = ('<svg viewBox="0 0 24 24" aria-hidden="true">'
              '<rect width="24" height="24" rx="6" fill="#fff"/>'
              '<rect x="11.4" y="6.2" width="8.2" height="3.6" rx="0.6" fill="#0f6cbd"/>'
              '<rect x="12.2" y="6.9" width="2.1" height="2.2" fill="#1b8ade"/>'
              '<rect x="15" y="6.9" width="2.1" height="2.2" fill="#3ba1e3"/>'
              '<path fill="#33aae6" d="M10.8 10h8.8v6.9a1 1 0 0 1-1 1h-6.8a1 1 0 0 1-1-1z"/>'
              '<path fill="#1b8ade" d="M10.8 10h8.8v.6l-4.4 3-4.4-3z"/>'
              '<rect x="3.8" y="8.7" width="8.7" height="8.7" rx="1.4" fill="#0364b8"/>'
              '<ellipse cx="8.15" cy="13.05" rx="2.35" ry="2.85" fill="none" stroke="#fff" '
              'stroke-width="1.5"/></svg>')
_L_CTGOV = ('<svg viewBox="0 0 24 24" aria-hidden="true"><rect width="24" height="24" rx="6" fill="#20558A"/>'
            '<path fill="none" stroke="#fff" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" '
            'd="M4 13h3l2-4.5 2.6 8 2-10 1.8 6.5H20"/></svg>')
_L_REDCAP = ('<svg viewBox="0 0 24 24" aria-hidden="true"><rect width="24" height="24" rx="6" fill="#C00000"/>'
             '<rect x="6.4" y="13" width="2.4" height="4.5" rx=".5" fill="#fff"/>'
             '<rect x="10.8" y="10" width="2.4" height="7.5" rx=".5" fill="#fff"/>'
             '<rect x="15.2" y="7" width="2.4" height="10.5" rx=".5" fill="#fff"/></svg>')
_L_FORMS = ('<svg viewBox="0 0 24 24" aria-hidden="true"><rect width="24" height="24" rx="6" fill="#1257b0"/>'
            '<g stroke="#fff" stroke-width="1.6" stroke-linecap="round"><path d="M9 8.5h7"/>'
            '<path d="M9 12h7"/><path d="M9 15.5h4.5"/></g>'
            '<circle cx="6.5" cy="8.5" r=".95" fill="#fff"/><circle cx="6.5" cy="12" r=".95" fill="#fff"/>'
            '<circle cx="6.5" cy="15.5" r=".95" fill="#fff"/></svg>')
_L_MARK = ('<svg viewBox="0 0 512 512" fill-rule="evenodd" aria-hidden="true">'
           '<path fill="#7fb3e6" d="M142 96H254A46 46 0 0 1 300 142V254A46 46 0 0 1 254 300H142A46 46 0 0 1 96 254'
           'V142A46 46 0 0 1 142 96ZM258 212H300V254A46 46 0 0 1 254 300H212V258A46 46 0 0 1 258 212Z"/>'
           '<path fill="#1257b0" d="M258 212H370A46 46 0 0 1 416 258V370A46 46 0 0 1 370 416H258A46 46 0 0 1 212 370'
           'V258A46 46 0 0 1 258 212ZM258 212H300V254A46 46 0 0 1 254 300H212V258A46 46 0 0 1 258 212Z"/></svg>')

_L_REDDIT = ('<svg viewBox="0 0 24 24" aria-hidden="true"><rect width="24" height="24" rx="6" fill="#FF4500"/>'
             '<circle cx="8.8" cy="13" r="1.15" fill="#fff"/><circle cx="15.2" cy="13" r="1.15" fill="#fff"/>'
             '<path d="M9 15.7c.9.75 2 1.05 3 1.05s2.1-.3 3-1.05" stroke="#fff" stroke-width="1.1" '
             'fill="none" stroke-linecap="round"/><circle cx="17.2" cy="8.4" r="1.4" fill="#fff"/>'
             '<circle cx="12.4" cy="7" r="1" fill="#fff"/>'
             '<path d="M12.4 7l4.2 1.1" stroke="#fff" stroke-width="1" fill="none" stroke-linecap="round"/></svg>')

# (svg fallback, label, brand colour, apistemic domain). The colour is kept for
# data completeness; `domain` (when set) pulls the real brand logo from the logo
# API and layers it over the drawn fallback. Web forms / CT.gov / REDCap have no
# clean consumer logo, so they keep their drawn mark (domain left blank).
SOURCE_ITEMS = [
    (_L_IG, "Instagram", "#E1306C", "instagram.com"),
    (_L_FB, "Facebook", "#0866FF", "facebook.com"),
    (_L_REDDIT, "Reddit", "#FF4500", "reddit.com"),
    # Gmail uses our own clean drawn mark (apistemic returned a mismatched/ugly art),
    # so no domain -> no hotlink for it.
    (_L_GMAIL, "Gmail", "#EA4335", ""),
    # outlook.com resolves to the generic Microsoft 4-square mark (mismatches the
    # "Outlook" label), so keep the drawn Outlook envelope instead.
    (_L_OUTLOOK, "Outlook", "#0F6CBD", ""),
    (_L_CTGOV, "ClinicalTrials.gov", "#20558A", ""),
    (_L_REDCAP, "REDCap", "#C00000", ""),
    (_L_FORMS, "Web forms", "#1257b0", ""),
]


def _place_svg(svg, cx, cy, size):
    """Re-root a 24x24 brand-logo <svg> string at (cx,cy) with the given box size, so it
    can be nested inside the master beam-network SVG at fixed coords."""
    return svg.replace(
        '<svg viewBox="0 0 24 24" aria-hidden="true">',
        f'<svg x="{cx - size / 2:.0f}" y="{cy - size / 2:.0f}" width="{size}" '
        f'height="{size}" viewBox="0 0 24 24">', 1)


def _node_logo(svg, domain, cx, cy, size):
    """Small inline logo (used in the centre inbox rows): the drawn brand SVG as a
    fallback, with the real apistemic logo layered on top when we have a domain. If the
    remote image fails to load, the drawn mark underneath still shows."""
    out = _place_svg(svg, cx, cy, size)
    if domain:
        out += (f'<image x="{cx - size / 2:.0f}" y="{cy - size / 2:.0f}" width="{size}" '
                f'height="{size}" href="{LOGO_API}{domain}" '
                f'preserveAspectRatio="xMidYMid meet"/>')
    return out


def _node_tile(svg, domain, cx, cy, s, uid):
    """One channel node as a uniform rounded-square app tile: same size, same corner
    radius, same soft shadow for EVERY channel so real logos and drawn marks read as one
    consistent set (no white-circle-behind-a-coloured-tile double container). The drawn
    SVG sits underneath as a fallback; the real apistemic logo is layered on top, clipped
    to the tile's rounded corners and slice-filled so square-cornered logos still round
    off cleanly and fill edge to edge."""
    half = s / 2.0
    x0, y0 = cx - half, cy - half
    rx = round(s * 0.26)  # ~13 at s=50 -> matches the drawn tiles' scaled 6/24 radius
    out = f'<g filter="url(#bmdtile)">'
    out += _place_svg(svg, cx, cy, s)  # fallback under
    if domain:
        cid = f"bmdclip{uid}"
        out += (f'<clipPath id="{cid}"><rect x="{x0:.0f}" y="{y0:.0f}" width="{s:.0f}" '
                f'height="{s:.0f}" rx="{rx}"/></clipPath>'
                f'<image x="{x0:.0f}" y="{y0:.0f}" width="{s:.0f}" height="{s:.0f}" '
                f'href="{LOGO_API}{domain}" preserveAspectRatio="xMidYMid slice" '
                f'clip-path="url(#{cid})"/>')
    out += '</g>'
    return out


def _build_beams():
    """Animated beam network: the BridgeMD inbox sits in the middle, the source logos
    flank it left/right, and a bright streak glides along each curved connector into the
    centre. The streak is a single dash swept with SMIL stroke-dashoffset over a
    normalized pathLength (=100), so it follows the curve EXACTLY and at uniform speed -
    no horizontal-gradient window that only lights where a vertical band crosses the
    path (the old janky look). One self-contained SVG, no JS."""
    w, h, cx, cy = 920, 480, 460, 240
    lx, rx, tile = 118, 802, 52
    ys = [72, 184, 296, 408]
    left = SOURCE_ITEMS[:4]
    right = SOURCE_ITEMS[4:]
    # centre inbox-card geometry (enlarged so it reads as the clear focal point). The
    # card is centred on (cx,cy); every header/row coord below is derived from these so
    # resizing the card keeps everything aligned. Beams dock onto its left/right edges.
    cardw, cardh = 320, 214
    cardx, cardy = cx - cardw // 2, cy - cardh // 2
    c_l, c_r = cardx, cardx + cardw

    paths, comets, nodes = [], [], []
    beam_col = "#3b6ef2"   # one calm brand-blue for every comet (no rainbow clutter)
    dur = 2.6              # seconds for a dot to travel source -> inbox
    n_total = 8            # spread the 8 comets evenly across `dur` for a steady flow
    tile_s = 50            # uniform node tile size

    # Comet = a soft glow dot + a crisp head + a few lagging tail dots, all riding the
    # SAME path via animateMotion so they follow the curve EXACTLY (no stroke-dash
    # fragments). (radius, opacity, time-lag behind the head). Tighter lags read as a
    # continuous streak rather than separate dots.
    comet_parts = [(7.0, 0.28, 0.00, True),   # blurred glow halo
                   (3.6, 1.00, 0.00, False),  # bright head
                   (2.9, 0.55, 0.05, False),  # tail
                   (2.2, 0.32, 0.10, False),
                   (1.5, 0.16, 0.15, False)]

    def add(items, x, is_left, base_idx):
        for i, ((svg, name, color, domain), y) in enumerate(zip(items, ys)):
            gi = base_idx + i                        # global comet index (0..7)
            pid = f"bmp{'l' if is_left else 'r'}{i}"
            if is_left:
                sx = x + tile / 2 - 6
                d = f"M{sx:.0f},{y} C{c_l - 30},{y} {c_l - 8},{cy} {c_l},{cy}"
            else:
                sx = x - tile / 2 + 6
                d = f"M{sx:.0f},{y} C{c_r + 30},{y} {c_r + 8},{cy} {c_r},{cy}"
            # Faint static lane the comet rides along. Inline presentation attrs (not CSS
            # classes): hydration can drop classes on injected inline-SVG children.
            paths.append(
                f'<path id="{pid}" d="{d}" fill="none" stroke="#e6ecf7" '
                'stroke-width="1.6"/>')
            # A comet <circle> has no cx/cy (animateMotion translates it from the SVG
            # origin), so a POSITIVE begin delay would leave it parked at (0,0) until it
            # starts. Use a NEGATIVE begin instead: every comet is already mid-flight at
            # the first frame, staggered by gi*step, and never sits at the corner.
            beg = gi * (dur / n_total)               # stagger magnitude
            for r, op, lag, blur in comet_parts:
                flt = ' filter="url(#bmdglow)"' if blur else ''
                comets.append(
                    f'<circle r="{r}" fill="{beam_col}" opacity="{op}"{flt}>'
                    f'<animateMotion dur="{dur}s" begin="{-(dur + beg + lag):.2f}s" '
                    f'calcMode="linear" repeatCount="indefinite" rotate="auto">'
                    f'<mpath href="#{pid}" xlink:href="#{pid}"/></animateMotion></circle>')
            # Uniform rounded-square app tile (real logo, drawn fallback under).
            nodes.append(
                f'{_node_tile(svg, domain, x, y, tile_s, pid)}'
                f'<text class="bmd-bn-lbl" x="{x}" y="{y + tile_s / 2 + 22:.0f}" '
                f'text-anchor="middle">{name}</text>')

    add(left, lx, True, 0)
    add(right, rx, False, 4)

    glow = (
        f'<circle cx="{cx}" cy="{cy}" r="92" fill="#1257b0" opacity=".07">'
        '<animate attributeName="r" values="86;104;86" dur="2.6s" repeatCount="indefinite"/>'
        '<animate attributeName="opacity" values=".1;.03;.1" dur="2.6s" '
        'repeatCount="indefinite"/></circle>')

    # Centre = a small but real-looking inbox card: header (icon + "Shared inbox" +
    # count) and a few message rows, each with its source logo, so the "one inbox"
    # target is unmistakable. All text/shape styling is inline (hydration-proof).
    rows = [
        (_L_IG, "Instagram", "&ldquo;how do I join?&rdquo;", True, "instagram.com"),
        (_L_GMAIL, "Gmail", "&ldquo;evening appt?&rdquo;", False, ""),
        (_L_CTGOV, "CT.gov", "new referral", False, ""),
    ]
    ff = 'font-family="Figtree,system-ui,sans-serif"'
    # All header/row coords are derived from cardx/cardy/cardw so the card can be
    # resized in one place (above) without anything drifting out of alignment.
    hdr_div = cardy + 44             # divider under the header
    rows_svg = ""
    ry0, rh = cardy + 48, 52
    for j, (svg, nm, pv, unread, domain) in enumerate(rows):
        ry = ry0 + j * rh
        if j == 0:
            rows_svg += (f'<rect x="{cardx}" y="{ry}" width="{cardw}" height="{rh}" '
                         'fill="rgba(18,87,176,.06)"/>')
        else:
            rows_svg += (f'<line x1="{cardx + 16}" y1="{ry}" x2="{cardx + cardw - 16}" '
                         f'y2="{ry}" stroke="#f3f6fb" stroke-width="1"/>')
        rows_svg += _node_logo(svg, domain, cardx + 26, ry + rh / 2, 28)
        rows_svg += (f'<text x="{cardx + 54}" y="{ry + 21}" {ff} font-size="16" '
                     f'font-weight="700" fill="#1f2a44">{nm}</text>')
        rows_svg += (f'<text x="{cardx + 54}" y="{ry + 38}" {ff} font-size="13" '
                     f'font-weight="500" fill="#7b8798">{pv}</text>')
        if unread:
            rows_svg += (f'<circle cx="{cardx + cardw - 18}" cy="{ry + 18}" r="4.5" '
                         'fill="#1257b0"/>')
    inbox_icon = (
        f'<svg x="{cardx + 16:.0f}" y="{cardy + 14:.0f}" width="22" height="22" '
        'viewBox="0 0 24 24" fill="none" stroke="#1257b0" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M3.5 12.5V6a2 2 0 0 1 2-2h13a2 2 0 0 1 2 2v6.5"/>'
        '<path d="M3.5 12.5H8l1.5 2.5h5L16 12.5h4.5V18a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>'
        '</svg>')
    centre = (
        f'<g filter="url(#bmdsh)"><rect x="{cardx}" y="{cardy}" width="{cardw}" '
        f'height="{cardh}" rx="18" fill="#fff" stroke="rgba(18,87,176,.16)"/></g>'
        + inbox_icon
        + f'<text x="{cardx + 46:.0f}" y="{cardy + 30:.0f}" {ff} font-size="17" '
        'font-weight="800" fill="#12122b">Shared inbox</text>'
        + f'<rect x="{cardx + cardw - 46:.0f}" y="{cardy + 15:.0f}" width="34" '
        'height="20" rx="10" fill="#eef2f8"/>'
        + f'<text x="{cardx + cardw - 29:.0f}" y="{cardy + 29:.0f}" {ff} font-size="12" '
        'font-weight="700" fill="#516079" text-anchor="middle">12</text>'
        + f'<line x1="{cardx}" y1="{hdr_div}" x2="{cardx + cardw}" y2="{hdr_div}" '
        'stroke="#eef2f8" stroke-width="1"/>'
        + rows_svg)
    defs = (
        '<defs>'
        '<filter id="bmdsh" x="-40%" y="-40%" width="180%" height="180%">'
        '<feDropShadow dx="0" dy="3" stdDeviation="5" flood-color="#0f1a3a" '
        'flood-opacity="0.12"/></filter>'
        '<filter id="bmdtile" x="-45%" y="-45%" width="190%" height="190%">'
        '<feDropShadow dx="0" dy="4" stdDeviation="6" flood-color="#1a2b52" '
        'flood-opacity="0.16"/></filter>'
        '<filter id="bmdglow" x="-60%" y="-60%" width="220%" height="220%">'
        '<feGaussianBlur stdDeviation="3"/></filter>'
        '</defs>')
    # Order: faint lanes, then comets riding them, then the tiles on top (so a comet
    # slides UNDER the node it docks into), then the centre inbox card.
    return (
        f'<div class="bmd-bn"><svg viewBox="0 0 {w} {h}" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" aria-hidden="true">'
        f'{defs}{glow}{"".join(paths)}{"".join(comets)}'
        f'{"".join(nodes)}{centre}</svg></div>')


SOURCES_HTML = (
    '<section id="bmd-sources"><div class="bmd-src-wrap">'
    '<p class="bmd-src-eyebrow">How it works</p>'
    '<h2 class="bmd-src-title">Every channel flows into one inbox</h2>'
    '<p class="bmd-src-sub">Instagram, Facebook, Reddit, Gmail, Outlook, '
    'ClinicalTrials.gov, REDCap, and web forms all stream into one shared inbox, sorted '
    'by study. The replies your team sends go right back out to the same '
    'channel.</p>'
    + _build_beams() +
    '<p class="bmd-src-note">Don&rsquo;t see your channel? '
    '<a href="' + CAL + '" target="_blank" rel="noopener">Contact us</a> '
    'and we&rsquo;ll look at adding it.</p>'
    '</div></section>'
)

# ---------------------------------------------------------------------------
# Footer. The Framer template footer is a huge block of dead Saify links (404,
# Blog Details, Review, Careers, Request, DiverseKit credit, social icons) with an
# oversized empty top area. We hide it (kill-list) and render this compact
# BridgeMD footer instead: real links only, correct copy, no socials/404.
FOOTER_LINKS = [
    ("/find-trial", "Trial finder"),
    ("/for-sites", "For sites"),
    (CAL, "Contact"),
    ("/privacy", "Privacy"),
    ("/terms", "Terms"),
]

FOOTER_HTML = (
    '<section id="bmd-footer"><div class="bmd-footer-wrap">'
    '<div class="bmd-footer-top">'
    '<div class="bmd-footer-brand"><span class="name">BridgeMD</span>'
    '<p>One shared inbox for clinical research teams to manage patient '
    'recruitment.</p></div>'
    '<nav class="bmd-footer-links">'
    + ''.join(
        f'<a href="{href}"'
        + (' target="_blank" rel="noopener"' if href.startswith('http') else '')
        + f'>{label}</a>'
        for href, label in FOOTER_LINKS)
    + '</nav></div>'
    '<div class="bmd-footer-bottom">'
    '<span>&copy; 2026 BridgeMD. All rights reserved.</span></div>'
    '</div></section>'
)

# Old template strings -> new copy. Applied to the SSR HTML (attributes + eyebrow)
# AND the JS bundle, since Framer's hydration re-renders text from the module data
# and would otherwise revert our SSR edits (same trap as the logo).
COPY = [
    # <title> + og/twitter title and description. The generic Saify -> BridgeMD swap
    # only fixes the name, leaving the template's cold-email pitch, so rewrite the
    # whole strings. Each appears 3x (base + og + twitter); one replace covers all.
    ("Saify | AI-Powered Cold Email Outreach &amp; Marketing",
     "BridgeMD | One shared inbox for clinical research sites"),
    ("Saify is the ultimate AI-driven email marketing solution designed to boost "
     "outreach, automate campaigns, and increase conversions. Start scaling your "
     "email strategy today!",
     "BridgeMD puts every message about your studies in one shared inbox. Email, "
     "Instagram, Facebook, Google Ads, and referrals arrive sorted and routed to "
     "the right coordinator."),
    # hero
    ("Unlimited Leads, Unlimited Outreach, Unlimited Growth", EYEBROW),
    ("Attract and Win Your Perfect Customers", f"{H1_L1} {H1_L2}"),
    ("The first unlimited B2B leads and AI outreach platform with 170M+ verified "
     "contacts to reach anyone, anytime.", SUBHEAD),
    # The JS bundle renders the hero H1/subhead as separate styled-text runs (same
    # reason the SSR HTML needed the dedicated _fix_split treatment below), so the
    # two whole-sentence replacements above never match inside the .mjs chunks.
    # Framer's hydration then re-renders these fragments straight from the old
    # template text, overwriting the correct SSR copy a beat after paint (the
    # "flash then reverts to the old headline" bug). Patch each run explicitly.
    ("Attract and Win Your", H1_L1),
    ("Perfect Customers", H1_L2),
    ("The first unlimited B2B leads and AI outreach platform with", SUBHEAD),
    ("170M+ verified contacts to reach anyone, anytime.", ""),
    # features section
    ("Designed to Fuel Unlimited Growth",
     "Everything your study team needs in one place"),
    ("We provide tools to scale your business with unlimited leads, AI-driven "
     "outreach, and everything needed to grow faster.",
     "One inbox for every recruitment channel, with pre-screening, routing, and a "
     "shared workspace, so your team can spend its time enrolling patients."),
    ("The First Unlimited B2B Leads Database", "See every applicant, pre-screened"),
    ("Access unlimited leads and connect with over 170 million verified prospects "
     "to reach your ideal customers effortlessly.",
     "Every person who contacts your site is scored and sorted, so coordinators "
     "know who to call first."),
    ("Stay Spam-Free with Warmup", "One shared workspace for your team"),
    ("Activate with one click to keep your emails out of spam and ensure reliable "
     "inbox delivery.",
     "Everyone works from the same inbox. When a teammate is away, their messages "
     "route to someone else so nothing waits."),
    ("Limitless Emails &amp; Outreach", "Route each message to the right person"),
    ("Limitless Emails & Outreach", "Route each message to the right person"),
    ("Send unlimited emails, scale fast, and manage campaigns effortlessly.",
     "Set a rule once and new inquiries land with the right coordinator "
     "automatically, by channel or by study."),
    ("AI-Powered Emails that Close Deals at Scale",
     "Connect your channels in minutes"),
    ("Access 170M+ verified leads, craft AI-powered emails effortlessly, and boost "
     "engagement with personalized, impactful communication.",
     "Link email, Instagram, Facebook, Google Ads, and referrals, then pick the "
     "intake each study uses."),
    # CTA
    ("Get Started with Saify Today",
     "Bring your patient recruitment into one inbox"),
    ("Boost your outreach with Saify\u2019s unlimited emails, AI tools, and easy "
     "campaign management!",
     "See how BridgeMD brings every recruitment channel into one shared inbox for "
     "your study team."),
    # footer tagline
    ("AI-powered tools for business growth, seamless scaling, and proven results.",
     "One shared inbox for clinical research teams to manage patient recruitment."),
    # nav labels (SSR). >word< keeps the match scoped to the visible link text.
    # Mapped by ORIGINAL Saify label (Home/About/Solution/Pricing) so there is no
    # cascade. Final nav (L->R): Why us, Product, Trial finder, Embed. hrefs are
    # wired separately in _wire_nav() below (all four ship pointing at /site-preview).
    (">Home<", ">Why us<"),
    (">About<", ">Product<"),
    (">Solution<", ">Trial finder<"),
    (">Pricing<", ">Embed<"),
    # nav labels (JS bundle). Framer bakes the SAME text into its component bundle as
    # backtick string literals (children:`About`, defaultValue:`Solution`, ...). The
    # >word< entries above never match those, so on hydrate Framer re-renders the nav
    # from the bundle and reverts our labels -> the header flicker. Scope to the
    # backtick literal so we only touch the exact nav strings, nothing else.
    ("`Home`", "`Why us`"),
    ("`About`", "`Product`"),
    ("`Solution`", "`Trial finder`"),
    ("`Pricing`", "`Embed`"),
]

# Real BridgeMD product screenshots (captured from the running demo) that replace the
# template's fabricated Saify mockups. Keyed by the underlying Framer asset hash so we
# overwrite EVERY md5-prefixed + srcset variant of each image. Interim shots; swap for
# final polished captures later.
PRODUCT = {
    '4ufOW7NDRif7jTF9nwJtcqOsxmw':  'product_shots/hero_inbox.png',    # hero dashboard
    'H72abFS6bS7RIKr4KJF741jYZQ':   'product_shots/card_applicants.png',
    '8jSIXe56zkJt9YNeya1XZ4nUt1o':  'product_shots/card_team.png',
    'F8RS7RlRKT2Wr0njw8G6qQSMV0':   'product_shots/card_routing.png',
    'iM0MOfO7pUZHZ5Fk8wMUlAu4Lw':   'product_shots/card_settings.png',
}

# BridgeMD wordmark, drawn at the template logo's exact 104x40 box so it drops into
# the nav/footer <img> (object-fit:cover) with no crop or squish. Glyph = the app
# `mark` (two rounded squares, mark-b light + mark-a dark), text forced to a fixed
# width via textLength so it can't overflow regardless of the fallback font.
BRAND_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="104" height="40" '
    'viewBox="0 0 104 40" fill="none">'
    '<g transform="translate(1 6) scale(0.0547)">'
    '<path fill="#7fb8ee" d="M142 96H254A46 46 0 0 1 300 142V254A46 46 0 0 1 254 '
    '300H142A46 46 0 0 1 96 254V142A46 46 0 0 1 142 96ZM258 212H300V254A46 46 0 0 1 '
    '254 300H212V258A46 46 0 0 1 258 212Z"/>'
    '<path fill="#1257b0" d="M258 212H370A46 46 0 0 1 416 258V370A46 46 0 0 1 370 '
    '416H258A46 46 0 0 1 212 370V258A46 46 0 0 1 258 212ZM258 212H300V254A46 46 0 0 1 '
    '254 300H212V258A46 46 0 0 1 258 212Z"/></g>'
    '<text x="35" y="26" textLength="66" lengthAdjust="spacingAndGlyphs" '
    'font-family="system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif" '
    'font-size="17" font-weight="800" letter-spacing="-0.4" fill="#12122b">'
    'BridgeMD</text></svg>'
)


# Each color is listed as hex + spaced rgb + unspaced rgb so we catch every form
# the template uses (index.html tokens, inline rgb(), and Framer's JS/CSS chunks
# which inject button gradients / ambient washes at runtime).
def _cm(src_hex, src_rgb, dst_hex, dst_rgb):
    sr = ', '.join(src_rgb); ur = ','.join(src_rgb)
    dr = ', '.join(dst_rgb); du = ','.join(dst_rgb)
    return [('#' + src_hex, '#' + dst_hex), ('#' + src_hex.upper(), '#' + dst_hex),
            (sr, dr), (ur, du)]


# Saify ships a violet/indigo palette: the accent (#3e3edf) plus the button-gradient
# light stop (#8585f4) and two lighter violet tints. Map the whole family to the
# current BridgeMD patient blue.
BLUE = (
    _cm('3e3edf', ('62', '62', '223'),    '1257b0', ('18', '87', '176'))     # accent
    + _cm('8585f4', ('133', '133', '244'), '7fb8ee', ('127', '184', '238'))  # grad light stop
    + _cm('ab9df2', ('171', '157', '242'), '7aaae0', ('122', '170', '224'))  # light violet
    + _cm('cacaf6', ('202', '202', '246'), 'd1e3f6', ('209', '227', '246'))  # pale violet
)


def _recolor_rasters(adir, hue_deg):
    # Framer bakes the hero ombre + dashboard mockup as raster (JPG/PNG); CSS color
    # maps can't touch them. Hue-shift the cyan..violet band to the brand hue so the
    # baked purple ombre matches the blue (or teal) theme.
    from PIL import Image
    tgt = int(round(hue_deg / 360 * 255))
    lut = [(tgt if 138 <= i <= 214 else i) for i in range(256)]
    n = 0
    for f in os.listdir(adir):
        if not f.lower().endswith(('.jpg', '.jpeg', '.png')):
            continue
        fp = os.path.join(adir, f)
        try:
            im = Image.open(fp)
        except OSError:
            continue
        alpha = None
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            alpha = im.getchannel("A")
        rgb = im.convert("RGB")
        H, S, V = rgb.convert("HSV").split()
        out = Image.merge("HSV", (H.point(lut), S, V)).convert("RGB")
        if alpha is not None:
            out = out.convert("RGBA")
            out.putalpha(alpha)
        if fp.lower().endswith('.png'):
            out.save(fp)
        else:
            out.save(fp, quality=92)
        n += 1
    return n


# Cache dir for the fetched brand logos, next to this script so it's committed and a
# rebuild doesn't need to re-hit the API (apistemic asks for <=1 rps server-side).
LOGO_CACHE = "brand_logos"


def _localize_brand_logos(h, adir, asset_prefix):
    """Bake the real brand logos in locally instead of hotlinking apistemic at runtime.

    An inline-SVG <image> pointing at a cross-origin URL renders as a broken-image
    placeholder in Chrome (it even paints over the drawn fallback), so we fetch each
    referenced logo ONCE (cached next to the script; apistemic wants <=1 rps + a
    contact User-Agent), drop it into assets/, and repoint the HTML at the local copy.
    If a fetch fails, the whole <image> tag is stripped so the hand-drawn fallback
    that sits underneath shows through - never a broken node."""
    os.makedirs(LOGO_CACHE, exist_ok=True)
    domains = sorted(set(re.findall(re.escape(LOGO_API) + r'([a-z0-9.\-]+)', h)))
    fetched = 0
    for i, dom in enumerate(domains):
        slug = re.sub(r'[^a-z0-9]', '-', dom.split('.')[0])
        cache_fp = os.path.join(LOGO_CACHE, f"{slug}.webp")
        if not os.path.exists(cache_fp):
            try:
                if i:
                    time.sleep(1.1)  # be polite: <=1 rps
                req = urllib.request.Request(
                    LOGO_API + dom,
                    headers={"User-Agent": "BridgeMD landing build "
                             "(+https://bridgemd.ca; contact harshil@bridgemd.ca)"})
                data = urllib.request.urlopen(req, timeout=15).read()
                if len(data) < 200:
                    raise ValueError("suspiciously small logo payload")
                with open(cache_fp, "wb") as fh:
                    fh.write(data)
            except Exception as e:  # noqa: BLE001 - any failure -> drop to fallback
                print(f"  [logo] {dom} fetch failed ({e}); using drawn fallback")
                h = re.sub(r'<image\b[^>]*href="' + re.escape(LOGO_API + dom)
                           + r'"[^>]*/>', '', h)
                continue
        out_name = f"bmd-logo-{slug}.webp"
        shutil.copyfile(cache_fp, os.path.join(adir, out_name))
        h = h.replace(LOGO_API + dom, asset_prefix + out_name)
        fetched += 1
    print(f"  [logo] localized {fetched}/{len(domains)} brand logos")
    return h


# ---- Shared pill header ------------------------------------------------------
# The site-side trial finder is a Jinja page that renders the `.pill-nav` component
# (templates/_nav.html + the PILL-NAV block in web/static/style.css). This static
# Framer page can't import a Jinja macro, so at build time we pull the SAME CSS out
# of style.css verbatim, inline it here with equivalent markup, and hide Framer's
# own nav. Net effect: the header is authored in ONE place (style.css) and a rebuild
# keeps `/` and `/find-trial` visually identical -- edit the component once, both update.
_STYLE_CSS = os.path.join(os.path.dirname(__file__), "..", "web", "static", "style.css")

_PILL_MARK = (
    '<svg class="brand-glyph" xmlns="http://www.w3.org/2000/svg" width="28" height="28" '
    'viewBox="96 96 320 320" fill="none">'
    '<path fill="#7fb8ee" d="M142 96H254A46 46 0 0 1 300 142V254A46 46 0 0 1 254 300H142A46 '
    '46 0 0 1 96 254V142A46 46 0 0 1 142 96ZM258 212H300V254A46 46 0 0 1 254 300H212V258A46 '
    '46 0 0 1 258 212Z"/>'
    '<path fill="#1257b0" d="M258 212H370A46 46 0 0 1 416 258V370A46 46 0 0 1 370 416H258A46 '
    '46 0 0 1 212 370V258A46 46 0 0 1 258 212ZM258 212H300V254A46 46 0 0 1 254 300H212V258A46 '
    '46 0 0 1 258 212Z"/></svg>')

_PILL_ARROW = (
    '<svg class="ico" xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
    'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/>'
    '<path d="m12 5 7 7-7 7"/></svg>')

_PILL_MENU = (
    '<svg class="ico" xmlns="http://www.w3.org/2000/svg" width="20" height="20" '
    'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round"><line x1="3" y1="6" x2="21" y2="6"/>'
    '<line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>')


def _pill_css():
    """The `.pill-nav` rules pulled verbatim from web/static/style.css (single source)."""
    try:
        css = open(_STYLE_CSS, encoding="utf-8").read()
    except OSError:
        return ""
    m = re.search(r'/\* PILL-NAV:START \*/(.*?)/\* PILL-NAV:END \*/', css, re.S)
    return m.group(1).strip() if m else ""


def _inject_pill_nav(h):
    css = _pill_css()
    if not css or 'id="bmd-pill-nav"' in h:
        return h
    links = [("How it works", "#features"),
             ("Trial finder", FINDER_URL), ("FAQ", "#bmd-faq")]
    center = ''.join('<a href="%s">%s</a>' % (href, lbl) for lbl, href in links)
    header = (
        '<header class="pill-nav" id="pillNav">'
        '<a class="brand" href="/">' + _PILL_MARK + 'BridgeMD</a>'
        '<nav class="pill-nav-center" aria-label="Primary">' + center + '</nav>'
        '<div class="pill-nav-auth">'
        '<a class="pill-nav-cta" href="' + CAL + '" target="_blank" rel="noopener">'
        'Request a demo' + _PILL_ARROW + '</a>'
        '<button type="button" class="pill-nav-burger" id="pillBurger" aria-expanded="false" '
        'aria-controls="pillSheet" aria-label="Open menu">' + _PILL_MENU + '</button>'
        '</div>'
        '<div class="pill-sheet" id="pillSheet" hidden>' + center +
        '<a class="pill-sheet-cta" href="' + CAL + '" target="_blank" rel="noopener">'
        'Request a demo</a></div>'
        '</header>')
    # Overlay as a fixed pill (so the hero keeps the top padding it already had for
    # Framer's own fixed nav) and hide Framer's nav + its fixed positioner entirely.
    override = ('.pill-nav{position:fixed;top:max(10px,env(safe-area-inset-top));'
                'left:12px;right:12px;margin:0 auto;z-index:1000}'
                '[data-framer-name="NavBar"],.framer-bfsnv8-container{display:none!important}')
    style = '<style id="bmd-pill-nav">' + css + override + '</style>'
    js = ('<script>(function(){var n=document.getElementById("pillNav");if(!n)return;'
          'function s(){n.classList.toggle("is-condensed",window.scrollY>80);}s();'
          'window.addEventListener("scroll",s,{passive:true});'
          'var b=document.getElementById("pillBurger"),sh=document.getElementById("pillSheet");'
          'if(b&&sh){var set=function(o){sh.hidden=!o;n.classList.toggle("is-menu",o);'
          'b.setAttribute("aria-expanded",o?"true":"false");};'
          'b.addEventListener("click",function(){set(sh.hidden);});'
          'sh.addEventListener("click",function(e){if(e.target.closest("a"))set(false);});'
          'document.addEventListener("keydown",function(e){if(e.key==="Escape")set(false);});'
          'window.addEventListener("resize",function(){if(window.innerWidth>820)set(false);});}'
          '})();</script>')
    h = h.replace('</head>', style + '</head>', 1)
    h = h.replace('data-framer-generated-page="">',
                  'data-framer-generated-page="">' + header, 1)
    h = h.replace('</body>', js + '</body>', 1)
    return h


def build(dst, colormap, asset_prefix, raster_hue=None, accent="#1257b0"):
    # fresh copy of the raw mirror (bare `assets/` refs everywhere)
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(SRC, dst)
    adir = os.path.join(dst, "assets")

    # Framer's JS chunks import each other by their ORIGINAL hashed basename, but we
    # saved every asset md5-prefixed. Create basename copies so those imports resolve
    # and the site hydrates (which is what plays the transitions).
    copies = 0
    for f in os.listdir(adir):
        m = re.match(r'^[0-9a-f]{10}_(.+\.(mjs|js))$', f)
        if m:
            base = os.path.join(adir, m.group(1))
            if not os.path.exists(base):
                shutil.copyfile(os.path.join(adir, f), base)
                copies += 1

    def _recolor(s):
        for a, b in colormap:
            s = s.replace(a, b)
        return s

    recolored = 0
    for f in os.listdir(adir):
        if not f.endswith(('.mjs', '.js', '.css', '.svg')):
            continue
        fp = os.path.join(adir, f)
        try:
            s = open(fp, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        ns = _recolor(s).replace(CDN_IMG, asset_prefix)
        for _old, _new in COPY:
            ns = ns.replace(_old, _new)
        ns = ns.replace('Saify', 'BridgeMD')
        if ns != s:
            open(fp, "w", encoding="utf-8").write(ns)
            recolored += 1

    rasters = _recolor_rasters(adir, raster_hue) if raster_hue is not None else 0

    # Swap the template's Saify wordmark (nav + footer) for the BridgeMD wordmark.
    logos = 0
    for f in os.listdir(adir):
        if f.endswith('NBX8F9r1xHm09nzwJUrqfmzhg.svg'):
            open(os.path.join(adir, f), 'w', encoding='utf-8').write(BRAND_SVG)
            logos += 1

    # Drop real BridgeMD product screenshots over the template mockups. MUST run after
    # the raster hue-shift so the (already-blue) screenshots aren't recolored again.
    shots = 0
    for f in os.listdir(adir):
        stem = os.path.splitext(f)[0]
        for base, src_shot in PRODUCT.items():
            if stem.endswith(base) and os.path.exists(src_shot):
                shutil.copyfile(src_shot, os.path.join(adir, f))
                shots += 1
                break

    # Image basename copies (strip the md5 prefix) so the repointed CDN URLs
    # (bare-hash, e.g. NBX8F9....svg) resolve to the FINAL overwritten files.
    # Runs last so it picks up the BridgeMD logo + product shots, not the originals.
    img_copies = 0
    for f in os.listdir(adir):
        m = re.match(r'^[0-9a-f]{10}_(.+\.(svg|png|jpg|jpeg|gif|webp))$', f, re.I)
        if m:
            shutil.copyfile(os.path.join(adir, f), os.path.join(adir, m.group(1)))
            img_copies += 1

    p = os.path.join(dst, "index.html")
    h = open(p, encoding="utf-8").read()

    # 1) absolute asset paths, catching EVERY occurrence incl. all srcset entries.
    #    idempotent: never double-prefix an already-absolute prefix ref.
    h = re.sub(r'(?<!/)assets/', asset_prefix, h)

    # 1a) repoint Framer CDN image refs (also present in the SSR HTML) to local.
    h = h.replace(CDN_IMG, asset_prefix)

    # 1a2) Strip Framer's React runtime. This page is a static marketing snapshot;
    # the heavy client bundle only re-hydrated it -- rewriting our patched DOM and
    # throwing the React #418/#422 server/client-mismatch spam, fetching an unscraped
    # CMS collection module (the 404), and booting the on-page editor. Dropping the
    # bundle + its modulepreloads leaves the SSR HTML as the final render: no
    # hydration, no flicker, clean console. The entrance animations survive because
    # Framer's "appear" system runs from the inline `animator` + appear script (still
    # present), which never depended on this bundle.
    h = re.sub(r'\s*<link rel="modulepreload"[^>]*>', '', h)
    h = re.sub(r'\s*<script[^>]*data-framer-bundle="main"[^>]*></script>', '', h)

    # 1a3) Remove third-party telemetry the template author baked in: their Google
    # Tag Manager container + GA property (G-BLG6F53Z3W) and Framer's page-events
    # beacon. No reason to pipe our visitors into a stranger's analytics, and it drops
    # stray network calls. All async / non-visual, so rendering is unaffected.
    h = re.sub(r'\s*<script[^>]*src="[^"]*ff52d0167f_js"[^>]*></script>', '', h)
    h = re.sub(r'\s*<script[^>]*src="[^"]*81ba3be112_script"[^>]*></script>', '', h)
    h = re.sub(r'\s*<script>\s*window\.dataLayer.*?</script>', '', h, flags=re.S)
    # ...and the guarded editor-init preload (only fires with a dev localStorage
    # flag, but there's no reason to ship it in a public build).
    h = re.sub(r'\s*<script>try\{if\(localStorage\.getItem\('
               r'"__framer_force_showing_editorbar_since"\).*?</script>',
               '', h, flags=re.S)

    # 1b) drop Framer's editor iframe (it frames framer.com -> CSP console noise).
    h = re.sub(r'<iframe id="__framer-editorbar".*?</iframe>', '', h, flags=re.S)

    # 1b2) recolor the template palette (same map used on the assets above).
    h = _recolor(h)

    # 1b4) hero copy. The SSR headline/subhead are split into per-word <span>s for the
    # appear animation, so a plain string replace can't touch the visible text. Rewrite
    # each RichTextContainer's inner markup (keeping Framer's preset classes/styles) to
    # plain text; the JS bundle got the same copy above so hydration stays in sync.
    _h1s = ('class="framer-text framer-styles-preset-1kq4rxl" data-styles-preset='
            '"kJ54mn6Fl" style="--framer-text-alignment:center;--framer-text-color:'
            'var(--token-2d28e01c-cef5-4fed-8a3e-b39db8015610, rgb(18, 18, 43))"')
    # Per-letter reveal: wrap each visible character in its own <span class="bmd-ch">
    # with an incrementing animation-delay so the headline "types" itself in instead of
    # fading as one block. Spaces stay plain text (kept out of the stagger) so natural
    # word spacing/wrapping is preserved. Delays continue across both lines.
    _L_BASE, _L_STEP = 0.5, 0.008

    def _letters(text, idx):
        out = []
        for ch in text:
            if ch == ' ':
                out.append(' ')
                continue
            out.append('<span class="bmd-ch" style="animation-delay:%.3fs">%s</span>'
                       % (_L_BASE + idx * _L_STEP, ch))
            idx += 1
        return ''.join(out), idx

    _l1, _i = _letters(H1_L1, 0)
    _l2, _i = _letters(H1_L2, _i)
    _hero_letters_end = _L_BASE + _i * _L_STEP  # when the last letter starts animating
    h = re.sub(
        r'(data-framer-name="Attract and Win Your Perfect Customers"[^>]*'
        r'style="transform:none">).*?</div>',
        r'\1' + f'<h1 {_h1s}>{_l1}</h1><h1 {_h1s}>{_l2}</h1>' + '</div>',
        h, flags=re.S)
    _ps = ('class="framer-text framer-styles-preset-1epzz2i" data-styles-preset='
           '"yb3XDH932" style="--framer-text-alignment:center;--framer-text-color:'
           'var(--token-bec2d801-4472-4895-bd77-b8757840a468, rgb(57, 57, 86))"')
    h = re.sub(
        r'(data-framer-name="The first unlimited B2B[^"]*"[^>]*'
        r'style="transform:none">).*?</div>',
        r'\1' + f'<p {_ps}>{SUBHEAD}</p>' + '</div>',
        h, flags=re.S)
    # section/card/CTA/nav copy (whole-string). Runs BEFORE the brand swap so the
    # original strings still contain "Saify".
    for _old, _new in COPY:
        h = h.replace(_old, _new)

    # Break the subhead onto two centered lines at the sentence boundary. Do it on the
    # visible TEXT node (>...<) so it hits every SSR breakpoint variant, not just the
    # one the injection regex matched. The data-framer-name attribute (="...") stays the
    # plain one-line string, so the hero-fade selectors below still match.
    h = h.replace(f'>{SUBHEAD}<', f'>{SUBHEAD.replace(". ", ".<br>", 1)}<')

    # Stats band card numbers + labels -> BridgeMD capability facts (see STATS_TEXT).
    for _old, _new in STATS_TEXT:
        h = h.replace(_old, _new)

    # rebrand: any remaining template name -> BridgeMD (title, meta, footer, alts).
    h = h.replace('Saify', 'BridgeMD')

    # Some big headings are split into per-word <span>s for the appear animation, so
    # the whole-string COPY replace above only fixed the data-framer-name attribute,
    # not the visible text. React 18 keeps SSR text on hydration mismatch, so rewrite
    # the SSR inner of each split container (anchored by its now-new data-framer-name)
    # to the plain new copy; the tag's Framer preset classes are preserved.
    def _fix_split(html, name_prefix, newtext):
        # Collapse the whole RichTextContainer inner (which may be several word-split
        # <p>/<h2> lines) down to a single tag holding the plain new copy, keeping the
        # first tag's Framer preset classes.
        pat = re.compile(
            r'(data-framer-name="' + re.escape(name_prefix) +
            r'[^"]*"[^>]*>\s*<(h1|h2|h3|h4|h5|p)\b[^>]*>).*?(</div>)', re.S)
        return pat.sub(
            lambda m: m.group(1) + newtext + '</' + m.group(2) + '>' + m.group(3),
            html, count=1)

    for _pfx, _txt in _SPLIT_FIX:
        h = _fix_split(h, _pfx, _txt)

    # Inject our embed + working trial finder section right before the CTA section.
    h = re.sub(r'(<section [^>]*class="framer-17hpso4")',
               EMBED_HTML + r'\1', h, count=1)

    # 1c) Kill the entrance "flash": the captured DOM has appear elements baked at
    # opacity:1, so they paint visible, then Framer's appear runtime resets them to
    # hidden and animates back in. Strip that inline final state on appear elements
    # so they start hidden (via the CSS rule injected below) and animate in once.
    def _strip_appear(m):
        tag = m.group(0)
        if 'data-framer-appear-id' not in tag:
            return tag
        return tag.replace('opacity: 1;', '').replace('transform: none;', '')
    h = re.sub(r'<[a-zA-Z0-9]+[^>]*>', _strip_appear, h)

    # 2a) The "Contact Us" buttons open the live product instead of a booking form:
    # relabel to "See live demo" and point at /app/home (login-gated in prod, demo-open
    # locally). Keyed on the button label so the header CTA + footer "Contact" links are
    # left alone. Must run before the ./contact -> CAL rewrite so these are claimed first.
    def _cta_to_demo(m):
        a = m.group(0)
        if '>Contact Us<' not in a:
            return a
        return (a.replace('href="./contact"', f'href="{APP_HOME}"')
                 .replace('>Contact Us<', '>See live demo<'))
    h = re.sub(r'<a\b.*?</a>', _cta_to_demo, h, flags=re.S)

    # 2b) The header CTA (filled pill, data-framer-name="Get Started", label "Contact")
    # becomes "Request A Demo". Keyed on that name so the plain footer "Contact" text
    # link is untouched. href stays ./contact -> becomes the booking link below.
    def _nav_contact_to_demo(m):
        a = m.group(0)
        if '>Contact<' in a and 'data-framer-name="Get Started"' in a:
            return a.replace('>Contact<', '>Request A Demo<')
        return a
    h = re.sub(r'<a\b.*?</a>', _nav_contact_to_demo, h, flags=re.S)

    # 2) rewire CTAs -> booking; internal (unscraped) links -> the preview page.
    h = h.replace('href="./contact"', f'href="{CAL}"')
    h = h.replace('href="./demo"', f'href="{CAL}"')
    h = h.replace('href="https://diversekit.lemonsqueezy.com/buy/97f23d28-1bd2-4f6b-96ca-ddac92b9dc35"', f'href="{CAL}"')
    h = re.sub(r'href="\./[^"]*"', 'href="/site-preview"', h)

    # 2c) Wire the nav. All four text links shipped pointing at /site-preview (the
    # catch-all above); repoint each at its real destination, matched by the visible
    # label so we hit the right anchor across every SSR breakpoint variant. The
    # (?!</a>) guards keep the match inside a single anchor. Trial finder opens the
    # real finder page in a new tab; the rest smooth-scroll to on-page sections.
    def _wire_nav(html, label, href, newtab=False):
        extra = ' target="_blank" rel="noopener"' if newtab else ''
        pat = re.compile(
            r'(<a\b(?:(?!</a>).)*?)href="/site-preview"'
            r'((?:(?!</a>).)*?<p[^>]*>' + re.escape(label) + r'</p>)', re.S)
        return pat.sub(
            lambda m: m.group(1) + 'href="' + href + '"' + extra + m.group(2), html)
    # The 4th nav slot (originally Saify "Pricing") is relabeled straight to
    # "Embed" above, so there's no clone step and no FAQ link in the header. The
    # FAQ section still ships on the page (injected below), just without a nav link
    # - one fewer item and no cloned slot means a smoother header on hydrate.
    h = _wire_nav(h, "Why us", "#features")
    h = _wire_nav(h, "Product", APP_HOME)
    h = _wire_nav(h, "Trial finder", FINDER_URL)
    h = _wire_nav(h, "Embed", "#bmd-embed")

    # The footer "Product" link ships as a bare <a>Product</a> (no inner <p>), so it
    # misses _wire_nav's <p>-anchored match and would otherwise stay on the dead
    # /site-preview catch-all. Point it at the real app too.
    def _wire_footer(html, label, href):
        pat = re.compile(
            r'(<a\b(?:(?!</a>).)*?)href="/site-preview"'
            r'((?:(?!</a>).)*?>' + re.escape(label) + r'</a>)', re.S)
        return pat.sub(
            lambda m: m.group(1) + 'href="' + href + '"' + m.group(2), html)
    h = _wire_footer(h, "Product", APP_HOME)

    # Anchor targets for the nav: tag the first (visible) variant of each section.
    h = h.replace('data-framer-name="Features Section"',
                  'id="features" data-framer-name="Features Section"', 1)
    h = h.replace('data-framer-name="Stats Section"',
                  'id="metrics" data-framer-name="Stats Section"', 1)

    # Inject the FAQ just before the CTA (lands right after the embed block).
    h = re.sub(r'(<section [^>]*class="framer-17hpso4")',
               FAQ_HTML + r'\1', h, count=1)

    # Inject the "sources -> one inbox" section, then the trust strip, right below the
    # hero (before Features). Injecting SOURCES first, then TRUST at the same anchor,
    # lands them in order: hero -> sources -> trust -> features.
    h = re.sub(r'(<section [^>]*class="framer-r3ortg")',
               SOURCES_HTML + r'\1', h, count=1)
    h = re.sub(r'(<section [^>]*class="framer-r3ortg")',
               TRUST_HTML + r'\1', h, count=1)

    # Bake the real brand logos referenced by the sources diagram into local assets
    # (fetched from apistemic) so the page never hotlinks a third party at runtime.
    h = _localize_brand_logos(h, adir, asset_prefix)

    # Replace the bloated Framer footer with our compact one (the Framer <footer>
    # itself is hidden via the kill-list below).
    h = re.sub(r'(<footer\b)', FOOTER_HTML + r'\1', h, count=1)

    # 3a) Hero entrance. Framer's appear animation only covers the NavBar/Button/Badge,
    # so the headline + subhead had no transition and popped in instantly while the nav
    # slid down. Give them a self-contained CSS fade-and-rise, keyed off the exact copy
    # we set as each container's data-framer-name (so the selector can't drift). It's
    # independent of Framer's runtime, so hydration can't fight it; the elements start
    # hidden (opacity:0) from first paint, so there's no flash before the transition.
    _hero_h1 = f"{H1_L1} {H1_L2}"
    # Subhead fades in as a block just after the headline finishes typing itself in.
    _sub_delay = round(_hero_letters_end + 0.15, 2)
    hero_in = (
        # Headline: the container only needs to be visible (override Framer's inline
        # opacity:0); the reveal is driven per character by .bmd-ch below.
        f'[data-framer-name="{_hero_h1}"]{{opacity:1}}'
        '.bmd-ch{display:inline-block;opacity:0;will-change:opacity,transform;'
        'animation:bmdLetterIn .3s cubic-bezier(.22,.61,.36,1) both}'
        '@keyframes bmdLetterIn{from{opacity:0;transform:translateY(.5em)}'
        'to{opacity:1;transform:none}}'
        # Subhead: block fade/rise, timed after the last letter.
        f'[data-framer-name="{SUBHEAD}"]'
        '{opacity:0;animation:bmdHeroIn .7s cubic-bezier(.22,.61,.36,1) both;'
        f'animation-delay:{_sub_delay}s}}'
        '@keyframes bmdHeroIn{from{opacity:0;transform:translateY(14px)}'
        'to{opacity:1;transform:none}}'
        '@media(prefers-reduced-motion:reduce){'
        f'[data-framer-name="{_hero_h1}"],[data-framer-name="{SUBHEAD}"]{{opacity:1}}'
        '.bmd-ch{animation:none;opacity:1}'
        f'[data-framer-name="{SUBHEAD}"]{{animation:none}}}}')

    # Recreate Framer's text presets natively. Framer ships each text style (hero,
    # section headings, body, nav, ...) as CSS strings INSIDE the JS chunks we
    # removed. Those rules set the --framer-font-size/-weight/-line-height/-letter-
    # spacing vars that the SSR base rule (.framer-text{font-size:var(--framer-font-
    # size)...}, still inline in the page) consumes. Framer baked those vars inline on
    # most text during SSR, so it survives -- but the hero H1/subhead we rewrote have
    # no inline vars and relied on the (now-gone) preset rule, which is why they lost
    # their size. Pull every preset rule out of the chunks, unscope it from the per-
    # page root hash (.framer-ZI9eg / .framer-9nO4Q) so it applies by preset class
    # alone, dedupe, and inject as a real stylesheet. Result: exact Framer sizing with
    # no bundle. Inline vars still win by specificity, so this only fills in the
    # rewritten hero and never fights the rest of the page.
    preset_rules, _seen = [], set()
    for _f in sorted(os.listdir(adir)):
        if not _f.endswith('.mjs'):
            continue
        try:
            _c = open(os.path.join(adir, _f), encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        for _rule in re.findall(r'`([^`]*framer-styles-preset-[^`]*)`', _c):
            _rule = re.sub(r'\.framer-[A-Za-z0-9]+\s+\.framer-styles-preset-',
                           '.framer-styles-preset-', _rule)
            # A backtick literal that merely *contains* "framer-styles-preset-"
            # somewhere in its body can be minified JS (an editor chunk), not a CSS
            # rule. Dumping that JS into the <style> corrupts CSS parsing and silently
            # kills every rule after it (e.g. the hero fade). Keep only captures that
            # actually START with a CSS selector/at-rule and carry no JS signatures.
            _stripped = _rule.lstrip()
            if not _stripped.startswith(('.framer-styles-preset-', '@media', '@supports')):
                continue
            if any(_tok in _rule for _tok in
                   ('function', '=>', 'return ', 'EditorState', 'charCodeAt',
                    'switch(', ';var ', ';let ', 'Number.isNaN')):
                continue
            _key = re.sub(r'\s+', ' ', _rule).strip()
            if _key and _key not in _seen:
                _seen.add(_key)
                preset_rules.append(_rule)
    preset_css = '\n'.join(preset_rules)

    # Load the same webfonts Framer loaded via JS (Figtree for display/body, Inter for
    # bold runs). Without this the presets fall back to system sans and the metrics
    # (and therefore wrapping/size) drift from the reference.
    fonts = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
             '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
             '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
             'family=Figtree:ital,wght@0,300..900;1,300..900&'
             'family=Inter:ital,wght@0,400..900;1,400..900&display=swap">')
    if fonts not in h:
        h = h.replace('</head>', fonts + '</head>', 1)

    # 3z) Re-create natively the three motion effects Framer's (removed) bundle drove:
    #   (a) Text shimmer: the eyebrow has a stacked aria-hidden <p class="text-shimmer-*">
    #       overlay; Framer generated its background-clip:text sweep at runtime. Rebuild
    #       it for any text-shimmer-* instance -- a light band travels across the glyphs
    #       while the darker base <p> shows through.
    #   (b) Scroll reveals ("load in as you scroll"): fade + rise each content section as
    #       it enters view. Gated by html.bmd-anim (set by a tiny head script BEFORE the
    #       body paints) so targets start hidden with no flash; if JS is off the class is
    #       never added and everything shows normally. Hero is excluded (it has its own
    #       entrance). Uses IntersectionObserver with a timeout + reduced-motion fallback.
    #   (c) Nav scroll morph: the fixed pill condenses (nudges up + tightens shadow/scale)
    #       once the page is scrolled, transitioning smoothly.
    _rv = ('#bmd-trust,#bmd-sources,.framer-r3ortg,#bmd-embed,.framer-kerj4b,'
           '#bmd-faq,#bmd-footer')
    fx_css = (
        # (a) shimmer
        '[class*="text-shimmer-"]{background-image:linear-gradient(100deg,'
        'transparent 0,transparent 38%,#b9bccb 50%,transparent 62%,transparent 100%);'
        'background-size:220% 100%;background-repeat:no-repeat;'
        '-webkit-background-clip:text;background-clip:text;'
        '-webkit-text-fill-color:transparent;color:transparent;'
        'animation:bmdShine 3.2s ease-in-out infinite}'
        '@keyframes bmdShine{0%{background-position:130% 0}100%{background-position:-30% 0}}'
        # (b) reveals
        'html.bmd-anim ' + _rv.replace(',', ',html.bmd-anim ') +
        '{opacity:0;transform:translateY(30px);'
        'transition:opacity .8s cubic-bezier(.22,.61,.36,1),'
        'transform .8s cubic-bezier(.22,.61,.36,1);will-change:opacity,transform}'
        'html.bmd-anim #bmd-trust.bmd-in,html.bmd-anim #bmd-sources.bmd-in,'
        'html.bmd-anim .framer-r3ortg.bmd-in,html.bmd-anim #bmd-embed.bmd-in,'
        'html.bmd-anim .framer-kerj4b.bmd-in,html.bmd-anim #bmd-faq.bmd-in,'
        'html.bmd-anim #bmd-footer.bmd-in'
        '{opacity:1;transform:none}'
        # (b2) staggered child reveals. On top of the section fade, the cards/items
        # INSIDE a section reveal one-by-one as it scrolls into view (delay is set
        # per-child in JS), so the eye is walked down the whole page instead of a
        # section popping in as one block. Child opacity multiplies the parent's, so
        # a card stays hidden until its own .bmd-in even after the section revealed.
        'html.bmd-anim .bmd-rv{opacity:0;transform:translateY(22px);'
        'transition:opacity .7s cubic-bezier(.22,.61,.36,1),'
        'transform .7s cubic-bezier(.22,.61,.36,1);will-change:opacity,transform}'
        'html.bmd-anim .bmd-rv.bmd-in{opacity:1;transform:none}'
        # (c) nav morph
        # Contract the pill's WIDTH on scroll (base max-width is 736px). The nav is
        # centered (container is translateX(-50%)), so shrinking max-width pulls both
        # edges toward the middle -- matching the Framer original -- without squishing
        # the height/text the way transform:scale did.
        '.framer-1m624rr{transition:max-width .4s cubic-bezier(.22,.61,.36,1),'
        'padding .4s cubic-bezier(.22,.61,.36,1),box-shadow .4s ease,'
        'background-color .4s ease}'
        '.framer-bfsnv8-container{transition:top .4s cubic-bezier(.22,.61,.36,1)}'
        # Condense on scroll: the "shrink" is mostly the tighter padding (lower pill
        # height) + lift + shadow; the width pull-in is deliberately GENTLE. With five
        # nav links the content is nearly as wide as the pill, and the logo and menu are
        # only separated by the flex spacer (.framer-16rkffi). Over-shrinking the width
        # collapses that spacer and the logo crowds "Why us". Keep max-width comfortably
        # above the content's natural width, and hold the spacer open with a real floor
        # so there is always a clear gap between the logo and the links.
        'html.bmd-scrolled .framer-1m624rr{max-width:660px!important;'
        'padding:8px 12px!important;'
        'box-shadow:rgba(2,2,18,.10) 0px 12px 30px -10px,'
        'rgba(2,2,18,.06) 0px 6px 12px -6px}'
        'html.bmd-scrolled .framer-16rkffi{min-width:40px}'
        # Nav spacing: leave Framer's own layout alone. The pill is [logo | Nav | CTA]
        # where the Nav wrapper is flex:1 with place-content:center, so the links are
        # already centered with equal gaps to the logo and the CTA (the reference
        # "Saify" look). Earlier hacks (auto-margins / min-width overrides) only pushed
        # the links off-center, so no nav-centering CSS is injected here on purpose.
        '@media(min-width:810px){html.bmd-scrolled .framer-bfsnv8-container{top:8px}}'
        # reduced motion: no shimmer, reveals shown immediately
        '@media(prefers-reduced-motion:reduce){[class*="text-shimmer-"]{animation:none}'
        'html.bmd-anim #bmd-trust,html.bmd-anim #bmd-sources,'
        'html.bmd-anim .framer-r3ortg,html.bmd-anim #bmd-embed,'
        'html.bmd-anim .framer-kerj4b,html.bmd-anim #bmd-faq,'
        'html.bmd-anim #bmd-footer,html.bmd-anim .bmd-rv'
        '{opacity:1!important;transform:none!important;transition:none}}'
        # smooth in-page scrolling for the nav anchors, with an offset so the fixed
        # nav pill doesn't cover the section heading it lands on.
        'html{scroll-behavior:smooth}'
        '#features,#metrics,#bmd-embed,#bmd-faq{scroll-margin-top:120px}'
        # (d) FAQ block (native <details>, no JS). Themed to match the site.
        '#bmd-faq{background:#fff;padding:96px 24px;'
        'font-family:"Figtree",system-ui,-apple-system,sans-serif}'
        '#bmd-faq .bmd-faq-wrap{max-width:820px;margin:0 auto}'
        '#bmd-faq .bmd-faq-head{text-align:center;margin:0 0 40px}'
        '#bmd-faq .pill{display:inline-block;padding:6px 14px;border-radius:999px;'
        'background:rgba(18,87,176,.08);color:#1257b0;font-size:13px;font-weight:600;'
        'letter-spacing:.02em;margin:0 0 18px}'
        '#bmd-faq h2{font-size:clamp(30px,3.6vw,46px);line-height:1.1;font-weight:800;'
        'color:#12122b;margin:0 0 14px;letter-spacing:-.02em}'
        '#bmd-faq .lead{color:#516079;font-size:18px;line-height:1.5;margin:0 auto;'
        'max-width:520px}'
        '#bmd-faq details{border:1px solid rgba(18,87,176,.14);border-radius:16px;'
        'background:#fff;margin:12px 0;overflow:hidden;'
        'box-shadow:0 2px 10px rgba(16,16,40,.04);transition:box-shadow .25s ease}'
        '#bmd-faq details[open]{box-shadow:0 8px 26px rgba(18,87,176,.10)}'
        '#bmd-faq summary{list-style:none;cursor:pointer;padding:20px 22px;'
        'font-weight:700;font-size:17px;color:#12122b;display:flex;'
        'justify-content:space-between;align-items:center;gap:16px}'
        '#bmd-faq summary::-webkit-details-marker{display:none}'
        '#bmd-faq summary::after{content:"+";font-size:24px;line-height:1;'
        'color:#1257b0;font-weight:400;flex:0 0 auto;transition:transform .25s ease}'
        '#bmd-faq details[open] summary{color:#1257b0}'
        '#bmd-faq details[open] summary::after{content:"\\2212"}'
        '#bmd-faq .a{padding:0 22px 20px;color:#42506a;font-size:15.5px;'
        'line-height:1.65}'
        # (e) trust / compliance strip (chip row under the hero)
        '#bmd-trust{background:#fff;padding:52px 24px 8px;'
        'font-family:"Figtree",system-ui,-apple-system,sans-serif}'
        '#bmd-trust .bmd-trust-wrap{max-width:960px;margin:0 auto;text-align:center}'
        '#bmd-trust .bmd-trust-head{color:#6b7590;font-size:14px;font-weight:600;'
        'letter-spacing:.01em;margin:0 0 22px}'
        # Compliance badges: each item is a rounded pill with a circular, brand-tinted
        # icon chip, so the strip reads as a set of professional trust badges rather
        # than a flat text row. One icon size, one type scale, so it stays consistent.
        '#bmd-trust .bmd-trust-row{display:flex;flex-wrap:wrap;justify-content:center;'
        'align-items:center;gap:12px 14px}'
        '#bmd-trust .bmd-trust-item{display:inline-flex;align-items:center;gap:11px;'
        'padding:9px 18px 9px 10px;background:#f6f9fd;'
        'border:1px solid rgba(18,87,176,.14);border-radius:999px;'
        'color:#1f2a44;font-size:15px;font-weight:600;letter-spacing:.01em;'
        'white-space:nowrap;box-shadow:0 1px 2px rgba(16,16,40,.05)}'
        '#bmd-trust .bmd-trust-item b{font-weight:600}'
        '#bmd-trust .bmd-trust-ic{display:inline-flex;align-items:center;'
        'justify-content:center;width:32px;height:32px;border-radius:50%;'
        'background:rgba(18,87,176,.10);color:#1257b0;flex:0 0 auto}'
        '#bmd-trust .bmd-trust-ic svg{width:18px;height:18px;display:block}'
        # (e2) sources -> one inbox: an animated beam network. The BridgeMD inbox is the
        # centre node, the source logos flank it, and a brand-coloured beam travels along
        # each connector into the centre. One self-contained SVG (SMIL), scales via the
        # viewBox, no JS.
        '#bmd-sources{background:#fff;padding:64px 24px 56px;'
        'font-family:"Figtree",system-ui,-apple-system,sans-serif}'
        '#bmd-sources .bmd-src-wrap{max-width:1060px;margin:0 auto;text-align:center}'
        '#bmd-sources .bmd-src-eyebrow{color:#1257b0;font-size:13px;font-weight:700;'
        'letter-spacing:.08em;text-transform:uppercase;margin:0 0 12px}'
        '#bmd-sources .bmd-src-title{color:#12122b;font-size:33px;line-height:1.15;'
        'font-weight:700;letter-spacing:-.02em;margin:0 0 14px}'
        '#bmd-sources .bmd-src-sub{color:#5b6478;font-size:16px;line-height:1.6;'
        'max-width:660px;margin:0 auto 34px}'
        '#bmd-sources .bmd-bn{max-width:1025px;margin:0 auto}'
        '#bmd-sources .bmd-bn svg{width:100%;height:auto;display:block;overflow:visible}'
        '#bmd-sources .bmd-bn-lane{fill:none;stroke:#d7e3f4;stroke-width:2}'
        '#bmd-sources .bmd-bn-beam{fill:none;stroke-width:3;stroke-linecap:round}'
        '#bmd-sources .bmd-bn-lbl{fill:#425067;font-size:18px;font-weight:700;'
        'font-family:"Figtree",system-ui,-apple-system,sans-serif}'
        '#bmd-sources .bmd-bn-c1{fill:#12122b;font-size:15px;font-weight:800;'
        'font-family:"Figtree",system-ui,-apple-system,sans-serif}'
        '#bmd-sources .bmd-bn-c2{fill:#7486a3;font-size:12px;font-weight:600;'
        'font-family:"Figtree",system-ui,-apple-system,sans-serif}'
        '#bmd-sources .bmd-src-note{margin:30px auto 0;color:#5b6478;font-size:15px}'
        '#bmd-sources .bmd-src-note a{color:#1257b0;font-weight:700;'
        'text-decoration:none;border-bottom:1px solid rgba(18,87,176,.35)}'
        '#bmd-sources .bmd-src-note a:hover{border-bottom-color:#1257b0}'
        '@media(max-width:640px){#bmd-sources .bmd-src-title{font-size:26px}}'
        # Comets are inline SMIL animateMotion (classes get stripped by hydration), so
        # disable motion by targeting every animate/animateMotion in the diagram.
        '@media(prefers-reduced-motion:reduce){'
        '#bmd-sources .bmd-bn animate,#bmd-sources .bmd-bn animateMotion'
        '{display:none}}'
        # (f) compact footer
        '#bmd-footer{font-family:"Figtree",system-ui,-apple-system,sans-serif;'
        'background:#fff;border-top:1px solid rgba(18,87,176,.12);'
        'padding:48px 24px 40px}'
        '#bmd-footer .bmd-footer-wrap{max-width:1120px;margin:0 auto}'
        '#bmd-footer .bmd-footer-top{display:flex;justify-content:space-between;'
        'gap:40px;flex-wrap:wrap;align-items:flex-start}'
        '#bmd-footer .bmd-footer-brand{max-width:380px}'
        '#bmd-footer .bmd-footer-brand .name{font-weight:800;font-size:20px;'
        'color:#12122b;letter-spacing:-.02em}'
        '#bmd-footer .bmd-footer-brand p{margin:10px 0 0;color:#6b7590;'
        'font-size:14.5px;line-height:1.55}'
        '#bmd-footer .bmd-footer-links{display:flex;gap:26px;flex-wrap:wrap}'
        '#bmd-footer .bmd-footer-links a{color:#42506a;text-decoration:none;'
        'font-size:14.5px;font-weight:600;transition:color .2s ease}'
        '#bmd-footer .bmd-footer-links a:hover{color:#1257b0}'
        '#bmd-footer .bmd-footer-bottom{margin-top:34px;padding-top:20px;'
        'border-top:1px solid rgba(18,87,176,.08);color:#8a93a6;font-size:13px}')

    # 3) hide Framer chrome + the template author's "Buy Template $59" badge and the
    #    fabricated / off-product sections (hidden, not deleted: React re-inserts
    #    deleted DOM on hydrate).
    #
    # !!! GOTCHA (this has bitten us repeatedly) !!!
    # This is a `display:none !important` kill-list keyed on Framer's per-section class
    # hashes (e.g. .framer-kerj4b). If a section "isn't there" on the page even though
    # its content is correct in index.html, CHECK HERE FIRST -- it's almost certainly
    # in this list. To bring a template section back, REMOVE its class from here (and
    # repopulate its copy above). Do not add a class here without noting what it hides.
    inject = ('<style>#__framer-badge-container,#__framer-editorbar-container,'
              '#__framer-editorbar,.framer-1wjt5g7,'
              '.framer-slkee2,.framer-dihgdb,.framer-105mysz,.framer-sj45vi,'
              # fabricated "logoipsum" strip, plus the Saify-specific Benefits and FAQ
              # sections we replace with our own. (Stats band .framer-kerj4b is kept and
              # repopulated with BridgeMD capability facts, so it is NOT hidden.)
              '.framer-grtzvc,.framer-svrp9g,.framer-t30al1,'
              # Integrations "logo wall" (generic SaaS logos, not our channels)
              '.framer-1xcmj2k,'
              # CTA band ("Bring your patient recruitment into one inbox") -- removed
              # per request; the nav Contact + embed CTAs still carry the demo path.
              '.framer-17hpso4,'
              # Framer template footer (dead Saify links / socials / 404 / big empty
              # top). Replaced by our compact #bmd-footer (a <section>, not <footer>).
              'footer'
              '{display:none!important}'
              '[data-framer-appear-id]{opacity:0}'
              + preset_css + hero_in + fx_css + EMBED_CSS + '</style>')
    if inject not in h:
        h = h.replace('</head>', inject + '</head>', 1)

    # Set the reveal gate class synchronously in <head>, before the body paints, so the
    # reveal targets start hidden (no flash). If JS is disabled this never runs and the
    # sections render normally.
    head_anim = "<script>document.documentElement.classList.add('bmd-anim')</script>"
    if head_anim not in h:
        h = h.replace('</head>', head_anim + '</head>', 1)

    # Body-end driver for the reveals + nav scroll morph. Kept tiny and defensive: any
    # failure (or no IntersectionObserver) just reveals everything.
    fx_js = (
        '<script>(function(){try{var r=document.documentElement;'
        # Shrink the nav only once it has scrolled past the hero CTA buttons (not at the
        # first pixel). Threshold = the hero button's document-bottom minus the nav height,
        # recomputed on resize. Pick the Cal.com CTA that is NOT inside the nav pill.
        'var nav=document.querySelector(".framer-1m624rr");var thr=240;'
        'var calc=function(){var ls=[].slice.call('
        'document.querySelectorAll(\'a[href*="cal.com"],a[href*="/demo"]\'));'
        'for(var k=0;k<ls.length;k++){var e=ls[k];if(nav&&nav.contains(e))continue;'
        'var rr=e.getBoundingClientRect();if(rr.width<1&&rr.height<1)continue;'
        'thr=rr.bottom+window.scrollY-72;break;}};'
        'calc();window.addEventListener("resize",calc,{passive:true});'
        'var s=function(){if(window.scrollY>thr)r.classList.add("bmd-scrolled");'
        'else r.classList.remove("bmd-scrolled");};s();'
        'window.addEventListener("scroll",s,{passive:true});'
        'var els=[].slice.call(document.querySelectorAll(' + repr(_rv) + '));'
        # Staggered child reveals: [parentSelector, childSelector] pairs. Each child
        # gets .bmd-rv + a per-index transition-delay, then joins the same observer,
        # so a grid's cards cascade in as the section scrolls into view.
        'var groups=' + json.dumps([
            ['#bmd-trust', '.bmd-trust-item'],
            ['.framer-kerj4b', '[data-framer-name="Number Card"]'],
            ['.framer-r3ortg', '[data-framer-name="Growth Card Small"],'
             '[data-framer-name="Growth Card Big"]'],
            ['#bmd-embed', '.bmd-embed-card'],
            ['#bmd-faq', 'details'],
        ]) + ';'
        'groups.forEach(function(g){'
        '[].slice.call(document.querySelectorAll(g[0])).forEach(function(p){'
        '[].slice.call(p.querySelectorAll(g[1])).forEach(function(k,i){'
        'k.classList.add("bmd-rv");k.style.transitionDelay=(i*0.09)+"s";'
        'els.push(k);});});});'
        'var show=function(e){e.classList.add("bmd-in");};'
        'var rm=window.matchMedia&&window.matchMedia("(prefers-reduced-motion:reduce)").matches;'
        'if(rm||!("IntersectionObserver" in window)){els.forEach(show);return;}'
        'var io=new IntersectionObserver(function(es){es.forEach(function(en){'
        'if(en.isIntersecting){show(en.target);io.unobserve(en.target);}});},'
        '{rootMargin:"0px 0px -12% 0px",threshold:.08});'
        'els.forEach(function(e){io.observe(e);});'
        # No blanket timeout reveal: it fires before the visitor scrolls to
        # below-the-fold sections, pre-showing them so they never get their
        # load-in. IO reveals each element on scroll; the no-IO branch above and
        # this try/catch cover the failure cases.
        '}catch(e){document.querySelectorAll(' + repr(_rv) + ').forEach(function(x){'
        'x.classList.add("bmd-in");});}})();</script>')
    if fx_js not in h:
        h = h.replace('</body>', fx_js + '</body>', 1)

    # Framer's hydrated router hijacks anchor clicks and uses the route compiled into
    # its JS (e.g. /demo), ignoring the href we set. A capture-phase listener runs
    # before Framer's handlers and forces every Cal-link anchor to actually open it.
    click_js = (
        '<script>'
        # Cal.com CTAs: Framer would route them to /demo. Force-open in a new tab.
        'window.addEventListener("click",function(e){'
        'var a=e.target&&e.target.closest?e.target.closest("a"):null;'
        'if(a&&a.href&&/cal\\.com/.test(a.href)){'
        'e.preventDefault();e.stopImmediatePropagation();'
        'window.open(a.href,"_blank","noopener");}'
        # Product -> the real app. Framer stops propagation on same-origin links, so
        # its router would swallow /app/* and never navigate. Force a same-tab load.
        'if(a&&a.getAttribute&&/^\\/app(\\/|$)/.test(a.getAttribute("href")||"")){'
        'e.preventDefault();e.stopImmediatePropagation();'
        'window.location.href=a.getAttribute("href");}},true);'
        # The trial-finder button lives inside #main, where Framer preventDefaults
        # pointerdown and swallows the follow-up click. Navigate on pointerdown at
        # window-capture (which fires before Framer) so the button actually works.
        'window.addEventListener("pointerdown",function(e){'
        'if(e.button&&e.button!==0)return;'
        'var el=e.target&&e.target.closest?e.target.closest("[data-bmd-go]"):null;'
        'if(el){e.preventDefault();e.stopImmediatePropagation();'
        'window.location.href=el.getAttribute("data-bmd-go");}},true);'
        '</script>')
    # Inject at the very top of <head> so this capture-phase listener is registered
    # before Framer's own internal-link handler. Framer stops propagation for
    # same-origin links, so a later listener never sees the click; being first wins.
    if click_js not in h:
        h = h.replace('<head>', '<head>' + click_js, 1)

    # BridgeMD favicon across the landing page. Framer shipped its own favicon and
    # the asset isn't even localized into landing/assets, so the old links 404.
    # Strip every Framer icon / apple-touch link and inject the real BridgeMD set,
    # served from Flask static (+ the root /favicon.ico route).
    h = re.sub(r'<link\b[^>]*\brel="(?:icon|apple-touch-icon)"[^>]*>', '', h)
    bmd_favicon = (
        '<link rel="icon" type="image/svg+xml" href="/static/logo.svg">'
        '<link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32.png">'
        '<link rel="icon" type="image/png" sizes="16x16" href="/static/favicon-16.png">'
        '<link rel="icon" href="/favicon.ico" sizes="any">'
        '<link rel="apple-touch-icon" sizes="180x180" href="/static/apple-touch-icon.png">')
    if 'href="/static/favicon-32.png"' not in h:
        h = h.replace('<head>', '<head>' + bmd_favicon, 1)

    # BridgeMD wordmark: the Framer export ships the mark as a boxless two-tone
    # glyph. Point both landing logos (nav + footer) at the filled blue-square mark
    # (same treatment as the favicon / Saify), served from Flask static.
    h = re.sub(r'/landing/assets/[0-9a-f]+_NBX8F9r1xHm09nzwJUrqfmzhg\.svg',
               '/static/logo-wordmark.svg', h)

    # Safety net: if the appear animation never runs (JS failure), don't leave any
    # appear element stuck hidden -> reveal anything still transparent after 2.5s.
    appear_fallback = (
        '<script>setTimeout(function(){'
        'document.querySelectorAll("[data-framer-appear-id]").forEach(function(e){'
        'if(parseFloat(getComputedStyle(e).opacity)<0.1){'
        'e.style.opacity="1";e.style.transform="none";}});},2500);</script>')
    if appear_fallback not in h:
        h = h.replace('</body>', appear_fallback + '</body>', 1)

    # Flatten only the washed-out light->dark gradient on primary CTAs to a solid
    # accent. Matched by the gradient itself (not a class) so the white secondary
    # button is left alone. Re-run a few times to survive Framer's hydration.
    # Tighten (not flatten) the primary CTA gradient: replace the washed-out light->
    # dark blue with a subtle lighter-accent -> accent. Keyed off the gradient itself
    # so the white secondary button is untouched. Re-run to survive hydration.
    ar, ag, ab = (int(accent[i:i+2], 16) for i in (1, 3, 5))
    lite = '#%02x%02x%02x' % tuple(round(c + (255 - c) * 0.22) for c in (ar, ag, ab))
    solid_js = (
        '<script>(function(){'
        'var G="linear-gradient(8deg, ' + lite + ' 0%, ' + accent + ' 100%)";'
        'function fix(){document.querySelectorAll("a,button").forEach(function(e){'
        'var bg=getComputedStyle(e).backgroundImage;'
        'if(!bg||!/gradient/.test(bg))return;'
        # skip the white secondary button (its gradient is all 255s)
        'var cols=bg.match(/rgb\\(\\d+,\\s*\\d+,\\s*\\d+\\)/g)||[];'
        'var colored=cols.some(function(c){return c.replace(/\\D/g,"")!=="255255255";});'
        'if(!colored)return;'
        'e.style.setProperty("background-image",G,"important");});}'
        'fix();[300,900,2000].forEach(function(t){setTimeout(fix,t);});'
        'document.addEventListener("DOMContentLoaded",fix);})();</script>')
    if 'setProperty("background-image"' not in h:
        h = h.replace('</body>', solid_js + '</body>', 1)

    # Unify typography. The Framer design mixes "Inter" and "Figtree" across text
    # nodes (near 50/50), so same-role elements render in different faces and the page
    # reads as sloppy/unprofessional. Force ONE family site-wide -- Figtree, which our
    # injected sections already use -- by rewriting every font token. Runs last so it
    # catches every SSR inline style and <style> declaration.
    h = h.replace('"Inter Placeholder"', '"Figtree Placeholder"')
    h = h.replace('"Inter"', '"Figtree"')

    h = h.replace('name="viewport" content="width=device-width"',
                  'name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"')

    # Swap Framer's own nav for the shared .pill-nav header (single source: style.css).
    # Runs last so the extracted CSS isn't touched by the font/color rewrites above.
    h = _inject_pill_nav(h)

    open(p, "w", encoding="utf-8").write(h)

    bare = len(re.findall(r'(?<!/)assets/', h))
    print(f"[{dst}] bare assets/ left: {bare} | refs: {h.count(asset_prefix)} | "
          f"cal: {h.count(CAL)} | basenames: {copies} | recolored: {recolored} | "
          f"rasters: {rasters} | logos: {logos} | shots: {shots} | "
          f"imgcopies: {img_copies} | swap: {'bmd-theme-swap' in h}")


build("../web/landing", BLUE, "/landing/assets/", raster_hue=214, accent="#1257b0")
