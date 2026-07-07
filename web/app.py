#!/usr/bin/env python3
"""TrialBridge — a doctor-facing wrapper on ClinicalTrials.gov.

Paste a de-identified patient note, get ranked recruiting trials with a plain
explanation of the fit, then refer a patient in one click and track that
referral through the pipeline (referred -> contacted -> screened -> enrolled)
for commission/attribution.

Run:
    cd matcher
    export LLM_API_KEY="sk-..."          # OpenAI-compatible (optional; see below)
    .venv/bin/python web/app.py          # http://127.0.0.1:5000

Without an LLM key the app still fetches + gates trials (deterministic age/sex
screening) but skips the per-trial eligibility reasoning.
"""
import functools
import json
import math
import os
import pathlib
import secrets
import sys
import urllib.parse
import urllib.request

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

# Reuse the matching engine + referral helpers from the parent package.
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import match_trials as mt  # noqa: E402
import refer as rf  # noqa: E402

import db  # noqa: E402
import fhir  # noqa: E402
import ingest  # noqa: E402
import mailer  # noqa: E402

app = Flask(__name__)

# Stable secret so sessions survive restarts. Prefer an env var (set this on any
# host so logins survive redeploys); otherwise generate + store one locally.
_env_secret = os.environ.get("SECRET_KEY", "").strip()
if _env_secret:
    app.secret_key = _env_secret
else:
    _secret_file = HERE / ".secret"
    if not _secret_file.exists():
        _secret_file.write_text(secrets.token_hex(32))
    app.secret_key = _secret_file.read_text().strip()

# Behind a hosting proxy (Render/Railway/Fly), trust X-Forwarded-* so redirects and
# secure cookies work over HTTPS.
if os.environ.get("BEHIND_PROXY", "0") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

MAX_MATCH = int(os.environ.get("WEB_MAX_MATCH", "6"))  # LLM calls per search
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024    # 16 MB upload cap
app.teardown_appcontext(db.close_db)

# Create tables on import so the app is safe under any launcher (flask run, wsgi).
db.init_db()


@app.template_filter("money")
def money(cents):
    return f"${(cents or 0) / 100:,.0f}"


# --------------------------------------------------------------------------- #
# Error handlers — never show a raw stack trace to a doctor.
# --------------------------------------------------------------------------- #
@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", code=404,
                           msg="That page doesn't exist."), 404


@app.errorhandler(413)
def too_large(_e):
    return render_template("error.html", code=413,
                           msg="That file is too large (16 MB max)."), 413


@app.errorhandler(405)
def bad_method(_e):
    return render_template("error.html", code=405,
                           msg="That action isn't allowed here."), 405


@app.errorhandler(500)
def server_error(_e):
    app.logger.exception("Unhandled error")
    return render_template("error.html", code=500,
                           msg="Something went wrong on our end. Please try again."), 500


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **k):
        if not g.user:
            return redirect(url_for("login", next=request.path))
        return view(*a, **k)
    return wrapped


@app.before_request
def load_user():
    uid = session.get("user_id")
    g.user = db.get_user(uid) if uid else None


@app.context_processor
def inject_globals():
    return {"user": g.user, "llm_on": bool(mt.LLM_API_KEY)}


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        f = request.form
        email = f.get("email", "").strip().lower()
        pw = f.get("password", "")
        name = f.get("name", "").strip()
        err = None
        if not (email and pw and name):
            err = "Name, email, and password are required."
        elif len(pw) < 8:
            err = "Use a password of at least 8 characters."
        elif db.get_user_by_email(email):
            err = "An account with that email already exists."
        if err:
            flash(err, "error")
        else:
            pw_hash = generate_password_hash(pw, method="pbkdf2:sha256")
            uid = db.create_user(email, pw_hash, name,
                                 f.get("specialty", ""), f.get("institution", ""))
            session.clear()
            session["user_id"] = uid
            return redirect(url_for("dashboard"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        user = db.get_user_by_email(email)
        if user and check_password_hash(user["password_hash"], pw):
            session.clear()
            session["user_id"] = user["id"]
            nxt = request.args.get("next")
            return redirect(nxt if nxt and nxt.startswith("/") else url_for("dashboard"))
        flash("Wrong email or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------- #
# Dashboard (clinician app lives under /app; "/" is the public consumer site)
# --------------------------------------------------------------------------- #
@app.route("/app")
@login_required
def dashboard():
    referrals = db.list_referrals(g.user["id"])
    counts = db.status_counts(g.user["id"])
    enrolled, earned, pipeline = db.commission_summary(g.user["id"])
    return render_template(
        "dashboard.html", referrals=referrals[:8], counts=counts,
        total=len(referrals), enrolled=enrolled, earned=earned,
        pipeline=pipeline, statuses=db.STATUSES)


# --------------------------------------------------------------------------- #
# Public consumer site ("/") — the patient front door + programmatic SEO.
# The clinician tool lives under /app; sponsors/sites are the paying side.
# --------------------------------------------------------------------------- #
import re as _re2


def slugify(s):
    return _re2.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


# Beachhead vertical: metabolic / cardiometabolic — huge self-motivated consumer
# search demand and heavy sponsor spend. These seed the programmatic SEO pages.
VERTICAL = "Metabolic & weight"
SEED_CONDITIONS = [
    "Obesity", "Type 2 diabetes", "Weight loss", "Prediabetes",
    "Fatty liver disease (NAFLD/NASH)", "High cholesterol", "Metabolic syndrome",
    "High blood pressure", "PCOS", "Chronic kidney disease",
]
SEED_CITIES = [
    "Toronto, ON", "Vancouver, BC", "Montreal, QC", "Calgary, AB",
    "New York, NY", "Los Angeles, CA", "Chicago, IL", "Houston, TX",
    "Miami, FL", "Boston, MA",
]
# The GLP-1 / peptide trend: people search by drug name, not condition. These
# power intervention-based landing pages and are queried via CT.gov query.intr.
SEED_DRUGS = [
    "Semaglutide", "Tirzepatide", "Retatrutide", "Survodutide",
    "Orforglipron", "CagriSema", "Liraglutide", "Dulaglutide",
]
_COND_BY_SLUG = {slugify(c): c for c in SEED_CONDITIONS}
_CITY_BY_SLUG = {slugify(c): c for c in SEED_CITIES}
_DRUG_BY_SLUG = {slugify(d): d for d in SEED_DRUGS}


def build_patient_note(condition, age="", sex="", about=""):
    """Turn a patient's self-reported details into a note the matcher can read."""
    lines = []
    if str(age).strip():
        lines.append(f"AGE: {str(age).strip()}")
    if sex in ("male", "female"):
        lines.append(f"SEX: {sex}")
    lines.append(f"Condition: {condition}.")
    if about.strip():
        lines.append(about.strip())
    return "\n".join(lines)


@app.route("/")
def home():
    return render_template("landing.html", vertical=VERTICAL,
                           conditions=SEED_CONDITIONS[:8], drugs=SEED_DRUGS,
                           slugify=slugify)


@app.route("/find", methods=["GET", "POST"])
def find():
    """Public, no-login patient search. Reuses the clinician matching engine but
    renders patient-friendly results."""
    if request.method == "GET":
        return render_template(
            "find.html", condition_value=request.args.get("condition", ""),
            location_value=request.args.get("location", ""))

    condition = request.form.get("condition", "").strip()
    intervention = request.form.get("intervention", "").strip()
    location = request.form.get("location", "").strip()
    age = request.form.get("age", "").strip()
    sex = request.form.get("sex", "").strip()
    about = request.form.get("about", "").strip()
    try:
        radius = int(request.form.get("radius", "50") or 0)
    except ValueError:
        radius = 50

    label = condition or intervention
    if not label:
        flash("Tell us the condition or treatment you're looking for.", "error")
        return render_template("find.html", location_value=location)
    if not location:
        flash("Enter your city or postal code so we only show trials near you.",
              "error")
        return render_template("find.html", condition_value=condition)

    coords, unit = None, "km"
    lat_in = request.form.get("lat", "").strip()
    lon_in = request.form.get("lon", "").strip()
    if lat_in and lon_in:
        try:
            coords = (float(lat_in), float(lon_in))
            unit = units_for(request.form.get("cc", ""))
        except ValueError:
            coords = None
    if coords is None:
        try:
            geo = geocode(location)
        except Exception:
            geo = None
        if geo:
            coords = (geo[0], geo[1])
            unit = units_for(geo[2])

    note = build_patient_note(label, age, sex, about)
    try:
        detected, results = run_search(note, condition, "", False, coords,
                                       radius, unit, interventional_only=True,
                                       intervention=intervention)
    except RuntimeError as e:
        flash(str(e), "error")
        return render_template("find.html", condition_value=label,
                               location_value=location)
    except Exception:
        app.logger.exception("public find failed")
        flash("Search failed unexpectedly. Please try again.", "error")
        return render_template("find.html", condition_value=label,
                               location_value=location)

    return render_template("patient_results.html", results=results,
                           condition=label, location=location, unit=unit)


@app.route("/interest", methods=["POST"])
def interest():
    """Patient asks to be contacted about a specific trial -> consented lead."""
    f = request.form
    if not f.get("consent"):
        flash("Please check the consent box so a coordinator can contact you.",
              "error")
        return redirect(request.referrer or url_for("find"))
    name = f.get("name", "").strip()
    contact = (f.get("email", "").strip() or f.get("phone", "").strip())
    if not name or not contact:
        flash("Add your name and an email or phone so the site can reach you.",
              "error")
        return redirect(request.referrer or url_for("find"))
    db.create_lead({
        "nct": f.get("nct", "").strip(), "title": f.get("title", "").strip(),
        "condition": f.get("condition", "").strip(),
        "location": f.get("location", "").strip(),
        "site": f.get("site", "").strip(), "name": name,
        "email": f.get("email", "").strip(), "phone": f.get("phone", "").strip(),
        "age": f.get("age", "").strip(), "sex": f.get("sex", "").strip(),
        "notes": f.get("about", "").strip(), "consent": 1, "source": "web",
    })
    return render_template("thanks.html", title=f.get("title", ""),
                           nct=f.get("nct", ""))


@app.route("/trials/<slug>")
def condition_page(slug):
    condition = _COND_BY_SLUG.get(slug)
    if not condition:
        abort(404)
    trials = []
    try:
        raw = mt.fetch_trials(condition, max_n=40)
        raw = [t for t in raw
               if (t.get("overallStatus") or "RECRUITING").upper() == "RECRUITING"
               and (t.get("studyType") or "").upper() != "OBSERVATIONAL"]
        trials = raw[:12]
    except Exception:
        app.logger.exception("condition page fetch failed")
    return render_template("condition.html", condition=condition, city=None,
                           trials=trials, cities=SEED_CITIES,
                           conditions=SEED_CONDITIONS, slugify=slugify)


@app.route("/trials/<slug>/<city_slug>")
def condition_city_page(slug, city_slug):
    condition = _COND_BY_SLUG.get(slug)
    city = _CITY_BY_SLUG.get(city_slug)
    if not condition or not city:
        abort(404)
    return render_template("condition.html", condition=condition, city=city,
                           trials=[], cities=SEED_CITIES,
                           conditions=SEED_CONDITIONS, slugify=slugify)


@app.route("/app/leads")
@login_required
def leads():
    return render_template("leads.html", leads=db.list_leads(),
                           counts=db.lead_counts())


@app.route("/trials/drug/<slug>")
def drug_page(slug):
    drug = _DRUG_BY_SLUG.get(slug)
    if not drug:
        abort(404)
    trials = []
    try:
        raw = mt.fetch_trials("", max_n=40, intervention=drug)
        raw = [t for t in raw
               if (t.get("overallStatus") or "RECRUITING").upper() == "RECRUITING"
               and (t.get("studyType") or "").upper() != "OBSERVATIONAL"]
        trials = raw[:12]
    except Exception:
        app.logger.exception("drug page fetch failed")
    return render_template("drug.html", drug=drug, trials=trials,
                           drugs=SEED_DRUGS, conditions=SEED_CONDITIONS,
                           slugify=slugify)


@app.route("/robots.txt")
def robots():
    body = ("User-agent: *\nAllow: /\nSitemap: "
            + url_for("sitemap", _external=True) + "\n")
    return app.response_class(body, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap():
    urls = [url_for("home", _external=True), url_for("find", _external=True)]
    for c in SEED_CONDITIONS:
        urls.append(url_for("condition_page", slug=slugify(c), _external=True))
        for city in SEED_CITIES:
            urls.append(url_for("condition_city_page", slug=slugify(c),
                                city_slug=slugify(city), _external=True))
    for d in SEED_DRUGS:
        urls.append(url_for("drug_page", slug=slugify(d), _external=True))
    items = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           + items + "</urlset>")
    return app.response_class(xml, mimetype="application/xml")


# --------------------------------------------------------------------------- #
# Location: geocoding + distance so we can rank by "nearby"
# --------------------------------------------------------------------------- #
import re as _re

_CA_POSTAL = _re.compile(r"^([A-Za-z]\d[A-Za-z])\s*\d[A-Za-z]\d$")   # L6P 3N6
_CA_FSA = _re.compile(r"^[A-Za-z]\d[A-Za-z]$")                        # L6P
_US_ZIP = _re.compile(r"^\d{5}$")                                    # 90210


def _zippopotam(country, code):
    """Resolve a postal code via zippopotam.us. Returns (lat, lon, cc) or None."""
    try:
        req = urllib.request.Request(
            f"https://api.zippopotam.us/{country}/{urllib.parse.quote(code)}",
            headers={"User-Agent": "TrialBridge/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        p = (data.get("places") or [None])[0]
        if p:
            return float(p["latitude"]), float(p["longitude"]), country.upper()
    except Exception:
        pass
    return None


def _nominatim(query):
    """Free-text geocode. Returns (lat, lon, country_code) or None."""
    try:
        q = urllib.parse.urlencode({"q": query, "format": "json", "limit": "1",
                                    "addressdetails": "1"})
        req = urllib.request.Request(
            f"https://nominatim.openstreetmap.org/search?{q}",
            headers={"User-Agent": "TrialBridge/1.0 (clinical trial finder)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        if data:
            cc = (data[0].get("address", {}).get("country_code") or "").upper()
            return float(data[0]["lat"]), float(data[0]["lon"]), cc
    except Exception:
        pass
    return None


def geocode(place):
    """City / postal code / ZIP / address -> (lat, lon, country_code) or None.
    Postal codes are handled first (Nominatim is unreliable for them)."""
    p = (place or "").strip()
    if not p:
        return None
    up = p.upper()

    m = _CA_POSTAL.match(up)
    if m:
        r = _zippopotam("CA", m.group(1))
        if r:
            return r
    elif _CA_FSA.match(up):
        r = _zippopotam("CA", up)
        if r:
            return r
    elif _US_ZIP.match(up):
        r = _zippopotam("US", up)
        if r:
            return r

    r = _nominatim(p)
    if r:
        return r
    if _CA_POSTAL.match(up):
        return _nominatim(f"{up[:3]} {up[3:]}, Canada")
    return None


def _nominatim_reverse(lat, lon):
    """(lat, lon) -> (label, country_code). Best-effort human-readable place."""
    try:
        q = urllib.parse.urlencode({"lat": lat, "lon": lon, "format": "json",
                                    "zoom": "12", "addressdetails": "1"})
        req = urllib.request.Request(
            f"https://nominatim.openstreetmap.org/reverse?{q}",
            headers={"User-Agent": "TrialBridge/1.0 (clinical trial finder)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        a = data.get("address", {})
        city = (a.get("city") or a.get("town") or a.get("village")
                or a.get("municipality") or a.get("county") or "")
        parts = [p for p in (city, a.get("state"), a.get("country")) if p]
        label = ", ".join(parts) or data.get("display_name", "")
        return label, (a.get("country_code") or "").upper()
    except Exception:
        return "", ""


def units_for(country_code):
    """US uses miles; everyone else (incl. Canada) uses kilometres."""
    return "mi" if (country_code or "").upper() == "US" else "km"


@app.route("/geo/reverse")
@login_required
def geo_reverse():
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
    except ValueError:
        return jsonify({"ok": False}), 400
    label, cc = _nominatim_reverse(lat, lon)
    return jsonify({"ok": bool(label), "label": label, "cc": cc,
                    "unit": units_for(cc)})


def haversine(lat1, lon1, lat2, lon2, unit="km"):
    r = 3958.8 if unit == "mi" else 6371.0   # earth radius in mi / km
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _is_active_site(s):
    """A site the patient could actually enroll at (recruiting or unlabeled)."""
    return (s.get("status") or "").upper() in ("", "RECRUITING")


def nearby_sites(trial, lat, lon, unit, radius):
    """Active sites with geo within radius, each annotated with distance,
    sorted nearest-first. Returns [] if none."""
    out = []
    for s in trial.get("locations", []):
        if s.get("lat") is None or s.get("lon") is None or not _is_active_site(s):
            continue
        d = haversine(lat, lon, s["lat"], s["lon"], unit)
        if radius and d > radius:
            continue
        out.append({**s, "distance": d, "contact": _coordinator(trial, s)})
    out.sort(key=lambda x: x["distance"])
    return out


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
def run_search(note, condition, country, require_site, coords=None, radius=50,
               unit="km", interventional_only=True, intervention=""):
    """Fetch -> hard-gate -> LLM-score, ranked by relevance then distance.
    `radius`/`unit` define the geographic limit. By default only interventional
    (treatment) trials are kept — a doctor refers for therapy, not to a registry.
    `intervention` searches by drug name (e.g. "semaglutide") instead of/along
    with a condition. Returns (search_label, results)."""
    profile = mt.patient_profile(note)
    if not condition and not intervention and mt.LLM_API_KEY:
        try:
            condition = mt.llm_chat(
                "You extract the single primary medical condition to search "
                "clinical trials for. Reply with ONLY the condition name, no "
                "punctuation.", note[:4000]).strip().strip(".")
        except Exception:
            condition = ""
    search_label = condition or intervention
    if not search_label:
        return "", []

    geo = None
    if coords and radius:
        geo = f"distance({coords[0]},{coords[1]},{int(radius)}{unit})"
    try:
        trials = mt.fetch_trials(condition, max_n=200, geo=geo,
                                 intervention=intervention)
    except Exception:
        raise RuntimeError(
            "Couldn't reach ClinicalTrials.gov right now. Please try again "
            "in a moment.")

    # Only genuinely active studies: recruiting overall (belt-and-suspenders on
    # top of the API filter), and interventional by default.
    trials = [t for t in trials
              if (t.get("overallStatus") or "RECRUITING").upper() == "RECRUITING"]
    if interventional_only:
        trials = [t for t in trials
                  if (t.get("studyType") or "").upper() != "OBSERVATIONAL"]

    # Country filter only matters when we're not already geo-scoping.
    if not coords and country and require_site:
        trials = [t for t in trials if mt.sites_in_country(t, country)]
    elif not coords and country:
        trials.sort(key=lambda t: not bool(mt.sites_in_country(t, country)))

    gated = [t for t in trials if mt.hard_gate(t, profile)[0]]

    # Pick which trials to screen. With a location, keep only trials with a
    # RECRUITING site inside the radius, screen the closest first — so we never
    # surface something across the country or a site that isn't enrolling.
    picks = []  # list of (trial, sites_nearby)
    if coords:
        within = []
        for t in gated:
            near = nearby_sites(t, coords[0], coords[1], unit, radius)
            if near:                              # has a reachable recruiting site
                within.append((t, near))
        within.sort(key=lambda x: x[1][0]["distance"])   # nearest site first
        picks = within[:MAX_MATCH]
    else:
        picks = [(t, []) for t in gated[:MAX_MATCH]]

    results = []
    for t, near in picks:
        if mt.LLM_API_KEY:
            try:
                m = mt.llm_match(note, t)
            except Exception as e:
                m = {"verdict": "error", "score": 0, "rationale": str(e)[:120],
                     "met": [], "not_met": [], "unknown": []}
        else:
            m = {"verdict": "possible", "score": 0,
                 "rationale": "Add an LLM key for eligibility reasoning.",
                 "met": [], "not_met": [], "unknown": []}
        if near:
            site, dist = near[0], near[0]["distance"]
        else:
            site, dist = rf.best_site(t, country), None

        # Summarize the study's other recruiting sites (beyond the nearby ones)
        # so the doctor can see where else it runs without a wall of text.
        # Order by proximity to the patient so the closest options show first.
        recruiting = [s for s in t.get("locations", [])
                      if (s.get("status") or "").upper() in ("", "RECRUITING")]
        near_ids = {(s.get("facility"), s.get("city")) for s in near}
        others = [s for s in recruiting
                  if (s.get("facility"), s.get("city")) not in near_ids]
        if coords:
            def _d(s):
                if s.get("lat") is None or s.get("lon") is None:
                    return float("inf")
                return haversine(coords[0], coords[1], s["lat"], s["lon"], unit)
            others.sort(key=_d)
        other_regions, seen = [], set()
        for s in others:
            reg = ", ".join(p for p in (s.get("city"), s.get("state"),
                                        s.get("country")) if p)
            if reg and reg not in seen:
                seen.add(reg)
                other_regions.append(reg)

        results.append({"trial": t, "match": m, "site": site,
                        "site_str": _site_str(site),
                        "coordinator": _coordinator(t, site),
                        "distance": dist, "unit": unit,
                        "nearby": near[:8], "nearby_total": len(near),
                        "other_count": len(others), "other_regions": other_regions[:5]})

    def rank_key(r):
        t, m = r["trial"], r["match"]
        obs = (t.get("studyType") or "").upper() == "OBSERVATIONAL"
        # 1st relevance (verdict), 2nd distance, then quality tiebreakers.
        dist_sort = r["distance"] if r["distance"] is not None else 1e9
        local = bool(mt.sites_in_country(t, country)) if country else True
        return (mt.VERDICT_RANK.get(m.get("verdict"), 3), round(dist_sort, 1),
                0 if local else 1, 0 if not obs else 1,
                len(m.get("not_met") or []), len(m.get("unknown") or []),
                -int(m.get("score") or 0))

    results.sort(key=rank_key)
    return search_label, results


def _site_str(site):
    s = site or {}
    return ", ".join(p for p in (s.get("facility"), s.get("city"),
                                 s.get("state"), s.get("country")) if p)


def _clean_name(name):
    """Drop junk that CT.gov sometimes stuffs into the contact name field
    (study-ID blurbs, URLs, 'No attachments…') so we only show real names."""
    n = (name or "").strip()
    if not n:
        return ""
    low = n.lower()
    bad = ("http", "reference study", "no attachments", "study id", "www.")
    if any(b in low for b in bad) or len(n) > 48:
        return ""
    return n


def _fmt_contact(c):
    name = _clean_name(c.get("name"))
    phone = (c.get("phone") or "").strip()
    email = (c.get("email") or "").strip()
    return " · ".join(b for b in (name, phone, email) if b)


def _coordinator(trial, site):
    for c in (site or {}).get("contacts", []):
        if c.get("role") != "PRINCIPAL_INVESTIGATOR":
            s = _fmt_contact(c)
            if s:
                return s
    for c in (trial.get("centralContacts") or []):
        s = _fmt_contact(c)
        if s:
            return s
    return ""


@app.route("/search", methods=["GET", "POST"])
@login_required
def search():
    if request.method == "GET":
        return render_template("search.html")
    note = request.form.get("note", "").strip()
    condition = request.form.get("condition", "").strip()
    country = request.form.get("country", "Canada").strip()
    require_site = bool(request.form.get("require_site"))
    include_obs = bool(request.form.get("include_observational"))
    location = request.form.get("location", "").strip()
    try:
        radius = int(request.form.get("radius", "50") or 0)
    except ValueError:
        radius = 50
    if not note:
        flash("Paste a de-identified patient note to search.", "error")
        return render_template("search.html")
    if not location:
        flash("Enter the patient's location so we only show trials near them.",
              "error")
        return render_template("search.html", note_value=note)

    coords, unit = None, "km"
    # Precise coordinates from "use my current location" take priority.
    lat_in = request.form.get("lat", "").strip()
    lon_in = request.form.get("lon", "").strip()
    if lat_in and lon_in:
        try:
            coords = (float(lat_in), float(lon_in))
            unit = units_for(request.form.get("cc", ""))
        except ValueError:
            coords = None
    if coords is None and location:
        try:
            geo = geocode(location)
        except Exception:
            geo = None
        if geo:
            coords = (geo[0], geo[1])
            unit = units_for(geo[2])
        else:
            flash(f"Couldn't find “{location}” — showing results by relevance "
                  "instead. Try a city, e.g. “Toronto, ON” or a postal code.",
                  "error")

    try:
        detected, results = run_search(note, condition, country, require_site,
                                       coords, radius, unit,
                                       interventional_only=not include_obs)
    except RuntimeError as e:
        flash(str(e), "error")
        return render_template("search.html", note_value=note)
    except Exception:
        app.logger.exception("search failed")
        flash("Search failed unexpectedly. Please try again.", "error")
        return render_template("search.html", note_value=note)

    if not detected:
        flash("Couldn't auto-detect a condition — type one in and search again.",
              "error")
        return render_template("search.html", note_value=note)

    profile = mt.patient_profile(note)
    return render_template("results.html", results=results, note=note,
                           condition=detected or condition, country=country,
                           require_site=require_site, profile=profile,
                           location=location, radius=radius, unit=unit,
                           located=bool(coords), include_obs=include_obs)


# --------------------------------------------------------------------------- #
# Import a note (upload/paste) -> de-identified text in the search box
# --------------------------------------------------------------------------- #
@app.route("/extract", methods=["POST"])
@login_required
def extract():
    """Import a note from an upload/paste. Extraction and AI de-identification
    are separate steps so we never lose the doctor's text: if de-id is off or
    fails, we still return the extracted text with a warning to redact."""
    f = request.files.get("file")
    raw = request.form.get("raw_text", "").strip()

    # 1) Get raw text (images require the vision model; there is no non-AI path).
    try:
        if f and f.filename:
            data = f.read()
            if ingest._ext(f.filename) in ingest.IMAGE_EXTS:
                text = ingest.vision_extract(data, ingest._ext(f.filename))
                flash("Read the image and de-identified it — review, then search.", "ok")
                return render_template("search.html", note_value=text)
            raw_text = ingest.extract_text(f.filename, data)
        elif raw:
            raw_text = raw
        else:
            flash("Choose a file or paste some text to import.", "error")
            return render_template("search.html")
    except ingest.IngestError as e:
        flash(str(e), "error")
        return render_template("search.html", note_value=raw)
    except Exception:
        app.logger.exception("extract (text) failed")
        flash("Couldn't read that. Please paste the note text instead.", "error")
        return render_template("search.html", note_value=raw)

    # 2) De-identify with the LLM if available; otherwise keep text + warn.
    if not mt.LLM_API_KEY:
        flash("Imported. AI de-identification is off — remove any names/identifiers "
              "before searching.", "error")
        return render_template("search.html", note_value=raw_text)
    try:
        cleaned = ingest.deidentify(raw_text)
        flash("Imported and de-identified — review the note, then search.", "ok")
        return render_template("search.html", note_value=cleaned)
    except ingest.IngestError:
        flash("Imported, but AI cleanup failed — please remove identifiers "
              "manually before searching.", "error")
        return render_template("search.html", note_value=raw_text)


# --------------------------------------------------------------------------- #
# Import a patient from a connected EHR (FHIR) -> de-identified note
# --------------------------------------------------------------------------- #
# Label shown once an EHR is linked. No real OAuth handshake yet — this connects
# to the public SMART/HAPI FHIR sandbox so the pull-by-ID flow can be used.
EHR_PROVIDER = os.environ.get("EHR_PROVIDER", "SMART Health IT (sandbox)")


@app.route("/ehr/connect", methods=["POST"])
@login_required
def ehr_connect():
    db.set_ehr_connection(g.user["id"], True, EHR_PROVIDER)
    flash(f"Connected to {EHR_PROVIDER}. You can now pull a patient by ID.", "ok")
    return redirect(request.form.get("next") or url_for("search"))


@app.route("/ehr/disconnect", methods=["POST"])
@login_required
def ehr_disconnect():
    db.set_ehr_connection(g.user["id"], False)
    flash("Disconnected your EHR.", "ok")
    return redirect(request.form.get("next") or url_for("search"))


@app.route("/ehr/import", methods=["POST"])
@login_required
def ehr_import():
    if not g.user["ehr_connected"]:
        flash("Connect your EHR first to pull patients by ID.", "error")
        return render_template("search.html")
    pid = request.form.get("patient_id", "").strip()
    want_sample = request.form.get("sample")
    try:
        if want_sample and not pid:
            pid = fhir.pick_sample_patient()
        summary, cond = fhir.fetch_patient_summary(pid)
    except fhir.FhirError as e:
        flash(str(e), "error")
        return render_template("search.html")
    except Exception:
        app.logger.exception("ehr import failed")
        flash("EHR import failed unexpectedly. Please try again.", "error")
        return render_template("search.html")
    flash("Loaded the patient from the EHR and de-identified it — review the "
          "note, then search.", "ok")
    return render_template("search.html", note_value=summary, condition_value=cond)


# --------------------------------------------------------------------------- #
# Refer + track
# --------------------------------------------------------------------------- #
_EMAIL_RE = _re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _extract_email(s):
    m = _EMAIL_RE.search(s or "")
    return m.group(0) if m else ""


@app.route("/refer", methods=["POST"])
@login_required
def refer():
    """Step 1: confirm the referral — capture consent + contact details before
    anything is saved or sent."""
    f = request.form
    nct = f.get("nct", "").strip()
    if not nct:
        flash("Couldn't start that referral (missing trial). Please try again.",
              "error")
        return redirect(request.referrer or url_for("dashboard"))
    label = f.get("patient_label", "").strip()
    if not label:
        p = mt.patient_profile(f.get("note", ""))
        label = f"{int(p['age']) if p['age'] else '?'}{(p['sex'] or '?')[0].upper()}"
    data = {k: f.get(k, "") for k in
            ("nct", "title", "condition", "country", "site", "coordinator",
             "verdict", "score", "rationale")}
    data["note"] = f.get("note", "")
    data["patient_label"] = label
    data["coordinator_email"] = _extract_email(f.get("coordinator", ""))
    return render_template("refer.html", d=data)


@app.route("/refer/confirm", methods=["POST"])
@login_required
def refer_confirm():
    """Step 2: consent recorded -> persist the referral and open its tracker."""
    f = request.form
    nct = f.get("nct", "").strip()
    if not nct:
        flash("Couldn't save that referral (missing trial).", "error")
        return redirect(url_for("dashboard"))
    if not f.get("consent"):
        flash("Please confirm patient consent before referring.", "error")
        return render_template("refer.html", d=request.form)
    ref_id = db.create_referral(g.user["id"], {
        "nct": nct, "title": f.get("title", ""),
        "patient_label": f.get("patient_label", "").strip() or "patient",
        "patient_summary": f.get("note", ""),
        "condition": f.get("condition", ""), "country": f.get("country", ""),
        "site": f.get("site", ""), "coordinator": f.get("coordinator", ""),
        "coordinator_email": f.get("coordinator_email", "").strip(),
        "patient_name": f.get("patient_name", "").strip(),
        "patient_contact": f.get("patient_contact", "").strip(),
        "consent": True,
        "verdict": f.get("verdict", ""), "score": f.get("score", 0),
        "rationale": f.get("rationale", ""),
    })
    flash("Referral saved. Send it to the site coordinator to close the loop.",
          "ok")
    return redirect(url_for("referral_detail", ref_id=ref_id))


@app.route("/referrals")
@login_required
def referrals():
    rows = db.list_referrals(g.user["id"])
    counts = db.status_counts(g.user["id"])
    enrolled, earned, pipeline = db.commission_summary(g.user["id"])
    return render_template("referrals.html", referrals=rows, counts=counts,
                           statuses=db.STATUSES, enrolled=enrolled,
                           earned=earned, pipeline=pipeline)


@app.route("/referrals.csv")
@login_required
def referrals_csv():
    import csv
    import io
    rows = db.list_referrals(g.user["id"])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["patient", "nct", "trial", "site", "coordinator_email", "status",
                "consent", "sent_at", "commission", "created", "updated"])
    for r in rows:
        w.writerow([r["patient_label"], r["nct"], r["title"], r["site"],
                    r["coordinator_email"], r["status"],
                    "yes" if r["consent"] else "no", r["notified_at"],
                    f"{(r['commission_cents'] or 0) / 100:.2f}",
                    r["created_at"], r["updated_at"]])
    from flask import Response
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=trialbridge_referrals.csv"})


@app.route("/referral/<int:ref_id>")
@login_required
def referral_detail(ref_id):
    ref = db.get_referral(ref_id, g.user["id"])
    if not ref:
        abort(404)
    share_url = url_for("coordinator_page", token=ref["token"], _external=True)
    subject, body = mailer.build_message(ref, share_url)
    mailto = "mailto:" + urllib.parse.quote(ref["coordinator_email"] or "") + \
        "?" + urllib.parse.urlencode({"subject": subject, "body": body})
    return render_template("referral_detail.html", ref=ref,
                           events=db.get_events(ref_id), statuses=db.STATUSES,
                           share_url=share_url, mailto=mailto,
                           email_subject=subject, email_body=body,
                           smtp_on=mailer.smtp_configured())


@app.route("/referral/<int:ref_id>/notify", methods=["POST"])
@login_required
def referral_notify(ref_id):
    ref = db.get_referral(ref_id, g.user["id"])
    if not ref:
        abort(404)
    email = request.form.get("coordinator_email", "").strip() or \
        ref["coordinator_email"]
    share_url = url_for("coordinator_page", token=ref["token"], _external=True)
    subject, body = mailer.build_message(ref, share_url)
    ok, msg = mailer.send_email(email, subject, body)
    if ok:
        db.mark_notified(ref_id, g.user["id"], email)
        flash("Referral emailed to the coordinator.", "ok")
    else:
        flash(msg + " Use the email/link below to send it manually.", "error")
    return redirect(url_for("referral_detail", ref_id=ref_id))


@app.route("/referral/<int:ref_id>/mark-sent", methods=["POST"])
@login_required
def referral_mark_sent(ref_id):
    """Doctor sent the referral out-of-band (mailto/copy) — record it."""
    if not db.mark_notified(ref_id, g.user["id"],
                            request.form.get("coordinator_email", "").strip()):
        abort(404)
    flash("Marked as sent to the coordinator.", "ok")
    return redirect(url_for("referral_detail", ref_id=ref_id))


# --------------------------------------------------------------------------- #
# Public coordinator page (tokenized, no login) — closes the referral loop.
# --------------------------------------------------------------------------- #
@app.route("/r/<token>")
def coordinator_page(token):
    ref = db.get_referral_by_token(token)
    if not ref:
        abort(404)
    return render_template("coordinator.html", ref=ref,
                           events=db.get_events(ref["id"]),
                           statuses=db.SITE_STATUSES)


@app.route("/r/<token>/status", methods=["POST"])
def coordinator_status(token):
    ref = db.get_referral_by_token(token)
    if not ref:
        abort(404)
    status = request.form.get("status", "")
    note = request.form.get("note", "").strip()
    if status not in db.SITE_STATUSES:
        flash("Please choose a valid status.", "error")
    elif db.update_status_by_token(token, status, note, actor="site"):
        flash(f"Thanks — marked as {status.replace('_', ' ')}.", "ok")
    return redirect(url_for("coordinator_page", token=token))


@app.route("/referral/<int:ref_id>/status", methods=["POST"])
@login_required
def referral_status(ref_id):
    status = request.form.get("status", "")
    note = request.form.get("note", "").strip()
    if status not in db.STATUSES:
        flash("Unknown status.", "error")
    elif db.update_status(ref_id, g.user["id"], status, note):
        flash(f"Status updated to {status.replace('_', ' ')}.", "ok")
    else:
        abort(404)
    return redirect(url_for("referral_detail", ref_id=ref_id))


@app.route("/referral/<int:ref_id>/commission", methods=["POST"])
@login_required
def referral_commission(ref_id):
    try:
        cents = int(round(float(request.form.get("amount", "0")) * 100))
    except ValueError:
        cents = 0
    db.set_commission(ref_id, g.user["id"], max(0, cents))
    flash("Commission updated.", "ok")
    return redirect(url_for("referral_detail", ref_id=ref_id))


if __name__ == "__main__":
    db.init_db()
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    print(f"TrialBridge on http://127.0.0.1:{port}  (LLM: "
          f"{'on' if mt.LLM_API_KEY else 'OFF — set LLM_API_KEY'})")
    app.run(host="127.0.0.1", port=port, debug=debug, threaded=True,
            use_reloader=False)
