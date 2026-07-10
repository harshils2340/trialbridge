"""Live staging smoke checks (hits deployed URL, not Flask test client).

Run from matcher/web:
  STAGING_BASE_URL="https://your-app.onrender.com" \
  /Users/harshils/GraphMD/matcher/.venv/bin/python staging_smoke.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from http import cookiejar


def _fail(msg: str):
    raise AssertionError(msg)


def _ok(msg: str):
    print(f"PASS: {msg}")


def _base_url() -> str:
    raw = (os.environ.get("STAGING_BASE_URL", "") or "").strip().rstrip("/")
    if not raw.startswith(("http://", "https://")):
        _fail("Set STAGING_BASE_URL to http(s)://...")
    return raw


def _open(opener, url, data=None):
    req = urllib.request.Request(url, data=data)
    with opener.open(req, timeout=30) as r:
        body = r.read().decode("utf-8", errors="replace")
        return r.status, body


def _csrf_token(html: str) -> str:
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    return m.group(1) if m else ""


def main():
    base = _base_url()
    cj = cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    status, body = _open(opener, base + "/healthz")
    if status != 200:
        _fail(f"/healthz returned {status}")
    payload = json.loads(body)
    if not payload.get("ok"):
        _fail("/healthz did not return ok=true")
    _ok("healthz")

    ops_key = (os.environ.get("OPS_READINESS_KEY", "") or "").strip()
    if ops_key:
        status, ready_body = _open(
            opener, base + "/ops/readiness?key=" + urllib.parse.quote(ops_key)
        )
        if status != 200:
            _fail(f"/ops/readiness returned {status}")
        ready = json.loads(ready_body or "{}")
        if "gaps" not in ready:
            _fail("/ops/readiness missing gaps field")
        _ok("ops readiness endpoint")

    status, home = _open(opener, base + "/")
    if status != 200:
        _fail(f"/ returned {status}")
    token = _csrf_token(home)
    if not token:
        _fail("missing csrf token on home page")
    _ok("home page + csrf token")

    form = urllib.parse.urlencode({
        "_csrf_token": token,
        "q": "obesity",
        "location": "Toronto, ON",
        "radius": "50",
        "interventional_only": "1",
    }).encode()
    status, find_body = _open(opener, base + "/find", data=form)
    if status != 200:
        _fail(f"/find returned {status}")
    if "Clinical trials" not in find_body and "No matching trials yet" not in find_body:
        _fail("search page did not render expected content")
    _ok("search POST /find")

    status, _ = _open(opener, base + "/how")
    if status != 200:
        _fail(f"/how returned {status}")
    _ok("how-it-works page")

    status, _ = _open(opener, base + "/app/leads")
    if status not in (200, 302):
        _fail(f"/app/leads returned unexpected status {status}")
    _ok("study-team endpoint reachable")

    print("")
    print("Live staging smoke PASSED")
    print(f"Base URL: {base}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FAIL: {exc}")
        sys.exit(1)
