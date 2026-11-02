# Roofing Ledger

Reads every invoice, receipt, quote, payslip and statement in your SharePoint / OneDrive folders and
Google Drives with Claude and gives the owner a live, verifiable picture of the business: revenue,
costs, profit, VAT, who owes what, per month, customer, supplier and job. Two-way sync: files added
in SharePoint or Drive appear in the ledger within minutes; files uploaded in the app are saved back
to SharePoint or Drive.

| Screen | URL | Who |
|---|---|---|
| Owner dashboard | `/` | owner (dashboard password) or admin |
| Admin | `/admin` | admin password only |

Documentation:

- **docs/PRD.md** — what the product does, requirements, bookkeeping rules, API, acceptance criteria
- **docs/DEPLOYMENT.md** — production setup (Docker + Caddy HTTPS or systemd), environment, backups
- **docs/SECURITY.md** — threat model, controls, findings fixed, residual risks, release checklist

## Quick start on a PC (evaluation)

1. Double-click `run.bat` (creates a virtual environment the first time).
2. Open <http://127.0.0.1:8000/admin>, create the admin password.
3. Admin → Claude API: paste the key, Test, Save. Admin → Company: name as printed on invoices.
4. Admin → Microsoft setup: enter the Entra app registration (instructions on the page), then
   Admin → Sources: Add a Microsoft 365 folder → paste the SharePoint folder link → sign in → the
   first sync starts. (Google Drive works the same way via Admin → Google setup.)
5. Admin → Security: set a dashboard password for the owner. Admin → Data: remove demo data.

The dashboard ships with demo data (including generated sample pages for previews) so it can be
explored before anything is connected.

## Production

See `docs/DEPLOYMENT.md`. In short:

```bash
cp .env.production.example .env.production   # PUBLIC_BASE_URL, LEDGER_DOMAIN, APP_SECRET_KEY
docker compose up -d --build
```

## Development

```
.venv\Scripts\python -m pytest tests        # 43 tests, no network (fake Claude, fake Drive, fake Microsoft Graph)
.venv\Scripts\python -m pip_audit           # dependency vulnerabilities
.venv\Scripts\python -m bandit -r app -ll   # static analysis
```

Project layout:

```
app/
  main.py        routes: pages, auth, owner API, admin API, OAuth, uploads, middleware
  settings.py    DB-backed settings, passwords, session tokens
  crypto.py      encryption of secrets at rest (APP_SECRET_KEY)
  sources.py     Google Drive (list, fetch, changes feed, upload, OAuth), local folders, source factory
  microsoft.py   Microsoft 365: Graph client, OAuth+PKCE, link resolution, listing, delta, uploads
  sync.py        sync pipeline, watcher/scheduler thread, uploads processing
  extract.py     Claude structured extraction + deterministic bookkeeping rules
  analytics.py   dashboard aggregates and company directory
  ask.py         natural-language questions
  previews.py    cached viewable copies of files; render_demo.py sample pages; images.py photo normalisation
  db.py          SQLite schema, migrations, queries
  logsetup.py    rotating log files
  static/        index.html + app.js (owner), admin.html + admin.js (admin), styles.css
tests/           pytest suite (fakes for Claude and Google Drive)
deploy/          Caddyfile; Dockerfile and docker-compose.yml at the root
```

Every module starts with a docstring explaining its role, design choices and debugging tips.
