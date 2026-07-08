# Trial summary structure

TrialBridge never shows the raw ClinicalTrials.gov description to patients. The
source text is long, written for researchers, and full of jargon. Every trial is
rewritten into short, plain English following the exact structure below.

`summarize.py` reads this file and feeds it to the model, so editing this file
changes how summaries are written. Keep it tight.

## Plain-English rules
- Write for a general audience (about an 8th-grade reading level).
- No medical jargon. If a term is unavoidable, explain it in a few words,
  e.g. "an EKG (a quick heart test)".
- Expand acronyms the first time they appear.
- Short, direct sentences. No marketing, no hype.
- Never promise benefit or imply the treatment works.
- Only use facts from the source text and trial fields. Never invent details.
- Neutral, calm tone. This is information, not medical advice.

## Fields (return JSON with exactly these keys, all plain English)
- `one_liner`  - one sentence, max ~140 characters: what is being tested and for whom.
- `purpose`    - 1-2 sentences: what the study is trying to find out.
- `who`        - one sentence: the kind of person the study is looking for.
- `what`       - 1-2 sentences: what taking part actually involves (treatment, visits, tests).
- `commitment` - one sentence: how long it lasts and what visits are needed.

Leave a field as an empty string if the source genuinely doesn't say.

## Where each field is shown
- Search result cards: `one_liner` only, kept to about two lines.
- Trial detail page: `purpose`, `who`, `what`, `commitment` shown as short
  labeled rows ("What it's testing", "Who it's for", "What's involved",
  "Time commitment").
