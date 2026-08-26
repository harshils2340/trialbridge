"""Workspaces without accounts: a private workspace bound to a browser cookie,
reachable by anyone with the link, owner-only for destructive actions."""
import os
import time

from flask import g, request

from . import models

COOKIE = "om_owner"
COOKIE_DAYS = 90
_last_sweep = 0.0


def owner_token():
    return request.cookies.get(COOKIE, "")


def remember_owner(token):
    """Ask the after_request hook to set the cookie on this response."""
    g._om_set_owner = token


def apply_cookie(resp):
    token = getattr(g, "_om_set_owner", None)
    if token:
        secure = os.environ.get("BEHIND_PROXY") == "1"
        resp.set_cookie(COOKIE, token, max_age=COOKIE_DAYS * 86400, httponly=True,
                        samesite="Lax", secure=secure, path="/omni")
    return resp


def is_owner(ws):
    tok = owner_token()
    return bool(tok) and tok == ws.get("owner_token")


def current_workspace():
    """The workspace this browser most recently created, if any."""
    return models.get_workspace_by_owner(owner_token())


def sweep_if_due():
    global _last_sweep
    if time.time() - _last_sweep < 3600:
        return
    _last_sweep = time.time()
    try:
        days = int(os.environ.get("OMNI_WS_TTL_DAYS", "14"))
    except ValueError:
        days = 14
    try:
        models.sweep_stale(days)
    except Exception:
        pass
