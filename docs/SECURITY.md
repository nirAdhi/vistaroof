# Security review and threat model

Scope: Roofing Ledger v3.2 (owner dashboard, admin area, Microsoft 365 / Google Drive / local
sources with two-way sync, document reading by Ollama, Claude or Gemini). First reviewed
2026-10-25; re-reviewed 2026-11-08 for the multi-provider release by code reading, automated
scanning (`pip-audit`, `bandit`) and the test suite in `tests/` (82 tests, of which 14 are
security-specific, including a router walk that proves every `/api` route needs a session).

## 1. What the system holds and who can reach it

| Asset | Sensitivity | Where |
|---|---|---|
| Company invoices, receipts, payslips (extracted figures; files only when the opt-in cache is on) | High (financial + personal data of employees/customers) | SQLite `documents`, optional preview cache, the source Drives / SharePoint |
| Claude API key, Gemini API key | High (billable; Gemini free tier is also a data-use question, see §4) | `settings` table, encrypted with `APP_SECRET_KEY`, returned only masked |
| Ollama server address | Low (LAN address) | `.env` only (`OLLAMA_BASE_URL`), never settable from the UI |
| Google OAuth refresh tokens (read access to whole Drives, create-file access) | High | `sources.token_json`, encrypted |
| Microsoft 365 refresh tokens (read, and optionally write, every file the signed-in account can reach) | High | `sources.token_json`, encrypted; delegated scopes only, PKCE flow, scopes stored with the token |
| Google OAuth client secret / Microsoft app client secret | Medium | `settings`, encrypted; never returned by the API |
| Admin password and user account passwords | High | PBKDF2-SHA256 hashes, 200k iterations, per-user salt (`settings`, `users.password_hash`); or `ADMIN_PASSWORD` from the environment, hashed at start |
| Session signing secret | High | `settings.session_secret` |

Actors: **admin** (full control: the built-in admin login or an account with the admin role),
**user** (a named account: read dashboard, upload, edit figures, ask questions),
**anonymous network user**, **a malicious document** placed in a watched folder, **the model
providers** (Anthropic, Google) and **the storage providers** (Microsoft, Google) as third parties,
and **anyone on the LAN** with respect to the Ollama server.

### What the server stores, and what it never touches

- **Stored:** the extracted figures per document (type, amounts, dates, counterparty, summary,
  the model's JSON) in `documents`; settings; sign-in tokens (encrypted); user accounts (hashed).
- **Not stored by default:** the document files. Each file is fetched from SharePoint / OneDrive /
  Drive once for extraction, sent to the chosen reader, and discarded. Opening a preview fetches it
  again. Admin → Data has an opt-in "keep a copy for faster previews" switch (`cache_previews`);
  turning it off deletes the copies.
- **Never changed:** the source folders. Sources connected without the upload option ask only for
  read scopes (`Files.Read.All`, `Sites.Read.All`), so the token cannot modify, move or delete
  anything. Corrections made in the app change only the ledger's own rows.

### Where document content goes, per reader

| Reader | What leaves the server | Transport | Who sees it |
|---|---|---|---|
| **Ollama** (default) | Extracted *text* of the document (PDF text layer, Excel / Word rows, text files); never the file bytes, never photos | Plain HTTP on the LAN to `OLLAMA_BASE_URL` | Only the Ollama host. It has **no authentication**: it must be reachable from the app's network only, never from the internet |
| **Claude** | The file itself (PDF, photo) or its text | HTTPS to api.anthropic.com | Anthropic, under its API data-processing terms |
| **Gemini** | The file itself (PDF, photo) or its text | HTTPS to generativelanguage.googleapis.com, key in the `x-goog-api-key` header (never in the URL) | Google, under the Gemini API terms — see §4 about the free tier |
| **Ask** (any reader) | Extracted rows and totals only, never files | as above | as above |

A second reader (fallback) can be named; it receives a file only when the first reader is
unreachable, unfunded, or cannot read it (a text-only model given a photo). The admin chooses both
readers; a user cannot.

## 2. Controls in place

### Authentication and sessions
- Every `/api` route requires a session; only the two HTML pages, `/static`, `/healthz`, the login
  endpoints and the OAuth callbacks are reachable anonymously. `tests/test_security.py::test_every_api_route_needs_a_session`
  walks the router so a new route cannot be added without protection, and the login dependency runs
  before body validation so an anonymous caller never learns a body schema from a 422.
- Named accounts (`users` table) with role `user` or `admin`; the built-in admin login is the
  fallback (`ADMIN_EMAIL` + password; only without `ADMIN_EMAIL` does an empty email name the
  admin). It is created on first run or taken from `ADMIN_PASSWORD` in the environment at every
  start; in that case the UI cannot change it, which keeps the operator's `.env.production` the
  single source of truth. The variable is only ever hashed into the database, never logged. User
  sessions get 403 on every `/api/admin/*` route and on the OAuth connect route; `/api/admin/login`
  refuses accounts without the admin role.
- Session cookie: `HttpOnly`, `SameSite=Lax`, `Secure` when served over HTTPS (or `PUBLIC_BASE_URL`
  is https), 14-day expiry embedded in the HMAC-signed token
  (`role.uid.token_version.expires.nonce.hmac`). For accounts the role is taken from the table on
  every request, never from the cookie, and the embedded `token_version` must match: disabling an
  account, changing its role or resetting its password bumps the version and ends its sessions at
  once. Changing the admin password rotates the signing secret and logs every browser out.
- An admin cannot demote, disable or delete the account they are signed in with (prevents lock-out).
- Login answers "wrong email or password" for unknown and disabled accounts alike and spends a hash
  computation either way, so emails cannot be enumerated by message or timing.
- Minimum password length 10; passwords are never logged or returned (the API strips
  `password_hash` from every user row).
- Login rate limit: 10 attempts per client IP per 15 minutes on both login endpoints, plus a 300 ms
  delay per attempt. With Docker/Caddy the real IP comes from `X-Forwarded-For` (uvicorn
  `--proxy-headers`).

### Cross-site request forgery
- `SameSite=Lax` cookies stop cross-site POSTs in modern browsers.
- Defence in depth: the middleware rejects any POST/PUT/PATCH/DELETE whose `Origin` (or `Referer`)
  host differs from the request host / `PUBLIC_BASE_URL` (403).
- The OAuth callback validates a random `state` bound to the source and expiring after 10 minutes.

### Browser hardening
- `Content-Security-Policy: default-src 'self'; script-src 'self'; connect-src 'self'; …` — no inline
  scripts, no third-party scripts or styles, no browser-side calls to any model provider (all model
  traffic is server-side). `frame-ancestors 'self'` prevents clickjacking.
- `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `Permissions-Policy`, HSTS on HTTPS,
  `Cache-Control: no-store` on `/api/*`, `no-cache` + content-hashed asset URLs for the pages and scripts.
- All user-visible strings are HTML-escaped in the front end (`esc()`); document names, counterparties,
  summaries, model messages and the Ollama / Gemini model lists come from untrusted sources.
- Interactive API docs (`/docs`, `/openapi.json`) are disabled.

### Secrets at rest and in transit
- With `APP_SECRET_KEY` set, the Claude key, the Gemini key, the Google client JSON, the Microsoft
  client secret and every OAuth token are Fernet-encrypted (AES-CBC + HMAC) before hitting SQLite.
  Key derivation: PBKDF2-SHA256 from the operator secret. Without the variable the app runs but
  Admin shows a persistent warning.
- API keys are only ever returned masked (`sk-ant-a…1234`, `AIzaSyT…kkkk`); "Test key" accepts a
  pasted key for one request without saving it; keys travel in headers, never in URLs or logs.

### Outbound requests (the server as a client)
- The app calls out only to: the configured storage providers, api.anthropic.com,
  generativelanguage.googleapis.com, and `OLLAMA_BASE_URL`. The Ollama and Gemini base URLs come
  from the environment, not from any form, so an admin account cannot be used to turn the server
  into a request proxy (no SSRF surface in the UI). Model names typed in the UI are validated
  (`[A-Za-z0-9._:-]`) before they are placed in a request path.
- Every outbound call has a connect and read timeout; retries are bounded (3 attempts) with backoff.
- A fatal provider answer (key rejected, no credit, quota exhausted, model missing, host unreachable)
  stops the whole sync with one message instead of writing thousands of error rows, and the files it
  touched return to *pending*.

### Input validation and limits
- All request bodies are Pydantic models with length/range limits; unknown enum values are rejected
  (reader and fallback names, model names, effort levels).
- Uploads: max 20 files per request, per-file size limit from Admin, allow-list of file types by
  content, file names reduced to their base name (no path components), local uploads never overwrite.
- Images are re-encoded through Pillow (decompression-bomb protection is Pillow's default) and
  HEIC/TIFF/BMP are converted; SVG is not accepted (script risk). PDFs read by the text extractor are
  capped by the per-file size limit and 60 pages.
- Drive folder ids are validated against `[A-Za-z0-9_-]`; SharePoint links are resolved through
  Graph, never fetched directly.
- Local-folder sources can be confined with `LOCAL_ROOTS`.
- SQL: every value is a bound parameter; the few statements built from column names use
  code-level whitelists (`db._SOURCE_COLUMNS`, `db._RUN_COLUMNS`, `db.EDITABLE_FIELDS`).

### Untrusted documents, prompt injection and untrusted model output
- Every reader receives documents as data inside `<document_text>` tags (or as a file part) and is
  told that instructions inside documents are to be transcribed, not followed. Claude answers under
  a strict output schema; Gemini under a response schema; Ollama under a grammar-constrained schema
  with up to two corrective retries.
- The model's answer is treated as untrusted in all three cases: enumerations are mapped back to the
  known values (`extract._validate_full`, `_validate_small`), strings are bounded, numbers are
  parsed defensively, unknown categories become `null`, and nothing the model writes can reach SQL
  or the page unescaped. Text quoted back to a model in a retry is bounded too.
- Bookkeeping treatment (revenue/cost/pipeline…) is derived in code from (type, direction), so the
  worst a hostile document can do is misclassify itself; it is flagged `suspicious_content` and the
  owner can see the original in the preview.
- The Ask feature only ever sees extracted rows, never files, and is told to treat them as data.

### Operational
- Errors return generic messages unless `APP_DEBUG=1`; full tracebacks go to the rotating log.
- The Docker image runs as a non-root user; the database lives in a named volume.
- `pip-audit`: **no known vulnerabilities** in the pinned dependency set (2026-11-08) — two
  transitive packages were pinned past CVEs during this review (finding 15).
- `bandit -r app -ll`: no medium/high findings.

## 3. Findings fixed during the reviews

| # | Finding | Severity | Fix |
|---|---|---|---|
| 1 | Owner dashboard and the entire read API were unauthenticated | Critical on a server | Session login for every data route; named user accounts with roles |
| 2 | Session tokens never expired | High | Expiry inside the signed token; secret rotation on password change |
| 3 | API key and Google tokens stored in plain text | High | Fernet encryption at rest with `APP_SECRET_KEY` |
| 4 | No login rate limiting beyond a fixed delay | High | Per-IP sliding window (10 / 15 min) |
| 5 | No CSP / security headers; inline `onclick` handler in the dashboard | Medium | Middleware headers, inline handler removed |
| 6 | No CSRF protection beyond SameSite | Medium | Origin/Referer check on state-changing methods |
| 7 | OAuth redirect URI derived from the `Host` header | Medium | Pinned via `PUBLIC_BASE_URL` in production |
| 8 | Exception text returned to clients (preview, re-read, ask) | Medium | Generic messages unless `APP_DEBUG` |
| 9 | Admin could register any directory as a source | Medium (admin is trusted, but limits blast radius) | `LOCAL_ROOTS` allow-list |
| 10 | No request size/count limits on JSON bodies and uploads | Medium | Pydantic limits, 20-file cap, per-file size cap |
| 11 | Minimum password length 6 | Low | Raised to 10 |
| 12 | SQL statements assembled from dict keys | Low (keys came from code) | Explicit whitelists + assertions |
| 13 | OpenAPI/Swagger pages exposed | Low | Disabled |
| 14 | Weak hash flagged by bandit in preview cache names | Info (not security use) | sha256 with `usedforsecurity=False` |
| 15 | (2026-11-08) `oauthlib` 4.0.0 and `pyasn1` 0.6.4, pulled in transitively by the Google client, carried CVE-2026-49272 and CVE-2026-119194…119197 | Medium | Pinned to 4.0.1 / 0.6.5 in `requirements.txt` |
| 16 | (2026-11-08) `POST /api/auth/password` validated the body before checking the session: an anonymous caller got a 422 describing the expected fields | Low (information) | Login dependency on the route; router-walk test added so it cannot recur |
| 17 | (2026-11-08) A model-produced value was quoted unbounded in the Ollama retry message and the log | Low | Bounded to 60 characters |
| 18 | (2026-11-08) Document files could only be read by a cloud provider; no option kept content on-premises | Design | Ollama reader (text only, LAN) with provider choice and fallback under admin control |

## 4. Residual risks and recommendations

- **Passwords only, no MFA.** Acceptable for a small company behind HTTPS; for stronger
  assurance put the app behind an identity-aware proxy (Cloudflare Access, Tailscale) or a VPN.
  The app already works correctly behind such proxies. Keep the number of admin-role accounts small
  and prefer accounts over the shared built-in admin login so sign-ins are attributable.
- **User role can edit figures and upload.** By design; every edit is marked `overridden` and the
  original is one click away in the preview.
- **Cloud readers see document contents.** Claude (Anthropic) and Gemini (Google) receive the
  files or their text. Both run under their API terms. **Gemini: use a project with billing
  enabled.** Google states that prompts sent through the free tier of the Gemini API may be used to
  improve its products, while paid usage is not; invoices and payslips should not go through the
  free tier. Choose Ollama as the reader to keep content on the LAN entirely.
- **The Ollama server has no authentication.** Anyone who can reach port 11434 can read what the
  app sends and use the GPU. Keep it on a private network / firewall it to the app host; never
  port-forward it. The app shows the Ollama address to every signed-in user on the Sync & setup tab
  (a LAN address, low sensitivity); hide it with a reverse proxy if staff should not see it.
- **Google and Microsoft tokens grant read access to whole Drives / sites.** Limit each source to a
  folder where possible, connect a dedicated accounts account, and disconnect sources no longer needed.
- **PDF parsing on the server.** With the Ollama reader the server itself parses PDFs (`pypdf`);
  a crafted file can cost CPU time. The per-file size limit and the 60-page cap bound it; keep
  `pypdf` current.
- **Rate limiting is in-process memory.** With more than one worker process (not the default) use a
  shared store or do limiting at the proxy.
- **Backups contain all financial data.** Store them encrypted (the file itself is not encrypted;
  only the secrets inside are).
- **Dependency drift.** Re-run `pip-audit` monthly and before each deployment; this review found two
  transitive CVEs that a pinned top-level set did not prevent.

## 5. Release checklist

```
.venv\Scripts\python -m pip_audit                 # 0 vulnerabilities      (2026-11-08: clean)
.venv\Scripts\python -m bandit -r app -ll         # 0 medium/high          (2026-11-08: clean)
.venv\Scripts\python -m pytest tests              # all green              (2026-11-08: 82 passed)
```
Then confirm in Admin → Users & security that all four status lines are green (admin login, at
least one user account, secrets encrypted, public URL pinned), and in Admin → Document reader that
the chosen reader shows *connected* / *ready* and, if a cloud reader is used, that its key belongs
to a billed project.
