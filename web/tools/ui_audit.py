"""Audit the study-team UI by measuring it, not by looking at it.

Screenshots tell you something is wrong; this tells you what and by how much.
It renders real pages and reports the defects that read as "broken" to a person:

  smell    clipped text, elements escaping their container, overlapping
           siblings, zero-size (unreachable) controls
  geom     top-bar / workspace geometry: is the study picker actually centred,
           does the workspace overflow the viewport, is the composer on screen
  clutter  how many controls stand between opening the inbox and reading a
           message, and how many toolbar rows are stacked above the list
  access   can every core feature be reached, is its control visible, is it
           actually clickable (not covered), and in how many clicks

Written after a round of UI work where a passing test suite and a clean diff
still shipped a row whose six stage tabs overflowed their column by 271px and
were clipped with no visible scrollbar. Geometry catches that; a diff does not.

Usage (needs a SITE_DEMO server running, and playwright installed):

    ../.venv/bin/python -m pip install playwright && playwright install chromium
    PORT=5055 DB_PATH=/tmp/demo.db SITE_DEMO=1 NO_LOGIN=1 ../.venv/bin/python app.py &

    ../.venv/bin/python tools/ui_audit.py                    # all checks
    ../.venv/bin/python tools/ui_audit.py smell geom         # some checks
    ../.venv/bin/python tools/ui_audit.py --base http://127.0.0.1:5055 --width 1512

NOTE: point this at a scratch database, not the real one. `access` opens modals
and `walkthrough`-style flows mutate demo state.
"""
from __future__ import annotations

import argparse
import sys

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - dependency is dev-only
    sys.exit("playwright is not installed. See the module docstring.")


# ---- helpers ---------------------------------------------------------------

def _open_first_thread(page, base):
    """Most checks only make sense with a conversation open."""
    page.goto(f"{base}/app/inbox", wait_until="load")
    page.wait_for_timeout(600)
    row = page.query_selector(".mh-thread-row")
    if row:
        row.click()
        page.wait_for_load_state("load")
        page.wait_for_timeout(600)


def _probe(page, selector):
    """Found / visible / clickable for one control.

    "Clickable" matters separately from "visible": a control can have a real
    layout box and still be unreachable because something is painted over it.
    """
    return page.evaluate(
        """(sel)=>{
          const e=document.querySelector(sel);
          if(!e) return {found:false};
          const r=e.getBoundingClientRect(); const cs=getComputedStyle(e);
          const vis=r.width>0&&r.height>0&&cs.visibility!=='hidden'&&cs.display!=='none';
          let clickable=null;
          if(vis){const el=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
                  clickable=!!(el&&(el===e||e.contains(el)||el.contains(e)));}
          return {found:true, vis, clickable,
                  label:(e.getAttribute('aria-label')||e.textContent||'').trim().slice(0,40)};
        }""", selector)


# ---- checks ----------------------------------------------------------------

def check_smell(page, base, width):
    """Layout defects in the app chrome."""
    _open_first_thread(page, base)
    out = page.evaluate("""()=>{
      const res={clipped:[],overflow:[],zero:[],overlap:[]};
      const name=e=>e.tagName.toLowerCase()+(typeof e.className==='string'&&e.className
        ?'.'+e.className.trim().split(/\\s+/).slice(0,2).join('.'):'');
      const scope=document.querySelectorAll(
        '.apptop *, .mh-thread-toolbar *, .mh-filterbar *, .mh-conversation-head *,'
        +' .mh-link-applicant *, .mh-composer *');
      for(const e of scope){
        const cs=getComputedStyle(e);
        if(cs.display==='none'||cs.visibility==='hidden') continue;
        const r=e.getBoundingClientRect();
        const leaf=e.children.length===0 && (e.textContent||'').trim();
        if(leaf && e.scrollWidth>e.clientWidth+1 && e.clientWidth>0)
          res.clipped.push(name(e)+' "'+e.textContent.trim().slice(0,26)+'" '
                           +e.clientWidth+'<'+e.scrollWidth);
        // Zero-size only counts when the thing is genuinely meant to be on
        // screen. <option>s inside a closed <select>, and anything inside a
        // collapsed <details> or a [hidden] subtree, legitimately measure 0.
        if((r.width<1||r.height<1)&&(leaf||e.tagName==='BUTTON'||e.tagName==='INPUT')
           && e.tagName!=='OPTION' && !e.closest('select')
           && !e.closest('[hidden]') && !e.closest('details:not([open])'))
          res.zero.push(name(e));
        const pe=e.parentElement;
        if(pe){const pr=pe.getBoundingClientRect();
          // Three things legitimately sit outside their parent's box and are
          // not defects: <option>s, absolutely-positioned popovers, and badges
          // deliberately notched onto a corner (channel icon on an avatar).
          const popover=/appswitch-menu|appmenu-panel/.test(name(e));
          const badge=getComputedStyle(e).position==='absolute'
                      || /channel-ic|appnav-count|dot/.test(name(e));
          if(pr.width>0 && e.tagName!=='OPTION' && !popover && !badge
             && (r.right>pr.right+2||r.left<pr.left-2))
            res.overflow.push(name(e)+' out of '+name(pe)+' by '
              +Math.round(Math.max(r.right-pr.right,pr.left-r.left))+'px');}
      }
      for(const sel of ['.apptop','.mh-thread-toolbar','.mh-filterbar','.mh-link-applicant']){
        const c=document.querySelector(sel); if(!c) continue;
        const kids=[...c.children].filter(k=>getComputedStyle(k).display!=='none');
        for(let i=0;i<kids.length;i++) for(let j=i+1;j<kids.length;j++){
          const a=kids[i].getBoundingClientRect(), d=kids[j].getBoundingClientRect();
          if(a.right>d.left+2&&d.right>a.left+2&&a.bottom>d.top+2&&d.bottom>a.top+2)
            res.overlap.push(sel+': '+name(kids[i])+' x '+name(kids[j]));}}
      for(const k in res) res[k]=[...new Set(res[k])].slice(0,10);
      return res;}""")
    bad = sum(len(v) for v in out.values())
    for key, items in out.items():
        print(f"  {key} ({len(items)})")
        for i in items:
            print(f"      {i}")
    return bad == 0


def check_geom(page, base, width):
    """Top-bar and workspace geometry."""
    _open_first_thread(page, base)
    g = page.evaluate("""()=>{
      const b=s=>{const e=document.querySelector(s); if(!e) return null;
        const r=e.getBoundingClientRect();
        return {x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),
                h:Math.round(r.height),cx:Math.round(r.x+r.width/2)};};
      const bar=b('.apptop'), sw=b('.appswitch-wrap'), ws=b('.mh-workspace'),
            comp=b('.mh-composer');
      return {
        topbar:bar, switcher:sw,
        // Centre against the BAR, not the viewport: the bar starts after the rail.
        switcherOffCentre: (bar&&sw)?Math.abs(sw.cx-bar.cx):null,
        workspaceOverflow: ws?(ws.y+ws.h)-innerHeight:null,
        composerOnScreen: comp?(comp.y+comp.h)<=innerHeight+2:null,
        horizOverflow: document.body.scrollWidth-innerWidth,
      };}""")
    for k, v in g.items():
        print(f"  {k}: {v}")
    return (g["switcherOffCentre"] in (0, None)
            and (g["workspaceOverflow"] or 0) <= 0
            and g["horizOverflow"] <= 0
            and g["composerOnScreen"] is not False)


def check_clutter(page, base, width):
    """How much chrome stands between opening the inbox and reading a message."""
    _open_first_thread(page, base)
    out = page.evaluate("""()=>{
      const vis=e=>e.getBoundingClientRect().width>0;
      const count=(sel,within)=>{const r=document.querySelector(within);
        return r?[...r.querySelectorAll(sel)].filter(vis).length:0;};
      const ctl='button,select,input,textarea,a[href]';
      const rows=['.mh-thread-toolbar','.mh-queue-head','.mh-filterbar']
        .filter(s=>{const e=document.querySelector(s);return e&&vis(e);});
      return {topbar: !!document.querySelector('.apptop'),
              header:count(ctl,'.mh-thread-toolbar'),
              filters:count(ctl,'.mh-filterbar'),
              conversation:count('button,select,input','.mh-conversation-head'),
              composer:count(ctl,'.mh-composer'),
              rowsAboveList:rows};}""")
    total = out["header"] + out["filters"] + out["conversation"] + out["composer"]
    for k, v in out.items():
        print(f"  {k}: {v}")
    print(f"  TOTAL controls before reading a message: {total}")
    return True


FEATURES = [
    ("Centralize sources", "/app/inbox", '[data-ui-open="sourceModal"]'),
    ("Bridget (AI)", "/app/inbox", "#copilot"),
    ("Handoff / away", "/app/inbox", '[data-ui-open="coverageModal"]'),
    ("Tagging @mentions", "/app/inbox", 'a[href*="/app/mentions"]'),
    ("Trial picker", "/app/inbox", "#studySwitch .appswitch"),
    ("Global search", "/app/inbox", "#omniInput"),
    ("Blast", "/app/leads", '[data-ui-open="broadcastModal"]'),
]


def check_access(page, base, width):
    """Every core feature: present, visible, and actually clickable."""
    ok = True
    for name, path, sel in FEATURES:
        page.goto(base + path, wait_until="load")
        page.wait_for_timeout(450)
        i = _probe(page, sel)
        # A control parked in a closed overflow menu measures as "covered".
        # That is not unreachable, it is one click deeper - so open the menu
        # and re-probe rather than reporting a defect that isn't there.
        if i.get("found") and not i.get("clickable"):
            page.evaluate("""(sel)=>{const e=document.querySelector(sel);
                const d=e&&e.closest('details'); if(d) d.open=true;}""", sel)
            page.wait_for_timeout(250)
            i = _probe(page, sel)
            if i.get("clickable"):
                i["label"] = (i.get("label") or "") + " [in overflow menu]"
        good = i.get("found") and i.get("vis") and i.get("clickable")
        ok = ok and bool(good)
        print(f"  {name:<22} found={i.get('found')} vis={i.get('vis')} "
              f"clickable={i.get('clickable')}" + ("" if good else "   <-- UNREACHABLE"))

    # Records + portal need a thread with a linked applicant AND an unconsumed
    # pull form - the records pull is one-shot per applicant, so probe several.
    target = None
    for t in (30, 33, 2, 12, 18, 21, 24, 36, 40):
        page.goto(f"{base}/app/inbox?thread={t}", wait_until="load")
        page.wait_for_timeout(350)
        if page.query_selector("[data-record-pull]"):
            target = t
            break
    if target is None:
        print("  Records / portal        no unpulled applicant left "
              "(records pull is one-shot; reseed the demo DB)")
        return ok
    clicks = 1
    rec = _probe(page, "[data-record-pull] button[type=submit]")
    if rec.get("found") and not rec.get("vis"):
        # Below ~1600px the applicant rail moves behind a tab by design.
        for btn in page.query_selector_all(".mh-detail-tabs button"):
            if "applicant" in (btn.inner_text() or "").lower():
                btn.click()
                page.wait_for_timeout(400)
                clicks = 2
                break
        rec = _probe(page, "[data-record-pull] button[type=submit]")
    por = _probe(page, '#portal')
    print(f"  {'Records / consent':<22} found={rec.get('found')} vis={rec.get('vis')} "
          f"clickable={rec.get('clickable')} clicks={clicks} (thread {target})")
    print(f"  {'Patient portal':<22} found={por.get('found')} vis={por.get('vis')} "
          f"clickable={por.get('clickable')} clicks={clicks}")
    return ok


CHECKS = {"smell": check_smell, "geom": check_geom,
          "clutter": check_clutter, "access": check_access}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("checks", nargs="*", default=[],
                    help=f"any of: {', '.join(CHECKS)} (default: all)")
    ap.add_argument("--base", default="http://127.0.0.1:5055")
    ap.add_argument("--width", type=int, default=1512,
                    help="viewport width; the applicant rail behaves "
                         "differently above/below ~1600")
    args = ap.parse_args()
    names = args.checks or list(CHECKS)
    bad = [n for n in names if n not in CHECKS]
    if bad:
        return ap.error(f"unknown check(s): {', '.join(bad)}")

    failed = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(
            viewport={"width": args.width, "height": 900}).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)[:160]))
        for name in names:
            print(f"\n== {name} == ({args.base} @ {args.width}px)")
            try:
                if not CHECKS[name](page, args.base, args.width):
                    failed.append(name)
            except Exception as exc:  # noqa: BLE001 - report, don't abort the run
                print(f"  ERROR: {type(exc).__name__}: {exc}"[:200])
                failed.append(name)
        browser.close()
    if errors:
        print(f"\nJS page errors ({len(errors)}):")
        for e in dict.fromkeys(errors):
            print("   ", e)
    print("\n" + ("FAIL: " + ", ".join(failed) if failed else "OK: no defects found"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
