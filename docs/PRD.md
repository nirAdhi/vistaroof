# Roofing Ledger — Product Requirements Document

| | |
|---|---|
| Product | Roofing Ledger |
| Version | 3.0 (production candidate) |
| Owner | Vista Roofing Limited |
| Date | 25 October 2026 |
| Status | Implemented; this document describes the shipped behaviour and the acceptance criteria it is tested against |

## 1. Summary

Roofing Ledger turns the pile of invoices, receipts, quotes, payslips and statements that a roofing
contractor keeps in Google Drive into a live picture of the business: what was invoiced, what was
spent, what is owed, what is quoted, per month, per customer, per supplier and per job. Documents are
read automatically by Claude; the owner can see the original file next to every figure and correct
anything by hand. Documents flow both ways: anything dropped into Google Drive appears in the ledger
within minutes, and anything uploaded through the app is saved into Google Drive.

## 2. Problem

- The company's financial paper trail lives as PDFs and phone photos in Google Drive, organised by
  habit rather than by system. Nobody has a running total without a bookkeeper.
- Receipts are often handwritten or printed till slips; many invoices carry handwritten "PAID"
  notes. Accounting packages expect typed-in data.
- The owner wants to answer questions like "who still owes me money", "what did Oak Drive cost me",
  "how much VAT do I owe this quarter" without reading every file.

## 3. Goals and non-goals

Goals
1. Zero data entry: every supported document in the connected Drives is read automatically.
2. Transparency: every number can be traced to the original document in two clicks.
3. Correctness over cleverness: bookkeeping treatment is deterministic and documented; the model
   only reads.
4. Two-way sync with Google Drive, near real time.
5. Operable by one admin without touching files on the server (all settings in the UI).
6. Safe to expose on the internet behind HTTPS.

Non-goals (this version)
- Replacing an accounting package or filing VAT returns.
- Double-entry bookkeeping, bank reconciliation, multi-currency conversion.
- Multi-tenant SaaS; one installation serves one company.
- Mobile app (the web UI is responsive).

## 4. Users and roles

| Role | Who | Can |
|---|---|---|
| User | the owner, office manager, bookkeeper — anyone who should see the figures | view every screen, upload documents, correct readings, ask questions, trigger a sync, change their own password |
| Admin | whoever set the system up (may be the same person) | everything a user can, plus: Claude key and model, company details, Google / Microsoft app registrations, add/connect/remove sources, **user accounts**, passwords, backups, demo data |

Everyone has a personal account: name, email (the sign-in name), password, role. Accounts are
created, renamed, disabled, reset and removed under Admin → Users & security; an admin can give
another account the admin role. The built-in admin password created on first run remains as the
fallback login (email left empty) and is the only credential that exists before accounts are added.
Disabling an account or resetting its password ends its sessions in every browser immediately.

## 5. Functional requirements

### 5.1 Sources (Admin → Sources)
- FR-1 Any number of **Microsoft 365** sources (SharePoint document libraries / folders, OneDrive,
  or "everything shared with the account") can be connected, each with its own Microsoft sign-in
  (OAuth 2.0 + PKCE via Microsoft Graph; delegated scopes `Files.Read.All`, `Sites.Read.All`,
  `User.Read`, `offline_access`, plus `Files.ReadWrite.All` when uploads are enabled for the
  source). A source is defined by a pasted SharePoint/OneDrive folder link; a per-source tenant lets
  a guest account sign in to the organisation that owns the files.
- FR-1b Any number of Google Drive accounts can be connected, each with its own Google sign-in
  (OAuth 2.0, scopes `drive.readonly` + `drive.file`). A source may be limited to one folder tree.
- FR-2 Any number of local folders (e.g. Google Drive for Desktop, OneDrive sync folder) can be
  registered; optionally restricted to administrator-defined roots (`LOCAL_ROOTS`).
- FR-3 Each source can be enabled/disabled, synced, fully re-read, disconnected, edited, removed
  (removing deletes its documents from the ledger, never from SharePoint/Drive).
- FR-4 Returning from the Microsoft or Google sign-in starts the first sync of that source immediately.

### 5.2 Reading documents
- FR-5 Supported inputs: PDF, JPEG/PNG/GIF/WebP/HEIC/HEIF/TIFF/BMP photos and scans, Word, Excel,
  CSV, text, Google Docs/Sheets/Slides (exported). Others are recorded as skipped with a reason.
- FR-6 For each document the system records: type (17 types incl. sales invoice, proforma, quote,
  credit note, purchase order, supplier invoice, receipt, remittance, payslip, expense claim, bank
  statement, delivery note, contract, insurance, tax document, certificate, other), direction
  (money in / out / none), counterparty and role, number, issue and due dates, currency, net, VAT,
  total, amount paid, payment status, income/expense category, job/site, line items, one-line
  summary, confidence and flags (handwritten, poor scan, total mismatch, suspicious content…).
- FR-7 Handwritten or stamped payment marks count as evidence of payment; till receipts are read as
  paid supplier receipts.
- FR-8 Phone photos are auto-rotated, converted (HEIC) and downscaled before reading.
- FR-9 Files are read once; a file is re-read only when it changes in the source, on an explicit
  "Full re-read", or via "Re-read with Claude" on one document. Hand-edited documents are never
  re-read automatically.

### 5.3 Bookkeeping rules (deterministic, in code)
| Counted as | Documents | Effect |
|---|---|---|
| Revenue | sales invoices issued; credit notes issued subtract | revenue, VAT on sales, receivables |
| Expense | supplier invoices, purchase receipts, payslips, expense claims; credit notes received subtract | costs, VAT on purchases, payables |
| Pipeline | quotes, estimates, proformas sent | shown separately |
| Commitment | purchase orders raised, supplier quotes/proformas received | shown separately |
| Payment | receipts issued, remittance advices | recorded, not double-counted |
| Reference | statements, contracts, insurance, certificates, tax forms, delivery notes | none |

Figures are on an invoice basis by document date, net of VAT when VAT is shown. Receivables =
sales invoices with status unpaid/partial/overdue/unknown less amount paid; overdue by due date.

### 5.4 Owner dashboard
- FR-10 Period filter (this year, last 12 months, last year, quarter, all time, custom) scoping every tab.
- FR-11 Overview: net profit (hero), revenue, costs, owed to us, we owe, VAT position, pipeline;
  monthly revenue vs costs; cumulative profit; costs by category; needs-attention list; document mix.
- FR-12 Money in / Money out: by customer / supplier, by category, unpaid invoices / bills with
  outstanding amounts and overdue highlighting, pipeline list, committed purchase orders.
- FR-13 Jobs: revenue, costs, margin and quoted value per job/site.
- FR-14 Companies: directory of every customer, supplier and employee with totals; a company window
  showing all its documents with the file and the extracted facts side by side, previous/next.
- FR-15 Documents: searchable, filterable ledger (type, treatment, payment status, processing
  status), CSV export, preview per row, details panel with hand editing and re-read.
- FR-16 Preview: the actual scan/photo/PDF for any document, in a window beside the facts; demo
  documents show generated sample pages.
- FR-17 Ask: natural-language questions answered from the ledger with document references.
- FR-18 Every chart has a table view; charts are colour-blind safe and labelled.

### 5.5 Two-way cloud sync (Microsoft 365 and Google Drive)
- FR-19 Watcher: every N seconds (default 120, admin-configurable, min 30, 0 = off) each connected
  source is asked for changes (Microsoft Graph *delta* per watched folder; Google Drive *changes*
  feed); local folders are re-scanned. Changed sources are synced immediately; a full re-scan of all
  sources runs every M minutes (optional).
- FR-20 Files deleted or moved out of scope in SharePoint/Drive are marked removed and leave the
  figures; they return if restored.
- FR-21 Upload: the owner can upload up to 20 files at a time into a chosen source: the SharePoint /
  OneDrive folder of the source (or a "Ledger Uploads" folder in the account's OneDrive), the Drive
  folder (or "Ledger Uploads" at the Drive root) or a local folder ("Ledger Uploads" sub-folder).
  Files over 4 MB go to Microsoft through an upload session. Uploaded files are read immediately and
  shown with their result and a preview. A read-only connection ("Can view" share, or uploads
  switched off for the source) reports why uploads are unavailable instead of failing silently.
- FR-22 The dashboard polls a version marker every 20 s and refreshes itself when documents arrive.
- FR-23 Sync requests that arrive while a sync runs are queued, not lost.

### 5.6 Administration
- FR-24 Claude: key (masked, testable), model, reading effort, Ask model, parallelism, size limit.
- FR-25 Company: name as printed on invoices, address, VAT number (used to tell own invoices from
  suppliers').
- FR-26 Google setup: upload/paste the OAuth client JSON once.
- FR-27 Sync log of every run (trigger, counts, errors) and the current run's failures.
- FR-28 Data: demo data load/remove, database backup download, delete all documents; document
  files are not stored on the server unless the admin turns on "keep a copy for faster previews"
  (`cache_previews`, default off), with a one-click delete of stored copies.
- FR-29 Users & security: user accounts (create with name/email/role/initial password, rename,
  change role, disable/enable, reset password, remove; self-service password change from the
  dashboard), admin password change (logs other sessions out),
  production status lines.

## 6. Non-functional requirements

| Area | Requirement | How it is met |
|---|---|---|
| Security | Authentication on every data route, encrypted secrets, CSRF and clickjacking protection, rate-limited logins, hardened headers, no exception leakage | see docs/SECURITY.md |
| Privacy | Data stays on the server except Google (storage) and Anthropic (reading) | architecture |
| Availability | Single process, automatic restart (Docker `restart: unless-stopped`), health endpoint | Dockerfile, /healthz |
| Performance | Dashboard aggregates computed in-memory from SQLite; < 200 ms for 10k documents | analytics.py |
| Cost | Only new/changed files are read; prompt caching on the system prompt; model/effort selectable | extract.py |
| Reliability | Per-file failures never abort a sync; interrupted syncs resume; migrations at start | sync.py, db.py |
| Operability | All settings in the UI; rotating logs; backup download | settings.py, logsetup.py |
| Accessibility | Keyboard-reachable charts, table twins, colour-blind-safe palette, dark mode | static/ |
| Testability | 34 automated tests with fake Claude and fake Drive; headless-browser verification | tests/ |

## 7. Architecture

```
Browser (owner / admin)  ──HTTPS──▶  Caddy  ──▶  uvicorn / FastAPI (app/main.py)
                                                   ├─ settings.py   (DB-backed settings, sessions, passwords)
                                                   ├─ sources.py    (Google Drive API, local folders, OAuth)
                                                   ├─ sync.py       (pipeline, watcher thread, uploads)
                                                   ├─ extract.py    (Claude structured extraction + rules)
                                                   ├─ analytics.py  (aggregates), ask.py (Q&A)
                                                   ├─ previews.py   (file cache), images.py, render_demo.py
                                                   └─ db.py         (SQLite, WAL)  →  /data/ledger.db
External: Google Drive API (read, changes feed, create file), Anthropic Claude API (Messages, structured outputs)
```

Data model: `settings` (key/value), `sources`, `documents`, `sync_runs` — see the docstring at the
top of `app/db.py`.

## 8. API (all JSON, cookie session)

| Method & path | Role | Purpose |
|---|---|---|
| GET /api/auth/me · POST /api/auth/login · POST /api/auth/logout · POST /api/auth/password | any | login (email + password; the built-in admin uses `ADMIN_EMAIL`, or an empty email when none is configured), own password change |
| GET/POST /api/admin/users · PATCH/DELETE /api/admin/users/{id} · POST /api/admin/users/{id}/password | admin | user accounts |
| GET /api/config, /api/meta, /api/version | owner | UI bootstrap, live-refresh marker |
| GET /api/summary?from&to · GET /api/companies | owner | aggregates |
| GET /api/documents… · GET /api/documents/{id} · GET /api/documents/{id}/preview[?embed=1] | owner | ledger, file |
| PATCH /api/documents/{id} · POST /api/documents/{id}/reprocess | owner | hand edit, re-read |
| POST /api/sync · GET /api/sync/status | owner | sync |
| GET /api/upload/targets · POST /api/upload (multipart) | owner | upload |
| POST /api/ask | owner | questions |
| /api/admin/* (settings, test-key, google-client, ms-client, sources, sync, sync-stop, watch-now, runs, backup, demo, passwords) | admin | administration |
| GET /admin/sources/{id}/connect → Google → GET /admin/oauth/callback | admin | Google OAuth |
| GET /admin/sources/{id}/connect → Microsoft → GET /admin/oauth/ms-callback | admin | Microsoft OAuth (PKCE) |
| GET /healthz | none | health |

## 9. Acceptance criteria (automated)

- Auth: unauthenticated requests to every data route return 401; owner gets 403 on admin routes;
  tokens expire and cannot be forged; 11th failed login in 15 min returns 429.
- Sync: incremental (unchanged files not re-read), forced, failing file recorded without aborting,
  re-read, override survives forced re-read, disable, remove.
- Drive: OAuth consent URL built from the uploaded client, state validated, cancelled sign-in
  reported, first sync after callback, two Drives with the same file id do not collide, exported
  Google Docs/Sheets reach the model as PDF/CSV.
- Microsoft 365: registration saved without exposing the secret; consent URL carries the source's
  tenant, PKCE challenge and requested scopes; link resolved through the shares API after sign-in
  (and immediately on edit); folder tree listed with paging; downloads follow the redirect without
  the bearer token; unsupported types skipped; delta watcher seeds, detects additions and deletions;
  uploads via single PUT and via upload session; read-only share (403) and read-only token reported;
  "shared with me" mode lists remote folders and files; token refresh saves the new token and a
  revoked refresh token yields "needs to be reconnected".
- Two-way: uploads land in the folder / Drive / SharePoint and are read immediately; unsupported,
  empty and oversized uploads rejected; watcher triggers only on changes; deletions remove figures
  and restores bring them back; busy syncs queue requests.
- Previews: real PDF/image/text/Word files served with correct types; HEIC converted; oversized
  images downscaled; demo pages rendered for every type.
- Security: CSP and headers present, cross-origin POST blocked, secrets encrypted with
  `APP_SECRET_KEY` and unreadable with another key, backup is a valid SQLite file, validation limits.

Run: `pytest tests` (43 tests, fake Claude + fake Google Drive + fake Microsoft Graph). Manual: the
headless-browser scripts used during development exercise login, admin setup, company window,
preview viewer and uploads.

## 10. Rollout

1. Deploy per docs/DEPLOYMENT.md; complete the going-live checklist.
2. Connect the SharePoint Invoices folder (HRAccounts site) first; review 20–30 documents against
   previews; fix company name/address if directions are wrong; then connect the Projects folder
   (VistaFiles site) and any Google Drives.
3. Remove demo data. Set the watcher interval (2 minutes default) and a daily full re-scan.
4. Train the owner on: period filter, Companies window, Preview, Upload, hand corrections, Ask.

## 11. Risks and open questions

| Risk | Mitigation |
|---|---|
| Misread amounts on poor scans | confidence + flags surface them in *Needs attention*; preview beside facts; hand edit |
| Duplicate documents (same invoice as PDF and photo) | `duplicate_suspected` flag; future: automatic duplicate detection by number+amount |
| Google token expiry for apps in "testing" mode (7 days) | publish the consent screen in Google Cloud, or reconnect from the source card |
| Claude cost spikes from a Drive full of unrelated files | limit sources to accounts folders; size cap; model/effort settings |
| Single server, single process | backups, Docker restart policy; the app is stateless apart from SQLite |

## 12. Future work (not in scope)

Duplicate detection, multi-currency conversion, bank-statement reconciliation, export to Sage /
QuickBooks / Xero, e-mail ingestion, mobile camera capture, per-user audit trail of hand edits.
