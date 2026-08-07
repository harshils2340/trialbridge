"""Content for the site-side feature pages (/for-sites/<slug>).

One dict per pillar of the coordinator OS. Kept as data rather than four
near-identical templates so the pages cannot drift apart, and so the honest bits
(status, limits, legal) are impossible to forget on a new page.

House rules for anything added here:
  * `status` must be truthful. "build" means not shipped, and the page says so.
  * `limits` is not optional. Every page states what it does NOT do.
  * `legal` must stay consistent with matcher/COMPLIANCE.md and the security
    section of for_sites.html (e.g. retention controls are In progress, SOC 2 is
    In progress, no money ever moves per referral).
"""

FEATURES = {
    "intake": {
        "nav": "One inbox",
        "icon": "inbox",
        "blurb": "Every applicant, every source, one queue.",
        "eyebrow": "Intake",
        "title": "Every applicant lands in one queue.",
        "lead": "Applicants arrive from your listing, your ads, physician referrals, "
                "your EHR, inbound email and last month's spreadsheet. BridgeMD "
                "collects all of it into a single queue per study, deduplicated and "
                "already tagged, so nothing sits unworked because it came in through "
                "the wrong door.",
        "status": ("live", "Live today"),
        "stage": "Moves found and contacted: fewer applicants go cold before anyone reads them.",
        "flow": [
            {"t": "Sources come in",
             "d": "Listing, ads, referral link, EHR match, email, CSV import."},
            {"t": "Matched to a study",
             "d": "Each person is tied to the trial they applied for, not a general pile."},
            {"t": "Deduplicated",
             "d": "The same person from two sources becomes one record with both sources kept."},
            {"t": "Worked as one queue",
             "d": "Your team opens one list per study, with status and last contact visible."},
        ],
        "does": [
            "One queue per study, with the source of every applicant kept on the record.",
            "Duplicate detection across sources, so the same person is not screened twice.",
            "Consent and contact state on the row, so you know who you may contact.",
            "Import of an existing spreadsheet, so a backlog is not lost on day one.",
        ],
        "limits": [
            "It does not pull patients out of your EHR on its own. EHR matching runs "
            "only where a site has connected it and a patient has authorized it.",
            "It does not contact anyone automatically. Outreach happens when your "
            "team sends it.",
        ],
        "legal": [
            {"h": "The patient opts in first",
             "d": "A person applies or authorizes the share before their details reach "
                  "a site. There is no list of patients being shopped around."},
            {"h": "De-identified by default",
             "d": "Matching runs on structured facts: age, diagnosis, medications, labs. "
                  "Identifiers are revealed to the study team the person applied to, "
                  "and not before."},
            {"h": "Recruitment copy stays truthful",
             "d": "Listings publish neutral facts from ClinicalTrials.gov. Any ad or "
                  "recruitment claim needs IRB/REB approval before it runs, and we do "
                  "not invent sponsor claims to fill a page."},
            {"h": "Nobody is paid per applicant",
             "d": "Sites and physicians pay nothing. Sponsors and CROs license the "
                  "software at a flat rate. No payment is ever tied to the volume or "
                  "value of referrals or enrollments."},
        ],
    },
    "pre-screen": {
        "nav": "AI pre-screen",
        "icon": "sparkle",
        "blurb": "Scored against the protocol, reasons shown.",
        "eyebrow": "Pre-screen",
        "title": "Know who is worth a call before you open the chart.",
        "lead": "Every applicant is checked against the study's inclusion and exclusion "
                "criteria and comes back with a verdict plus the lines behind it. You "
                "still decide. The point is that you decide in a minute instead of "
                "reading forty screeners to find the four that matter.",
        "status": ("live", "Live today"),
        "stage": "Moves contacted to screened: coordinator time goes to the applicants who can actually enroll.",
        "flow": [
            {"t": "Criteria are read in",
             "d": "Inclusion and exclusion criteria come from the protocol or the "
                  "ClinicalTrials.gov record for the study."},
            {"t": "The applicant is checked",
             "d": "Screener answers, and authorized record values where available, are "
                  "compared criterion by criterion."},
            {"t": "A verdict with reasons",
             "d": "Likely eligible, needs review, or likely ineligible, with the value "
                  "and the criterion shown on every line."},
            {"t": "A human decides",
             "d": "Your team confirms or overrides, and the decision is logged with who "
                  "made it."},
        ],
        "does": [
            "A per-criterion breakdown, so you can see why a verdict came out that way.",
            "Flags for the criteria it could not check, instead of guessing at them.",
            "A queue sorted so likely-eligible applicants surface first.",
            "A log of every verdict, override and status change.",
        ],
        "limits": [
            "It does not determine eligibility. It is decision support; the study team "
            "under the PI decides who is eligible.",
            "It does not replace source verification. Values a patient reported still "
            "need confirming against the record at the screening visit.",
            "It does not screen on criteria that need a clinical judgment call it "
            "cannot make. Those come back as needs review.",
        ],
        "legal": [
            {"h": "Decision support, not a decision",
             "d": "The verdict is a suggestion with its reasoning exposed. Eligibility "
                  "is determined by the study team, and the page never presents the "
                  "model as the decider."},
            {"h": "Every line is traceable",
             "d": "Each check names the criterion it was measured against and the value "
                  "it used, so a monitor can follow the reasoning."},
            {"h": "Minimum necessary data",
             "d": "Scoring runs on structured, minimized facts. Where identifiable data "
                  "is processed, it is under a Business Associate Agreement, and it is "
                  "never used to train a model."},
            {"h": "Full audit trail",
             "d": "Who reviewed an applicant, what they changed and when, kept for "
                  "inspection."},
        ],
    },
    "scheduling": {
        "nav": "Scheduling",
        "icon": "calendar",
        "blurb": "Booking, invites and reminders, no phone tag.",
        "eyebrow": "Scheduling",
        "title": "Accept once. Booking and reminders handle themselves.",
        "lead": "The gap between yes and a screening visit on the calendar is where "
                "enrollment quietly dies. Approve an applicant and BridgeMD sends the "
                "booking link, puts the visit on the calendar, and reminds the patient "
                "before the day.",
        "status": ("live", "Live today"),
        "stage": "Moves screened to enrolled: fewer no-shows and fewer people lost between the reply and the visit.",
        "flow": [
            {"t": "You approve",
             "d": "One action on an applicant your team wants to see."},
            {"t": "A booking link goes out",
             "d": "By email or SMS, on a private link, for the visit type you chose."},
            {"t": "The patient picks a slot",
             "d": "The visit lands on your calendar with a proper invite attached."},
            {"t": "Reminders run",
             "d": "The patient is reminded before the visit, and the applicant moves to "
                  "Screening visit on your board."},
        ],
        "does": [
            "Private booking links per applicant, so nobody sees anyone else's slots.",
            "Calendar invites your team can open in whatever calendar they use, plus "
            "Google Calendar sync where a site has connected it.",
            "Email and SMS reminders before the visit.",
            "Visit status on the applicant record, so the funnel updates itself.",
        ],
        "limits": [
            "It is not a clinic-wide scheduling system. It books study visits, not your "
            "whole practice.",
            "SMS requires a site to turn it on and provide a number. Email works out of "
            "the box.",
        ],
        "legal": [
            {"h": "Minimum necessary in every message",
             "d": "Reminders reference the appointment and the site. They do not "
                  "announce a diagnosis or a study drug to whoever picks up the phone."},
            {"h": "Opt out at any time",
             "d": "Every message carries a way to stop, and a stop is honoured across "
                  "the platform, not just for one study."},
            {"h": "Consent before contact",
             "d": "Only applicants who asked to be contacted are contacted, and the "
                  "record shows when they agreed."},
            {"h": "Retention controls are in progress",
             "d": "Setting how long scheduling and message history is kept, and having "
                  "it delete itself after that, is being built. We are not claiming it "
                  "as shipped."},
        ],
    },
    "documents": {
        "nav": "Documents AI",
        "icon": "file-check",
        "blurb": "Protocol answers, amendment diffs, one vault.",
        "eyebrow": "Sponsor and CRO paperwork",
        "title": "The work that is not the patient.",
        "lead": "Amendments never stop, the same essential document gets asked for four "
                "times, and the answer you need is on page 148. You cannot paste a "
                "protocol into a consumer chatbot without breaching your "
                "confidentiality agreement, so it stays manual. This is the part we are "
                "building to take off your desk.",
        "status": ("live", "Live today"),
        "stage": "Efficiency: cuts startup and amendment cycle time, and hands the hours back to screening and enrolling.",
        "flow": [
            {"t": "Documents land in a study workspace",
             "d": "Protocol, ICF, amendments and essential documents, scoped to that "
                  "study and the people on it."},
            {"t": "Ask in plain language",
             "d": "Answers come back with the section they came from, so you can check "
                  "them against the approved document."},
            {"t": "Amendments get diffed",
             "d": "Version to version: what was added, tightened or removed, and what it "
                  "means for visits, budget and re-consent."},
            {"t": "One current version",
             "d": "Whoever needs a document pulls the current one instead of asking you "
                  "to re-send it, and expiring documents get flagged before they lapse."},
        ],
        "does": [
            "Answers with citations back to the section, not unsourced summaries.",
            "A version diff that names the downstream work: visit schedule, budget "
            "impact, re-consent list.",
            "A vault for the documents you are asked for repeatedly, with expiry dates.",
            "Access scoped per study and per role, with an audit trail.",
        ],
        "limits": [
            "It answers from the documents you upload - it will not invent policy "
            "that is not in them.",
            "It is not a regulatory record. The approved protocol and the IRB/REB "
            "approved consent form remain the source of truth; the diff and the answers "
            "are working aids.",
            "It does not file anything with an IRB/REB or a sponsor on your behalf.",
        ],
        "legal": [
            {"h": "Never trained on your documents",
             "d": "Sponsor and CRO material is processed under enterprise API terms with "
                  "no training on your content and no retention on the provider side. "
                  "That is the whole reason this exists instead of a consumer chatbot."},
            {"h": "Confidentiality respected",
             "d": "Documents stay in the study workspace, scoped by role. They are "
                  "yours, they are not shared across customers, and they are deleted on "
                  "request."},
            {"h": "Answers cite their source",
             "d": "Every answer points at the section it came from, so nothing has to be "
                  "taken on faith and a monitor can follow it."},
            {"h": "BAA available",
             "d": "Where a site or sponsor needs a Business Associate Agreement in place "
                  "before anything moves, we sign one."},
        ],
    },
}

ORDER = ["intake", "pre-screen", "scheduling", "documents"]


def nav_items():
    """The pillars in nav order, each with its slug, for the header dropdown."""
    return [dict(FEATURES[s], slug=s) for s in ORDER if s in FEATURES]


def get(slug: str):
    """Feature dict plus its slug and its neighbours, or None for an unknown slug."""
    if slug not in FEATURES:
        return None
    i = ORDER.index(slug)
    nxt = ORDER[(i + 1) % len(ORDER)]
    return dict(FEATURES[slug], slug=slug,
                next_slug=nxt, next_nav=FEATURES[nxt]["nav"])
