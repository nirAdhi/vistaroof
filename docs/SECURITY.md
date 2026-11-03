# Security review and threat model

Scope: Roofing Ledger v3.0 (owner dashboard, admin area, Google Drive two-way sync, Claude
extraction). Reviewed 2026-10-25 by code reading, automated scanning (`pip-audit`, `bandit`) and the
test suite in `tests/` (34 tests, of which 11 are security-specific).

## 1. What the system holds and who can reach it

| Asset | Sensitivity | Where |
|---|---|---|
| Company invoices, receipts, payslips (files + extracted figures) | High (financial + personal data of employees/customers) | SQLite `documents`, preview cache, source Drives |
| Claude API key | High (billable) | `settings` table, encrypted with `APP_SECRET_KEY` |
| Google OAuth refresh tokens (read access to whole Drives, create-file access) | High | `sources.token_json`, encrypted |
| Microsoft 365 refresh tokens (read, and optionally write, every file the signed-in account can reach) | High | `sources.token_json`, encrypted; delegated scopes only, PKCE flow, scopes stored with the token |
| Google OAuth client secret / Microsoft app client secret | Medium | `settings`, encrypted; never returned by the API |
| Admin password and user account passwords | High | PBKDF2-SHA256 hashes, 200k iterations, per-user salt (`settings`, `users.password_hash`) |
| Session signing secret | High | `settings.session_secret` |

Actors: **admin** (full control: the built-in admin password or an account with the admin role),
**user** (a named account: read dashboard, upload, edit figures, ask questions),
**anonymous network user**, **a malicious document** placed in a watched Drive, **Google / Anthropic**
as third parties.

## 2. Controls in place

### Authentication and sessions
- Every data endpoint requires a session; only the two HTML pages, `/static`, `/healthz`, the login
  endpoints and the OAuth callback are reachable anonymously (`tests/test_security.py::test_everything_requires_login`).
- Named accounts (`users` table) with role `user` or `admin`; the built-in admin password is the
  fallback (empty email). User sessions get 403 on every `/api/admin/*` route and on the OAuth
  connect route; `/api/admin/login` refuses accounts without the admin role.
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
- `Content-Security-Policy: default-src 'self'; script-src 'self'; …` — no inline scripts, no third-party
  scripts or styles, images only from the app. `frame-ancestors 'self'` prevents clickjacking.
- `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `Permissions-Policy`, HSTS on HTTPS,
  `Cache-Control: no-store` on `/api/*`.
- All user-visible strings are HTML-escaped in the front end (`esc()`); document names, counterparties
  and summaries come from untrusted files.
- Interactive API docs (`/docs`, `/openapi.json`) are disabled.

### Secrets at rest
- With `APP_SECRET_KEY` set, the Claude key, the Google client JSON and every Google token are
  Fernet-encrypted (AES-CBC + HMAC) before hitting SQLite. Key derivation: PBKDF2-SHA256 from the
  operator secret. Without the variable the app runs but Admin shows a persistent warning.
- The API key is only ever returned masked (`sk-ant-a…1234`).

### Input validation and limits
- All request bodies are Pydantic models with length/range limits; unknown enum values are rejected.
- Uploads: max 20 files per request, per-file size limit from Admin, allow-list of file types by
  content, file names reduced to their base name (no path components), local uploads never overwrite.
- Images are re-encoded through Pillow (decompression-bomb protection is Pillow's default) and
  HEIC/TIFF/BMP are converted; SVG is not accepted (script risk).
- Drive folder ids are validated against `[A-Za-z0-9_-]`.
- Local-folder sources can be confined with `LOCAL_ROOTS`.
- SQL: every value is a bound parameter; the few statements built from column names use
  code-level whitelists (`db._SOURCE_COLUMNS`, `db._RUN_COLUMNS`, `db.EDITABLE_FIELDS`).

### Untrusted documents and prompt injection
- The model receives documents as data inside `<document_text>` tags, is told that instructions inside
  documents are to be transcribed not followed, and must answer in a fixed JSON schema (structured
  outputs). A document cannot make the model call tools or write free text into the ledger.
- Bookkeeping treatment (revenue/cost/pipeline…) is derived in code from (type, direction), so the
  worst a hostile document can do is misclassify itself; it is flagged `suspicious_content` and the
  owner can see the original in the preview.
- The Ask feature only ever sees extracted rows, never files, and is told to treat them as data.

### Operational
- Errors return generic messages unless `APP_DEBUG=1`; full tracebacks go to the rotating log.
- The Docker image runs as a non-root user; the database lives in a named volume.
- `pip-audit`: **no known vulnerabilities** in the pinned dependency set (2026-10-25).
- `bandit -r app`: no medium/high findings after review (remaining low findings are `random` used
  for demo data generation, which is not security-relevant).

## 3. Findings fixed during this review

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

## 4. Residual risks and recommendations

- **Passwords only, no MFA.** Acceptable for a small company behind HTTPS; for stronger
  assurance put the app behind an identity-aware proxy (Cloudflare Access, Tailscale) or a VPN.
  The app already works correctly behind such proxies. Keep the number of admin-role accounts small
  and prefer accounts over the shared built-in admin password so sign-ins are attributable.
- **User role can edit figures and upload.** By design; every edit is marked `overridden` and the
  original is one click away in the preview.
- **Google tokens grant read access to whole Drives.** Limit each source to a folder where possible,
  connect a dedicated accounts Google account, and disconnect sources no longer needed.
- **Anthropic and Google see document contents** (Claude for extraction, Google as the storage).
  Both are under their respective data-processing terms; the extracted figures are stored only locally.
- **Rate limiting is in-process memory.** With more than one worker process (not the default) use a
  shared store or do limiting at the proxy.
- **Backups contain all financial data.** Store them encrypted (the file itself is not encrypted;
  only the secrets inside are).
- **Dependency drift.** Re-run `pip-audit` monthly and before each deployment.

## 5. Release checklist

```
pip-audit                     # 0 vulnerabilities
bandit -r app -ll             # 0 medium/high
pytest tests                  # all green
```
Then confirm in Admin → Users & security that all four status lines are green (admin password, at
least one user account, secrets encrypted, public URL pinned).
