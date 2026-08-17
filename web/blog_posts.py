"""Content for the public resources section (/blog and /blog/<slug>).

Editorial rules (these are not optional, see matcher/COMPLIANCE.md and the
.cursor/rules on claims discipline):

  * Every statistic is a THIRD-PARTY, published industry/academic figure,
    attributed to a named source, and hedged ("commonly cited", "studies have
    reported"). We do not invent precise numbers, and we never present a chart
    figure as BridgeMD's own result.
  * No claims about BridgeMD's OWN outcomes ("we cut enrollment X%") unless
    measured and cited elsewhere. These posts describe the problem and the
    approach, not guaranteed results.
  * No sponsor/recruitment claims that would need IRB/REB approval.
  * CTAs stay soft and truthful: "see how we handle X", never a promise.

`body` is trusted HTML authored in-repo and rendered with |safe. Keep it to
<h2>/<p>/<ul>/<li>/<blockquote>/<a> plus the chart figure component:

  <figure class="blog-fig">
    <div class="blog-fig-t">Title of the chart</div>
    <div class="blog-bars">
      <div class="blog-bar">
        <span class="blog-bar-l">Row label</span>
        <span class="blog-bar-track"><span class="blog-bar-fill" style="width:80%"></span></span>
        <span class="blog-bar-v">80%</span>
      </div>
    </div>
    <figcaption>Source line. Always attribute; hedge the number.</figcaption>
  </figure>

Fill variants: default (accent), .is-2 (lighter accent), .is-muted (grey).
Every chart MUST have a figcaption naming its source.
"""

POSTS = {
    # ---- Patient / discovery (top of the "found" funnel) ---------------------
    "why-so-few-patients-hear-about-clinical-trials": {
        "title": "Why so few patients ever hear about clinical trials",
        "dek": "Most patients would consider a trial. Most are never told one "
               "exists. The recruitment gap is awareness, not willingness.",
        "tag": "Access",
        "date": "2026-08-16",
        "read": 5,
        "author": "BridgeMD Team",
        "audience": "patient",
        "body": """
<p>Patients are often assumed to be uninterested in clinical trials. The data shows the opposite. When patients are asked directly, most are open to participating. The gap is awareness: trials and the patients who would join them never find each other.</p>

<h2>Most patients would say yes if asked</h2>
<p>In repeated public surveys, the willingness is high and the awareness is low. The Center for Information and Study on Clinical Research Participation (CISCRP) and Research!America have both reported that a large majority of the public would consider a trial, yet most say they were never informed one was an option for their condition.</p>

<figure class="blog-fig">
  <div class="blog-fig-t">Willingness vs. awareness among patients</div>
  <div class="blog-bars">
    <div class="blog-bar">
      <span class="blog-bar-l">Would consider a trial</span>
      <span class="blog-bar-track"><span class="blog-bar-fill" style="width:84%"></span></span>
      <span class="blog-bar-v">~84%</span>
    </div>
    <div class="blog-bar">
      <span class="blog-bar-l">Never told one existed</span>
      <span class="blog-bar-track"><span class="blog-bar-fill is-2" style="width:75%"></span></span>
      <span class="blog-bar-v">~75%</span>
    </div>
  </div>
  <figcaption>Commonly cited ranges from CISCRP Perceptions &amp; Insights studies and Research!America public opinion surveys. Figures vary by year and population.</figcaption>
</figure>

<h2>Referral is how most people actually find out</h2>
<p>Patients overwhelmingly say they would trust and act on a recommendation from their own doctor. But most clinicians are not running trials and have no easy way to know which studies are recruiting nearby. So the single most trusted channel - a doctor saying "there is a study you might fit" - is also the one that most often does not happen.</p>

<h2>The result is a tiny, unrepresentative pool</h2>
<p>The consequences show up most clearly in oncology, where the National Cancer Institute has long reported that only a small fraction of adult cancer patients - commonly cited as under 5% - ever enroll in a treatment trial. When so few people enroll, and the ones who do are the few who happened to hear, studies also skew toward whoever has the access, time, and proximity to find them.</p>

<blockquote>The task is not convincing patients to care. It is making recruiting trials findable to the patients who already would join.</blockquote>

<h2>What actually closes the gap</h2>
<p>Discovery has to meet patients where they already are: a search that works in plain English, listings a normal person can read, and a way to express interest without a phone tree. That is the "found" and "contacted" end of the funnel, and it is where BridgeMD starts. You can <a href="/">search recruiting trials near you</a> and only ask a study team to reach out when you want them to.</p>

<p class="blog-note">Survey figures above are third-party estimates and vary by study, year, and population. They are cited as commonly-referenced ranges, not BridgeMD data.</p>
""",
        "sources": [
            "CISCRP (Center for Information and Study on Clinical Research "
            "Participation) Perceptions & Insights public survey series.",
            "Research!America national public opinion surveys on clinical trial "
            "participation.",
            "National Cancer Institute (NCI) data on adult cancer trial "
            "enrollment rates.",
        ],
    },
    "what-happens-after-you-apply-for-a-clinical-trial": {
        "title": "What happens after you apply for a clinical trial?",
        "dek": "Applying is not the same as enrolling. Here is the normal path from "
               "interest to a screening call, consent, and a final study-team "
               "decision.",
        "tag": "For patients",
        "date": "2026-07-28",
        "read": 4,
        "author": "BridgeMD Team",
        "audience": "patient",
        "body": """
<p>Most people hear about clinical trials from a doctor, a website, an ad, or a friend. The confusing part is what happens next. Does applying mean you are in the study? Who sees your information? How long does it take?</p>

<p>The short answer: applying only tells the study team you are interested. The site still has to confirm the details, check the study rules, and walk you through consent before you can enroll.</p>

<h2>Step 1: You find a study that may fit</h2>
<p>A trial usually lists the condition being studied, where it is running, the age range, and some high-level eligibility rules. Those rules can be hard to read, so BridgeMD helps you search by condition or plain English and shows recruiting studies nearby.</p>

<h2>Step 2: You send your interest</h2>
<p>When you apply, you share only the details needed for the study team to follow up. That may include your name, contact information, age range, condition, and a few screening answers. Applying does not force you to join, and it does not mean you are eligible yet.</p>

<h2>Step 3: The coordinator checks if it is worth a screening call</h2>
<p>The coordinator reviews your answers against the study's inclusion and exclusion rules. They may ask about medications, recent lab results, other studies you are in, or parts of your medical history. If something is unclear, they will ask before making a decision.</p>

<h2>Step 4: You learn the details before deciding</h2>
<p>If the study still looks possible, the site explains the schedule, risks, possible benefits, costs, payment or reimbursement if listed, and what data they collect. You can ask questions. You can say no. You can leave a study later, too.</p>

<h2>Step 5: Enrollment only happens after consent and final checks</h2>
<p>Enrollment is a site decision, not a website decision. The study team confirms eligibility, reviews consent, and may run labs or exams before enrolling you. Many people who apply are not eligible, and that is normal - screening exists precisely to catch that.</p>

<blockquote>BridgeMD makes finding and applying easier. It does not guarantee enrollment, treatment, payment, or eligibility.</blockquote>

<p>If you want to start, <a href="/">search recruiting trials near you</a> and apply only to studies you actually want the site to contact you about.</p>
""",
        "sources": [
            "ClinicalTrials.gov patient education on participating in clinical "
            "studies.",
            "FDA and HHS materials on informed consent and clinical trial "
            "participation.",
        ],
    },
    "how-clinical-trial-eligibility-works": {
        "title": "Why clinical trial eligibility rules are so specific",
        "dek": "A trial can be the right condition but still not be the right study. "
               "Screening exists to protect participants and keep the research "
               "answer clean - and it fails a lot of applicants by design.",
        "tag": "Eligibility",
        "date": "2026-08-02",
        "read": 5,
        "author": "BridgeMD Team",
        "audience": "patient",
        "body": """
<p>Clinical trial eligibility can feel frustrating. You may have the exact condition a study is about and still be told you do not qualify. That does not mean you did anything wrong. It means the study has strict rules - and being turned down is far more common than most applicants expect.</p>

<h2>Screening is designed to say "no" often</h2>
<p>Across therapeutic areas, published analyses report that a large share of screened patients never make it to enrollment. Screen-failure rates vary widely by disease and protocol, but rates around a third - and considerably higher in some fields - are routinely reported. A "no" is usually the system working, not a mistake.</p>

<figure class="blog-fig">
  <div class="blog-fig-t">Roughly what a screening funnel looks like</div>
  <div class="blog-bars">
    <div class="blog-bar">
      <span class="blog-bar-l">Applied / expressed interest</span>
      <span class="blog-bar-track"><span class="blog-bar-fill" style="width:100%"></span></span>
      <span class="blog-bar-v">100%</span>
    </div>
    <div class="blog-bar">
      <span class="blog-bar-l">Pass pre-screen</span>
      <span class="blog-bar-track"><span class="blog-bar-fill is-2" style="width:55%"></span></span>
      <span class="blog-bar-v">~55%</span>
    </div>
    <div class="blog-bar">
      <span class="blog-bar-l">Enroll after full screening</span>
      <span class="blog-bar-track"><span class="blog-bar-fill is-muted" style="width:35%"></span></span>
      <span class="blog-bar-v">~35%</span>
    </div>
  </div>
  <figcaption>Illustrative funnel. Screen-failure rates commonly reported around a third and much higher in some areas; exact shape varies by protocol and therapeutic area.</figcaption>
</figure>

<h2>Inclusion rules say who the study is looking for</h2>
<p>Inclusion criteria describe the people the study is designed for. They can include age, diagnosis, disease stage, past treatments, current medications, lab values, or how long someone has had a condition.</p>

<h2>Exclusion rules say when the study may not be safe or useful</h2>
<p>Exclusion criteria list things that may make participation unsafe or make the study results harder to interpret. Examples include pregnancy, certain medications, another active trial, a recent procedure, or a lab value outside the study range.</p>

<h2>One word can matter</h2>
<p>Rules often turn on timing and detail. "Within 30 days" is different from "within 90 days". "Stable dose" is different from "any dose". A medication brand name may matter less than the drug class. This is why coordinators ask follow-up questions before deciding - and why a good first-pass filter saves everyone time.</p>

<h2>What you can do before applying</h2>
<ul>
<li>Know your diagnosis and roughly when you were diagnosed.</li>
<li>Keep a current medication list.</li>
<li>Know whether you are in another trial now.</li>
<li>Ask the coordinator what made you eligible, ineligible, or uncertain.</li>
</ul>

<p class="blog-note">Screen-failure figures are third-party estimates that vary widely by therapeutic area; the funnel above is illustrative, not BridgeMD data. This article is general education, not medical advice.</p>
""",
        "sources": [
            "ClinicalTrials.gov glossary and patient education on eligibility "
            "criteria.",
            "Published analyses of screen-failure rates across therapeutic areas.",
            "ICH Good Clinical Practice principles for protocol-defined "
            "eligibility.",
        ],
    },
    "are-clinical-trials-safe-private-and-free": {
        "title": "Are clinical trials safe, private, and free to join?",
        "dek": "Every study is different. Here are the questions to ask before you "
               "apply or consent.",
        "tag": "Safety",
        "date": "2026-07-24",
        "read": 5,
        "author": "BridgeMD Team",
        "audience": "patient",
        "body": """
<p>People often ask the same questions before applying to a clinical trial: Is it safe? Will I be paid? Is my information private? Will I get a placebo?</p>

<p>Those are the right questions. A good study team should answer them clearly before you sign anything.</p>

<h2>Safety: trials follow a reviewed protocol</h2>
<p>Clinical trials are run under a protocol that explains who can join, what happens during the study, what risks are known, and how participants are monitored. An ethics board - called an IRB in the United States or an REB in Canada - reviews studies before they open and continues to oversee them.</p>

<h2>Privacy: only share what the study needs</h2>
<p>Before applying, share only what is needed for the site to contact and screen you. Before enrolling, the consent form should explain what information the study collects, who can see it, and how it is protected.</p>

<h2>Cost and payment: ask what is covered</h2>
<p>Many studies cover study-related visits, tests, and the study drug. Some reimburse travel or pay a stipend for time. The amount, if any, is set by the study, not by BridgeMD. Do not join only because of payment.</p>

<h2>Placebo: not every trial uses one</h2>
<p>Some trials compare a new treatment to a placebo. Some compare it to standard care. Some have no placebo. If a study uses a placebo, the site should explain it before you decide.</p>

<h2>Questions to ask the coordinator</h2>
<ul>
<li>What visits, tests, or procedures are required?</li>
<li>What costs are covered?</li>
<li>Is there travel reimbursement or a stipend?</li>
<li>Could I receive a placebo?</li>
<li>What happens if I want to leave the study?</li>
<li>Who can see my information?</li>
</ul>

<blockquote>You are allowed to ask questions, take time, and say no. Applying is interest. Consent is the real decision point.</blockquote>
""",
        "sources": [
            "FDA patient education on clinical trials and informed consent.",
            "ClinicalTrials.gov information on study participation, risks, "
            "benefits, and privacy.",
        ],
    },

    # ---- Site / operations (the "found -> retained" case for BridgeMD) -------
    "why-trials-miss-enrollment-timelines": {
        "title": "Why most clinical trials miss their enrollment timelines",
        "dek": "Recruitment, not the science, is the most common reason trials run "
               "late. And the failure is rarely a lack of interested patients - it "
               "is the funnel between interest and a signed consent.",
        "tag": "Enrollment",
        "date": "2026-08-12",
        "read": 6,
        "author": "BridgeMD Team",
        "body": """
<p>Every protocol is written around an enrollment timeline: so many participants, across so many sites, by a certain date. Most trials do not hit it. Industry analyses have reported for years that the majority of studies fail to enroll on schedule, and that patient recruitment is the single most common cause of delay and of early termination.</p>

<h2>The problem is bigger than it looks</h2>
<p>The headline figures have been stable for years. Commonly cited industry analyses estimate that roughly <b>80% of trials fail to finish enrollment on time</b>, and that recruitment shortfalls are the leading reason studies are extended, re-planned, or closed early.</p>

<figure class="blog-fig">
  <div class="blog-fig-t">Trials that meet their enrollment timeline</div>
  <div class="blog-bars">
    <div class="blog-bar">
      <span class="blog-bar-l">Miss the deadline</span>
      <span class="blog-bar-track"><span class="blog-bar-fill" style="width:80%"></span></span>
      <span class="blog-bar-v">~80%</span>
    </div>
    <div class="blog-bar">
      <span class="blog-bar-l">Enroll on time</span>
      <span class="blog-bar-track"><span class="blog-bar-fill is-muted" style="width:20%"></span></span>
      <span class="blog-bar-v">~20%</span>
    </div>
  </div>
  <figcaption>Commonly cited industry estimate (widely referenced from Tufts CSDD / CenterWatch reporting). Verify current figures against the primary source.</figcaption>
</figure>

<p>The shortfall is not spread evenly across sites. A well-known pattern in industry reporting is that a meaningful share of activated sites enroll <b>zero</b> participants, and a large share <b>under-enroll</b> relative to target - so a handful of sites end up carrying the study.</p>

<figure class="blog-fig">
  <div class="blog-fig-t">How activated sites perform against target</div>
  <div class="blog-bars">
    <div class="blog-bar">
      <span class="blog-bar-l">Enroll zero patients</span>
      <span class="blog-bar-track"><span class="blog-bar-fill is-muted" style="width:11%"></span></span>
      <span class="blog-bar-v">~11%</span>
    </div>
    <div class="blog-bar">
      <span class="blog-bar-l">Under-enroll</span>
      <span class="blog-bar-track"><span class="blog-bar-fill is-2" style="width:37%"></span></span>
      <span class="blog-bar-v">~37%</span>
    </div>
    <div class="blog-bar">
      <span class="blog-bar-l">Meet or exceed target</span>
      <span class="blog-bar-track"><span class="blog-bar-fill" style="width:52%"></span></span>
      <span class="blog-bar-v">~52%</span>
    </div>
  </div>
  <figcaption>Commonly cited distribution from industry site-performance reporting; exact splits vary by therapeutic area and year.</figcaption>
</figure>

<p>Each of those outcomes is expensive. A delayed trial burns fixed costs every day it runs long, and an under-enrolled study can lose the statistical power it was designed for.</p>

<h2>It is usually not an awareness problem</h2>
<p>The instinct is to spend more on ads to get "more patients". But interest is rarely the bottleneck. What breaks is everything between a patient's interest and a coordinator actually screening them:</p>
<ul>
<li>Listings are written for regulators, not people, so a qualified patient can not tell whether a study is relevant or even nearby.</li>
<li>There is often no simple way to apply, so interest dies in an inbox or on a phone tree.</li>
<li>Applicants arrive from six different sources into six different places, so some are never worked at all.</li>
<li>By the time a coordinator responds, the patient has moved on.</li>
</ul>
<blockquote>Pouring more people into the top of a leaking funnel does not fix the leak. It just makes the waste more expensive.</blockquote>

<h2>Where the funnel actually leaks</h2>
<p>It helps to look at recruitment as five stages and ask where people fall out: <b>found &rarr; contacted &rarr; screened &rarr; enrolled &rarr; retained</b>. A patient who found the study but never got a reply is lost at "contacted". A qualified applicant nobody pre-screened is lost at "screened". A "yes" that never turned into a booked visit is lost at "enrolled". Most sites do not know which stage is leaking, because the data lives in email, spreadsheets, and someone's memory.</p>

<h2>What moves the number</h2>
<p>Sites that enroll well do a few basic things consistently: they pull every applicant source into one place so nobody is dropped; they pre-screen against the protocol so coordinator time goes to the people who can actually qualify; they respond fast; and they shorten the gap between "yes" and a screening visit on the calendar. This is operations, not marketing.</p>
<p>That operational layer is exactly what BridgeMD is built to run. If you want to see it, <a href="{demo}">watch the product tour</a>.</p>

<p class="blog-note">Figures above are third-party industry estimates, not BridgeMD data, and are cited as commonly-referenced ranges. Verify the current numbers against the primary sources before quoting them.</p>
""",
        "sources": [
            "Tufts Center for the Study of Drug Development (CSDD) reports on "
            "enrollment performance and trial timelines.",
            "CenterWatch / industry site-performance reporting on under-enrolling "
            "and non-enrolling sites.",
            "Peer-reviewed reviews of recruitment as a cause of trial delay and "
            "termination (e.g. Trials, BMJ Open).",
            "ClinicalTrials.gov / NIH data on trial status and terminations.",
        ],
    },
    "the-clinical-trial-retention-problem": {
        "title": "Enrollment is only half the job: the retention problem",
        "dek": "A patient who drops out mid-study can cost more than one who never "
               "enrolled. Dropout rates are high, and much of the loss is "
               "operational, not medical.",
        "tag": "Retention",
        "date": "2026-08-10",
        "read": 5,
        "author": "BridgeMD Team",
        "body": """
<p>Sites are measured on enrollment, so retention gets less attention. That is a mistake. A participant who consents and then drops out has consumed screening, enrollment, and visit costs and returned no usable endpoint - and if enough of them leave, the study can lose the power it was designed for.</p>

<h2>Dropout is common, and it adds up</h2>
<p>Across trials, reviews have long reported average dropout in the range of a third of participants, higher in long or demanding protocols. Every one of those exits is a patient the site has to replace by enrolling someone new - which means the retention leak quietly raises the enrollment target.</p>

<figure class="blog-fig">
  <div class="blog-fig-t">Typical participant attrition over a study</div>
  <div class="blog-bars">
    <div class="blog-bar">
      <span class="blog-bar-l">Complete the study</span>
      <span class="blog-bar-track"><span class="blog-bar-fill" style="width:70%"></span></span>
      <span class="blog-bar-v">~70%</span>
    </div>
    <div class="blog-bar">
      <span class="blog-bar-l">Drop out before end</span>
      <span class="blog-bar-track"><span class="blog-bar-fill is-muted" style="width:30%"></span></span>
      <span class="blog-bar-v">~30%</span>
    </div>
  </div>
  <figcaption>Commonly cited average; dropout varies widely and runs higher in long or burdensome protocols. Cited as a referenced range, not BridgeMD data.</figcaption>
</figure>

<h2>Why people leave is often fixable</h2>
<p>Some attrition is unavoidable - adverse events, moving away, disease progression. But a large share is logistical: visits that are hard to schedule, long travel, poor reminders, confusing instructions, or simply feeling like the study is disorganized. Those are operational failures, and operational failures can be engineered out.</p>
<blockquote>Every dropout you prevent is one you do not have to re-enroll. Retention is enrollment you already paid for.</blockquote>

<h2>What keeps participants in</h2>
<ul>
<li><b>Visit windows that are visible.</b> Booking the next visit inside its protocol window, before the patient leaves, beats chasing them later.</li>
<li><b>Reminders that actually land</b>, with what to bring and how to prepare.</li>
<li><b>Fast, human responses</b> when a participant has a question or needs to reschedule.</li>
<li><b>Re-consent handled cleanly</b> when a protocol changes, so the study never feels chaotic.</li>
</ul>

<h2>The operational point</h2>
<p>Retention is not a soft "patient experience" nicety. It is throughput. The same system that shortens cycle time on scheduling and documents is the system that keeps enrolled patients from slipping away between visits. That is the "retained" end of the funnel BridgeMD is built to hold. <a href="{demo}">See how the scheduling and reminders fit together</a>.</p>

<p class="blog-note">Dropout figures are third-party estimates that vary widely by protocol and therapeutic area. They are cited as referenced ranges, not BridgeMD data.</p>
""",
        "sources": [
            "Peer-reviewed reviews of participant retention and attrition in "
            "clinical trials (e.g. Trials, Contemporary Clinical Trials).",
            "NIH / NIHR guidance on trial retention strategies.",
        ],
    },
    "hidden-cost-of-protocol-amendments": {
        "title": "The hidden cost of protocol amendments for research sites",
        "dek": "A sponsor changes the protocol. For the site, that one email can mean "
               "an IRB resubmission, re-consenting enrolled patients, retraining "
               "staff, and rebuilding the visit calendar.",
        "tag": "Site operations",
        "date": "2026-08-07",
        "read": 6,
        "author": "BridgeMD Team",
        "body": """
<p>Protocol amendments are treated as a sponsor problem. The sponsor writes them, the sponsor pays for them, and the sponsor tracks their cost. But the work an amendment creates does not stay at the sponsor. It lands on every site, and most of it is invisible in anyone's budget.</p>

<h2>Amendments are the norm, not the exception</h2>
<p>Research from the Tufts Center for the Study of Drug Development has long found that most protocols undergo at least one substantial amendment, that later-phase studies average several, and that a significant share of those amendments were <b>avoidable</b>. In other words, changes mid-study are not a rare event you can plan around. They are a recurring tax on running a trial.</p>

<figure class="blog-fig">
  <div class="blog-fig-t">Substantial amendments per protocol, by phase</div>
  <div class="blog-bars">
    <div class="blog-bar">
      <span class="blog-bar-l">Phase II (avg)</span>
      <span class="blog-bar-track"><span class="blog-bar-fill is-2" style="width:60%"></span></span>
      <span class="blog-bar-v">~2.2</span>
    </div>
    <div class="blog-bar">
      <span class="blog-bar-l">Phase III (avg)</span>
      <span class="blog-bar-track"><span class="blog-bar-fill" style="width:90%"></span></span>
      <span class="blog-bar-v">~3.3</span>
    </div>
    <div class="blog-bar">
      <span class="blog-bar-l">Judged avoidable</span>
      <span class="blog-bar-track"><span class="blog-bar-fill is-muted" style="width:45%"></span></span>
      <span class="blog-bar-v">~45%</span>
    </div>
  </div>
  <figcaption>Commonly cited Tufts CSDD figures on amendment frequency and avoidability. Verify current numbers against the primary source before quoting.</figcaption>
</figure>

<h2>The sponsor cost is visible. The site cost is not.</h2>
<p>When a substantial amendment lands, a site typically has to:</p>
<ul>
<li>Acknowledge receipt and work out what actually changed, often from a cover letter that says "minor administrative changes".</li>
<li>Submit the change to its IRB or REB and wait for approval.</li>
<li>Update the informed consent form to the new version.</li>
<li><b>Re-consent every already-enrolled participant</b> the change affects.</li>
<li>Retrain staff, and update the visit schedule, worksheets, and patient instructions.</li>
</ul>
<p>Tufts has also put a price on the sponsor side - the direct cost of a substantial amendment runs into the hundreds of thousands of dollars on average, and higher in late-phase studies. None of that figure captures the uncompensated hours it creates at each site.</p>

<h2>"Minor administrative changes" rarely is</h2>
<p>The phrase on the cover letter and the reality on the schedule of assessments are often two different things. An amendment described as minor can add a visit, tighten an exclusion so patients already in screening need re-review, or change a fasting requirement that flows into patient instructions. The site only finds out by reading two protocol versions side by side, line by line.</p>

<h2>Making amendments survivable</h2>
<p>The sites that handle amendments well turn each one into a tracked checklist instead of a scramble: what changed, which steps this site owes, who has re-consented, and what is still open. Version-to-version diffs make "minor" honest, and a live re-consent list makes sure no enrolled patient is missed.</p>
<p>That is the job of BridgeMD's <a href="{demo}">sponsor-updates workflow</a>: one tracked checklist per amendment, with the re-consent step booking real visits so nothing is lost between "the sponsor emailed us" and "everyone re-signed".</p>

<p class="blog-note">Figures above are third-party industry estimates (Tufts CSDD), not BridgeMD data. Verify current numbers against the primary source before quoting them.</p>
""",
        "sources": [
            "Tufts Center for the Study of Drug Development (CSDD) studies on "
            "protocol amendment frequency, cost, and avoidability.",
            "ICH E6(R2) Good Clinical Practice guidance on protocol changes and "
            "re-consent.",
        ],
    },
    "where-a-coordinators-week-goes": {
        "title": "Where a research coordinator's week actually goes",
        "dek": "Most of a coordinator's week is not spent with patients. It is spent "
               "moving information between systems that do not talk to each other.",
        "tag": "Coordinator life",
        "date": "2026-08-04",
        "read": 5,
        "author": "BridgeMD Team",
        "body": """
<p>Ask a study coordinator what they do and they will say "run the study". Watch the actual week and most of it is administrative: chasing applicants, checking eligibility by hand, booking visits, sending the same documents twice, and keeping trackers up to date in a spreadsheet. The patient-facing part, the reason anyone took the job, is squeezed into whatever is left.</p>

<h2>The work that is not the patient</h2>
<p>The recurring load tends to look like this:</p>
<ul>
<li><b>Intake.</b> Applicants arrive from the listing, ads, physician referrals, the EHR, inbound email, and last month's spreadsheet, and someone has to pull them together and dedupe them.</li>
<li><b>Pre-screening.</b> Reading long screeners against a longer list of inclusion and exclusion criteria to find the few people worth a call.</li>
<li><b>Scheduling.</b> Phone tag to get a screening visit on the calendar, then reminders so it is not a no-show.</li>
<li><b>Documents.</b> Sending the same essential document to four people who all have access, and answering questions buried on page 148 of the protocol.</li>
<li><b>Trackers.</b> Rebuilding, by hand, the status view the sponsor and the PI both want to see.</li>
</ul>

<figure class="blog-fig">
  <div class="blog-fig-t">A coordinator's week, roughly</div>
  <div class="blog-bars">
    <div class="blog-bar">
      <span class="blog-bar-l">Admin, data entry &amp; coordination</span>
      <span class="blog-bar-track"><span class="blog-bar-fill" style="width:70%"></span></span>
      <span class="blog-bar-v">majority</span>
    </div>
    <div class="blog-bar">
      <span class="blog-bar-l">Direct patient / screening time</span>
      <span class="blog-bar-track"><span class="blog-bar-fill is-muted" style="width:30%"></span></span>
      <span class="blog-bar-v">the rest</span>
    </div>
  </div>
  <figcaption>Illustrative split reflecting commonly-reported coordinator workload patterns (ACRP/SOCRA and site-workload literature). Directional, not a measured BridgeMD figure.</figcaption>
</figure>

<h2>Why it compounds</h2>
<p>Two things make this worse than it sounds. First, screen-failure rates are high; a large share of the people a coordinator carefully screens will not qualify, so a lot of that effort produces nothing. Second, the work is fragmented across tools that do not share state, so every task carries a tax of context-switching and copying data from one place to another.</p>
<blockquote>Every hour a coordinator spends reconciling spreadsheets is an hour not spent screening and enrolling patients.</blockquote>

<h2>The lever is time, redirected</h2>
<p>The point of taking admin off a coordinator is not "tidiness". It is capacity. Cutting the cycle time on intake, pre-screening, scheduling, and documents hands hours back, and those hours go straight into the stages that actually move enrollment: screening more applicants, enrolling more of them, and keeping the enrolled from dropping out.</p>
<p>That is the whole idea behind BridgeMD as an operating system for the site, rather than one more single-purpose tool. <a href="{demo}">See how the pieces fit together</a>.</p>

<p class="blog-note">Descriptions of coordinator workload and screen-failure rates reflect commonly-reported industry patterns, not BridgeMD data, and vary widely by therapeutic area. The chart is directional. Verify against primary sources before quoting.</p>
""",
        "sources": [
            "Association of Clinical Research Professionals (ACRP) and SOCRA "
            "material on clinical research coordinator workload.",
            "Published studies on site workload and screen-failure rates across "
            "therapeutic areas.",
        ],
    },
}

# Newest first. Add new slugs to the front.
ORDER = [
    "why-so-few-patients-hear-about-clinical-trials",
    "why-trials-miss-enrollment-timelines",
    "the-clinical-trial-retention-problem",
    "hidden-cost-of-protocol-amendments",
    "where-a-coordinators-week-goes",
    "how-clinical-trial-eligibility-works",
    "what-happens-after-you-apply-for-a-clinical-trial",
    "are-clinical-trials-safe-private-and-free",
]


import datetime as _dt


def _human(iso: str) -> str:
    """'2026-08-04' -> 'August 4, 2026'. Falls back to the raw string."""
    try:
        return _dt.datetime.strptime(iso, "%Y-%m-%d").strftime("%B %-d, %Y")
    except Exception:
        try:  # platforms without %-d (belt and suspenders)
            return _dt.datetime.strptime(iso, "%Y-%m-%d").strftime("%B %d, %Y")
        except Exception:
            return iso


def list_posts():
    """All posts in display order (newest first), each with its slug."""
    return [dict(POSTS[s], slug=s, date_h=_human(POSTS[s]["date"]))
            for s in ORDER if s in POSTS]


def list_patient_posts(limit=None):
    """Patient/public education posts for the homepage and public SEO surface."""
    posts = [p for p in list_posts() if p.get("audience") == "patient"]
    return posts[:limit] if limit else posts


def get(slug: str):
    """One post plus its slug and its neighbours, or None for an unknown slug."""
    if slug not in POSTS:
        return None
    i = ORDER.index(slug)
    prev_slug = ORDER[i - 1] if i > 0 else None
    next_slug = ORDER[i + 1] if i < len(ORDER) - 1 else None
    return dict(
        POSTS[slug], slug=slug, date_h=_human(POSTS[slug]["date"]),
        prev_slug=prev_slug, prev_title=POSTS[prev_slug]["title"] if prev_slug else None,
        next_slug=next_slug, next_title=POSTS[next_slug]["title"] if next_slug else None,
    )
