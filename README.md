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
   - already registered → sends an "already registered" email with a one-click "forgot your password" link
     (see [Password reset](#password-reset-for-returning-students));
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

### Team copy of student emails

Set `TEAM_COPY_EMAIL` (an environment variable, not code, because this repository is public) and every email sent to a
student (welcome and "already registered") is also delivered to that address as a BCC, so the team can follow up. Leave it
empty for no copy. The copy shows the student's address in the *To* header. Note that the welcome email contains the
student's temporary password. If a copy is rejected, the student's email is still sent without it.
The password-reset email (below) is never copied to the team. The team copy of the "already registered" email does
contain that student's working reset link.

### Password reset for returning students

Moodle's own "forgot password" page only shows a generic message, so the "already registered" email links to
`/reset-password` on this service instead (the link falls back to Moodle's page if no public address is known).

1. The link carries a signed token (HMAC, derived from `WEBHOOK_SECRET`) that holds only the student's email and an
   expiry (`RESET_LINK_MINUTES`, default 60). Nothing is stored.
2. Opening it shows a confirmation page and changes nothing, so email link scanners cannot trigger a reset.
3. Pressing the button sets a new random temporary password in Moodle (change forced at next login unless
   `FORCE_PASSWORD_CHANGE=false`) and **emails** it to the student. The password is never shown on the page or logged.
4. A link works once, and the same email can be reset at most once per `RESET_COOLDOWN_MINUTES` (default 10).
5. Accounts are not touched when the email matches no account or more than one, or the account is suspended or does
   not use manual authentication: the student is told to contact `info@fajr.com`.

Setup: `PUBLIC_BASE_URL` (the public address of this service, no trailing slash) is only needed if
`RENDER_EXTERNAL_URL` is not set automatically (Render sets it). The Moodle web-service user behind `MOODLE_TOKEN`
must be allowed to call **`core_user_update_users`**: add it to the external service's function list.
Limits: the used-link list and the cooldown live in memory (per worker), so a restart forgets them; the link still
expires on its own.

### If Odoo is down or slow

Each Odoo call waits at most `ODOO_TIMEOUT` seconds (default 10). If Odoo does not answer (timeout, network error,
502/503), registration still completes in Moodle and the welcome email is sent (without a student code), the response
carries the `odoo_failed` warning, and the student is queued for a retry: after `ODOO_RETRY_DELAY_MINUTES` (default 20)
the service tries to create the student in Odoo again, up to `ODOO_RETRY_MAX_ATTEMPTS` times (default 3, `0` disables it).
Log lines: `Odoo retry n/N scheduled`, `... succeeded`, and `Odoo retry GAVE UP ... add this student to Odoo manually`.

Limits: the retry queue lives in memory only, so a restart or deploy of the service drops pending retries; errors caused
by the data itself (missing gender, Odoo validation errors) are not retried. A retry never creates a duplicate: the student
is looked up by email first.

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
