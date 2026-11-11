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

## How syncing works (full read once, then changes only)

The first sync of a source lists every file and reads each one with the document reader. After that
nothing is re-read unless it changed: the watcher asks SharePoint / OneDrive / Google Drive for
*changes* every `watch_interval_seconds` (Admin → Document reader; default 120 s, minimum 10 s)
using their change feeds (Microsoft Graph delta / Drive changes). A check is one small request per
folder and costs no GPU or API usage; only a new or modified file is downloaded and read. The
dashboard notices new figures within 20 s and refreshes itself. "Full re-read" re-reads everything
and is only for when the reader or its prompt changed.

A sync that starts by itself is one of these, and the Sync log (Admin → Sync log, and the Sync tab
on the dashboard) says which in its "Started by" column: a change reported by SharePoint / Drive, a
request queued behind the previous run (a Sync button pressed while one was running), the auto-sync
timer, or a fresh sign-in. A watch-triggered run re-lists the whole folder (a few minutes for
thousands of files) but only downloads and reads what changed, plus error rows that have not yet
used their three attempts.

Each source card in Admin shows how many of its files were read, skipped and failed; the numbers and
"View documents" open the dashboard's Documents tab filtered to that source (and status) — the tab
has an "All sources" selector for the same purpose. A spreadsheet or text export too long to be one
document (over 300,000 characters after empty cells are trimmed, e.g. a job-costing workbook with a
sheet per job) is skipped with that reason: enter the figures you need by hand, or save the relevant
sheet as its own file.

## When the reader is off (and email alerts)

Every figure lives in the app's own database, so the dashboard, documents, companies, previews and
hand edits keep working when the document reader is unreachable — the Ollama PC switched off, a
Claude or Gemini key out of credit. Only *reading new files* pauses: new documents wait as *pending*
and the next change check reads them once the reader is back; nothing has to be re-synced.

Admin → Alerts sends an email (SMTP: Microsoft 365, Gmail or any mail server; the password is stored
encrypted) when the reader goes down or comes back and when a sync is stopped because a reader
could not be used. The same alert is not repeated within an hour; a recovery is always sent; "Send
test email" checks the settings. Initial values: `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY`,
`SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `ALERT_TO` in `.env`.

## Backups (Admin → Data → Backups)

Two kinds, written to a folder on the server — point it at a mounted USB / external drive — and
also downloadable as a zip to save on a drive attached to your own PC:

| | Application backup | Full backup |
|---|---|---|
| Contents | `ledger.db` (every figure, setting, source, user; secrets stay encrypted), `documents.csv`, `summary.json`, `manifest.json` in one zip | The same zip under `app/` plus every document file fetched again from SharePoint / OneDrive / Drive / local folders under `files/<source>/<folder>/<name>` |
| Takes | seconds | minutes to hours (one download per file; nothing on the sources is changed) |
| Schedule | every N hours (`backup_every_hours`), keeping the newest `backup_keep` | on demand, with progress and Stop |
| Restore | stop the app, replace `data/ledger.db` with the one in the zip, start | same; the `files` tree is a plain copy of the folders |

`BACKUP_DIR` / `BACKUP_EVERY_HOURS` / `BACKUP_KEEP` in `.env` give the starting values;
`BACKUP_ROOTS` can confine where backups may be written. Backups are listed, downloadable and
deletable from the same card; only files matching the app's own naming pattern are ever touched.

## Which model reads the documents (Ollama, Claude or Gemini)

The reader is chosen under Admin → Document reader (default from `LLM_PROVIDER` in `.env`;
`ollama` when unset). A second reader can be named as the fallback.

| | `ollama` (default) | `claude` | `gemini` |
|---|---|---|---|
| Where the content goes | A self-hosted Ollama server on your network; nothing leaves the LAN | The Anthropic API | The Google Gemini API |
| Reads | Text PDFs, Excel, Word, LibreOffice (.odt/.ods), text/CSV (text is extracted first, `app/textract.py`); photos and scanned PDFs too when `OLLAMA_VISION_MODEL` names a vision model (e.g. `qwen3-vl:2b`) | Everything, incl. photos and scanned PDFs | Everything, incl. photos and scanned PDFs |
| Needs | `OLLAMA_BASE_URL`, `OLLAMA_MODEL` (e.g. `qwen2.5:3b`) | A Claude key (Admin) | A Gemini key (Admin); model `auto` = newest Flash the key can use |
| Prompt | Short prompt + flat JSON schema written for a 3B model | Full prompt + strict schema | Full prompt + response schema |
| Client | `app/ollama.py` (native API over httpx) | Anthropic SDK | `app/gemini.py` (REST over httpx, key in a header) |

```
LLM_PROVIDER=ollama                          # ollama | claude | gemini
LLM_FALLBACK=                                # second reader: claude | gemini | ollama, or empty
OLLAMA_BASE_URL=http://192.168.0.183:11434   # the Ollama PC; keep it off the internet, it has no login
OLLAMA_MODEL=qwen2.5:3b
# OLLAMA_VISION_MODEL=qwen3-vl:2b   # optional: photos and scanned PDFs read locally too (ollama pull qwen3-vl:2b); images are shrunk to 1600 px
# OLLAMA_NUM_CTX=8192  OLLAMA_TIMEOUT=180  OLLAMA_KEEP_ALIVE=30m
# OLLAMA_VISION_TIMEOUT=300  OLLAMA_VISION_MAX_SIDE=1280   # per photo / scan; a slower file is skipped, never an outage
# GEMINI_API_KEY=AIza...  GEMINI_MODEL=auto    (or paste the key in Admin, stored encrypted; auto = newest Flash model for the key)
```

Adding a fourth provider is a transport module like `app/gemini.py`, an entry in
`extract.EXTRACTORS`, a branch in `ask._answer`, and the name in `config.PROVIDERS` (see `app/llm.py`).

Every Ollama request uses temperature 0, a JSON schema in `format`, `num_ctx` ≥ 8192 (long documents
are cut to fit, keeping the start and the end), `keep_alive` 30m and a 180 s timeout. A malformed
answer is retried twice with the error pointed out. If the Ollama PC is asleep the sync stops with
one clear message instead of failing every file; photos and scanned PDFs are marked *skipped* with
the reason unless the Claude fallback is on.

A file is never allowed to stall the sync: when a request times out but the Ollama server still
answers a probe, that *file* is skipped as "reader too slow" (hand it to the fallback reader if one
is set, enter it by hand, or Re-read it later) and the run carries on. Only a server that answers
nothing is reported as unreachable. A file whose read fails with an error is retried on the next two
syncs at most; after three attempts it rests as an error (with "gave up" in the message) until it
changes, a Full re-read, Re-read, or a hand entry. Rows left "processing" by a restart mid-sync go
back to pending at start-up.

### Ask about the books with the local model

The Ask tab sends the question, the dashboard aggregates and the ledger rows to the chosen reader;
the files themselves are never sent. With Ollama the prompt is kept small so a 3B model on a small
GPU answers in seconds, not minutes: a trimmed summary plus at most 60 rows picked for the question
(rows naming the customer or job asked about, unpaid invoices for "who owes us", expenses for
"supplier", newest first). The Ollama PC answers one request at a time, so while a sync is reading
documents a question waits behind them; the Ask tab shows a live timer and says so, and a question the
model cannot finish in `OLLAMA_TIMEOUT` seconds is reported as such (not as an outage).

### Entering a document by hand

Handwritten receipts, faint carbon copies and odd scans are often quicker to type in than to keep
re-reading. Any *skipped*, *error* or *pending* document has an **Enter by hand** button (Documents
list, the preview and the details drawer): look at the file, choose what it is, fill in the amounts
and save. The figures count in the dashboard immediately, show as "by hand" (reader `manual` in the
Sync tab), and are never overwritten by a sync; "Re-read" lets a model try again later. **Not a
financial document** marks a note or site photo as nothing to count so it leaves the skipped list.

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
.venv\Scripts\python -m pytest tests        # 105 tests, no network (fake Claude, fake Ollama, fake Gemini, fake Drive, fake Graph, fake SMTP)
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
  backup.py      application / full backups to a folder (USB drive), zip downloads, retention, schedule; repair.py date repair
  notify.py      email alerts over SMTP (reader down / back, sync stopped by a reader failure)
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
