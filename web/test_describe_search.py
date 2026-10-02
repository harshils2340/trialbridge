"""Describe-it search without the AI: the sentence still becomes a search.

Run: python test_describe_search.py
"""
import os
import tempfile

os.environ["DB_PATH"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["SITE_DEMO"] = "0"
os.environ["ALERTS_BACKGROUND"] = "0"
os.environ["REMINDERS_BACKGROUND"] = "0"

import app  # noqa: E402


def test_keywords():
    cases = {
        "I have chronic back pain": "chronic back pain",
        "I have had chronic back pain for 3 years": "chronic back pain",
        "my son has type 1 diabetes": "type 1 diabetes",
        "looking for trials for long covid fatigue": "long covid fatigue",
        "Im suffering from migraines": "migraines",
        "3 years of knee arthritis": "knee arthritis",
    }
    for text, want in cases.items():
        got = app._describe_keywords(text)
        assert got == want, (text, got)
    print("PASS: a description becomes the medical words in it")


def test_ranking_drops_passing_mentions():
    def r(title, dist, conds=()):
        return {"trial": {"title": title, "conditions": list(conds)}, "distance": dist}
    results = [r("Exercise for mild cognitive impairment", 1),
               r("Chronic constipation treatment", 59),
               r("Chiropractic care for chronic spinal pain", 59, ["Back Pain"]),
               r("PRT for chronic low back pain", 94),
               r("Chronic pain master protocol", 238)]
    out = [x["trial"]["title"] for x in app._rank_by_words(results, "chronic back pain")]
    assert out[:2] == ["Chiropractic care for chronic spinal pain", "PRT for chronic low back pain"], out
    assert "Exercise for mild cognitive impairment" not in out
    assert "Chronic constipation treatment" not in out
    print("PASS: trials that only mention the words in passing are dropped")


if __name__ == "__main__":
    test_keywords()
    test_ranking_drops_passing_mentions()
    print("All describe search tests passed")
