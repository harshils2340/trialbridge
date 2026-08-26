"""No em dashes in user-facing copy or AI output."""
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from copy_sanitize import sanitize_copy, contains_em_dash, EM_DASH  # noqa: E402


def test_sanitize_spaced_em_dash():
    assert sanitize_copy("Hi — there") == "Hi, there"
    assert sanitize_copy("pick one — I can only") == "pick one, I can only"


def test_sanitize_html_entity():
    assert sanitize_copy("read it out &mdash; the password") == "read it out, the password"


def test_sanitize_none():
    assert sanitize_copy(None) is None


def test_llm_chat_applies_sanitize(monkeypatch):
    import match_trials as mt

    class FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            import json
            return json.dumps(self._payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def fake_urlopen(req, timeout=60):
        return FakeResp({"choices": [{"message": {"content": "Hello — world"}}]})

    monkeypatch.setattr(mt, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(mt, "LLM_BASE_URL", "http://example.com")
    monkeypatch.setattr(mt.urllib.request, "urlopen", fake_urlopen)
    assert mt.llm_chat("sys", "user") == "Hello, world"


def test_no_em_dash_in_user_facing_web_copy():
    """Static guard: templates and demo data must not ship em dashes."""
    root = HERE.parent
    scan = [
        HERE / "templates",
        HERE / "static",
        HERE / "db.py",
        HERE / "app.py",
        HERE / "mailer.py",
        HERE / "notifications.py",
        HERE / "copilot",
        root / "marketing" / "clean_landing.py",
    ]
    # The character itself, its HTML entities, and the Python/JS escape: the
    # escape is how "\\u2014" strings in db.py and app.py slipped past a grep
    # for the character for weeks. Guard code that names the escape in order
    # to catch it is exempt by line.
    needles = (EM_DASH, "&mdash;", "&#8212;", "&#x2014;", "\\u2014")
    exempt = ("_scrub_em_dashes", "_no_em_dashes", "xe2", "LIKE ?", "noqa: dash")
    bad = []
    for base in scan:
        paths = [base] if base.is_file() else base.rglob("*")
        for path in paths:
            if not path.is_file() or path.suffix not in {".py", ".html", ".js"}:
                continue
            if ".min." in path.name or "/shots/" in str(path):
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if any(x in line for x in needles) and not any(e in line for e in exempt):
                    bad.append(f"{path.relative_to(root)}:{n}")
    assert not bad, f"em dash still in: {bad}"


if __name__ == "__main__":
    tests = [
        test_sanitize_spaced_em_dash,
        test_sanitize_html_entity,
        test_sanitize_none,
        test_no_em_dash_in_user_facing_web_copy,
    ]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"All {len(tests)} passed.")
