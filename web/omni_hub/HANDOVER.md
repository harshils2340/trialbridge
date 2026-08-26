# Omni handover — phone, identity, vision

Internal notes from a product conversation (Aug 26, 2026). This is intent and positioning, not a build spec. The demo still simulates most connections.

---

## One-line vision

The front door of a local service business, in one place, that knows who just reached out.

Not an AI that answers the phone. Not Ads Manager. Not Salesforce. The shared brain of the front desk: every inbound lands as a person, routine replies get drafted, humans confirm anything that should not auto-send.

## Who it is for

Leasing offices, dental / medspa, PI law, home health, therapy, staffing, similar. Small teams. Leads do not live in a CRM. They live in Gmail, Instagram DMs, a front-desk voicemail, maybe Facebook, maybe a portal (Zillow, Avvo, Zocdoc).

The person named Sam is the system. After hours and staff turnover are where money dies. Speed-to-lead is the business.

**How these businesses actually market (majority of our use cases):** Instagram, Facebook, Gmail. Not Google Ads accounts. Not tracking numbers. They post, maybe boost a reel, people DM; Facebook is Messenger plus the odd lead form; everything else dumps into Gmail.

---

## What we decided about phone

### Connecting is not “type your number”

You cannot paste the front-desk number into Omni and have Verizon send calls. Carriers do not offer that.

A real connect talks to whatever already owns the channel (Twilio, OpenPhone, RingCentral, or voicemail-to-email). Omni files what that system sends. It does not dial or pick up.

Twilio is the generic “phone as an API” (SMS + voice + recordings). BridgeMD already uses Twilio for outbound SMS. If the office already lives in OpenPhone / RingCentral, connect that instead of making them buy a Twilio number.

**Texts and calls are two different plugs.** One Connect does not turn on both.

### Porting vs forwarding vs a new number

Porting = move `(312) 555-0100` from Verizon to Twilio/OpenPhone. Same digits on the door. High friction. People will hesitate: downtime fear, “a developer company owns our line,” Comcast bundle, hunt groups / fax.

Forwarding = keep the old line, send unanswered calls onward. Easy yes. Voice only. **SMS does not follow.** Two systems to babysit.

A brand-new public number is easy to demo and a bad sell (signs, Google, website).

**Do not lead with porting.** Order of yes:

1. Gmail / portal forward / paste a voicemail
2. Forward unanswered calls, or voicemail-to-email (trial)
3. A new *intake* number (“text this for tours”) — extra line, not a replacement
4. Connect OpenPhone if they already have it
5. Port the front desk number only after they trust the inbox

### Easiest path that keeps front-desk practice the same

**Voicemail-to-email, not call forwarding.**

Keep the number, keep the desk phones, keep the existing mailbox. Turn on the carrier/VoIP feature that emails a transcript (and often audio) when someone leaves a message. Auto-forward that mail to the workspace intake address — same pattern as Zillow.

Desk still rings. They still pick up live. After hours still hits the usual box. Omni gets a copy.

**That path is voicemails only.** It does not capture:

- Calls they answered
- Missed calls with no message
- Texts to that number
- Live audio / hold / transfers

That is acceptable as the first phone story. Do not pretend it is a full call log.

---

## Identity / right-rail history

How a message *arrives* (voicemail email, IG DM, Gmail) is separate from what you *see* when you open it.

**Product:** click a voicemail (or any thread). Right panel, under details, a quiet **History** block.

- Most people: first contact. One line: “No other messages from this number.” Then ignore it.
- Some people: “Also texted Aug 12 · Instagram DM Aug 10” with links.

Do not make this the main screen. Do not merge every channel into one giant thread. Keep one row per conversation per source; attach them to the same person when phone or email matches.

**Why it matters:** the useful omnichannel moment is not “we took the call.” It is “this voicemail is the same person who DMed on Instagram and emailed Gmail.” Without that, Omni is a nicer voicemail list.

**Not built today.** Contacts exist (`name`, `handle`, `email`, `phone`) but match is exact `handle` only. Numbers are not normalized. Ingest always opens a new conversation. Side panel is this thread’s fields only. No “other conversations from this contact.”

Linking key: normalized phone and email. Voicemail emails usually include caller ID — enough to join if they texted or submitted a form with the same number.

You still would not see live calls the desk answered unless someone logs them.

---

## Ads vs the real doors

Do not sell Meta/Google Ads Manager. These customers are not media buyers.

Connecting “ads” for our majority case means connecting **how they already market:**

| Door | What it actually is |
|---|---|
| Instagram | The real channel. Organic DMs from the grid; click-to-message if they boost a post. Often answered from someone’s personal phone. |
| Facebook | Messenger + lead ads if they have them (many do not check the lead-ads inbox). |
| Gmail | The dump: site forms, portal mail, “I saw you on IG, emailing instead,” even Meta’s “new lead” emails. One Gmail connect catches a lot. |

Google for *these* businesses is usually **Maps / Google Business Profile** (call, message, website), not Google Ads. Treat it like the front desk and Gmail, not like a campaign tool.

Google Local Services Ads and per-campaign tracking numbers are real for lawyers / home services later. Not the default story.

Omni should close the loop (ad or post → person → booked / not a fit / never answered). It should not optimize budgets.

Typical repeat path to show on the right: IG Friday → Gmail Saturday → voicemail after hours. Not “Campaign X vs Y.”

---

## How this becomes a company (beyond the demo)

**Wedge:** replace the chaos of getting a new person in the door. Not the whole company. Vertical CRMs (Yardi, Dentrix, Clio) own *after* they are a customer. Chatbots already failed because they say the wrong thing. AI phone agents try to fire the receptionist; owners will not trust that first.

**What wins:** every door in, one picture of the person, routine handled, the rest queued with a draft and a reason. Approval and guardrails (fair housing, fees, medical, legal) are the product. Memory is the product.

**Config in English** is distribution. A 12-person office will not sit through a six-month CRM implementation. “Tell us how you work, pick where leads come from” can. Each vertical is a packaged playbook (fields, banned questions, tone, what auto-sends).

**Expansion after you are the inbox** (same sales motion, more job):

- Follow-up that does not depend on Sam
- After-hours that matches daytime
- Book tour / consult on the thread
- Route (urgent, wrong state, needs a clinician)
- Which doors become customers (so they stop boosting the dumb reel)
- New hire opens Omni and the business already knows how to talk
- Handoff into the real system of record

**Lock-in** is not the phone number. It is the playbook, the history, and the actual work living there.

**Do not become:** Intercom for SaaS support, a campaign optimizer, or “we answer every call so you do not need a front desk.”

---

## Demo vs real (so nobody gets confused)

Today, Connect is a toggle: mark source connected, drop in seed threads. Paste box and file upload are real. The intake address on Connections (`{wid}@in.…`) is the intended design for Zillow-style and voicemail-to-email forwards; live inbound mail into Omni is not a full phone system.

Voicemail is already a first-class *source kind* in the spec (labeled “Phone and voicemail”, mode upload). SMS is a separate source. Facebook lead ads and Instagram exist as kinds. That is the shape. The conversation above is what to do with that shape, not a claim that Twilio or Meta OAuth is wired.

---

## Product principles from this thread

1. Keep their practices. Sneak in. Do not steal the front desk number on day one.
2. Voicemail-to-email before porting. Porting is a later “we live here now” step.
3. History on the right, quiet when empty. Most contacts are first-time; some are not.
4. Instagram + Facebook + Gmail are the majority connect set. Phone is the extra door. Ads suites are not.
5. One person, many threads — not one mega-thread, not three strangers.
6. Omni files and remembers. It does not replace the live answer.

---

## Open

- Exact copy on the History empty state
- Whether missed-call-no-voicemail is worth a later pipe (forwarding / OpenPhone) or stays out of v1
- Whether a separate “text us” intake number is offered alongside voicemail-to-email
- Gmail + IG + FB as the default onboarding trio for most templates, with phone/voicemail optional
