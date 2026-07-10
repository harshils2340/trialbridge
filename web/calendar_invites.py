"""Calendar invite helpers for screening visits.

ICS generation is always available so patients can add visits to calendar apps
without third-party dependencies. Google Calendar sync is optional and guarded
by environment variables; failures are non-fatal by design.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import urllib.error
import urllib.parse
import urllib.request


def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _ics_escape(text: str) -> str:
    """Escape text for ICS content lines."""
    txt = str(text or "")
    txt = txt.replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,")
    txt = txt.replace("\r\n", r"\n").replace("\n", r"\n")
    return txt


def _parse_visit_at(raw: str) -> dt.datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return dt.datetime.strptime(raw, fmt)
        except ValueError:
            continue
    try:
        return dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


def _fmt_ics_local(ts: dt.datetime) -> str:
    return ts.strftime("%Y%m%dT%H%M%S")


def _field(row, key: str, default=""):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        val = row[key]
        return default if val is None else val
    except Exception:
        return default


def build_visit_ics(lead, visit, invite_url: str = "") -> str:
    """Generate an ICS file payload for one visit."""
    start = _parse_visit_at(_field(visit, "visit_at", ""))
    if start is None:
        # Keep a deterministic fallback rather than failing user flow.
        start = dt.datetime.utcnow().replace(second=0, microsecond=0)
    duration_min = max(5, int(os.environ.get("VISIT_INVITE_DURATION_MINUTES", "30")))
    end = start + dt.timedelta(minutes=duration_min)
    stamp = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    visit_id = _field(visit, "id", "0") or "0"
    lead_id = _field(lead, "id", "0") or "0"
    uid = f"bridgemd-lead{lead_id}-visit{visit_id}@bridgemd.local"
    title = (_field(lead, "title") or _field(lead, "nct") or
             "Clinical trial visit").strip()
    kind = (_field(visit, "kind") or "screening").replace("-", " ").title()
    summary = f"{kind} visit - {title}"
    lines = [
        f"Trial: {_field(lead, 'nct') or 'n/a'}",
        f"Visit type: {kind}",
    ]
    if _field(visit, "note"):
        lines.append(f"Note: {_field(visit, 'note')}")
    if invite_url:
        lines.append(f"BridgeMD details: {invite_url}")
    description = "\\n".join(lines)
    location = str(_field(visit, "location", "")).strip()
    organizer = os.environ.get("VISIT_ICS_ORGANIZER_NAME", "BridgeMD Study Team")

    payload = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//BridgeMD//Screening Visit//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{_ics_escape(uid)}",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{_fmt_ics_local(start)}",
        f"DTEND:{_fmt_ics_local(end)}",
        f"SUMMARY:{_ics_escape(summary)}",
        f"DESCRIPTION:{_ics_escape(description)}",
        f"ORGANIZER;CN={_ics_escape(organizer)}:mailto:no-reply@bridgemd.local",
    ]
    if location:
        payload.append(f"LOCATION:{_ics_escape(location)}")
    if invite_url:
        payload.append(f"URL:{_ics_escape(invite_url)}")
    payload += ["END:VEVENT", "END:VCALENDAR", ""]
    return "\r\n".join(payload)


def _google_sync_ready() -> tuple[bool, str, str]:
    enabled = _env_flag("GOOGLE_CALENDAR_SYNC", "0")
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "").strip()
    access_token = os.environ.get("GOOGLE_CALENDAR_ACCESS_TOKEN", "").strip()
    if not enabled:
        return False, calendar_id, access_token
    if not calendar_id or not access_token:
        return False, calendar_id, access_token
    return True, calendar_id, access_token


def maybe_sync_google_event(lead, visit, invite_url: str = ""):
    """Best-effort Google Calendar push.

    Returns:
      {"attempted": bool, "ok": bool, "detail": str}
    """
    ready, calendar_id, access_token = _google_sync_ready()
    if not ready:
        return {"attempted": False, "ok": False, "detail": "disabled_or_unconfigured"}

    start = _parse_visit_at(_field(visit, "visit_at", ""))
    if start is None:
        return {"attempted": False, "ok": False, "detail": "invalid_visit_time"}
    duration_min = max(5, int(os.environ.get("VISIT_INVITE_DURATION_MINUTES", "30")))
    end = start + dt.timedelta(minutes=duration_min)
    tz_name = os.environ.get("GOOGLE_CALENDAR_TIMEZONE", "UTC").strip() or "UTC"
    title = (_field(lead, "title") or _field(lead, "nct") or
             "Clinical trial visit").strip()
    kind = (_field(visit, "kind") or "screening").replace("-", " ").title()
    event_id = f"bridgemd-lead{_field(lead, 'id')}-visit{_field(visit, 'id')}"
    payload = {
        "summary": f"{kind} visit - {title}",
        "description": (
            f"BridgeMD trial: {_field(lead, 'nct') or 'n/a'}\n"
            f"Visit type: {kind}\n"
            f"{('Note: ' + str(_field(visit, 'note'))) if _field(visit, 'note') else ''}\n"
            f"{('Details: ' + invite_url) if invite_url else ''}"
        ).strip(),
        "location": _field(visit, "location") or "",
        "start": {"dateTime": start.isoformat(), "timeZone": tz_name},
        "end": {"dateTime": end.isoformat(), "timeZone": tz_name},
        "iCalUID": f"{event_id}@bridgemd.local",
    }
    base = os.environ.get(
        "GOOGLE_CALENDAR_API_BASE",
        "https://www.googleapis.com/calendar/v3",
    ).rstrip("/")
    encoded_id = urllib.parse.quote(calendar_id, safe="")
    url = f"{base}/calendars/{encoded_id}/events"
    req = urllib.request.Request(
        url=url,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            if 200 <= getattr(res, "status", 500) < 300:
                return {"attempted": True, "ok": True, "detail": "created"}
            return {"attempted": True, "ok": False, "detail": f"http_{res.status}"}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="ignore")[:160]
        except Exception:
            detail = ""
        return {"attempted": True, "ok": False, "detail": f"http_{e.code}:{detail}"}
    except Exception as e:  # noqa: BLE001
        return {"attempted": True, "ok": False, "detail": str(e)[:160]}
