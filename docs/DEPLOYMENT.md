# Deployment guide

Roofing Ledger is a single-process web app (FastAPI + SQLite) that runs anywhere Python 3.12 or Docker
runs. This guide covers the recommended production setup: one small Linux server (1 vCPU, 1 GB RAM is
plenty), Docker Compose, Caddy for automatic HTTPS.

## 1. Before you start

| You need | Why |
|---|---|
| A domain name pointing at the server (A record), e.g. `ledger.vistaroofing.ie` | HTTPS certificate, Google OAuth redirect |
| Ports 80 and 443 open on the server firewall | Caddy obtains certificates on 80, serves on 443 |
| A Claude API key | Reading documents |
| A Google OAuth client (Desktop or Web type) | Connecting Google Drives |
| A long random `APP_SECRET_KEY` | Encrypts the API key and Google tokens in the database |

Generate the secret on any machine with Python:

```
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## 2. Install with Docker Compose (recommended)

```bash
git clone <your repository> roofing-ledger && cd roofing-ledger
cp .env.production.example .env.production
nano .env.production            # PUBLIC_BASE_URL, LEDGER_DOMAIN, APP_SECRET_KEY
docker compose up -d --build
docker compose logs -f app      # wait for "Application startup complete"
```

Open `https://<your domain>/admin`, create the admin password, then follow *Admin → Google setup*
and *Admin → Sources*. Create an account for the owner and each colleague under *Admin → Users &
security*; they sign in at `https://<your domain>/` with their email and password.

**Admin login.** The simplest option is to put it in `.env.production`:

```
ADMIN_EMAIL=you@example.com     # sign-in name; required at login once set
ADMIN_PASSWORD=<10+ characters>
```

**Document reader.** `LLM_PROVIDER=claude` uses the Anthropic API, `gemini` the Google Gemini API
(keys saved in Admin, encrypted); `ollama` (the default when the variable is absent) uses a
self-hosted Ollama server. The admin can change the reader and the fallback later in Admin →
Document reader; the environment only sets the starting values.

```
LLM_PROVIDER=ollama                          # ollama | claude | gemini
LLM_FALLBACK=                                # second reader when the first is unreachable / unfunded / cannot read a photo
OLLAMA_BASE_URL=http://192.168.0.183:11434   # must be reachable from the app container; never expose it publicly
OLLAMA_MODEL=qwen2.5:3b                      # ollama pull qwen2.5:3b on that machine
# GEMINI_API_KEY=                            # or paste it in Admin; use a project with billing enabled (see docs/SECURITY.md)
# GEMINI_MODEL=auto                          # newest Flash model the key can use (Google retires names for new accounts)
```

Verify from inside the container with `python -m app.llm_check` (Docker:
`docker compose exec app python -m app.llm_check`). A public deployment that should keep using
Claude must set `LLM_PROVIDER=claude` explicitly.

**Backups to a USB / external drive.** Mount the drive on the host and make it visible to the app,
then set the folder in Admin → Data → Backups (or `BACKUP_DIR`):

```
# LXC / systemd: the folder is simply a path on the host, e.g.
BACKUP_DIR=/mnt/usb/ledger-backups
BACKUP_EVERY_HOURS=24        # automatic application backup once a day
BACKUP_KEEP=14               # keep two weeks of them
# Docker: add the mount to the app service in docker-compose.yml, then use the container path
#   volumes:
#     - ledger-data:/data
#     - /mnt/usb/ledger-backups:/backups
# BACKUP_DIR=/backups
# BACKUP_ROOTS=/backups      # optional: the admin cannot point backups anywhere else
```

A full backup (every document file fetched again from the sources) needs free space of roughly the
size of the folders in SharePoint / OneDrive; the card shows the free space of the chosen folder.

The app applies these at every start, so `/admin` never asks you to create a password, and resetting
it is: edit the file, `docker compose up -d`. Without `ADMIN_PASSWORD` the first visit to `/admin`
asks for a password; if it is forgotten, `docker compose exec app python -m app.reset_admin` clears
it (and logs everyone out). User account passwords are reset from *Admin → Users & security*.

What the stack does:

- `app` runs uvicorn with `--proxy-headers` so it knows requests arrived over HTTPS (Secure cookies,
  HSTS) and sees the real client IP (login rate limiting). Data lives in the `ledger-data` volume.
- `caddy` terminates TLS, renews certificates automatically, forwards to the app, and limits request
  bodies to 120 MB (uploads).

### Microsoft 365 (SharePoint / OneDrive) app registration

In Microsoft Entra (entra.microsoft.com → App registrations) register *Roofing Ledger* with a **Web**
redirect URI of `https://<your domain>/admin/oauth/ms-callback`, create a client secret, and add the
delegated Microsoft Graph permissions `Files.Read.All`, `Files.ReadWrite.All`, `Sites.Read.All`,
`User.Read`, `offline_access`. Enter the client id, secret and tenant under *Admin → Microsoft setup*
(the page shows the exact redirect URI to paste). Then add each SharePoint folder under *Admin →
Sources* with its "Share → Copy link" URL and sign in with an account that can see it.

**Guest accounts.** If the person signing in is outside the organisation that owns the files (for
example the folders were shared to them with "Specific people → Can view"), put that organisation's
domain (e.g. `vistaroofing.ie`) in the source's *Organisation (tenant)* field. The organisation's
Microsoft 365 admin may have to allow external sharing (SharePoint admin centre → Policies →
Sharing → "New and existing guests", and the same on the HRAccounts / VistaFiles sites) and, if the
tenant requires admin consent for third-party apps, approve the app once. For uploads the share must
be "Can edit".

### Google OAuth redirect URI

In Google Cloud Console, if you created a **Web application** client, add
`https://<your domain>/admin/oauth/callback` as an authorised redirect URI. A **Desktop** client
needs no change. In both cases make sure every Google account you will connect is listed as a test
user (or publish the consent screen).

## 3. Install without Docker (systemd)

```bash
sudo apt install python3.12 python3.12-venv
sudo useradd --system --create-home ledger
sudo -u ledger bash -c '
  cd ~ && git clone <repo> app && cd app &&
  python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt'
sudo tee /etc/systemd/system/roofing-ledger.service <<'EOF'
[Unit]
Description=Roofing Ledger
After=network.target
[Service]
User=ledger
WorkingDirectory=/home/ledger/app
EnvironmentFile=/home/ledger/app/.env.production
Environment=DATA_DIR=/home/ledger/data
ExecStart=/home/ledger/app/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=always
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now roofing-ledger
```

Put Caddy or nginx in front, forwarding to `127.0.0.1:8000` and setting `X-Forwarded-Proto`.
`deploy/Caddyfile` works unchanged with `reverse_proxy 127.0.0.1:8000`.

## 4. Environment variables

| Variable | Required in production | Meaning |
|---|---|---|
| `PUBLIC_BASE_URL` | yes | `https://ledger.example.com`. Pins the OAuth redirect, marks cookies Secure, enables production warnings |
| `APP_SECRET_KEY` | yes | Encryption key for secrets at rest. Losing it means re-entering the API key and reconnecting Drives |
| `DATA_DIR` | no (Docker sets `/data`) | Database, preview cache, logs |
| `LOCAL_ROOTS` | recommended | Restricts local-folder sources to these directories (`;`-separated) |
| `COOKIE_SECURE` | no | `auto` (default), `true`, `false` |
| `APP_DEBUG` | **must be unset/0** | Puts exception details into API responses when on |
| `PORT`, `HOST` | no | Listening address |

## 5. Operations

**Logs**: `docker compose logs app` and `<DATA_DIR>/logs/ledger.log` (rotating, 5 × 5 MB).

**Health**: `GET /healthz` returns `{"ok": true, ...}` without authentication; Docker's HEALTHCHECK uses it.

**Backups**: *Admin → Data → Download backup* gives a consistent copy of `ledger.db`. For automated
backups copy the volume nightly:

```bash
docker run --rm -v roofing-ledger_ledger-data:/data -v $PWD/backups:/b alpine \
  sh -c 'cp /data/ledger.db /b/ledger-$(date +%F).db'
```

Keep `APP_SECRET_KEY` with the backups (in a password manager, not next to the files): without it the
encrypted secrets in the backup are unreadable, though all document data is.

**Restore**: stop the app, replace `ledger.db` in the data volume, start the app.

**Upgrades**: `git pull && docker compose up -d --build`. Database migrations run automatically at
start (`db.init_db`).

**Updating dependencies**: `pip-audit` and `bandit -r app` are part of the release checklist in
`docs/SECURITY.md`; `requirements.txt` is pinned to audited versions.

## 6. Sizing and cost

- Storage: the database grows ~5 KB per document; the preview cache holds one copy of each viewed
  file (set a cron job to clear `<DATA_DIR>/previews` if disk is tight; it is rebuilt on demand).
- Claude usage: roughly 2,000–6,000 input tokens per page. A few hundred documents cost a few euro
  with the default model; the watcher only reads files that are new or changed.
- Google Drive API: the watcher makes one `changes.list` call per Drive per interval (default every
  2 minutes = 720 calls/day/Drive, far below Google's free quota).

## 7. Going live checklist

1. `PUBLIC_BASE_URL`, `LEDGER_DOMAIN`, `APP_SECRET_KEY` set; `APP_DEBUG` unset.
2. `https://<domain>/healthz` returns ok; `http://` redirects to `https://`.
3. Admin password created; dashboard (owner) password set under Security.
4. Claude key saved and tested; Google client uploaded; each Drive connected **with the upload
   permission** (source card says "Uploads from the dashboard enabled").
5. Demo data removed (*Admin → Data*).
6. First full sync complete; spot-check a few documents against their previews.
7. Backup downloaded once and the secret key stored in the password manager.
