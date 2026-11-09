# Fajr-autom
# Fajr Online - Student Registration Automation

## Overview
Automated student registration for the **Online** department of Fajr Online Arabic Language Center.
Connects the WordPress admission form (Fluent Forms) → Flask webhook → Odoo + Moodle LMS + Brevo email.

**On-site submissions are ignored** (the on-site department follows them up manually).

## Flow
1. Student fills the admission form on WordPress.
2. WPCode sends a webhook to Flask (`POST /webhook`, header `X-Webhook-Token`) – the snippet lives in [`wordpress/`](wordpress/README.md).
3. `study_mode = On-site` → ignored (`{"status": "skipped"}`). Missing/unknown `study_mode` → `400`.
4. Flask creates (or finds) the student in Odoo.
5. Flask looks the student up in Moodle:
   - already registered → sends an "already registered" email;
   - new → creates the account (random password, forced change at first login),
     enrols in the placement course, adds to the Male/Female group, and sends the welcome email via Brevo.

## Webhook contract

Authentication: send `WEBHOOK_SECRET` in the header `X-Webhook-Token`
(`?token=` also works but ends up in server access logs). Without a configured secret the endpoint returns `503`.

| Field | Required | Notes |
|---|---|---|
| `study_mode` | yes | `Online` / `On-site` (case, spaces and dashes are ignored). Also accepted as `studyMode`, `study_type` |
| `email` | yes | trimmed and lower-cased; used as the Moodle username |
| `first_name`, `last_name` | yes | |
| `gender` | yes | `Male` / `Female` (any case). Anything else: student is created but **not** added to a group (`gender_unknown` warning) |
| `birth_date` | for Odoo | `YYYY-MM-DD`, or `DD/MM/YYYY` (set `BIRTH_DATE_ORDER=mdy` for `MM/DD/YYYY`) |
| `mobile` | no | |
| `arabic_name` | no | |
| `placement_slot` | no | the placement-test slot the student picked (also `placement_date`, `exam_date`); shown in the welcome email |

Responses (always JSON):

| HTTP | `status` | Meaning |
|---|---|---|
| 200 | `success` | everything done |
| 200 | `exists` | student already in Moodle |
| 200 | `partial` | account created but something failed – see `warnings` (`odoo_failed`, `enrol_failed`, `group_failed`, `gender_unknown`, `email_failed`) |
| 200 | `skipped` | on-site submission |
| 400 | `error` | bad input or Moodle rejected the user (`study_mode_missing`, `study_mode_unrecognized`, `moodle_create_failed`, …) |
| 403 / 503 | | wrong secret / secret not configured |
| 502 | `error` | Moodle unreachable (nothing was created there) |

Details of failures are in the server log, never in the response.

## Environment Variables
Copy `.env.example` to `.env` and fill it. Required: `MOODLE_URL`, `MOODLE_TOKEN`, `BREVO_API_KEY`,
`ODOO_URL`, `ODOO_DB`, `ODOO_USERNAME`, `ODOO_PASSWORD`, `WEBHOOK_SECRET`.
Optional settings (course/group/branch IDs, date order, timeouts) are listed in `.env.example`.

Before going live:
- the sender domain of `info@fajr.com` must be verified in Brevo (SPF/DKIM);
- if the Moodle web-service token or Brevo is restricted by IP, allow the server's IP.

## Run locally
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill it
python app.py             # http://127.0.0.1:5003
```

## Tests
No network access needed (Moodle, Odoo and Brevo are mocked):
```bash
python -m unittest discover -s tests -v
```

## Deployment
The app is a standard WSGI app: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120`.
It needs a host that can run Python processes (a VPS, Render – see `render.yaml` – or similar);
shared web-hosting plans that only serve PHP cannot run it.
