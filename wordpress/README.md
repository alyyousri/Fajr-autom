# WordPress side (WPCode snippet)

`wpcode-snippet.php` is the code that runs **inside WordPress** (WPCode plugin → PHP Snippet).
It sends each Fluent Forms admission-form submission to the Flask webhook in this repo.
This copy is the versioned source of truth; WordPress does not read it from GitHub,
so after any change here, paste the new version into WPCode.

## Install / update
1. WPCode → Code Snippets → open the existing Fajr snippet (or Add New → PHP Snippet).
2. Replace its whole content with `wpcode-snippet.php` (never keep two copies active).
3. Set `FAJR_WEBHOOK_SECRET` to the same value as `WEBHOOK_SECRET` on the server.
4. Check `FAJR_LEVEL_TEST_FORM_ID` is the Form ID of the admission form
   (Fluent Forms → All Forms → shortcode `[fluentform id="X"]`).
5. Activate.

## Secret handling
**Never commit the real secret** – this repo is public and the constant is intentionally empty here.
Either paste the secret into the snippet inside WordPress only, or define it once in `wp-config.php`:

```php
define('FAJR_WEBHOOK_SECRET', 'the-same-value-as-WEBHOOK_SECRET');
```

The snippet uses the `wp-config.php` value when it exists, so the snippet text can stay identical everywhere.

## Fields sent
`first_name`, `last_name`, `arabic_name`, `mobile`, `email`, `birth_date`, `gender`,
`study_mode` (Online / On-site – the server ignores On-site) and `placement_slot` (optional).
Field names are matched in `fajr_field_map()`; add your form's field name there if it differs.
If `study_mode` cannot be found, nothing is sent and the log lists the field names that were received.

## Logs
The snippet writes `[Fajr Webhook] ...` lines with `error_log()` (hosting PHP error log, or
`wp-content/debug.log` when `WP_DEBUG_LOG` is on). Personal data is not logged.
Typical lines: `OK (200): {"status":"success",...}`, `{"status":"skipped",...}` for On-site,
`FAILED (403)` for a wrong secret.
