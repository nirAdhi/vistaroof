# Roofing Ledger

Reads every invoice, receipt, quote, payslip and statement in your SharePoint / OneDrive folders and
Google Drives with Claude and gives the owner a live, verifiable picture of the business: revenue,
costs, profit, VAT, who owes what, per month, customer, supplier and job. Two-way sync: files added
in SharePoint or Drive appear in the ledger within minutes; files uploaded in the app are saved back
to SharePoint or Drive.

| Screen | URL | Who |
|---|---|---|
| Owner dashboard | `/` | any user account (email + password); admins too |
| Admin | `/admin` | accounts with the admin role, or the built-in admin password |

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
5. Admin → Users & security: create an account (name, email, password) for the owner and anyone
   else who should see the ledger. Admin → Data: remove demo data.

Admin login: either create the password in the browser on first visit, or put `ADMIN_EMAIL` and
`ADMIN_PASSWORD` in `.env.production` (or `.env`) and the app takes them at every start; the admin
then signs in with exactly that email and password.
Forgot a browser-created password? `.venv\Scripts\python -m app.reset_admin` (Docker:
`docker compose exec app python -m app.reset_admin`) clears it; `/admin` then asks for a new one.

Read-only by design: documents stay in SharePoint / OneDrive / Drive and are only read; the server
keeps the extracted figures, not the files (an opt-in switch under Admin → Data keeps copies for
faster previews). Untick "Allow uploads" on a source and the app never asks for write access.

The dashboard ships with demo data (including generated sample pages for previews) so it can be
explored before anything is connected.

## Which model reads the documents (Ollama, Claude or Gemini)

The reader is chosen under Admin → Document reader (default from `LLM_PROVIDER` in `.env`;
`ollama` when unset). A second reader can be named as the fallback.

| | `ollama` (default) | `claude` | `gemini` |
|---|---|---|---|
| Where the content goes | A self-hosted Ollama server on your network; nothing leaves the LAN | The Anthropic API | The Google Gemini API |
| Reads | Text PDFs, Excel, Word, text/CSV (text is extracted first, `app/textract.py`) | Everything, incl. photos and scanned PDFs | Everything, incl. photos and scanned PDFs |
| Needs | `OLLAMA_BASE_URL`, `OLLAMA_MODEL` (e.g. `qwen2.5:3b`) | A Claude key (Admin) | A Gemini key (Admin), model e.g. `gemini-2.5-flash` |
| Prompt | Short prompt + flat JSON schema written for a 3B model | Full prompt + strict schema | Full prompt + response schema |
| Client | `app/ollama.py` (native API over httpx) | Anthropic SDK | `app/gemini.py` (REST over httpx, key in a header) |

```
LLM_PROVIDER=ollama                          # ollama | claude | gemini
LLM_FALLBACK=                                # second reader: claude | gemini | ollama, or empty
OLLAMA_BASE_URL=http://192.168.0.183:11434   # the Ollama PC; keep it off the internet, it has no login
OLLAMA_MODEL=qwen2.5:3b
# OLLAMA_NUM_CTX=8192  OLLAMA_TIMEOUT=180  OLLAMA_KEEP_ALIVE=30m
# GEMINI_API_KEY=AIza...  GEMINI_MODEL=gemini-2.5-flash    (or paste the key in Admin, stored encrypted)
```

Adding a fourth provider is a transport module like `app/gemini.py`, an entry in
`extract.EXTRACTORS`, a branch in `ask._answer`, and the name in `config.PROVIDERS` (see `app/llm.py`).

Every Ollama request uses temperature 0, a JSON schema in `format`, `num_ctx` ≥ 8192 (long documents
are cut to fit, keeping the start and the end), `keep_alive` 30m and a 180 s timeout. A malformed
answer is retried twice with the error pointed out. If the Ollama PC is asleep the sync stops with
one clear message instead of failing every file; photos and scanned PDFs are marked *skipped* with
the reason unless the Claude fallback is on.

Checks:

```
.venv\Scripts\python -m app.llm_check            # version, installed models, is OLLAMA_MODEL pulled, one-line probe
.venv\Scripts\python -m app.llm_check --sample   # + classifies two built-in sample documents
.venv\Scripts\python scripts\ollama_smoke.py     # end to end: a text invoice, a PDF receipt and an Excel statement
```

The same check is behind the **Check now** button under Admin → Claude API, where the reader can
also be switched between Ollama and Claude (and the Claude fallback turned on) without touching
`.env`. The app re-checks the reader every minute: the dashboard header shows a badge (**AI: Ollama ·
connected / UNREACHABLE / model missing**, or **AI: Claude · ready / no key**), the admin status
strip shows the same, a toast announces every change, and the Sync & setup tab lists how many
documents each model has read. Only the extracted figures are stored, never the files.

## Production

See `docs/DEPLOYMENT.md`. In short:

```bash
cp .env.production.example .env.production   # PUBLIC_BASE_URL, LEDGER_DOMAIN, APP_SECRET_KEY
docker compose up -d --build
```

## Development

```
.venv\Scripts\python -m pytest tests        # 82 tests, no network (fake Claude, fake Ollama, fake Gemini, fake Drive, fake Graph)
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
  extract.py     document reading: Claude structured extraction, the small-model (Ollama) prompt + schema, bookkeeping rules
  ollama.py      Ollama client (native /api/chat with JSON schema, num_ctx, keep_alive; health check)
  gemini.py      Gemini client (REST generateContent with a response schema; key test; schema converter)
  textract.py    plain-text extraction (PDF text layer, Excel, Word) for text-only models
  llm.py         provider façade (ready? health?) + llm_check.py command line; errors.py shared ApiUnavailable
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
