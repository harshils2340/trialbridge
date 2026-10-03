"""SEO surface: study titles, robots, llms.txt, IndexNow.

Run: python test_seo.py
"""
import os
import tempfile

os.environ["DB_PATH"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["SITE_DEMO"] = "0"
os.environ["ALERTS_BACKGROUND"] = "0"
os.environ["REMINDERS_BACKGROUND"] = "0"
os.environ["INDEXNOW"] = "0"

import app  # noqa: E402
import db  # noqa: E402

TRIAL = {
    "title": "A Research Study Investigating How Well the Medicine Zenagamtide Helps "
             "People With Excess Body Weight Lose Weight Compared to Semaglutide",
    "conditions": ["Overweight"],
    "interventions": [{"type": "DRUG", "name": "Zenagamtide"},
                      {"type": "DRUG", "name": "Semaglutide 2.4 mg"},
                      {"type": "DRUG", "name": "Placebo"}],
    "locations": [{"city": "Houston", "state": "Texas", "status": "RECRUITING"}],
    "minAge": "18 Years", "sex": "ALL",
}


def test_study_titles():
    facts = app._study_facts(TRIAL)
    seo = app._study_seo(TRIAL, "Overweight", {}, facts)
    assert seo["title"] == "Overweight clinical trial: Zenagamtide vs Semaglutide", seo
    assert len(seo["title"]) <= 75
    assert seo["heading"] == "Overweight study: Zenagamtide vs Semaglutide"
    assert seo["description"].endswith("Apply free on BridgeMD.")
    assert len(seo["description"]) <= 158 and "—" not in seo["description"]
    # Comparison arms and vague phrases never become the "treatment".
    t = dict(TRIAL, interventions=[{"type": "OTHER", "name": "Usual Medical Care"},
                                   {"type": "BEHAVIORAL", "name": "High frequency"}])
    assert app._seo_interventions(t) == ""
    # "Paid" only when the listing says participants are paid.
    paid = dict(TRIAL, briefSummary="Participants will receive $50 per visit.")
    assert app._study_seo(paid, "Overweight", {}, facts)["title"].startswith("Paid ")
    print("PASS: study titles lead with the condition, treatment and place")


def test_pay_words_need_participants():
    P = app._pay_likelihood
    assert P({"briefSummary": "patients with compensated cirrhosis"})["tier"] == "none"
    assert P({"briefSummary": "history of hepatic decompensation"})["tier"] == "none"
    assert P({"briefSummary": "time away from paid employment"})["tier"] == "none"
    assert P({"briefSummary": "you will be compensated for your time"})["tier"] != "none"
    assert not app._pays_participants({"criteria": "receiving workers' compensation for their knee injury"})
    assert app._pays_participants({"briefSummary": "Volunteers will receive a $25 gift card"})
    print("PASS: medical and job uses of pay words are not counted as payment")


def test_robots_llms_indexnow():
    c = app.app.test_client()
    robots = c.get("/robots.txt").data.decode()
    assert "User-agent: AhrefsBot\nDisallow: /" in robots
    assert "User-agent: *" in robots and "Allow: /" in robots
    for allowed in ("Googlebot", "Bingbot", "GPTBot", "ClaudeBot", "PerplexityBot"):
        assert allowed not in robots, allowed
    assert "Sitemap:" in robots
    llms = c.get("/llms.txt")
    assert llms.status_code == 200 and b"# BridgeMD" in llms.data
    key = c.get(f"/{app.INDEXNOW_KEY}.txt")
    assert key.status_code == 200 and key.data.decode() == app.INDEXNOW_KEY
    sent = []
    with app.app.app_context():
        db.init_db()
        n = app.indexnow_sweep(post=lambda p: sent.append(p) or True)
        assert n > 0 and sent[0]["key"] == app.INDEXNOW_KEY
        assert app.indexnow_sweep(post=lambda p: sent.append(p) or True) == 0
    print("PASS: robots blocks SEO crawlers only; llms.txt and IndexNow work")


if __name__ == "__main__":
    test_study_titles()
    test_pay_words_need_participants()
    test_robots_llms_indexnow()
    print("All SEO tests passed")
