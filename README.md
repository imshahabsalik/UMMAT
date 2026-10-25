# UMMAT — B2B supplier storefront prototype

Flask + HTML/CSS/JS + SQLite storage. **No paid services, no API keys.**

## Run
```
pip install -r requirements.txt
python app.py
```
Open http://127.0.0.1:5000  (set `FLASK_DEBUG=1` for auto-reload while developing)

## What works
- Homepage: cover image, categories (clickable filters), real product cards, supplier stores, working search (product / supplier / category).
- About Us: two-page brochure recreation (unchanged).
- Create My Store: business details + **manually entered products** (name, price, unit, MOQ, optional photo, optional description). *Add Another Product* appends a blank row without touching earlier rows; *Remove* deletes a row.
- Supplier signup collects a required personal name and business category; the personal name is editable in store settings and kept private from public store data.
- Supplier accounts: email + 12–128 character password, Werkzeug password hashes, email verification, password reset, login/logout, and a protected dashboard.
- Store management: suppliers can update their own business details, add/edit/delete products, or permanently delete their store and its enquiries. New products receive stable IDs; legacy products remain addressable by their original list position until edited.
- Browser POST forms use session-backed CSRF tokens. `/api/stores` omits private account fields.
- Server-side validation of every field; text is kept if there is an error.
- Product photos and supplier logos: JPG/PNG/WebP only, max 5 MB each, fully decoded, orientation-corrected, resized to 1600 px, metadata stripped and re-encoded before storage.
- Storefronts at `/store/<slug>` (unique slugs, custom 404, in-store search).
- Buyer enquiries: "Request Quote" saves the product name and buyer details to SQLite, appears in the supplier dashboard, and sends a supplier email alert when SMTP is configured. Set `UMMAT_SALES_EMAIL` to also copy each enquiry to a salesperson.
  View them at `/admin/enquiries?key=...` after setting `UMMAT_ADMIN_KEY` (page is disabled otherwise).
- `/api/generate-listing` (rule-based helper) is still present but nothing requires it.

## Files
```
app.py                  routes, validation, SQLite storage and JSON import
ummat.sqlite3           SQLite database (created automatically)
stores.json             one-time import source; preserved after migration
enquiries.json          one-time import source; preserved after migration
templates/              public pages, supplier login/dashboard/management pages, quote modal
static/                 style.css (original), extra.css, create_store.js, quote.js, uploads/
```

## Supplier accounts and existing stores

Set a stable secret before running locally or in production. For example, in PowerShell:
```powershell
$env:UMMAT_SECRET_KEY = (python -c "import secrets; print(secrets.token_hex(32))")
python app.py
```
For an HTTPS deployment, also set `UMMAT_HTTPS=1` so the session cookie is marked Secure. Set `UMMAT_ENV=production`; the app refuses to start in production without `UMMAT_SECRET_KEY`. Keep both secret values out of Git.

On the first app start, existing `stores.json` and `enquiries.json` data are imported into SQLite and backed up beside the original files as `*.pre-sqlite.bak`. The JSON files are preserved and are not used for new writes. Set `UMMAT_DATABASE_PATH` to choose a different database location; keep that location on persistent storage and include it in backups.

Stores imported without account credentials are left unchanged and cannot be claimed through registration. Verify the supplier's identity outside the website first, then link the account with the one-time command below. It prompts for email and password, refuses to replace existing credentials, and makes a timestamped backup of the SQLite database before saving:
```powershell
python link_existing_supplier.py
```
Use the store's exact slug (shown in its public URL). Never link an account based only on a submitted store name or URL.

The SQLite database and uploads remain local disk storage. Keep persistent storage and backups when hosting. SQLite transactions serialize writes across app processes. A database-backed rate limiter protects registrations, logins, account email actions and enquiries. Legacy accounts imported before verification are treated as verified.

## Email setup (no paid API)

The app uses Python's built-in SMTP client. For local use, it automatically reads `.env` (which is ignored by Git); values already set by the host take precedence. In development without complete SMTP details, email links are written to the server log. For real delivery, configure the SMTP login, password/key, and verified sender (see `.env.example`); this requires no paid API, though providers may impose sending limits. Never commit SMTP credentials. Set `UMMAT_PUBLIC_BASE_URL` to the public HTTPS site URL so generated links work outside your computer. Email verification is disabled by default so suppliers can log in before SMTP is configured. After configuring SMTP and a verified sender, set `UMMAT_REQUIRE_EMAIL_VERIFICATION=1` to require verification for new signups and logins. Password reset emails still require SMTP in production.

One no-cost option is Brevo's current Free plan, which lists 300 email sends per day. Create an account, verify the sender address, then copy the SMTP login and SMTP key from its SMTP settings (the SMTP key is the password, not an API key). Brevo documents `smtp-relay.brevo.com` and port `587`. Set these on the deployment host's secret/environment settings:

```text
UMMAT_ENV=production
UMMAT_SMTP_HOST=smtp-relay.brevo.com
UMMAT_SMTP_PORT=587
UMMAT_SMTP_USERNAME=<SMTP login shown in Brevo>
UMMAT_SMTP_PASSWORD=<SMTP key>
UMMAT_EMAIL_FROM=<verified sender address>
UMMAT_PUBLIC_BASE_URL=https://<your deployed domain>
```

Do not put real credentials in `.env.example`, source control, or public chat. Check the provider's current limits before launch; free-plan terms can change.

## Backups and restore verification

Run `python backup_data.py` to create a timestamped ZIP containing a consistent SQLite snapshot and all uploaded files. The command checks SQLite integrity and the ZIP, then applies the configured retention (default 30 days). Run `python backup_data.py --verify path\to\ummat-backup-YYYYMMDD-HHMMSS.zip` to test the archive in a temporary directory without touching live data. Set `UMMAT_BACKUP_DIR` to a different persistent disk location.

Schedule `python backup_data.py` with Windows Task Scheduler (e.g. nightly) or cron (`0 2 * * * cd /path/to/app && python backup_data.py`). Store a copy away from the application disk for disaster recovery.

## Production setup

Install `requirements.txt` and run the `Procfile` command (`gunicorn`, Linux hosts only). Configure a persistent writable path for both `UMMAT_DATABASE_PATH` and `UMMAT_BACKUP_DIR`, set `UMMAT_ENV=production`, a random `UMMAT_SECRET_KEY`, `UMMAT_HTTPS=1`, the public URL and optional SMTP variables. Configure HTTPS at the host. Keep `UMMAT_TRUST_PROXY=0` unless the app is behind a known trusted proxy. Do not use Flask's development server publicly. A hosting provider and its persistent disk are not selected in this repository, so this is deployment preparation rather than a live staging deployment.

## Local checks

Run `python -m unittest discover -s tests -v` and `python backup_data.py` to run the automated flow and backup checks. For SMTP delivery, configure the environment first. Then run `python app.py` for a local browser check.

## Deploying publicly — limits to know about
- **SQLite and uploaded images live on local disk.** Most hosts wipe local disk on deploy/restart. Configure persistent storage and regular backups before going live.
- Run with a production server (`gunicorn app:app`), never `debug=True`, behind HTTPS.
