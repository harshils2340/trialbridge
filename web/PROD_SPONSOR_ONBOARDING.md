# BridgeMD production + sponsor onboarding checklist

Use this as the go/no-go sheet before onboarding paid site sponsors.

## 1) Live integrations (no sandbox fallbacks)

- [ ] Records: `RECORDS_PROVIDER=metriport` + `RECORDS_API_KEY` + `RECORDS_WEBHOOK_SECRET`
- [ ] Payer checks: `PAYER_API_URL` + `PAYER_API_KEY`
- [ ] Travel/logistics: `LOGISTICS_API_URL` + `LOGISTICS_API_KEY`
- [ ] Notifications email: `NOTIFY_LIVE=1`, verified sender domain, SMTP vars set
- [ ] Optional SMS: `NOTIFY_SMS=1`, Twilio vars set
- [ ] `GET /ops/readiness?key=...` returns `ok=true`

## 2) Infrastructure hardening

- [ ] Persistent DB path configured (`DB_PATH=/var/data/bridgemd.db`)
- [ ] Alerts/reminders running via cron with `ALERTS_CRON_KEY`
- [ ] Background loops disabled in web workers (`ALERTS_BACKGROUND=0`, `REMINDERS_BACKGROUND=0`)
- [ ] Daily backup job runs `python backup_db.py`
- [ ] Weekly restore drill runs `BACKUP_FILE=... python restore_drill.py`

## 3) Security/compliance minimum

- [ ] `NO_LOGIN=0` in production
- [ ] Unique `SECRET_KEY` set in production
- [ ] Least-privilege role behavior verified: study team can only access claimed NCT leads
- [ ] Incident response contact and escalation owner assigned
- [ ] Contract packet ready (Terms, Privacy, BAA/DSA)

## 4) Site setup pack (per sponsor/site)

- [ ] Organization profile completed (`/app/site`)
- [ ] Claimed NCT list matches active trials
- [ ] Intake SLA and escalation email set in profile
- [ ] CTMS and REDCap endpoint URLs filled in profile
- [ ] REDCap connectivity validated (push from `/app/leads`)

## 5) Reporting handoff (per sponsor)

- [ ] Dashboard reviewed (`/app/dashboard`)
- [ ] Source attribution reviewed (`By acquisition channel`)
- [ ] Verified outcomes being reconciled (`/app/leads/*/reconcile`)
- [ ] Shared exports:
  - [ ] `GET /app/dashboard/summary.json`
  - [ ] `GET /app/dashboard/export.csv`

