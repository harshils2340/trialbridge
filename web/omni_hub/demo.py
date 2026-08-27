"""Static demo inbox shown on the /omni landing page.

This is a *mock*, not a workspace: no database, no LLM, no per-visitor state.
It exists so the landing page can show the thing the builder produces - a
simplified copy of the real product inbox at /app/inbox - while the visitor
types their own prompt. Every row, message and draft below is fixed copy, and
switching rows or views happens in the browser (see static/omni.js).

Keep this file boring. If something here needs logic, it belongs in the real
inbox, not in the landing page's demo.
"""

# The prompt the hero box starts life pre-filled with. It matches the
# clinical_trial_site seed template, so pressing Enter runs exactly the same
# interview a visitor gets by picking that example chip.
DEMO_TEMPLATE_KEY = "clinical_trial_site"
DEMO_PROMPT = (
    "We run a clinical research site with three trials recruiting right now. "
    "Leads come from ClinicalTrials.gov, Facebook ads, our trial finder form, "
    "and referring doctors by email. Pull out their age, condition, medications "
    "and how far they will travel, flag anyone who looks excluded, and draft "
    "the pre-screen reply for me."
)

BRAND = "Northwind Clinical Research"
STUDY = "Type 2 diabetes"

SOURCES = [
    {"kind": "ctgov", "icon": "ctgov", "mono": "CT", "label": "ClinicalTrials.gov"},
    {"kind": "facebook", "icon": "facebook", "mono": "FB", "label": "Facebook lead ads"},
    {"kind": "gmail", "icon": "gmail", "mono": "GM", "label": "Study email"},
    {"kind": "form", "icon": "", "mono": "TF", "label": "Trial finder form"},
]

# Short names on purpose: all four have to sit in one narrow column without
# scrolling, the way they do in the real inbox.
VIEWS = [
    {"key": "all", "name": "All"},
    {"key": "needs_reply", "name": "Needs reply"},
    {"key": "likely", "name": "Likely fits"},
    {"key": "check", "name": "Flagged"},
]

# Stage tones are the same names the real inbox uses, so the pills read
# identically on both pages.
THREADS = [
    {
        "id": "t1", "name": "Dana Whitfield", "source": "ctgov", "time": "12m",
        "subject": "Saw the diabetes study on ClinicalTrials.gov",
        "snippet": "I am 54, type 2 for about nine years, on metformin only. "
                   "I can drive to your Evanston site.",
        "stage": "New", "tone": "brand", "awaiting": True,
        "views": ["all", "needs_reply", "likely"],
        "facts": [
            ("Age", "54", True), ("Condition", "Type 2 diabetes", True),
            ("On insulin", "No", True), ("Travel", "Up to 45 min", True),
            ("A1c", "Unknown", False),
        ],
        "messages": [
            {"who": "them", "author": "Dana Whitfield", "time": "12m",
             "body": "Hi - I saw the type 2 diabetes study listed on "
                     "ClinicalTrials.gov and wanted to see if I qualify. I am 54, "
                     "diagnosed about nine years ago, and I take metformin only, "
                     "no insulin. I am in Skokie so Evanston is an easy drive."},
        ],
        "draft": "Hi Dana - thanks for reaching out about the type 2 diabetes "
                 "study. From what you have shared you look like a good fit for "
                 "pre-screening. The next step is a 15 minute phone call to go "
                 "through a few eligibility questions. Would Thursday morning "
                 "work for you?",
    },
    {
        "id": "t2", "name": "Marcus Bell", "source": "facebook", "time": "48m",
        "subject": "Facebook lead form - diabetes trial",
        "snippet": "Started insulin last spring. Interested but only if it is "
                   "close to Oak Park.",
        "stage": "Pre-screen", "tone": "warn", "awaiting": True,
        "views": ["all", "needs_reply", "check"],
        "facts": [
            ("Age", "61", True), ("Condition", "Type 2 diabetes", True),
            ("On insulin", "Yes", True), ("Travel", "Oak Park only", True),
            ("A1c", "8.4", True),
        ],
        "messages": [
            {"who": "them", "author": "Marcus Bell", "time": "48m",
             "body": "Filled in your form on Facebook. I am 61, type 2, and I "
                     "started on insulin last spring. Last A1c was 8.4. I would "
                     "want somewhere near Oak Park if that is possible."},
            {"who": "note", "author": "Bridget", "time": "48m",
             "body": "Insulin exclusion check: this protocol excludes anyone on "
                     "insulin at screening. Flagged before you reply."},
        ],
        "draft": "Hi Marcus - thank you for your interest. For this study we can "
                 "only enrol people who are not currently on insulin, so you "
                 "would not be eligible this time. We have two other trials "
                 "opening soon - may I keep your details and reach out when one "
                 "of them starts?",
    },
    {
        "id": "t3", "name": "Dr. Priya Raman", "source": "gmail", "time": "2h",
        "subject": "Referral - two patients for your diabetes trial",
        "snippet": "Sending over two of my patients who fit your criteria. "
                   "Charts attached.",
        "stage": "Screening", "tone": "violet", "awaiting": False,
        "views": ["all", "likely"],
        "facts": [
            ("Referred by", "Dr. Priya Raman", True),
            ("Patients", "2", True), ("Condition", "Type 2 diabetes", True),
            ("On insulin", "No", True), ("Travel", "Unknown", False),
        ],
        "messages": [
            {"who": "them", "author": "Dr. Priya Raman", "time": "2h",
             "body": "Hi - I have two patients who I think would fit the type 2 "
                     "criteria you sent round. Neither is on insulin. Happy to "
                     "send charts if you confirm they are worth screening."},
            {"who": "you", "author": "You", "time": "1h",
             "body": "Thank you Dr. Raman - please do send them over. I will get "
                     "both booked for pre-screen this week."},
        ],
        "draft": "Dr. Raman - both charts came through, thank you. I have put "
                 "them in the screening queue and will confirm visit dates by "
                 "Friday.",
    },
    {
        "id": "t4", "name": "Alicia Moreno", "source": "form", "time": "5h",
        "subject": "Trial finder form - is there an age limit?",
        "snippet": "My mother is 79 and has type 2. Is she too old for this?",
        "stage": "New", "tone": "brand", "awaiting": True,
        "views": ["all", "needs_reply", "check"],
        "facts": [
            ("Age", "79", True), ("Condition", "Type 2 diabetes", True),
            ("On insulin", "Unknown", False), ("Travel", "Unknown", False),
            ("Asking for", "A relative", True),
        ],
        "messages": [
            {"who": "them", "author": "Alicia Moreno", "time": "5h",
             "body": "I filled in your trial finder for my mother. She is 79 and "
                     "has had type 2 for a long time. Is there an upper age "
                     "limit for this study?"},
        ],
        "draft": "Hi Alicia - thanks for asking on your mother's behalf. This "
                 "study enrols adults up to 75, so she would not be eligible for "
                 "this one. I would be glad to check her against our other open "
                 "trials if you can share a little more.",
    },
    {
        "id": "t5", "name": "Grant Okafor", "source": "ctgov", "time": "Yesterday",
        "subject": "Took part in a study with you in 2023",
        "snippet": "I was in your hypertension trial two years ago. Can I join "
                   "this one?",
        "stage": "Outreach", "tone": "info", "awaiting": False,
        "views": ["all", "likely"],
        "facts": [
            ("Age", "47", True), ("Condition", "Type 2 diabetes", True),
            ("On insulin", "No", True), ("Travel", "Up to 30 min", True),
            ("Repeat participant", "Yes", True),
        ],
        "messages": [
            {"who": "them", "author": "Grant Okafor", "time": "Yesterday",
             "body": "Hello again - I took part in your hypertension study back "
                     "in 2023. I have since been diagnosed with type 2 and saw "
                     "you are recruiting. Am I allowed to join another one?"},
            {"who": "note", "author": "Bridget", "time": "Yesterday",
             "body": "Repeat participant: Grant is already in your records from "
                     "the 2023 hypertension study."},
        ],
        "draft": "Grant, good to hear from you. Taking part before does not rule "
                 "you out here - there is a 90 day washout and you are well past "
                 "it. Shall I book you in for a pre-screen call?",
    },
]

# The choices the demo prompt stands in for, shown beside the mock so it reads
# as "this is what your sentence decided" rather than a generic screenshot.
DECISIONS = [
    ("Sources", "ClinicalTrials.gov, Facebook ads, trial finder, study email"),
    ("Fields", "Age, condition, medications, travel radius, A1c"),
    ("Views", "Needs reply, Likely fits, Flagged"),
    ("Autonomy", "Bridget drafts, you press Send"),
]


def landing_demo():
    """Everything templates/omni/_demo_inbox.html needs, in one dict."""
    return {
        "brand": BRAND, "study": STUDY, "sources": SOURCES, "views": VIEWS,
        "threads": THREADS, "decisions": DECISIONS,
        "prompt": DEMO_PROMPT, "template_key": DEMO_TEMPLATE_KEY,
    }
