# Distribution Readiness (Site Intake First)

Use this to get TrialBridge production-ready for real site onboarding and mock-client E2E tests.

## 1) Required infra setup (you do once)

1. Render service on paid always-on plan (avoid sleep/wake delays).
2. Persistent disk mounted and `DB_PATH` set (do not run production on ephemeral SQLite).
3. Outbound SMTP configured (needed for patient email code verification and notifications).
4. Set `PUBLIC_BASE_URL` to your real domain.
5. Keep clinician bypass off: `NO_LOGIN=0`.

## 2) Required env vars

Must-have:

- `SECRET_KEY`
- `BEHIND_PROXY=1`
- `NO_LOGIN=0`
- `DB_PATH=/var/data/trialbridge.db` (or equivalent persistent path)
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM`
- `PUBLIC_BASE_URL`

Recommended:

- `LLM_API_KEY`
- `NOTIFY_LIVE=1`
- `SITE_NOTIFY_EMAIL`
- `ALERTS_BACKGROUND=0`
- `REMINDERS_BACKGROUND=0`
- `ALERTS_CRON_KEY` (for cron-triggered `/alerts/run` and `/reminders/run`)

## 3) One-command mock E2E test

From `matcher/web`:

```bash
/Users/harshils/GraphMD/matcher/.venv/bin/python test_e2e_intake_flow.py
```

Expected output includes:

- `E2E intake flow PASSED`
- checks for:
  - patient signup/verify/onboarding/apply
  - site receives lead in ATS
  - site accept/message/schedule/status/reconcile
  - patient sees updates and can reply

## 4) Site onboarding in < 1 hour

1. Create site account.
2. Open `Site setup` and claim active NCT IDs.
3. Confirm contact email in profile.
4. Receive a test applicant (from mock client).
5. Process in ATS: accept -> message -> send scheduling link.
6. Verify patient can see/respond in `My applications`.
7. Mark status + reconciliation proof.

## 5) Go/No-Go before opening patient traffic

Go only if all are true:

- New applicant appears in site queue within 30s.
- Site can send first message + scheduling link within 2 minutes.
- Patient sees updates in-app with no manual DB/admin intervention.
- Reconciliation event writes successfully.
- No auth/login code failures in last 24h.

If any fail, fix before scaling distribution.
