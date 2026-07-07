#!/usr/bin/env python3
"""Self-serve web UI for the trial matcher: paste a de-identified note ->
ranked recruiting trials -> generate a tracked referral. Stdlib only.

Run:
  export OPENAI_API_KEY="sk-..."   # or LLM_API_KEY / OPEN_API_KEY
  export LLM_MODEL="gpt-4o-mini"
  python3 app.py                    # http://localhost:8000
"""
import html
import http.server
import socketserver
import urllib.parse

import match_trials as mt
import refer

PORT = 8000
MAX_LLM = 8

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trial finder</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#f6f7f8;color:#111827;
font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}}
.wrap{{max-width:680px;margin:0 auto;padding:48px 20px 96px}}
h1{{font-size:22px;font-weight:650;margin:0 0 6px;letter-spacing:-.01em}}
.sub{{color:#6b7280;margin:0 0 28px;font-size:15px}}
.card{{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:20px}}
label{{display:block;font-weight:600;font-size:13px;color:#374151;margin:0 0 8px}}
textarea,input[type=text]{{width:100%;border:1px solid #d1d5db;border-radius:9px;
padding:12px 13px;font-size:15px;font-family:inherit;color:#111827;background:#fff}}
textarea{{min-height:200px;resize:vertical;line-height:1.55}}
textarea:focus,input:focus{{outline:none;border-color:#111827}}
.hint{{color:#9ca3af;font-weight:400}}
.rowline{{display:flex;gap:12px;align-items:center;margin-top:14px;flex-wrap:wrap}}
.rowline input[type=text]{{flex:1;min-width:160px}}
.chk{{display:flex;align-items:center;gap:8px;color:#6b7280;font-size:14px;margin-top:14px}}
button{{background:#111827;color:#fff;border:0;border-radius:9px;padding:12px 20px;
font-size:15px;font-weight:600;cursor:pointer;margin-top:18px}}
button:hover{{background:#000}}
button.link{{background:none;color:#2563eb;padding:0;margin:0;font-weight:600;font-size:14px}}
button.link:hover{{background:none;text-decoration:underline}}
.summary{{color:#6b7280;font-size:14px;margin:22px 0 14px}}
.trial{{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:18px 20px;
margin-bottom:14px}}
.trial h3{{font-size:16px;font-weight:640;margin:8px 0 6px;line-height:1.35}}
.verdict{{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600}}
.dot{{width:9px;height:9px;border-radius:50%;display:inline-block}}
.d-likely_eligible{{background:#16a34a}} .v-likely_eligible{{color:#15803d}}
.d-possible{{background:#d97706}} .v-possible{{color:#b45309}}
.d-unlikely,.d-error{{background:#dc2626}} .v-unlikely,.v-error{{color:#b91c1c}}
.sc{{color:#9ca3af;font-weight:500}}
.meta{{color:#6b7280;font-size:13.5px;margin:3px 0}}
.mono{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px}}
.why{{color:#374151;font-size:14.5px;margin:12px 0 6px}}
.blk{{margin:10px 0 2px;font-weight:600;font-size:13px;color:#374151}}
ul{{margin:4px 0 0;padding-left:18px}} li{{margin:3px 0;color:#4b5563;font-size:14px}}
.blockers li{{color:#b91c1c}}
details{{margin-top:8px}} summary{{cursor:pointer;color:#6b7280;font-size:13px}}
a{{color:#2563eb;text-decoration:none}} a:hover{{text-decoration:underline}}
.banner{{background:#fef3c7;border:1px solid #fde68a;color:#92400e;border-radius:9px;
padding:10px 13px;font-size:13.5px;margin-bottom:20px}}
pre{{white-space:pre-wrap;font-family:ui-monospace,monospace;font-size:13px;
color:#374151;margin:0}}
</style></head><body><div class="wrap">{body}</div></body></html>"""


def esc(s):
    return html.escape(str(s or ""))


def form_html(note="", condition="", country="Canada", rs=True):
    banner = ("" if mt.LLM_API_KEY else
              '<div class="banner">Eligibility scoring is off (no API key found). '
              "You'll still get the age/sex-screened trial list.</div>")
    return f"""
<h1>Find a trial for your patient</h1>
<p class="sub">Paste a de-identified summary. No names, MRNs, or dates of birth.</p>
{banner}
<form class="card" method="post" action="/match">
  <label>Patient summary</label>
  <textarea name="note" placeholder="e.g. 34F, biopsy-confirmed Class IV lupus nephritis, failed mycophenolate and azathioprine, no prior biologic, eGFR 62, active disease...">{esc(note)}</textarea>
  <div class="rowline">
    <input type="text" name="condition" placeholder="Condition (optional \u2014 auto-detected)" value="{esc(condition)}">
    <input type="text" name="country" value="{esc(country)}" placeholder="Country">
  </div>
  <label class="chk"><input type="checkbox" name="require_site" value="1" {'checked' if rs else ''}>
    Only trials with a site in that country</label>
  <button type="submit">Find trials</button>
</form>
"""


def detect_condition(note):
    if not mt.LLM_API_KEY:
        return ""
    try:
        return mt.llm_chat(
            "Extract the single primary medical condition to search clinical "
            "trials for, from this patient summary. Reply with ONLY a short "
            "search phrase (e.g. 'lupus nephritis'), no punctuation or extra words.",
            note)[:80].strip().strip('."')
    except Exception:
        return ""


def run_pipeline(note, condition, country, require_site):
    profile = mt.patient_profile(note)
    trials = mt.fetch_trials(condition, max_n=300)
    n_fetched = len(trials)
    if country and require_site:
        trials = [t for t in trials if mt.sites_in_country(t, country)]
    elif country:
        trials.sort(key=lambda t: not bool(mt.sites_in_country(t, country)))
    candidates, gated = [], []
    for t in trials:
        ok, _ = mt.hard_gate(t, profile)
        (candidates if ok else gated).append(t)
    candidates = candidates[:MAX_LLM]
    results = []
    for t in candidates:
        m = None
        if mt.LLM_API_KEY:
            try:
                m = mt.llm_match(note, t)
            except Exception as e:
                m = {"verdict": "error", "score": 0, "rationale": str(e)[:120],
                     "met": [], "not_met": [], "unknown": []}
        results.append((t, m))

    def rank_key(tm):
        t, m = tm
        has_local = bool(mt.sites_in_country(t, country)) if country else True
        obs = (t.get("studyType") or "").upper() == "OBSERVATIONAL"
        m = m or {}
        return (mt.VERDICT_RANK.get(m.get("verdict"), 2), 0 if has_local else 1,
                0 if not obs else 1, len(m.get("not_met") or []),
                len(m.get("unknown") or []), -int(m.get("score") or 0))

    results.sort(key=rank_key)
    return profile, results, n_fetched, len(gated)


VLABEL = {"likely_eligible": "Likely eligible", "possible": "Possible",
          "unlikely": "Unlikely", "error": "Error"}


def trial_html(t, m, country):
    site = refer.best_site(t, country)
    s = site or {}
    site_str = ", ".join(p for p in (s.get("facility"), s.get("city"),
                                     s.get("state")) if p)
    coord = next((c for c in s.get("contacts", [])
                  if c.get("role") != "PRINCIPAL_INVESTIGATOR"), None)
    obs = (t.get("studyType") or "").upper() == "OBSERVATIONAL"
    ttype = "registry (not a treatment)" if obs else f"phase {t['phase'] or 'NA'}"
    p = ['<div class="trial">']
    if m:
        v = m.get("verdict", "possible")
        p.append(f'<div class="verdict"><span class="dot d-{esc(v)}"></span>'
                 f'<span class="v-{esc(v)}">{esc(VLABEL.get(v, v))}</span>'
                 f'<span class="sc">· {esc(m.get("score"))}/100</span></div>')
    p.append(f'<h3>{esc(t["title"])}</h3>')
    p.append(f'<p class="meta"><span class="mono">{esc(t["nctId"])}</span> · '
             f'{esc(ttype)} · '
             f'<a href="https://clinicaltrials.gov/study/{esc(t["nctId"])}" '
             f'target="_blank">ClinicalTrials.gov</a></p>')
    if site_str:
        st = f' · {esc(s.get("status"))}'.lower() if s.get("status") else ""
        p.append(f'<p class="meta">{esc(site_str)}{st}</p>')
    if coord:
        ext = f" x{esc(coord['phoneExt'])}" if coord.get("phoneExt") else ""
        p.append(f'<p class="meta">{esc(coord.get("name"))} · '
                 f'{esc(coord.get("phone"))}{ext} · {esc(coord.get("email"))}</p>')
    if m and m.get("rationale"):
        p.append(f'<p class="why">{esc(m["rationale"])}</p>')
    if m and m.get("not_met"):
        p.append('<div class="blk">Potential blockers</div><ul class="blockers">'
                 + "".join(f"<li>{esc(x)}</li>" for x in m["not_met"][:5]) + "</ul>")
    if m and m.get("unknown"):
        p.append('<div class="blk">Confirm with patient</div><ul>'
                 + "".join(f"<li>{esc(x)}</li>" for x in m["unknown"][:5]) + "</ul>")
    if m and m.get("met"):
        p.append("<details><summary>Appears to meet ("
                 + str(len(m["met"])) + ")</summary><ul>"
                 + "".join(f"<li>{esc(x)}</li>" for x in m["met"][:8]) + "</ul></details>")
    if not m:
        p.append('<p class="meta">Passed age/sex screen \u2014 add an API key to score.</p>')
    p.append(f'<form method="post" action="/refer" style="margin-top:14px">'
             f'<input type="hidden" name="nct" value="{esc(t["nctId"])}">'
             f'<input type="hidden" name="note" value="{esc(t["_note"])}">'
             f'<input type="hidden" name="country" value="{esc(country)}">'
             f'<button class="link" type="submit">Generate referral \u2192</button></form>')
    p.append("</div>")
    return "".join(p)


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, body, code=200):
        data = PAGE.format(body=body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.split("?")[0] == "/":
            self._send(form_html())
        else:
            self._send("<h1>Not found</h1>", 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        form = urllib.parse.parse_qs(self.rfile.read(n).decode())
        g = lambda k, d="": form.get(k, [d])[0]

        if self.path == "/match":
            note, country = g("note"), g("country", "Canada")
            rs = bool(g("require_site"))
            condition = g("condition").strip() or detect_condition(note)
            if not note.strip():
                self._send(form_html(note, condition, country, rs))
                return
            if not condition:
                self._send('<h1>Need a condition</h1><p class="sub">Couldn\'t '
                           'auto-detect it \u2014 add one and try again.</p>'
                           + form_html(note, "", country, rs))
                return
            profile, results, nf, ng = run_pipeline(note, condition, country, rs)
            for t, _ in results:
                t["_note"] = note
            cards = "".join(trial_html(t, m, country) for t, m in results) \
                or '<div class="trial">No trials passed screening.</div>'
            summ = (f'<p class="summary">{esc(condition)} · screened {nf} recruiting '
                    f'trials, ruled out {ng} by age/sex, showing {len(results)}.</p>')
            back = ('<form method="post" action="/back" style="display:inline">'
                    f'<input type=hidden name=note value="{esc(note)}">'
                    f'<input type=hidden name=condition value="{esc(condition)}">'
                    f'<input type=hidden name=country value="{esc(country)}">'
                    '<button class="link">\u2190 New search</button></form>')
            self._send(f"<h1>Trials for this patient</h1>{summ}{back}{cards}")

        elif self.path == "/back":
            self._send(form_html(g("note"), g("condition"), g("country", "Canada")))

        elif self.path == "/refer":
            self._refer(g("nct"), g("note"), g("country", "Canada"))
        else:
            self._send("<h1>Not found</h1>", 404)

    def _refer(self, nct, note, country):
        trial = mt.fetch_study(nct)
        match = None
        if mt.LLM_API_KEY:
            try:
                match = mt.llm_match(note, trial)
            except Exception:
                pass
        site = refer.best_site(trial, country)
        rows = refer.read_ledger()
        ref_id = refer.next_ref_id(rows)
        packet = refer.build_packet(ref_id, trial, site, match, note, "", country)
        refer.PACKETS.mkdir(exist_ok=True)
        (refer.PACKETS / f"{ref_id}.md").write_text(packet)
        prof = mt.patient_profile(note)
        tag = f"{int(prof['age']) if prof['age'] else '?'}{(prof['sex'] or '?')[0].upper()}"
        s = site or {}
        rows.append({"ref_id": ref_id, "created": refer.now(), "nct": trial["nctId"],
                     "title": trial["title"][:80], "patient": tag, "physician": "",
                     "site": ", ".join(x for x in (s.get("facility"), s.get("city"))
                                       if x), "status": "referred",
                     "last_update": refer.now()})
        refer.write_ledger(rows)
        refer.log_event(ref_id, "referred", "created via web")
        self._send(f"<h1>Referral {esc(ref_id)} created</h1>"
                   f'<p class="summary">Saved and logged (status: referred).</p>'
                   f'<div class="trial"><pre>{esc(packet)}</pre></div>'
                   '<a href="/">\u2190 Back</a>')

    def log_message(self, *a):
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    with Server(("", PORT), Handler) as httpd:
        print(f"Trial finder -> http://localhost:{PORT}")
        print(f"LLM matching: {'ON' if mt.LLM_API_KEY else 'OFF (set API key)'}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nbye")


if __name__ == "__main__":
    main()
