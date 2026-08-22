import os, re, hashlib, urllib.parse, sys
from playwright.sync_api import sync_playwright

BASE = "https://fancy-cogwheel-201934.framer.app/"
OUT = "/Users/harshils/GraphMD/marketing_test"
ASSETS = os.path.join(OUT, "assets")
os.makedirs(ASSETS, exist_ok=True)

RES_RE = re.compile(r'\.(css|js|woff2?|ttf|otf|png|jpg|jpeg|webp|svg|avif|gif)(\?|$)', re.I)

def fname(url):
    p = urllib.parse.urlparse(url)
    base = os.path.basename(p.path) or "res"
    base = re.sub(r'[^A-Za-z0-9_.-]', '_', base)
    h = hashlib.md5(url.encode()).hexdigest()[:10]
    return f"{h}_{base}"

saved = {}   # abs url -> local filename in assets/

PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")

with sync_playwright() as pw:
    b = pw.chromium.launch(proxy={"server": PROXY}) if PROXY else pw.chromium.launch()
    pg = b.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
    grabbed = []

    def on_response(resp):
        try:
            url = resp.url
            if url.startswith("data:"):
                return
            ct = resp.headers.get("content-type", "")
            if RES_RE.search(url) or any(k in ct for k in
                    ["text/css", "javascript", "font", "image/"]):
                grabbed.append((url, ct, resp.body()))
        except Exception:
            pass

    pg.on("response", on_response)
    pg.goto(BASE, wait_until="load", timeout=60000)
    pg.wait_for_timeout(2500)
    h = pg.evaluate("document.body.scrollHeight")
    y = 0
    while y < h:
        pg.evaluate(f"window.scrollTo(0,{y})")
        pg.wait_for_timeout(130)
        y += 600
        h = pg.evaluate("document.body.scrollHeight")
    pg.evaluate("window.scrollTo(0,0)")
    pg.wait_for_timeout(1200)

    # Harvest every asset URL actually used in the rendered DOM (images that
    # Framer lazy-loads via JS won't appear in the network log otherwise).
    dom_urls = pg.evaluate("""() => {
      const out = new Set();
      const abs = u => { try { return new URL(u, location.href).href; } catch(e){ return null; } };
      document.querySelectorAll('img').forEach(im => {
        if (im.currentSrc) out.add(im.currentSrc);
        if (im.src) out.add(im.src);
        (im.getAttribute('srcset')||'').split(',').forEach(p => {
          const u = p.trim().split(/\\s+/)[0]; if (u) out.add(abs(u));
        });
      });
      document.querySelectorAll('source').forEach(s => {
        (s.getAttribute('srcset')||'').split(',').forEach(p => {
          const u = p.trim().split(/\\s+/)[0]; if (u) out.add(abs(u));
        });
      });
      document.querySelectorAll('*').forEach(el => {
        const bg = getComputedStyle(el).backgroundImage;
        if (bg && bg !== 'none') {
          const m = bg.match(/url\\((['\"]?)(.*?)\\1\\)/g) || [];
          m.forEach(x => { const u = x.replace(/url\\((['\"]?)(.*?)\\1\\)/, '$2'); if (u && !u.startsWith('data:')) out.add(abs(u)); });
        }
      });
      return [...out].filter(Boolean);
    }""")

    # Fetch anything not already grabbed, through the browser context (uses proxy).
    for u in dom_urls:
        if u in saved or u.startswith("data:"):
            continue
        try:
            r = pg.context.request.get(u, timeout=30000)
            if r.ok:
                grabbed.append((u, r.headers.get("content-type", ""), r.body()))
        except Exception:
            pass

    html = pg.content()
    b.close()

# write assets
for url, ct, body in grabbed:
    if url in saved:
        continue
    fn = fname(url)
    with open(os.path.join(ASSETS, fn), "wb") as f:
        f.write(body)
    saved[url] = fn

# rewrite url() inside CSS files (their refs live in the same assets/ dir)
for url, fn in list(saved.items()):
    if not (fn.endswith(".css") or "css" in url):
        continue
    path = os.path.join(ASSETS, fn)
    try:
        txt = open(path, encoding="utf-8", errors="ignore").read()
    except Exception:
        continue
    def repl(m):
        ref = m.group(1).strip('\'"')
        if ref.startswith("data:"):
            return m.group(0)
        ab = urllib.parse.urljoin(url, ref)
        if ab in saved:
            return f"url({saved[ab]})"
        return m.group(0)
    txt = re.sub(r'url\(([^)]+)\)', repl, txt)
    open(path, "w", encoding="utf-8").write(txt)

# By default strip scripts so the captured DOM renders as-is (no hydration
# blanking). Set KEEP_JS=1 to preserve Framer's runtime so transitions work.
if not os.environ.get("KEEP_JS"):
    html = re.sub(r'<script[\s\S]*?</script>', '', html, flags=re.I)

# rewrite absolute asset urls in the HTML to local ./assets/<fn>
# longest urls first to avoid partial clobbering; also handle &amp;-encoded forms
for url in sorted(saved.keys(), key=len, reverse=True):
    local = f"assets/{saved[url]}"
    html = html.replace(url, local)
    if "&" in url:
        html = html.replace(url.replace("&", "&amp;"), local)

with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as f:
    f.write(html)

print(f"saved {len(saved)} assets; html {len(html)} bytes")
