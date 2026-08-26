"""Real ingestion for sources without an API: a pasted lead email, a forwarded
portal notification, a voicemail transcript, or an uploaded file becomes a
conversation, gets its fields extracted and its rules run. No key needed."""
import re

from copy_sanitize import sanitize_copy

from . import extract
from . import models
from . import rules as rules_mod
from .ai import extract_llm
from .ai import llm as llm_mod

_HEADER = re.compile(r"^\s*(from|subject|name|phone|email|practice area|to|date)\s*:\s*(.*)$",
                     re.I | re.M)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(\+?1?[\s.-]?)?\(?(\d{3})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})")
_PORTAL_SENDERS = ("zillow", "avvo", "zocdoc", "psychologytoday", "psychology today",
                   "indeed", "care.com", "apartments.com", "realtor.com", "google")


def parse_email_text(text):
    """Pull a contact and a body out of raw pasted text. Returns
    {name, handle, email, phone, subject, body, portal}."""
    text = sanitize_copy(text or "").strip()
    headers = {}
    for m in _HEADER.finditer(text):
        headers.setdefault(m.group(1).lower(), m.group(2).strip())
    body = text
    # Drop a leading header block (everything up to the first blank line)
    # when the text starts with header lines.
    if text.lower().startswith(("from:", "subject:", "name:", "to:", "date:")):
        parts = re.split(r"\n\s*\n", text, maxsplit=1)
        if len(parts) == 2:
            body = parts[1].strip()
    # "Message: ..." style portals: the message is the body.
    mm = re.search(r"(?im)^\s*message\s*:\s*(.+)$", text)
    if mm:
        tail = text[mm.start(1):].strip()
        if len(tail) > 20:
            body = tail
    name = headers.get("name", "")
    frm = headers.get("from", "")
    portal = ""
    for p in _PORTAL_SENDERS:
        if p in frm.lower():
            portal = p
            break
    if not name and frm and not portal:
        nm = re.match(r"\s*\"?([^\"<]+?)\"?\s*<", frm)
        name = nm.group(1).strip() if nm else frm.split("@")[0].strip()
    if not name:
        nm = re.search(r"(?i)\bmy name is ([A-Z][a-z]+(?: [A-Z][a-z]+)?)", text)
        if nm:
            name = nm.group(1)
    if not name:
        nm = re.search(r"(?i)\b(?:this is|i am|i'm) ([A-Z][a-z]+ [A-Z][a-z]+)\b", text)
        if nm:
            name = nm.group(1)
    email = headers.get("email", "")
    if not email:
        em = _EMAIL.search(body) or (_EMAIL.search(frm) if not portal else None)
        email = em.group(0) if em else ""
    phone = headers.get("phone", "")
    if not phone:
        pm = _PHONE.search(body)
        if pm:
            phone = f"({pm.group(2)}) {pm.group(3)}-{pm.group(4)}"
    handle = email or phone or (name.lower().replace(" ", ".") if name else "")
    subject = headers.get("subject", "")
    if not subject or portal:
        first = re.sub(r"\s+", " ", body).strip()
        subject = (first[:56] + ("..." if len(first) > 56 else "")) if first else (subject or "New inquiry")
    return {"name": name or "Unknown", "handle": handle or f"paste:{abs(hash(text)) % 100000}",
            "email": email, "phone": phone, "subject": subject, "body": body or text,
            "portal": portal}


def ingest_text(ws, spec, source, text, propose=None):
    """Create a conversation from pasted text. Returns the conversation id."""
    parsed = parse_email_text(text)
    contact = models.upsert_contact(ws["id"], parsed["name"], parsed["handle"],
                                    parsed["email"], parsed["phone"])
    stage = spec["stages"][0]["key"] if spec.get("stages") else ""
    conv_id = models.create_conversation(ws["id"], source["id"], contact["id"],
                                         parsed["subject"], stage, unread=1)
    models.add_message(ws["id"], conv_id, "inbound", parsed["body"], author=parsed["name"])
    conv = models.get_conversation(ws["id"], conv_id)
    extract.extract_conversation(ws, spec, conv, llm=extract_llm.extract if llm_mod.available() else None)
    models.add_event(ws["id"], "ingested", {"source": source["key"], "portal": parsed["portal"]},
                     conversation_id=conv_id)
    rules_mod.run_all(ws, spec, propose=propose)
    return conv_id


def read_upload(storage):
    """Best-effort text from an uploaded file: txt/csv/md directly, pdf via the
    existing ingest helper when available."""
    name = (storage.filename or "").lower()
    data = storage.read()
    if name.endswith(".pdf"):
        try:
            import ingest as clinical_ingest  # existing PDF text helper
            return clinical_ingest.extract_text(data, name)
        except Exception:
            return ""
    try:
        return data.decode("utf-8", "ignore")
    except Exception:
        return ""
