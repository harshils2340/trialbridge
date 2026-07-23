"""Ad-creative image renderer for recruitment campaigns.

This is the "design engine" behind the ad maker. Today it composes the approved
copy into a clean, typographic SVG template - deterministic, editable, and
IRB-reviewable. We intentionally do NOT generate photorealistic imagery: for
clinical-trial recruitment, an invented photo of a "patient" can imply benefit
and would not be ethics-board-approved (see matcher/COMPLIANCE.md). Template
cards keep the copy neutral and truthful.

SWAP SEAM: `render_ad_svg` is the only thing the app calls. To move to a hosted
design tool later, replace its body with a Figma REST render call
(GET /v1/images/:file_key?ids=<nodeId>, passing text via Figma variables) or an
image-generation model - the route and template don't change.
"""
from __future__ import annotations

import html

# ── Sizes: the surfaces sites actually post to. (w, h) in px; SVG scales. ──
SIZES = {
    "square": (1080, 1080),   # Instagram / Facebook feed
    "story":  (1080, 1920),   # IG/FB story, 9:16
    "flyer":  (850, 1100),    # printable campus flyer, ~US Letter
}

# ── Accents: named -> (bar/ink color, tint for soft fills). Default is "ink"
# (near-black) to match the app's black-not-blue brand. All are on-brand. ──
ACCENTS = {
    "ink":    ("#0f172a", "#eef2f7"),
    "blue":   ("#1a6eb0", "#e8f2fb"),
    "green":  ("#16a34a", "#e7f7ec"),
    "violet": ("#6d28d9", "#f2ecfd"),
    "teal":   ("#0e7490", "#e6f6fb"),
}

TEMPLATES = ("clean", "bold", "flyer")

# Neutral, ethics-board-friendly boilerplate. Never edited into a claim.
_EYEBROW = "RESEARCH STUDY"
_FOOTER = ("Reviewed by a research ethics board \u00b7 Participation is voluntary "
           "\u00b7 This is research, not treatment.")
_CTA = "See if you may be eligible"


def _esc(s: str) -> str:
    return html.escape((s or "").strip())


def _wrap(text: str, max_chars: int, max_lines: int):
    """Greedy word wrap to <= max_lines lines of ~max_chars each. Overflow on the
    last line is ellipsized so copy never spills out of the frame."""
    words = (text or "").split()
    lines, cur = [], ""
    for w in words:
        cand = f"{cur} {w}".strip()
        if len(cand) <= max_chars or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = w
            if len(lines) == max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and (len(" ".join(words)) > len(" ".join(lines))):
        last = lines[-1]
        lines[-1] = (last[: max_chars - 1].rstrip() + "\u2026") if last else "\u2026"
    return lines


def _tspans(lines, x, y, line_h):
    return "".join(
        f'<tspan x="{x}" y="{y + i * line_h}">{_esc(ln)}</tspan>'
        for i, ln in enumerate(lines)
    )


def render_ad_svg(headline: str, body: str = "", *, template: str = "clean",
                  accent: str = "ink", size: str = "square",
                  org: str = "", cta: str = _CTA) -> str:
    """Compose an ad card as an SVG string. `org` is the site/study name shown
    small at the top. Copy is caller-supplied (already IRB-gated in the app)."""
    template = template if template in TEMPLATES else "clean"
    ink, tint = ACCENTS.get(accent, ACCENTS["ink"])
    w, h = SIZES.get(size, SIZES["square"])
    pad = int(w * 0.075)
    inner = w - pad * 2
    font = ('font-family="-apple-system,BFN,Segoe UI,Helvetica,Arial,sans-serif"')

    headline = headline or "Volunteers sought for a research study"
    # Character budgets scale with the frame width.
    h_chars = max(10, int(inner / (w * 0.052)))
    b_chars = max(16, int(inner / (w * 0.026)))
    h_lines = _wrap(headline, h_chars, 4)
    b_lines = _wrap(body, b_chars, 4) if body else []

    h_size = int(w * 0.072)
    h_lh = int(h_size * 1.12)
    b_size = int(w * 0.030)
    b_lh = int(b_size * 1.35)
    eyebrow = _esc(org.upper()) if org else _EYEBROW

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
        f'width="{w}" height="{h}" role="img">',
    ]

    if template == "bold":
        # Accent band across the top, big headline reversed out of it.
        band = int(h * 0.42)
        parts.append(f'<rect width="{w}" height="{h}" fill="#ffffff"/>')
        parts.append(f'<rect width="{w}" height="{band}" fill="{ink}"/>')
        parts.append(
            f'<text x="{pad}" y="{int(pad*1.15)}" {font} font-size="{int(w*0.024)}" '
            f'font-weight="700" letter-spacing="2" fill="#ffffff" opacity="0.8">'
            f'{eyebrow}</text>')
        hy = int(band * 0.42)
        parts.append(
            f'<text {font} font-size="{h_size}" font-weight="800" fill="#ffffff">'
            f'{_tspans(h_lines, pad, hy, h_lh)}</text>')
        by = band + int(pad * 0.9)
        if b_lines:
            parts.append(
                f'<text {font} font-size="{b_size}" fill="#334155">'
                f'{_tspans(b_lines, pad, by, b_lh)}</text>')
    elif template == "flyer":
        # Formal paper with a border - reads as a posted notice.
        parts.append(f'<rect width="{w}" height="{h}" fill="#ffffff"/>')
        m = int(pad * 0.5)
        parts.append(
            f'<rect x="{m}" y="{m}" width="{w-2*m}" height="{h-2*m}" fill="none" '
            f'stroke="{ink}" stroke-width="3"/>')
        cx = w // 2
        parts.append(
            f'<text x="{cx}" y="{int(pad*1.6)}" {font} font-size="{int(w*0.026)}" '
            f'font-weight="700" letter-spacing="3" fill="{ink}" '
            f'text-anchor="middle">{eyebrow}</text>')
        parts.append(
            f'<line x1="{pad}" y1="{int(pad*1.95)}" x2="{w-pad}" '
            f'y2="{int(pad*1.95)}" stroke="{ink}" stroke-width="2"/>')
        hy = int(pad * 3.0)
        centered = "".join(
            f'<tspan x="{cx}" y="{hy + i*h_lh}">{_esc(ln)}</tspan>'
            for i, ln in enumerate(h_lines))
        parts.append(
            f'<text {font} font-size="{h_size}" font-weight="800" fill="{ink}" '
            f'text-anchor="middle">{centered}</text>')
        if b_lines:
            by = hy + len(h_lines) * h_lh + int(pad * 0.6)
            cb = "".join(
                f'<tspan x="{cx}" y="{by + i*b_lh}">{_esc(ln)}</tspan>'
                for i, ln in enumerate(b_lines))
            parts.append(
                f'<text {font} font-size="{b_size}" fill="#334155" '
                f'text-anchor="middle">{cb}</text>')
    else:  # clean
        parts.append(f'<rect width="{w}" height="{h}" fill="#ffffff"/>')
        parts.append(f'<rect x="0" y="0" width="{int(w*0.014)}" height="{h}" '
                     f'fill="{ink}"/>')
        parts.append(
            f'<text x="{pad}" y="{int(pad*1.2)}" {font} font-size="{int(w*0.024)}" '
            f'font-weight="700" letter-spacing="2" fill="{ink}">{eyebrow}</text>')
        hy = int(pad * 2.1)
        parts.append(
            f'<text {font} font-size="{h_size}" font-weight="800" fill="#0f172a">'
            f'{_tspans(h_lines, pad, hy, h_lh)}</text>')
        if b_lines:
            by = hy + len(h_lines) * h_lh + int(pad * 0.5)
            parts.append(
                f'<text {font} font-size="{b_size}" fill="#334155">'
                f'{_tspans(b_lines, pad, by, b_lh)}</text>')

    # ── CTA pill + ethics footer, pinned near the bottom (all templates). ──
    cta_y = h - int(pad * 2.15)
    pill_h = int(w * 0.066)
    pill_w = int(len(cta) * (w * 0.0165)) + int(pad * 0.9)
    pill_x = (w - pill_w) // 2 if template == "flyer" else pad
    parts.append(
        f'<rect x="{pill_x}" y="{cta_y}" width="{pill_w}" height="{pill_h}" '
        f'rx="{pill_h//2}" fill="{ink}"/>')
    parts.append(
        f'<text x="{pill_x + pill_w//2}" y="{cta_y + int(pill_h*0.66)}" {font} '
        f'font-size="{int(w*0.026)}" font-weight="700" fill="#ffffff" '
        f'text-anchor="middle">{_esc(cta)}</text>')

    foot = _wrap(_FOOTER, int(inner / (w * 0.017)), 2)
    fy = h - int(pad * 0.85)
    fx = (w // 2) if template == "flyer" else pad
    anchor = ' text-anchor="middle"' if template == "flyer" else ""
    parts.append(
        f'<text {font} font-size="{int(w*0.019)}" fill="#5a6a7e"{anchor}>'
        + "".join(
            f'<tspan x="{fx}" y="{fy - (len(foot)-1-i)*int(w*0.026)}">{_esc(ln)}</tspan>'
            for i, ln in enumerate(foot))
        + '</text>')

    parts.append("</svg>")
    return "".join(parts)
