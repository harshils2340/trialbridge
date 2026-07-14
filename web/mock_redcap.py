"""A tiny stand-in for a real REDCap server, for testing BridgeMD's REDCap
integration end to end WITHOUT an institutional account.

It speaks the exact slice of the REDCap API that ``redcap.py`` calls:

  * ``content=instrument``  -> list the project's instruments/surveys
  * ``content=record`` (import, returnContent=count|ids) -> create/update a record
  * ``content=surveyLink``  -> return a unique survey URL for a record+instrument
  * ``content=record`` (export of ``<instrument>_complete``) -> completion status

It also HOSTS the survey itself: a real, pre-filled HTML form the patient fills
in inside BridgeMD's embedded iframe. On submit it marks the record complete and
fires the same webhook a real REDCap "Data Entry Trigger" / survey-completion
callback would fire back to BridgeMD. So the whole live path runs for real -
nothing in the app is stubbed.

Run it:

    cd matcher/web
    MOCK_REDCAP_TOKEN=TESTTOKEN ../.venv/bin/python mock_redcap.py

Then in BridgeMD Site setup -> REDCap:
    API URL:  http://127.0.0.1:5098/api/
    Token:    (whatever MOCK_REDCAP_TOKEN prints on startup)

This is a TEST DOUBLE. It stores records in memory and does no auth beyond a
single shared token. Never point it at, or feed it, real PHI.
"""
import json
import os
import urllib.parse
import urllib.request

from flask import Flask, Response, abort, request

app = Flask(__name__)

# --- Config ---------------------------------------------------------------- #
PORT = int(os.environ.get("MOCK_REDCAP_PORT", "5098"))
API_TOKEN = os.environ.get("MOCK_REDCAP_TOKEN", "TESTTOKEN")
# Where a real REDCap would call back on survey completion (Data Entry Trigger).
WEBHOOK_URL = os.environ.get(
    "BRIDGEMD_WEBHOOK_URL", "http://127.0.0.1:5099/integrations/redcap/webhook")
WEBHOOK_SECRET = os.environ.get("REDCAP_WEBHOOK_SECRET", "").strip()
PUBLIC_BASE = os.environ.get("MOCK_REDCAP_BASE", f"http://127.0.0.1:{PORT}")

# The project's instruments (unique_name -> human label), mirroring a real
# project. The first is a survey used for intake/screening.
INSTRUMENTS = [
    {"instrument_name": "patient_intake", "instrument_label": "Patient Intake & Consent"},
    {"instrument_name": "screening_eligibility", "instrument_label": "Screening Eligibility Checklist"},
    {"instrument_name": "medical_history", "instrument_label": "Medical History"},
]

# In-memory record store: record_id -> {field: value}. A stand-in for the DB a
# real REDCap keeps.
RECORDS = {}


# --- Helpers --------------------------------------------------------------- #
def _check_token():
    tok = request.form.get("token", "")
    if not tok or tok != API_TOKEN:
        # REDCap answers a bad token with 403 + a plain message.
        abort(Response("ERROR: You do not have API privileges (bad token).",
                       status=403, mimetype="text/plain"))


def _json(payload):
    return Response(json.dumps(payload), mimetype="application/json")


def _label_for(name):
    for i in INSTRUMENTS:
        if i["instrument_name"] == name:
            return i["instrument_label"]
    return name.replace("_", " ").title()


# --- REDCap API endpoint --------------------------------------------------- #
@app.route("/api/", methods=["POST"])
@app.route("/api", methods=["POST"])
def api():
    _check_token()
    content = request.form.get("content", "")

    # 1) List instruments/surveys.
    if content == "instrument":
        return _json(INSTRUMENTS)

    # 2) Import a record (create/update) with pre-filled fields.
    if content == "record":
        # Export of completion status (has records[]/fields[]).
        rid = request.form.get("records[0]", "").strip()
        field = request.form.get("fields[0]", "").strip()
        if rid and field:
            rec = RECORDS.get(rid, {})
            return _json([{field: str(rec.get(field, "0")), "record_id": rid}])
        # Otherwise it's an import: data is a JSON list of record dicts.
        try:
            rows = json.loads(request.form.get("data", "[]"))
        except (ValueError, TypeError):
            rows = []
        ids = []
        for row in rows:
            rid = str(row.get("record_id") or (len(RECORDS) + 1))
            stored = dict(RECORDS.get(rid, {}))
            stored.update({k: v for k, v in row.items()})
            stored.setdefault("record_id", rid)
            RECORDS[rid] = stored
            ids.append(rid)
        if request.form.get("returnContent") == "ids":
            return _json(ids)
        return _json({"count": len(ids)})

    # 3) Mint a unique survey link for a record + instrument.
    if content == "surveyLink":
        rid = request.form.get("record", "").strip()
        instrument = request.form.get("instrument", "").strip() or "patient_intake"
        RECORDS.setdefault(rid, {"record_id": rid})
        # surveyLink returns the raw URL as the body (not JSON).
        return Response(f"{PUBLIC_BASE}/survey/{instrument}/{rid}",
                        mimetype="text/plain")

    return Response(f"ERROR: unsupported content '{content}'.",
                    status=400, mimetype="text/plain")


# --- The hosted survey (what the patient actually fills in) ----------------- #
_SURVEY_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{label}</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
    background: #f4f6f8; color: #1a1a1a; margin: 0; padding: 24px; }}
  .rc {{ max-width: 640px; margin: 0 auto; background: #fff; border: 1px solid #dcdfe3;
    border-radius: 6px; overflow: hidden; }}
  .rc-top {{ background: #900; color: #fff; padding: 12px 20px; font-size: 13px; }}
  .rc-top b {{ font-size: 15px; display: block; }}
  .rc-body {{ padding: 22px 24px; }}
  .rc-body h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .rc-note {{ color: #667; font-size: 13px; margin: 0 0 18px; }}
  label {{ display: block; font-weight: 600; font-size: 13px; margin: 14px 0 5px; }}
  input[type=text], input[type=email], select {{ width: 100%; padding: 9px 10px;
    border: 1px solid #b9c0c7; border-radius: 4px; font-size: 14px; box-sizing: border-box; }}
  .rc-prefill {{ background: #eaf4ff; border: 1px solid #bfe0; border-color: #b6d8f5;
    color: #245; font-size: 12.5px; padding: 8px 11px; border-radius: 4px; margin-bottom: 8px; }}
  .rc-q {{ margin: 16px 0; }}
  .rc-q > span {{ font-weight: 600; font-size: 13px; }}
  .rc-opt {{ display: block; font-weight: 400; margin: 6px 0; font-size: 14px; }}
  .rc-submit {{ margin-top: 22px; background: #0a7; color: #fff; border: 0; padding: 11px 20px;
    border-radius: 4px; font-size: 15px; font-weight: 600; cursor: pointer; }}
  .rc-req {{ color: #c00; }}
</style></head>
<body>
  <div class="rc">
    <div class="rc-top"><b>{label}</b>Survey · record {rid} · powered by REDCap (mock)</div>
    <form class="rc-body" method="post" action="/survey/{instrument}/{rid}">
      <h1>{label}</h1>
      <p class="rc-note">Please review your details and complete the questions below.
        <span class="rc-req">* required</span></p>
      <div class="rc-prefill">Pre-filled by your study team from your application.</div>

      <label>First name</label>
      <input type="text" name="first_name" value="{first_name}">
      <label>Last name</label>
      <input type="text" name="last_name" value="{last_name}">
      <label>Email</label>
      <input type="email" name="email" value="{email}">
      <label>Condition</label>
      <input type="text" name="condition" value="{condition}">
      <label>Trial (NCT)</label>
      <input type="text" name="nct" value="{nct}">

      <div class="rc-q">
        <span>1. Are you currently taking any investigational drugs? <span class="rc-req">*</span></span>
        <label class="rc-opt"><input type="radio" name="q_invest" value="no" required> No</label>
        <label class="rc-opt"><input type="radio" name="q_invest" value="yes"> Yes</label>
      </div>
      <div class="rc-q">
        <span>2. Can you attend in-person visits at the study site? <span class="rc-req">*</span></span>
        <label class="rc-opt"><input type="radio" name="q_visits" value="yes" required> Yes</label>
        <label class="rc-opt"><input type="radio" name="q_visits" value="no"> No</label>
      </div>
      <div class="rc-q">
        <label>3. Anything else the study team should know?</label>
        <input type="text" name="q_notes" placeholder="Optional">
      </div>

      <button class="rc-submit" type="submit">Submit survey</button>
    </form>
  </div>
</body></html>"""

_DONE_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Response recorded</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
    background: #f4f6f8; margin: 0; padding: 48px 24px; text-align: center; color: #1a1a1a; }}
  .rc {{ max-width: 520px; margin: 0 auto; background: #fff; border: 1px solid #dcdfe3;
    border-radius: 6px; padding: 36px 28px; }}
  .rc h1 {{ font-size: 22px; margin: 0 0 8px; color: #0a7; }}
  .rc p {{ color: #556; font-size: 14px; }}
</style></head>
<body><div class="rc">
  <h1>&#10003; Thank you!</h1>
  <p>Your response for record {rid} was recorded. You can close this window; the
     study team has been notified.</p>
</div></body></html>"""


@app.route("/survey/<instrument>/<rid>", methods=["GET"])
def survey(instrument, rid):
    rec = RECORDS.get(rid, {"record_id": rid})
    html = _SURVEY_PAGE.format(
        label=_label_for(instrument), instrument=instrument, rid=rid,
        first_name=rec.get("first_name", ""), last_name=rec.get("last_name", ""),
        email=rec.get("email", ""), condition=rec.get("condition", ""),
        nct=rec.get("nct", ""))
    # No X-Frame-Options / frame-ancestors -> embeddable in BridgeMD's iframe.
    return Response(html, mimetype="text/html")


@app.route("/survey/<instrument>/<rid>", methods=["POST"])
def survey_submit(instrument, rid):
    rec = dict(RECORDS.get(rid, {"record_id": rid}))
    for k, v in request.form.items():
        rec[k] = v
    rec[f"{instrument}_complete"] = "2"  # 2 == Complete
    RECORDS[rid] = rec
    _fire_webhook(rid, instrument)
    return Response(_DONE_PAGE.format(rid=rid), mimetype="text/html")


def _fire_webhook(rid, instrument):
    """Server-to-server callback, exactly like a REDCap Data Entry Trigger."""
    fields = {"record": rid, "instrument": instrument,
              f"{instrument}_complete": "2"}
    url = WEBHOOK_URL
    if WEBHOOK_SECRET:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}secret={urllib.parse.quote(WEBHOOK_SECRET)}"
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"User-Agent": "REDCap-Mock/1.0"})
    if WEBHOOK_SECRET:
        req.add_header("X-Redcap-Token", WEBHOOK_SECRET)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"[mock-redcap] webhook -> {url} : {r.status}")
    except Exception as e:  # noqa: BLE001 - test tool, just log
        print(f"[mock-redcap] webhook failed: {e}")


@app.route("/")
def home():
    return Response(
        "Mock REDCap is running.\n"
        f"API:   POST {PUBLIC_BASE}/api/  (token required)\n"
        f"Token: {API_TOKEN}\n"
        f"Records in memory: {len(RECORDS)}\n",
        mimetype="text/plain")


if __name__ == "__main__":
    print("=" * 60)
    print(" Mock REDCap server")
    print(f"   API URL : {PUBLIC_BASE}/api/")
    print(f"   Token   : {API_TOKEN}")
    print(f"   Webhook : {WEBHOOK_URL}")
    print(" Paste the API URL + Token into BridgeMD Site setup -> REDCap.")
    print("=" * 60)
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)
