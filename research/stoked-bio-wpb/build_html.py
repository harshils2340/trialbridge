"""Build the WP-B generative chemistry briefing page (single HTML file)."""
import html
import json
import math

from competitors import COMPETITORS, DEALS, PRICING

E = html.escape
TIER_CLASS = {
    "Client": "t-client", "Direct": "t-direct", "Well-funded AI designer": "t-funded",
    "Incumbent software": "t-incumbent", "Indirect / complementor": "t-indirect",
    "Emerging / open": "t-emerging", "Niche: AI antibiotics": "t-niche",
}

# ---------- positioning map ----------
W, H, PADL, PADR, PADT, PADB = 760, 520, 64, 24, 24, 58
pw, ph = W - PADL - PADR, H - PADT - PADB
sx = lambda x: PADL + x / 10 * pw
sy = lambda y: PADT + (10 - y) / 10 * ph
# manual label offsets to keep labels clear of each other (dx, dy, anchor)
OFF = {
    "SyntheMol": (-10, -14, "end"), "Iktos": (10, 4, "start"), "PostEra": (10, 4, "start"),
    "Onepot": (-10, 4, "end"), "Chemify": (10, 4, "start"), "Insilico": (10, -8, "start"),
    "XtalPi": (10, 4, "start"), "Variational AI": (10, -6, "start"), "Model Medicines": (-10, 4, "end"),
    "Isomorphic": (10, -16, "start"), "Iambic": (10, 4, "start"), "Genesis": (10, 4, "start"),
    "Terray": (10, 4, "start"), "Recursion": (-10, 16, "end"), "Aqemia": (-10, 4, "end"),
    "Schrödinger": (10, 4, "start"), "Enamine REAL": (-10, 20, "end"), "infiniSee": (0, -12, "middle"),
    "Retrosynthesis": (-10, 18, "end"), "Open-source synth. models": (-10, 4, "end"),
    "NVIDIA GenMol": (10, -6, "start"), "REINVENT 4": (10, 4, "start"), "Boltz": (10, 4, "start"),
    "Lilly TuneLab": (-8, -10, "start"), "Tamarind": (10, 10, "start"), "Lila": (-10, 4, "end"),
    "Phare Bio": (10, 4, "start"),
}
svg = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-labelledby="mapTitle" class="map">',
       '<title id="mapTitle">Positioning map: synthesis grounding versus commercial model</title>']
# quadrant shading: white space
svg.append(f'<rect x="{sx(7)}" y="{sy(10)}" width="{sx(10)-sx(7)}" height="{sy(6.5)-sy(10)}" class="zone"/>')
svg.append(f'<text x="{sx(7)+8}" y="{sy(10)+16}" class="zone-label">Synthesis-first + own programs</text>')
for v in range(0, 11, 2):
    svg.append(f'<line x1="{sx(v)}" y1="{PADT}" x2="{sx(v)}" y2="{PADT+ph}" class="grid"/>')
    svg.append(f'<line x1="{PADL}" y1="{sy(v)}" x2="{PADL+pw}" y2="{sy(v)}" class="grid"/>')
    svg.append(f'<text x="{sx(v)}" y="{PADT+ph+16}" class="tick" text-anchor="middle">{v}</text>')
    svg.append(f'<text x="{PADL-8}" y="{sy(v)+4}" class="tick" text-anchor="end">{v}</text>')
svg.append(f'<text x="{PADL+pw/2}" y="{H-12}" class="axis" text-anchor="middle">Synthesis grounding → (0 = none, 10 = every design has a make-able route or is made in-house)</text>')
svg.append(f'<text transform="translate(16 {PADT+ph/2}) rotate(-90)" class="axis" text-anchor="middle">Sells tools ← → Owns drug programs</text>')
for c in sorted(COMPETITORS, key=lambda c: c["tier"] == "Client"):
    cx, cy = sx(c["x"]), sy(c["y"])
    r = 5 if not c["funding_usd_m"] else max(5, min(18, 3 + 2.2 * math.log10(c["funding_usd_m"] + 1) * 2))
    cls = TIER_CLASS[c["tier"]]
    dx, dy, anc = OFF.get(c["short"], (10, 4, "start"))
    if anc != "middle":
        dx = (r + 5) * (1 if dx > 0 else -1)
    svg.append(f'<g class="pt {cls}"><circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}"/>'
               f'<text x="{cx+dx:.1f}" y="{cy+dy:.1f}" text-anchor="{anc}">{E(c["short"])}</text></g>')
# arrow: SyntheMol licensing option moves left/down
s = next(c for c in COMPETITORS if c["tier"] == "Client")
svg.append(f'<line x1="{sx(s["x"])-4}" y1="{sy(s["y"])+10}" x2="{sx(9.2)}" y2="{sy(5.4)}" class="arrow" marker-end="url(#ah)"/>')
svg.append(f'<text x="{sx(10)}" y="{sy(5.4)+16}" class="arrow-label" text-anchor="end">if licensed</text>')
svg.append(f'<text x="{sx(10)}" y="{sy(5.4)+30}" class="arrow-label" text-anchor="end">as a platform</text>')
svg.insert(2, '<defs><marker id="ah" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" class="arrowhead"/></marker></defs>')
svg.append("</svg>")
MAP = "\n".join(svg)

# ---------- capital chart (log scale) ----------
funded = sorted([c for c in COMPETITORS if c["funding_usd_m"]], key=lambda c: -c["funding_usd_m"])
lo, hi = 1, 3000
bar_rows = []
for c in funded:
    pct = (math.log10(c["funding_usd_m"]) - math.log10(lo)) / (math.log10(hi) - math.log10(lo)) * 100
    val = f'${c["funding_usd_m"]:,.0f}M' if c["funding_usd_m"] >= 10 else f'${c["funding_usd_m"]:.1f}M'
    bar_rows.append(f'<div class="bar-row {TIER_CLASS[c["tier"]]}"><span class="bar-name">{E(c["short"])}</span>'
                    f'<span class="bar-track"><span class="bar" style="width:{pct:.1f}%"></span></span>'
                    f'<span class="bar-val">{val}</span></div>')
BARS = "\n".join(bar_rows)

# ---------- table ----------
trs = []
for c in COMPETITORS:
    t = c["threat"]
    tcls = {"High": "hi", "Medium": "med", "Low": "low", "Complementor": "comp"}.get(t, "none")
    trs.append(f'''<tr data-tier="{E(c["tier"])}" class="{TIER_CLASS[c["tier"]]}">
<th scope="row"><span class="dot"></span>{E(c["name"])}<small>{E(c["hq"])}</small></th>
<td><span class="chip">{E(c["tier"])}</span></td>
<td>{E(c["approach"])}<small>{E(c["product"])}</small></td>
<td>{E(c["model"])}</td>
<td>{E(c["funding"])}</td>
<td>{E(c["deals"])}<small>{E(c["evidence"])}</small></td>
<td><span class="threat {tcls}">{E(t)}</span></td>
<td>{E(c["why"])} <a href="{E(c["src"].split(" ; ")[0])}" target="_blank" rel="noopener">source</a></td>
</tr>''')
TABLE = "\n".join(trs)
TIERS = [t for t in TIER_CLASS if t != "Client"]
FILTERS = "".join(f'<button type="button" class="filter" data-tier="{E(t)}" aria-pressed="false">{E(t)}</button>' for t in TIERS)

PRICE = "\n".join(f'<tr><th scope="row">{E(a)}</th><td>{E(b)}</td><td>{E(c)}</td><td><span class="conf">{E(d)}</span></td></tr>' for a, b, c, d in PRICING)
DEAL = "\n".join(
    f'<tr><th scope="row">{E(n)}</th><td class="num">{y}</td><td class="num">{"—" if u is None else f"${u:,.1f}M".replace(".0M","M")}</td>'
    f'<td class="num">${h:,.0f}M</td><td class="num">{"—" if u is None else f"{u/h*100:.1f}%"}</td><td>{E(note)}</td></tr>'
    for n, y, u, h, note in DEALS)

SOURCES = sorted({s for c in COMPETITORS for s in c["src"].split(" ; ")})
SRC = "\n".join(f'<li><a href="{E(s)}" target="_blank" rel="noopener">{E(s)}</a></li>' for s in SOURCES)

page = open("template.html", encoding="utf-8").read()
for k, v in dict(MAP=MAP, BARS=BARS, TABLE=TABLE, FILTERS=FILTERS, PRICE=PRICE, DEAL=DEAL, SRC=SRC,
                 N=str(len(COMPETITORS) - 1)).items():
    page = page.replace("{{" + k + "}}", v)
open("wpb-genchem-briefing.html", "w", encoding="utf-8").write(page)
print("written", len(page))
