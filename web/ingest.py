"""Turn an uploaded clinical note into a clean, de-identified summary.

Doctors shouldn't retype a note. They upload/paste it and we extract the text
(PDF / Word / plain text) or read an image with a vision model, then run an LLM
pass to strip identifiers and structure it for trial screening.

Every function degrades gracefully: on any failure it raises IngestError with a
short, human message the UI can show — it never crashes the request.
"""
import base64
import html
import io
import json
import re
import urllib.request
import zipfile

import match_trials as mt

MAX_TEXT = 20000            # chars we keep from any source
IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif"}
TEXT_EXTS = {"txt", "text", "md", "markdown", "csv", "rtf", "log"}
MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "webp": "image/webp", "gif": "image/gif"}


class IngestError(Exception):
    """Human-readable failure that the UI can surface as a flash message."""


def _ext(filename):
    return (filename.rsplit(".", 1)[-1].lower() if "." in filename else "")


# --------------------------------------------------------------------------- #
# Raw text extraction (no LLM)
# --------------------------------------------------------------------------- #
def _from_pdf(data):
    try:
        from pypdf import PdfReader
    except Exception:
        raise IngestError("PDF support isn't installed (pip install pypdf).")
    try:
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception:
        raise IngestError("That PDF couldn't be read. Try exporting it as text.")
    if len(text.strip()) < 20:
        raise IngestError("This looks like a scanned PDF (no selectable text). "
                          "Upload it as an image instead, or paste the text.")
    return text


def _from_docx(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
    except Exception:
        raise IngestError("That Word file couldn't be read. Try 'Save as' .txt.")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    return html.unescape(xml)


def extract_text(filename, data):
    """Return plain text from a text/PDF/Word upload. Images return None so the
    caller can route them to the vision path. Raises IngestError otherwise."""
    ext = _ext(filename)
    if ext in IMAGE_EXTS:
        return None
    if ext == "pdf":
        return _from_pdf(data)[:MAX_TEXT]
    if ext == "docx":
        return _from_docx(data)[:MAX_TEXT]
    if ext in TEXT_EXTS or ext == "":
        try:
            return data.decode("utf-8", "ignore")[:MAX_TEXT]
        except Exception:
            raise IngestError("Couldn't read that file as text.")
    if ext == "doc":
        raise IngestError("Old .doc isn't supported — save as .docx or .txt.")
    raise IngestError(f"Unsupported file type: .{ext}")


# --------------------------------------------------------------------------- #
# LLM passes
# --------------------------------------------------------------------------- #
DEID_SYSTEM = (
    "You are a clinical scribe preparing a note for clinical-trial screening. "
    "Rewrite the input as a DE-IDENTIFIED patient summary. REMOVE every direct "
    "identifier: names, initials, MRNs, dates of birth, exact dates, addresses, "
    "phone numbers, emails, and provider/hospital names. Keep clinical facts. "
    "Start with two lines exactly:\nAGE: <number or 'unknown'>\n"
    "SEX: <male|female|unknown>\nThen a concise summary: primary diagnosis, "
    "key labs/vitals with values, current medications, relevant comorbidities, "
    "and prior treatments. Never invent facts not present in the input. Output "
    "plain text only."
)


def deidentify(text):
    """LLM pass to strip identifiers + structure. Falls back to raw text."""
    if not mt.LLM_API_KEY:
        raise IngestError("AI de-identification is off (set LLM_API_KEY). "
                          "The raw text was imported — remove identifiers before searching.")
    try:
        out = mt.llm_chat(DEID_SYSTEM, text[:MAX_TEXT])
    except Exception:
        raise IngestError("The AI cleanup step failed. The raw text was imported "
                          "— please review and remove identifiers before searching.")
    return (out or "").strip()


def vision_extract(data, ext):
    """Read a photo/scan of a note with a vision model and return a
    de-identified summary directly. Raises IngestError on any problem."""
    if not mt.LLM_API_KEY:
        raise IngestError("Reading an image needs an AI key (set LLM_API_KEY).")
    mime = MIME.get(ext, "image/png")
    b64 = base64.b64encode(data).decode()
    body = json.dumps({
        "model": mt.LLM_MODEL,
        "messages": [
            {"role": "system", "content": DEID_SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": "Transcribe this clinical note and "
                 "return the de-identified summary as instructed."},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ]},
        ],
        "temperature": 0,
    }).encode()
    try:
        req = urllib.request.Request(
            f"{mt.LLM_BASE_URL}/chat/completions", data=body,
            headers={"Authorization": f"Bearer {mt.LLM_API_KEY}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as r:
            resp = json.load(r)
        return resp["choices"][0]["message"]["content"].strip()
    except Exception:
        raise IngestError("Couldn't read that image. Make sure your model "
                          "supports vision, or paste the text instead.")
