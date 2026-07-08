# BridgeMD — Legal & Compliance Reference (US + Canada)

> **This is not legal advice.** It is an engineering/product reference so that every
> feature we build stays inside well-established regulatory lines. Before launching
> any monetized or referral-related feature, get a written opinion from healthcare
> regulatory counsel in each operating jurisdiction (and, for research recruitment,
> an IRB/REB). Treat everything below as **guardrails**, not permission.

**Read this before designing anything that touches: referrals, payments, "commission",
advertising/posting trials, or patient data.**

---

## 0. The one rule that shapes the whole product

**You cannot pay a physician (or anyone) for referring a patient, and you cannot pay
per patient enrolled.** This is a "finder's fee" / kickback and is illegal or
professionally prohibited in both the US and Canada. It is the single biggest legal
risk to this business.

Consequences of getting this wrong:
- **US:** criminal felony (AKS), civil False Claims Act liability, exclusion from
  Medicare/Medicaid, per-claim penalties.
- **Canada:** criminal secret-commission charges, provincial college discipline
  (loss of licence for the physician), and voiding of the arrangement.

Everything else in this doc explains the boundaries around that rule and what we
*can* do instead.

---

## 1. United States

### 1.1 Federal Anti-Kickback Statute (AKS) — 42 U.S.C. § 1320a-7b(b)
- **What it bans:** knowingly offering, paying, soliciting, or receiving *any*
  remuneration to induce referrals of, or to generate business for, items/services
  **reimbursable by a federal healthcare program** (Medicare, Medicaid, TRICARE, VA).
- **Scope:** criminal (felony), intent-based, and *one purpose* of the payment being
  to induce referrals is enough. "Remuneration" = anything of value (cash, discounts,
  free software, gifts, above-market fees).
- **Product impact:** paying a referring physician per enrolled patient is a textbook
  violation whenever that patient's care touches federal programs.

### 1.2 Stark Law (Physician Self-Referral) — 42 U.S.C. § 1395nn
- **What it bans:** a physician referring Medicare/Medicaid patients for "designated
  health services" to an entity the physician (or family) has a financial
  relationship with, unless an exception applies. Strict liability (intent not
  required).
- **Product impact:** relevant if physicians ever have equity/financial ties to sites
  or to us and refer within that web.

### 1.3 EKRA — Eliminating Kickbacks in Recovery Act (18 U.S.C. § 220)
- Broader than AKS: applies to **clinical treatment facilities, labs, and recovery
  homes regardless of payer** (including private insurance/cash-pay). Bans
  percentage-of-revenue and per-patient compensation to marketers/referral sources.
- **Product impact:** means "just avoid Medicare patients" is **not** a safe design.

### 1.4 Clinical-trial recruitment specifics (FDA / HHS-OIG / IRB)
- **Finder's fees & bonus/enrollment payments to physicians are disfavored** by
  HHS-OIG and FDA/IRB guidance because they create undue influence over enrollment
  and can compromise informed consent.
- **Recruitment materials are advertising** and typically require **IRB review and
  approval** before use (21 CFR 50 / 56, FDA guidance on recruiting study subjects).
  We cannot let sponsors "post" a trial with claims that haven't been IRB-cleared.
- Payments to the **research site/institution** for the **actual work of conducting
  the study** (per-subject study budgets, coordinator time, procedures) are normal —
  but that is the site being paid for *work performed*, and it must be **fair market
  value (FMV), set in advance, and not tied to the volume/value of outside
  referrals.** A referring physician who is not the enrolling investigator doing that
  work cannot be slotted into that budget as a workaround.

### 1.5 Other US laws that apply
- **False Claims Act (31 U.S.C. § 3729):** claims tainted by an AKS violation are
  false claims → treble damages + per-claim penalties.
- **HIPAA:** governs use/disclosure of Protected Health Information (PHI). No selling
  PHI; marketing uses need authorization; de-identify (Safe Harbor / Expert
  Determination) before processing. See §3.
- **FTC Act:** truthful, non-deceptive advertising; endorsement/testimonial rules;
  no unsubstantiated health claims.
- **State law:** many states have their own all-payer anti-kickback and
  **fee-splitting** statutes, plus **Corporate Practice of Medicine (CPOM)** doctrines
  restricting non-physician entities from sharing in medical fees. Check each state.

### 1.6 AKS "safe harbors" (the narrow lanes that can be legal)
Payments only protected if they *fully* meet a safe harbor. The relevant ones:
- **Bona fide employment / personal services & management contracts:** written,
  ≥1-year term, compensation set **in advance**, **FMV**, and **not determined by the
  volume or value of referrals**. Payment must be for actual, needed services.
- **Fair market value for real services** is the recurring theme: pay for *work*, never
  for *referrals*, and never on a per-patient or percentage-of-business basis tied to
  federal-program business.

---

## 2. Canada

Canada has **no single AKS**, but a stack of criminal, health-insurance, professional,
and privacy rules produce the same bottom line: **no paying for referrals, no
fee-splitting.**

### 2.1 Criminal Code
- **s. 426 — Secret commissions:** criminal to give/accept a reward as an inducement
  for doing business-related acts without the principal's knowledge. Covers kickbacks.
- **s. 121 — Frauds on the government** (relevant where public funds involved).

### 2.2 Canada Health Act + provincial health-insurance law
- Public medicare framework; provincial statutes (e.g., Ontario's **Health Insurance
  Act** and **Commitment to the Future of Medicare Act**) restrict billing practices,
  extra-billing, and improper payments tied to insured services.

### 2.3 Provincial medical regulatory colleges (the sharpest edge)
- Each province's College (e.g., **CPSO** in Ontario, **CPSBC** in BC, **CPSA** in
  Alberta) sets binding policies. Common prohibitions:
  - **Fee-splitting / accepting benefits:** physicians must not accept payment,
    rebates, or benefits for referring patients or for products/services.
  - **Conflict of interest:** referrals must be in the patient's clinical interest,
    free of financial inducement, and disclosed.
  - **Advertising standards:** truthful, verifiable, no comparative/superlative or
    misleading claims.
- A physician who takes a per-referral payment risks **discipline up to licence loss**,
  regardless of the payer. This is why the referring physician **must not be paid** by
  us.

### 2.4 Clinical trials in Canada (Health Canada)
- **Food and Drugs Act & Regulations, Division 5 (Part C)** govern clinical trials;
  **REB (Research Ethics Board)** approval is required, and recruitment materials need
  REB sign-off (analogous to the US IRB).
- **Tri-Council Policy Statement (TCPS 2)** governs research ethics, including
  recruitment and undue inducement.

### 2.5 Privacy law
- **PIPEDA** (federal, commercial handling of personal info) and **provincial health
  privacy acts** (e.g., Ontario **PHIPA**, Alberta **HIA**, BC **PIPA/FIPPA**, Quebec
  **Law 25**). Require consent for collection/use/disclosure of personal health
  information, data-minimization, and secure handling. See §3.

---

## 3. Patient data / privacy rules (both countries)

- **De-identify before processing.** Our pipeline strips names/identifiers before any
  note leaves the clinician's hands or hits an LLM. Keep it that way. Aim for HIPAA
  Safe Harbor–level de-identification (remove the 18 identifiers) and PHIPA/PIPEDA
  data-minimization.
- **Consent to contact / refer.** A patient must consent before their (even
  de-identified) info is routed to a study site, and before any identifiable contact
  details are shared. Our referral flow captures explicit consent — this is a legal
  requirement, not a nicety.
- **No sale of PHI.** Do not monetize patient data. Business logging of PHI (EHR pull)
  must be minimized, encrypted, access-controlled, and retained only as needed.
- **BAAs / data agreements.** In the US, any vendor touching PHI needs a Business
  Associate Agreement. In Canada, data-sharing/processing agreements + adequate
  safeguards, and care with cross-border (US) data transfer disclosures.

---

## 4. What this means for BridgeMD's business model

### ❌ Do NOT
- Pay referring physicians a commission, bonus, or finder's fee per patient / per
  enrollment. **(This includes the current "commission" concept in the app — it must
  be reframed or removed for physician users. See §6.)**
- Tie *anyone's* compensation to the volume or value of referrals of
  federal/insured-program patients.
- Let sponsors post recruitment claims that haven't been IRB/REB-approved.
- Move or sell identifiable patient data, or contact a patient's site without consent.

### ✅ CAN do (compliant lanes)
1. **Free tool for physicians = clinical decision support.** Giving physicians a free
   search/matching tool to help them find trials for their patients is a benefit to
   *patients* and is generally fine — as long as the physician is **not paid for using
   it or for referrals.** This is our safe entry point.
2. **Charge the sponsor/CRO/site with a SaaS license.** Flat or seat-based software
   fees for search, workflow, and analytics — **not** priced per referral or per
   enrollment. This is selling software, not buying referrals.
3. **Pay for bona fide services at FMV.** If we ever pay a site/coordinator, it must be
   for real work (data entry, coordination, advertising services we perform), set in
   advance, at fair market value, documented, and independent of referral volume.
4. **Patient self-referral / patient-facing model.** Patients can search and express
   interest themselves. Sponsor-funded *recruitment advertising* is allowed if it is
   IRB/REB-approved, truthful, and non-coercive.
5. **Sites/PIs receiving per-subject study budgets from sponsors** for conducting the
   trial is normal and legal — but that is between sponsor and site for study conduct,
   and must not be repackaged as a referral payment to an outside physician.

### The clean framing
> **We sell software to the paying side (sponsors/sites) and give physicians a free
> tool that helps their patients. Money never changes hands on a per-referral basis.**

---

## 5. "Posting" trials & advertising — rules

- **IRB/REB approval first.** Any sponsor-supplied recruitment content (eligibility
  blurbs, patient-facing descriptions, incentives) must be approved by the study's
  ethics board before we display it as recruitment advertising. Prefer pulling
  neutral, factual data straight from ClinicalTrials.gov (public record).
- **Truthful & non-misleading** (FTC / college advertising rules). No promises of
  benefit, cure, or "free treatment" framing; no coercive incentives.
- **Fair balance.** Recruitment materials shouldn't overstate benefits or minimize
  risks.
- **Compensation to subjects** (if any) must be reasonable, non-coercive, and
  IRB/REB-approved — never framed as a payment for enrolling.

---

## 6. Required product changes flagged by this doc

> Tracked here so we don't ship something illegal. Update as we address them.

- [ ] **"Commission" for referring physicians** — the current referral tracker frames
  earnings/commission to the referring doctor. This conflicts with §0/§1/§2. Reframe
  to one of:
  - remove physician-facing payment entirely (pure clinical tool), or
  - reframe the tracker as **outcome/status tracking only** (no $ to the physician), or
  - restrict any FMV service payments to **bona fide site/coordinator work**, clearly
    separated from referral volume, with counsel sign-off.
- [ ] **Consent gating** — keep explicit patient consent required before any
  identifiable info is shared with a site. (Already implemented — do not regress.)
- [ ] **Advertising content** — if we ever let sponsors post custom recruitment copy,
  add an IRB/REB-approval attestation gate before display.
- [ ] **PHI handling** — keep de-identification mandatory; add BAAs/DPAs before any
  production PHI processing or EHR integration goes live.

---

## 7. How to use this file (for every future change)

Before building/monetizing anything, ask:
1. **Does money flow to a referral source based on referrals/enrollment?** If yes →
   stop, redesign. (§0)
2. **Is patient data identifiable, and did the patient consent to this use/disclosure?**
   (§3)
3. **Is any displayed recruitment claim IRB/REB-approved and truthful?** (§5)
4. **If we pay someone, is it FMV for real work, set in advance, and independent of
   referral volume?** (§1.6, §4)
5. **Have we flagged anything requiring counsel/IRB in §6?**

If a feature can't clearly answer these, it doesn't ship until counsel reviews it.

---

*Sources are the named statutes/regulations/policies above (US: AKS 42 U.S.C.
§ 1320a-7b(b), Stark 42 U.S.C. § 1395nn, EKRA 18 U.S.C. § 220, FCA 31 U.S.C. § 3729,
HIPAA, FDA 21 CFR 50/56, HHS-OIG guidance; Canada: Criminal Code ss. 121/426, Canada
Health Act & provincial health-insurance acts, provincial College policies e.g. CPSO,
Health Canada Food & Drugs Act Div. 5, TCPS 2, PIPEDA/PHIPA & provincial privacy law).
Verify current text and consult counsel before relying on any point.*
