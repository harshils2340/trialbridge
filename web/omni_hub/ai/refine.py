"""Setup changes in plain words -> patch ops.

"add a field for whether they already have a lawyer"
"add a view of serious injuries with no attorney"
"flag anyone mentioning a court date"
"auto reject anyone outside CA and NV"
"let Dana send pricing replies on her own" / "review everything before sending"
"make it more formal" / "sign off as Dana at Harbor Point"
"rename stage Contacted to Reached" / "add a stage called Waiting on records"

Deterministic first (regexes over the spec's own vocabulary), the model second
when a key exists and nothing matched. An unparseable request returns an
honest "tell me which field" rather than a wrong change.
"""
import re

from . import llm
from .. import spec as spec_mod

_STATES = {"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN",
           "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV",
           "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN",
           "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC"}
_STATE_NAMES = {"illinois": "IL", "indiana": "IN", "wisconsin": "WI", "michigan": "MI",
                "california": "CA", "nevada": "NV", "new york": "NY", "texas": "TX",
                "florida": "FL", "ohio": "OH", "arizona": "AZ", "georgia": "GA",
                "canada": "Canada", "us": "US", "usa": "US", "united states": "US"}
_WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
            "ten": 10, "fourteen": 14, "thirty": 30, "sixty": 60, "ninety": 90}


def _clean(s):
    return re.sub(r"\s+", " ", (s or "").strip().strip(".!?\"'"))


def _label_from_phrase(phrase):
    p = _clean(phrase)
    p = re.sub(r"^(?:for |called |named |about |whether or not |whether |if |that |the |a |an )+", "", p, flags=re.I)
    p = re.sub(r"^(?:they|someone|the person|the lead|the applicant|the caller|people)\s+", "", p, flags=re.I)
    p = re.sub(r"^(?:have|has|had|are|is|were|was|do|does|did)\s+", lambda m: m.group(0), p)
    p = p.strip()
    if not p:
        return ""
    return p[0].upper() + p[1:]


def _infer_type(phrase):
    low = phrase.lower()
    m = re.search(r"one of ([a-z0-9 ,/]+?)(?:\)|$)", low) or re.search(r"\(([a-z0-9 ,/]+)\)", low)
    if m:
        opts = [o.strip() for o in re.split(r",|/| or ", m.group(1)) if o.strip()]
        if 2 <= len(opts) <= 8:
            return "enum", [spec_mod.slug(o) for o in opts]
    if re.search(r"\b(list of|which|what kinds?)\b", low):
        return "list", []
    if re.search(r"\b(how many|number of|count|amount|budget|price|income|age|hours|years|how much|how old|size)\b", low):
        return "number", []
    if re.search(r"\b(date|when|day|deadline|move[- ]in|start date|available)\b", low):
        return "date", []
    if re.search(r"^(whether|if|has|have|is|are|do|does|did|can|will)\b", low) or " or not" in low:
        return "bool", []
    return "text", []


def _fields(spec):
    return spec.get("fields") or []


def _find_field(spec, words, want_types=None):
    """Best field whose label/key/hint shares words with the phrase."""
    words = set(re.findall(r"[a-z][a-z']+", words.lower()))
    best, score = None, 0
    for f in _fields(spec):
        if want_types and f["type"] not in want_types:
            continue
        hay = set(re.findall(r"[a-z][a-z']+", f"{f['label']} {f['key'].replace('_', ' ')} {f.get('hint', '')}".lower()))
        s = len(words & hay)
        if s > score:
            best, score = f, s
    return best if score else None


def _num_in(text):
    m = re.search(r"\b(\d+)\b", text)
    if m:
        return int(m.group(1))
    for w, n in _WORDNUM.items():
        if re.search(r"\b" + w + r"\b", text):
            return n
    return None


def _states_in(text):
    found = [t for t in re.findall(r"\b([A-Z]{2})\b", text) if t in _STATES]
    low = text.lower()
    for name, abbr in _STATE_NAMES.items():
        if re.search(r"\b" + re.escape(name) + r"\b", low) and abbr not in found:
            found.append(abbr)
    return found


def condition_from_phrase(spec, phrase):
    """Turn 'serious injuries with no attorney' into an AST over spec fields."""
    low = " " + phrase.lower() + " "
    clauses = []
    used = set()

    # Built-ins.
    if re.search(r"\b(urgent|high priority)\b", low):
        clauses.append({"field": "priority", "op": "in", "value": ["urgent", "high"]})
    if re.search(r"\bunassigned\b|\bno owner\b", low):
        clauses.append({"field": "assignee", "op": "is_empty"})
    if re.search(r"\b(needs? (a )?reply|awaiting reply|unanswered|waiting on us)\b", low):
        clauses.append({"field": "awaiting_reply", "op": "eq", "value": True})
    if re.search(r"\bresolved\b|\bclosed\b", low):
        clauses.append({"field": "status", "op": "eq", "value": "resolved"})
    for s in spec.get("stages") or []:
        if re.search(r"\b" + re.escape(s["label"].lower()) + r"\b", low) and s["key"] not in ("new",):
            clauses.append({"field": "stage", "op": "eq", "value": s["key"]})
    for c in spec.get("connectors") or []:
        meta = spec_mod.CONNECTOR_KINDS.get(c["kind"], {})
        if re.search(r"\b" + re.escape(meta.get("label", c["kind"]).lower()) + r"\b", low) \
                or re.search(r"\bfrom " + re.escape(c["kind"].replace("_", " ")) + r"\b", low):
            clauses.append({"field": "source", "op": "eq", "value": c["kind"]})
            break

    # "mentioning X" / "that say X" -> text contains.
    m = re.search(r"\b(?:mention(?:s|ing)?|say(?:s|ing)?|about|asking about|contain(?:s|ing)?|with the words?)\s+(?:a |an |the )?([a-z0-9' -]{3,40}?)(?:\s+(?:and|or|,)|$)", low.strip())
    if m:
        needle = _clean(m.group(1))
        if needle and not _find_field(spec, needle, ("enum", "bool")):
            clauses.append({"field": "text", "op": "contains", "value": needle})

    # Enum options by label.
    for f in _fields(spec):
        if f["type"] != "enum":
            continue
        hits = []
        for opt in f["options"]:
            words = opt.replace("_", " ")
            if re.search(r"\b" + re.escape(words) + r"s?\b", low) or \
                    (len(words) > 4 and re.search(r"\b" + re.escape(words.rstrip("s")) + r"\w*\b", low)):
                hits.append(opt)
        if hits:
            neg = re.search(r"\b(?:not|except|other than|excluding)\s+" + re.escape(hits[0].replace("_", " ")), low)
            clauses.append({"field": f"f.{f['key']}", "op": "not_in" if neg else "in", "value": hits})
            used.add(f["key"])

    # Bool fields: "no lawyer", "without insurance", "has a lawyer", "represented".
    for f in _fields(spec):
        if f["type"] != "bool" or f["key"] in used:
            continue
        words = [w for w in re.findall(r"[a-z]{4,}", f["label"].lower())
                 if w not in ("already", "with", "have", "has", "been", "got", "does", "did", "they", "their", "whether")]
        if not words:
            continue
        pat = r"\b(" + "|".join(re.escape(w) for w in words) + r")\w*\b"
        m = re.search(pat, low)
        if not m:
            continue
        before = low[max(0, m.start() - 24):m.start()]
        neg = bool(re.search(r"\b(no|not|without|never|haven't|hasn't|don't|doesn't|un)\s*(a |an |the )?$", before)) \
            or re.search(r"\bno " + pat, low)
        clauses.append({"field": f"f.{f['key']}", "op": "neq" if neg else "eq", "value": True})
        used.add(f["key"])

    # Numbers: "under 18", "over $5000", "more than 5 years".
    for f in _fields(spec):
        if f["type"] != "number" or f["key"] in used:
            continue
        words = re.findall(r"[a-z]{3,}", f["label"].lower())
        if not any(re.search(r"\b" + re.escape(w) + r"\w*\b", low) for w in words):
            continue
        m = re.search(r"\b(under|below|less than|younger than|fewer than|at most|up to)\s+\$?(\d[\d,]*)", low)
        if m:
            clauses.append({"field": f"f.{f['key']}", "op": "lt" if "most" not in m.group(1) and "up to" not in m.group(1) else "lte",
                            "value": float(m.group(2).replace(",", ""))})
            used.add(f["key"])
            continue
        m = re.search(r"\b(over|above|more than|older than|at least|greater than)\s+\$?(\d[\d,]*)", low)
        if m:
            clauses.append({"field": f"f.{f['key']}", "op": "gte" if "least" in m.group(1) else "gt",
                            "value": float(m.group(2).replace(",", ""))})
            used.add(f["key"])

    # Dates: "available in 30 days", "this month", "older than 18 months", "next two weeks".
    date_fields = [f for f in _fields(spec) if f["type"] == "date"]
    if date_fields:
        target = _find_field(spec, phrase, ("date",)) or date_fields[0]
        m = re.search(r"\b(?:within|in|over|during) (?:the )?(?:next )?(\d+|[a-z]+) (day|week|month)s?\b", low)
        if m and (_num_in(m.group(1)) is not None):
            n = _num_in(m.group(1)) * {"day": 1, "week": 7, "month": 30}[m.group(2)]
            clauses.append({"field": f"f.{target['key']}", "op": "within_days", "value": n})
        elif re.search(r"\bthis (week|month)\b", low):
            n = 7 if "week" in low else 30
            clauses.append({"field": f"f.{target['key']}", "op": "within_days", "value": n})
        elif re.search(r"\b(older than|more than|over) (\d+|[a-z]+) (day|week|month|year)s?( old| ago)?\b", low):
            mm = re.search(r"\b(?:older than|more than|over) (\d+|[a-z]+) (day|week|month|year)s?", low)
            n = _num_in(mm.group(1)) * {"day": 1, "week": 7, "month": 30, "year": 365}[mm.group(2)]
            clauses.append({"field": f"f.{target['key']}", "op": "older_than_days", "value": n})

    # Text fields holding a state: "outside CA and NV", "in IL".
    state_field = next((f for f in _fields(spec) if f["type"] == "text" and "state" in f["key"]), None)
    if state_field:
        states = _states_in(phrase)
        if states:
            if re.search(r"\b(outside|not in|other than|except|beyond)\b", low):
                clauses.append({"field": f"f.{state_field['key']}", "op": "not_empty"})
                clauses.append({"field": f"f.{state_field['key']}", "op": "not_in", "value": states})
            else:
                clauses.append({"field": f"f.{state_field['key']}", "op": "in", "value": states})
    country_field = next((f for f in _fields(spec) if f["type"] == "text" and "country" in f["key"]), None)
    if country_field and not state_field:
        found = [v for k, v in _STATE_NAMES.items() if v in ("US", "Canada") and re.search(r"\b" + k + r"\b", low)]
        if found:
            op = "not_in" if re.search(r"\b(outside|not in|except)\b", low) else "in"
            if op == "not_in":
                clauses.append({"field": f"f.{country_field['key']}", "op": "not_empty"})
            clauses.append({"field": f"f.{country_field['key']}", "op": op, "value": sorted(set(found))})

    # "unknown X"
    m = re.search(r"\b(?:unknown|missing|no)\s+([a-z ]{3,30})$", low.strip())
    if m and not clauses:
        f = _find_field(spec, m.group(1))
        if f:
            clauses.append({"field": f"f.{f['key']}", "op": "is_unknown"})

    if not clauses:
        return None
    return {"all": clauses}


def _view_name(phrase):
    p = _clean(re.sub(r"^(?:of|for|called|named|showing|with|that shows?)\s+", "", _clean(phrase), flags=re.I))
    return (p[0].upper() + p[1:])[:40] if p else "New view"


def _terminal_stage(spec, hint=""):
    stages = spec.get("stages") or []
    for s in stages:
        if s.get("terminal") and re.search(r"declin|reject|not_a_fit|not a fit|refer", s["key"] + s["label"].lower()):
            return s["key"]
    return next((s["key"] for s in stages if s.get("terminal")), stages[-1]["key"] if stages else "")


_FIELD_RX = re.compile(
    r"^(?:please\s+)?(?:add|create|track|start tracking|pull out|extract|capture)\s+(?:a |an |the |new |another )*"
    r"(?:field|column|property|attribute)?\s*(?:for|called|named|that tracks|tracking|to track|:)?\s*(.+)$", re.I)
_VIEW_RX = re.compile(
    r"^(?:please\s+)?(?:add|create|make|show me|give me|build)\s+(?:a |an |the |new )*(?:view|filter|list|tab|saved search)\s*(.*)$", re.I)
_FLAG_RX = re.compile(
    r"^(?:please\s+)?(?:flag|mark|tag|highlight)\s+(?:anyone|anybody|anything|any lead|leads?|people|those|everyone|conversations?|messages?)?\s*"
    r"(?:who|that|which|whose|mentioning|mentions|with|when|if|where|as)?\s*(.+)$", re.I)
_REJECT_RX = re.compile(
    r"^(?:please\s+)?(?:auto[- ]?reject|auto[- ]?decline|reject|decline|turn away|screen out)\s+(?:anyone|anybody|anything|any lead|leads?|people|those|everyone)?\s*"
    r"(?:who|that|which|whose|with|when|if|where|from)?\s*(.+)$", re.I)
_URGENT_RX = re.compile(
    r"^(?:please\s+)?(?:mark|make|treat|set)\s+(.+?)\s+(?:as\s+)?(urgent|high priority|top priority)$", re.I)
_MOVE_RX = re.compile(
    r"^(?:please\s+)?(?:move|put|send)\s+(?:anyone|anybody|leads?|people|those|everyone)?\s*(?:who|that|which|with|when|if)?\s*(.+?)\s+(?:to|into)\s+(?:the )?(?:stage )?(.+)$", re.I)
_POLICY_AUTO_RX = re.compile(
    r"^(?:let|allow|permit)\s+(?:the agent|omni|it|[a-z]+)\s+(?:send|answer|reply to|handle)\s+(.+?)\s+(?:on (?:its|her|his|their) own|automatically|without (?:me|review|asking))\.?$", re.I)
_POLICY_REVIEW_RX = re.compile(
    r"^(?:stop (?:auto[- ]?)?(?:replying|sending|answering)(?: to)?|review|require review for|i want to review|always review|hold|don't (?:auto[- ]?)?(?:send|reply to))\s+(.+?)(?:\s+(?:before (?:sending|they go out)|first|myself))?\.?$", re.I)
_TONE_RX = re.compile(r"\b(warm|warmer|plain|plainer|formal|more formal|upbeat|casual|friendly|friendlier|professional)\b", re.I)
_SIGNOFF_RX = re.compile(r"^(?:sign (?:off|it|them|replies) as|signature should be|sign as)\s+(.+)$", re.I)
_RENAME_RX = re.compile(r"^(?:rename|call)\s+(?:the )?(?:stage )?[\"']?(.+?)[\"']?\s+(?:to|as)\s+[\"']?(.+?)[\"']?$", re.I)
_ADD_STAGE_RX = re.compile(r"^(?:add|create)\s+(?:a |an |new )*stage\s+(?:called |named )?[\"']?(.+?)[\"']?(?:\s+(before|after)\s+(.+))?$", re.I)
_REMOVE_RX = re.compile(r"^(?:remove|delete|drop|get rid of|hide)\s+(?:the )?(field|column|view|filter|rule|stage|source)\s+(?:for |called |named |about )?[\"']?(.+?)[\"']?$", re.I)
_SOURCE_RX = re.compile(r"^(?:add|connect|hook up|bring in)\s+(?:a |an |the |my )*(.+?)(?:\s+(?:as a source|source|account|leads?))?$", re.I)
_PLAYBOOK_RX = re.compile(r"^(?:remember that|remember|note that|always mention|always say|tell people|our|we)\s+(.+)$", re.I)
_NAME_RX = re.compile(r"^(?:call the (?:agent|assistant)|name the (?:agent|assistant)|the agent(?:'s name)? is|rename the agent to)\s+[\"']?([A-Za-z]+)[\"']?$", re.I)


def parse(text, spec):
    """Returns {"ops": [...], "reply": str, "hint": str}. ops may be empty."""
    t = _clean(text)
    low = t.lower()
    ops = []
    reply = ""

    m = _NAME_RX.match(t)
    if m:
        name = m.group(1)[:24]
        return {"ops": [{"op": "set_business", "agent_name": name}], "reply": f"Replies will come from {name}.", "hint": ""}

    m = _SIGNOFF_RX.match(t)
    if m:
        return {"ops": [{"op": "set_tone", "signoff": _clean(m.group(1))}],
                "reply": "Every reply will sign off that way.", "hint": ""}

    m = _REMOVE_RX.match(t)
    if m:
        kind, name = m.group(1).lower(), _clean(m.group(2))
        if kind in ("field", "column"):
            f = _find_field(spec, name)
            return {"ops": [{"op": "remove_field", "key": f["key"] if f else spec_mod.slug(name)}],
                    "reply": "Removed.", "hint": ""}
        if kind in ("view", "filter"):
            v = next((v for v in spec.get("views") or [] if v["name"].lower() == name.lower()), None)
            return {"ops": [{"op": "remove_view", "key": v["key"] if v else spec_mod.slug(name), "name": name}],
                    "reply": "Removed.", "hint": ""}
        if kind == "rule":
            r = next((r for r in spec.get("rules") or [] if name.lower() in (r.get("text") or r["name"]).lower()), None)
            return {"ops": [{"op": "remove_rule", "key": r["key"] if r else spec_mod.slug(name)}],
                    "reply": "Removed.", "hint": ""}
        if kind == "stage":
            return {"ops": [{"op": "remove_stage", "key": spec_mod.slug(name)}], "reply": "Removed.", "hint": ""}
        if kind == "source":
            return {"ops": [{"op": "remove_connector", "key": spec_mod.slug(name)}], "reply": "Removed.", "hint": ""}

    m = _RENAME_RX.match(t)
    if m and not _FIELD_RX.match(t):
        old, new = _clean(m.group(1)), _clean(m.group(2))
        s = next((s for s in spec.get("stages") or [] if s["label"].lower() == old.lower()), None)
        if s:
            return {"ops": [{"op": "rename_stage", "key": s["key"], "label": new}],
                    "reply": f"{old} is now {new}.", "hint": ""}
        f = _find_field(spec, old)
        if f:
            return {"ops": [{"op": "update_field", "key": f["key"], "label": new}],
                    "reply": f"{f['label']} is now {new}.", "hint": ""}

    m = _ADD_STAGE_RX.match(t)
    if m:
        label = _clean(m.group(1))
        pos = None
        if m.group(2) and m.group(3):
            ref = _clean(m.group(3)).lower()
            keys = [s["key"] for s in spec.get("stages") or []]
            idx = next((i for i, s in enumerate(spec.get("stages") or []) if s["label"].lower() == ref), None)
            if idx is not None:
                pos = idx if m.group(2).lower() == "before" else idx + 1
            del keys
        return {"ops": [{"op": "add_stage", "stage": {"label": label, "tone": "info"}, "position": pos}],
                "reply": f"Added the stage {label}.", "hint": ""}

    m = _POLICY_AUTO_RX.match(t)
    if m:
        cats = _categories_in(m.group(1))
        return {"ops": [{"op": "set_policy", "category": c, "mode": "auto_send"} for c in cats],
                "reply": "Those will send on their own, labeled in the thread.", "hint": ""}
    m = _POLICY_REVIEW_RX.match(t)
    if m or re.search(r"\b(review everything|i review everything|nothing (?:sends|goes out) without me)\b", low):
        cats = _categories_in(m.group(1)) if m else list(spec_mod.CATEGORIES)
        if re.search(r"\beverything\b|\ball replies\b|\ball messages\b", low):
            cats = list(spec_mod.CATEGORIES)
        return {"ops": [{"op": "set_policy", "category": c, "mode": "review"} for c in cats],
                "reply": "Those will wait for you before sending.", "hint": ""}

    if re.search(r"\b(tone|sound|voice|make (?:it|replies|the replies))\b", low) or re.search(r"\b(more|less) (formal|casual|friendly|warm)\b", low):
        tm = _TONE_RX.search(t)
        if tm:
            v = tm.group(1).lower()
            voice = {"warmer": "warm", "plainer": "plain", "more formal": "formal", "casual": "warm",
                     "friendly": "warm", "friendlier": "warm", "professional": "formal"}.get(v, v)
            return {"ops": [{"op": "set_tone", "voice": voice}], "reply": f"Replies will sound {voice}.", "hint": ""}

    m = _URGENT_RX.match(t)
    if m:
        cond = condition_from_phrase(spec, m.group(1))
        if cond:
            name = f"Mark {_clean(m.group(1))} as urgent"
            return {"ops": [{"op": "add_rule", "rule": {"name": name, "text": t, "when": cond,
                                                        "then": {"type": "set_priority", "value": "urgent"}}}],
                    "reply": "Done. They will sort to the top.", "hint": ""}

    m = _REJECT_RX.match(t)
    if m:
        cond = condition_from_phrase(spec, m.group(1))
        if not cond and re.search(r"\b(mention|say|saying|about|contain|talk)\w*\b", low):
            needle = _clean(re.sub(r"^(?:a |an |the )", "", _clean(m.group(1)), flags=re.I))
            if needle:
                cond = {"all": [{"field": "text", "op": "contains", "value": needle.lower()}]}
        if cond:
            return {"ops": [{"op": "add_rule", "rule": {"name": f"Auto reject {_clean(m.group(1))}", "text": t, "when": cond,
                                                        "then": {"type": "auto_reject", "stage": _terminal_stage(spec)}}}],
                    "reply": "Matching conversations move to the declined stage with a polite decline drafted for you.",
                    "hint": ""}
        return {"ops": [], "reply": "", "hint": _hint(spec, "reject")}

    m = _MOVE_RX.match(t)
    if m and not _FIELD_RX.match(t):
        stage_name = _clean(m.group(2)).lower()
        stage = next((s for s in spec.get("stages") or [] if s["label"].lower() == stage_name or s["key"] == spec_mod.slug(stage_name)), None)
        cond = condition_from_phrase(spec, m.group(1))
        if stage and cond:
            return {"ops": [{"op": "add_rule", "rule": {"name": f"Move {_clean(m.group(1))} to {stage['label']}", "text": t,
                                                        "when": cond, "then": {"type": "set_stage", "stage": stage["key"]}}}],
                    "reply": "Done.", "hint": ""}

    m = _VIEW_RX.match(t)
    if m:
        phrase = m.group(1) or ""
        cond = condition_from_phrase(spec, phrase)
        if cond:
            name = _view_name(phrase)
            return {"ops": [{"op": "add_view", "view": {"name": name, "filter": cond,
                                                        "sort": {"by": "last_message_at", "dir": "desc"}}, "pinned": True}],
                    "reply": f"{name} is on the front of the inbox.", "hint": ""}
        return {"ops": [], "reply": "", "hint": _hint(spec, "view")}

    m = _FLAG_RX.match(t)
    if m and not _FIELD_RX.match(t):
        phrase = m.group(1)
        cond = condition_from_phrase(spec, phrase)
        if not cond and re.search(r"\b(mention|say|saying|about|contain|talk)\w*\b", low):
            # "flag anyone mentioning a court date": the words after the cue are
            # what to look for in the text itself.
            needle = _clean(re.sub(r"^(?:a |an |the )", "", _clean(phrase), flags=re.I))
            if needle:
                cond = {"all": [{"field": "text", "op": "contains", "value": needle.lower()}]}
        if cond:
            label = _label_from_phrase(re.sub(r"^(?:mention(?:s|ing)?|who|that|with)\s+", "", _clean(phrase), flags=re.I))[:30]
            return {"ops": [{"op": "add_rule", "rule": {"name": f"Flag {_clean(phrase)}", "text": t, "when": cond,
                                                        "then": {"type": "flag", "label": label, "tone": "warn"}}}],
                    "reply": "Flagged conversations show the tag in the list and in the thread.", "hint": ""}
        return {"ops": [], "reply": "", "hint": _hint(spec, "flag")}

    m = _FIELD_RX.match(t)
    if m and re.search(r"\b(field|column|track|extract|pull out|capture)\b", low):
        phrase = m.group(1)
        typ, options = _infer_type(phrase)
        label = _label_from_phrase(re.sub(r"\(.*?\)|one of .*$", "", phrase, flags=re.I))
        if label:
            field = {"label": label, "type": typ, "options": options, "hint": _clean(phrase),
                     "filterable": True, "show_in_list": bool(re.search(r"\b(in the list|on the row|up front)\b", low))}
            return {"ops": [{"op": "add_field", "field": field}],
                    "reply": f"I will read every conversation for {label.lower()} and show it in the details panel.",
                    "hint": ""}

    m = _SOURCE_RX.match(t)
    if m and re.search(r"\b(source|account|connect|leads?|zillow|gmail|outlook|instagram|facebook|indeed|avvo|zocdoc|fax|csv|zapier|webhook|sms|text)\b", low):
        want = m.group(1).lower()
        for kind, meta in spec_mod.CONNECTOR_KINDS.items():
            if meta["label"].lower() in want or kind.replace("_", " ") in want:
                return {"ops": [{"op": "add_connector", "kind": kind}],
                        "reply": f"{meta['label']} is on the Connections page.", "hint": ""}

    m = _PLAYBOOK_RX.match(t)
    if m:
        line = "- " + _clean(m.group(1))
        pb = (spec.get("playbook") or "").strip()
        return {"ops": [{"op": "set_playbook", "text": (pb + "\n" + line).strip()}],
                "reply": "Added to the playbook, so drafts can use it.", "hint": ""}

    return {"ops": ops, "reply": reply, "hint": _hint(spec, "")}


def _categories_in(text):
    low = text.lower()
    cats = []
    if re.search(r"\b(pric|cost|fee|rate|insurance|money|tuition|rent|quote)", low):
        cats.append("pricing")
    if re.search(r"\b(medical|legal|clinical|eligib|qualif|diagnos|advice)", low):
        cats.append("medical_legal")
    if re.search(r"\b(routine|acknowledg|schedul|booking|common|simple|everything|all)", low):
        cats.append("routine")
    return cats or ["routine"]


def _hint(spec, kind):
    labels = ", ".join(f["label"].lower() for f in (spec.get("fields") or [])[:8])
    if kind == "view":
        return f"Tell me which field to filter on. You have: {labels}."
    if kind in ("flag", "reject"):
        return f"Tell me what to look for, for example a word people mention or one of your fields: {labels}."
    return ("I can add a field, add a view, add a rule (flag, mark urgent, auto reject, move to a stage), "
            "change what sends on its own, change the tone, rename or add a stage, add a source, "
            f"or add something to the playbook. Your fields: {labels}.")


LLM_SYSTEM = """You turn a request to change an intake inbox's setup into JSON patch ops. You are given the
current setup (fields, stages, views, rules, connectors) and the request. Use ONLY these ops:
add_field {field:{label,type text|number|enum|bool|date|list,options,hint}}, add_view {view:{name,filter}},
add_rule {rule:{name,text,when,then:{type flag|set_priority|set_stage|auto_reject, label|value|stage}}},
set_policy {category routine|pricing|medical_legal, mode auto_send|review}, set_tone {voice warm|plain|formal|upbeat, signoff},
rename_stage {key,label}, add_stage {stage:{label}}, remove_field {key}, remove_view {key}, add_connector {kind}, set_playbook {text}.
Filters use {"all":[{"field":"f.<key>","op":"eq|neq|in|not_in|contains|gt|gte|lt|lte|is_unknown|within_days|older_than_days","value":...}]};
built-in fields: stage, status, priority, assignee, awaiting_reply, source, text. Never invent field keys that are not in the setup
unless you are adding that field in the same response. Never add fields about race, religion, national origin, familial status,
disability, age or immigration status. Never use em dashes. Respond with ONLY {"ops":[...],"reply":"one short sentence"}."""


def parse_with_llm(text, spec):
    if not llm.available():
        return None
    catalog = {
        "fields": [{"key": f["key"], "label": f["label"], "type": f["type"], "options": f.get("options")}
                   for f in spec.get("fields") or []],
        "stages": [{"key": s["key"], "label": s["label"]} for s in spec.get("stages") or []],
        "views": [{"key": v["key"], "name": v["name"]} for v in spec.get("views") or []],
        "rules": [{"key": r["key"], "text": r.get("text")} for r in spec.get("rules") or []],
        "connectors": [c["kind"] for c in spec.get("connectors") or []],
    }
    import json
    out = llm.chat_json(LLM_SYSTEM, f"SETUP (JSON): {json.dumps(catalog)}\n\nREQUEST: {text}\n\nRespond with JSON now.")
    if not out or not isinstance(out.get("ops"), list):
        return None
    ops = [o for o in out["ops"] if isinstance(o, dict) and o.get("op") in spec_mod.PATCH_OPS]
    return {"ops": ops, "reply": str(out.get("reply") or "")[:200], "hint": ""} if ops else None
