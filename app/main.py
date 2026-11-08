"""FastAPI application: owner dashboard, admin area, JSON API, Google OAuth, uploads.

Route groups
------------
Pages          ``/`` (owner dashboard), ``/admin`` (admin area) — static HTML, data loaded by JS.
Auth           ``/api/auth/*`` dashboard login (email + password of a user account, or the built-in
               admin password with an empty email), own password change; ``/api/admin/setup|login|logout``.
Owner API      ``/api/config``, ``/api/summary``, ``/api/documents*``, ``/api/companies``, ``/api/ask``,
               ``/api/sync*``, ``/api/upload*``, ``/api/version`` — any logged-in role.
Admin API      ``/api/admin/*`` — admin role only; ``/api/admin/users*`` manages the user accounts.
Google OAuth   ``/admin/sources/{id}/connect`` → Google → ``/admin/oauth/callback``.
Microsoft      same Connect route → Microsoft sign-in → ``/admin/oauth/ms-callback`` (microsoft.py).
Ops            ``/healthz``.

Security model (see docs/SECURITY.md)
-------------------------------------
* Every data endpoint requires a session cookie (``rl_session``: HttpOnly, SameSite=Lax, Secure on
  HTTPS). Tokens are HMAC-signed with role, user id, token version and expiry (settings.py); user
  sessions are re-checked against the ``users`` table on every request.
* State-changing requests must carry an Origin/Referer matching the request host (CSRF defence in
  depth on top of SameSite).
* Login endpoints are rate limited per client IP (10 attempts / 15 min).
* Security headers (CSP, nosniff, frame-ancestors, HSTS on HTTPS) on every response.
* Error responses hide exception details unless ``APP_DEBUG=1``.

Debugging tips
--------------
* 401 = not logged in (or cookie not sent: on an https PUBLIC_BASE_URL the cookie is Secure and
  will not be sent over plain http). 403 = logged in as a plain user on an admin endpoint, or Origin mismatch.
* ``/healthz`` reports database + scheduler state without authentication.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import tempfile
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from . import analytics, ask, config, db, demo, extract, llm, logsetup, microsoft, previews, settings, sources, sync

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
SESSION_COOKIE = "rl_session"
MAX_UPLOAD_FILES = 20


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    logsetup.setup()
    db.init_db()
    warning = settings.apply_env_admin()  # ADMIN_PASSWORD / ADMIN_EMAIL from .env, if given
    if warning:
        log.error("%s", warning)
    elif settings.admin_from_env():
        log.info("admin password taken from ADMIN_PASSWORD (sign-in name: %s)", config.ADMIN_EMAIL or "<empty email>")
    if config.IS_PRODUCTION:
        if not settings.public()["secrets_encrypted"]:
            log.warning("APP_SECRET_KEY is not set: secrets are stored unencrypted. Set it before going live.")
    else:
        log.info("PUBLIC_BASE_URL not set: running in local mode (cookies not marked Secure).")
    sync.start_scheduler()
    llm.refresh_in_background()  # populate the "document reader" status without delaying start-up
    yield


app = FastAPI(title="Roofing Ledger", version="3.0", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
db.init_db()


# ====================================================================== middleware: headers + CSRF


def _request_is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"


class SecurityMiddleware(BaseHTTPMiddleware):
    """Adds security headers and rejects cross-site state-changing requests."""

    async def dispatch(self, request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin:
                host = urlparse(origin).netloc.lower()
                allowed = {request.headers.get("host", "").lower()}
                if config.PUBLIC_BASE_URL:
                    allowed.add(urlparse(config.PUBLIC_BASE_URL).netloc.lower())
                if host not in allowed:
                    return JSONResponse({"detail": "cross-site request blocked"}, status_code=403)
        response = await call_next(request)
        h = response.headers
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "SAMEORIGIN"
        h["Referrer-Policy"] = "same-origin"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        h["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "frame-src 'self'; frame-ancestors 'self'; form-action 'self'; base-uri 'self'; object-src 'none'; connect-src 'self'"
        )
        if _request_is_https(request):
            h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if request.url.path.startswith("/api/"):
            h["Cache-Control"] = "no-store"
        elif request.url.path.startswith("/static/") or request.url.path in ("/", "/admin"):
            # Always revalidate pages and assets: after an upgrade a browser must never run an old
            # app.js against a new admin.html (that combination throws and leaves a blank page).
            # Assets are also linked with ?v=<content hash>, so a changed file gets a new URL.
            h["Cache-Control"] = "no-cache"
        return response


app.add_middleware(SecurityMiddleware)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    """Log the traceback, return a generic message (details only with APP_DEBUG=1)."""
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    detail = f"{type(exc).__name__}: {exc}" if config.DEBUG else "Internal error. Check the server log."
    return JSONResponse({"detail": detail}, status_code=500)


# ====================================================================== auth helpers

_attempts: dict[str, deque] = defaultdict(deque)
RATE_LIMIT, RATE_WINDOW = 10, 15 * 60


def _rate_limited(request: Request) -> bool:
    """True if this client IP has made too many login attempts in the window."""
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    q = _attempts[ip]
    while q and q[0] < now - RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return True
    q.append(now)
    return False


def _cookie_secure(request: Request) -> bool:
    if config.COOKIE_SECURE in ("true", "1", "yes"):
        return True
    if config.COOKIE_SECURE in ("false", "0", "no"):
        return False
    return _request_is_https(request) or config.PUBLIC_BASE_URL.startswith("https://")


def _set_session(response: Response, request: Request, ident: settings.Identity) -> None:
    response.set_cookie(
        SESSION_COOKIE, settings.make_session_token(ident), httponly=True, samesite="lax",
        secure=_cookie_secure(request), max_age=settings.SESSION_TTL_SECONDS, path="/",
    )


def current_identity(request: Request) -> settings.Identity | None:
    """Who is signed in (None when the cookie is missing, expired, forged or the account was disabled)."""
    cached = getattr(request.state, "identity", None)
    if cached is None:
        cached = settings.resolve_session(request.cookies.get(SESSION_COOKIE))
        request.state.identity = cached
    return cached


def current_role(request: Request) -> str | None:
    ident = current_identity(request)
    return ident.role if ident else None


def require_login(request: Request) -> str:
    role = current_role(request)
    if role is None:
        raise HTTPException(401, "login required")
    return role


def require_admin(request: Request) -> str:
    role = current_role(request)
    if role is None:
        raise HTTPException(401, "admin login required")
    if role != "admin":
        raise HTTPException(403, "admin rights required")
    return role


def _require_api_key() -> None:
    """Documents can only be read when a model backend is usable (Ollama configured, or a Claude key saved)."""
    if not settings.llm_ready():
        raise HTTPException(400, "No Claude API key configured. Add one in Admin → Claude, or set LLM_PROVIDER=ollama.")


def _base_url(request: Request) -> str:
    """Public origin for redirects: pinned by PUBLIC_BASE_URL in production, else derived from the request."""
    return config.PUBLIC_BASE_URL or str(request.base_url).rstrip("/")


# ====================================================================== pages & health


def _asset_version() -> str:
    """Short hash of the static JS/CSS; substituted into the pages' asset links as ``?v=…`` so browsers
    fetch fresh copies after every upgrade (and keep caching when nothing changed)."""
    digest = hashlib.sha256()
    for name in ("styles.css", "app.js", "admin.js"):
        digest.update((STATIC / name).read_bytes())
    return digest.hexdigest()[:12]


ASSET_VERSION = _asset_version()


def _page(name: str) -> HTMLResponse:
    html = (STATIC / name).read_text(encoding="utf-8").replace("{{v}}", ASSET_VERSION)
    return HTMLResponse(html)


@app.get("/")
def index():
    return _page("index.html")


@app.get("/admin")
def admin_page():
    return _page("admin.html")


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """Unauthenticated liveness/readiness probe (no sensitive data)."""
    try:
        with db.connect() as conn:
            conn.execute("SELECT 1").fetchone()
        db_ok = True
    except Exception:
        db_ok = False
    return {"ok": db_ok, "version": app.version, "sync_running": sync.status()["running"]}


# ====================================================================== dashboard login (everyone)


class Password(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class Login(BaseModel):
    """Email + password of a user account or of the built-in admin (empty email only when no ADMIN_EMAIL is set)."""

    email: str = Field("", max_length=200)
    password: str = Field(min_length=1, max_length=200)


def _login(body: Login, request: Request) -> settings.Identity:
    """Shared by both login boxes: rate limit, slow down, check, uniform 401."""
    if _rate_limited(request):
        raise HTTPException(429, "too many attempts, try again in 15 minutes")
    time.sleep(0.3)  # blunt brute-force damper on top of the rate limit
    ident = settings.authenticate(body.email, body.password)
    if ident is None:
        raise HTTPException(401, "wrong email or password")
    return ident


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    ident = current_identity(request)
    return {"role": ident.role if ident else None, "user": ident.public() if ident else None,
            "needs_setup": not settings.get("admin_password_hash"), "email_required": bool(config.ADMIN_EMAIL)}


@app.post("/api/auth/login")
def auth_login(body: Login, request: Request, response: Response) -> dict[str, Any]:
    """Dashboard login: any active account, or the built-in admin (see settings.authenticate)."""
    ident = _login(body, request)
    _set_session(response, request, ident)
    return {"ok": True, "role": ident.role, "user": ident.public()}


@app.post("/api/auth/logout")
def auth_logout(response: Response) -> dict[str, Any]:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


class PasswordChange(BaseModel):
    current: str = Field(max_length=200)
    new: str = Field(max_length=200)


@app.post("/api/auth/password")
def auth_change_own_password(body: PasswordChange, request: Request, response: Response) -> dict[str, Any]:
    """A signed-in user changes their own password; every other browser of theirs is logged out."""
    ident = current_identity(request)
    if ident is None:
        raise HTTPException(401, "login required")
    if ident.is_builtin_admin:
        raise HTTPException(400, "the admin password is changed under Admin → Security")
    user = db.get_user(ident.uid, with_hash=True)
    if user is None or not settings.verify_password(body.current, user.get("password_hash")):
        raise HTTPException(400, "current password is wrong")
    if len(body.new) < settings.MIN_PASSWORD_LENGTH:
        raise HTTPException(400, f"new password must be at least {settings.MIN_PASSWORD_LENGTH} characters")
    db.update_user(ident.uid, password_hash=settings.hash_password(body.new))
    db.bump_user_token(ident.uid)  # logs out the other browsers...
    _set_session(response, request, settings.identity_for_user(db.get_user(ident.uid, with_hash=True)))  # ...but keeps this one
    return {"ok": True}


# ====================================================================== owner API


@app.get("/api/config", dependencies=[Depends(require_login)])
def get_config(request: Request) -> dict[str, Any]:
    srcs = db.list_sources(include_secrets=True)
    for s in srcs:
        s["can_upload"] = s["connected"] and sources.can_upload(s)
        s.pop("token_json", None)
    return {
        "company": settings.get("company_name"), "currency": settings.get("base_currency"), "model": settings.get("extract_model"),
        "has_api_key": settings.has_api_key(), "has_google_client": settings.google_client_config() is not None,
        **settings.llm_public(), "llm_status": llm.status(),
        "sources": srcs, "counts": db.status_counts(), "demo_loaded": demo.count() > 0,
        "watch_interval_seconds": settings.get_int("watch_interval_seconds", 120),
        "auto_sync_minutes": settings.get_int("auto_sync_minutes", 0), "role": current_role(request), "version": db.ledger_version(),
        "user": current_identity(request).public(),  # type: ignore[union-attr]  (require_login guarantees a session)
    }


@app.get("/api/version", dependencies=[Depends(require_login)])
def version() -> dict[str, Any]:
    """Polled by the dashboard: when this changes, new documents have arrived. Carries the reader status for the header badge."""
    return {"version": db.ledger_version(), "sync_running": sync.status()["running"], "llm": llm.status()}


@app.get("/api/meta", dependencies=[Depends(require_login)])
def meta() -> dict[str, Any]:
    return {
        "doc_types": extract.DOC_TYPES, "directions": extract.DIRECTIONS, "payment_statuses": extract.PAYMENT_STATUSES,
        "expense_categories": extract.EXPENSE_CATEGORIES, "income_categories": extract.INCOME_CATEGORIES,
        "treatments": extract.TREATMENT_LABELS, "models": [{"id": m, "label": l} for m, l in extract.MODELS],
    }


@app.get("/api/summary", dependencies=[Depends(require_login)])
def summary(date_from: str | None = Query(None, alias="from"), date_to: str | None = Query(None, alias="to")) -> dict[str, Any]:
    return analytics.compute(db.all_done_documents(), date_from or None, date_to or None)


@app.get("/api/companies", dependencies=[Depends(require_login)])
def companies() -> list[dict[str, Any]]:
    return analytics.companies(db.all_done_documents())


@app.get("/api/documents", dependencies=[Depends(require_login)])
def documents(
    date_from: str | None = Query(None, alias="from"), date_to: str | None = Query(None, alias="to"),
    doc_type: str | None = None, treatment: str | None = None, status: str | None = None,
    payment_status: str | None = None, counterparty: str | None = None, project: str | None = None,
    source_ref: int | None = None, q: str | None = Query(None, max_length=200), limit: int = Query(2000, ge=1, le=10000),
) -> list[dict[str, Any]]:
    return db.list_documents({
        "date_from": date_from, "date_to": date_to, "doc_type": doc_type, "treatment": treatment, "status": status,
        "payment_status": payment_status, "counterparty": counterparty, "project": project, "source_ref": source_ref,
        "q": q, "limit": limit,
    })


@app.get("/api/documents/{doc_id}", dependencies=[Depends(require_login)])
def document(doc_id: int) -> dict[str, Any]:
    row = db.get_document(doc_id)
    if row is None:
        raise HTTPException(404, "document not found")
    return row


@app.get("/api/documents/{doc_id}/preview", dependencies=[Depends(require_login)])
def document_preview(doc_id: int, embed: bool = False):
    """The actual file (PDF / image / text). ``embed=1`` wraps images in a page that fits the frame width."""
    row = db.get_document(doc_id)
    if row is None:
        raise HTTPException(404, "document not found")
    try:
        data, mime = previews.get(row)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        log.warning("preview of document %s failed: %s", doc_id, exc)
        raise HTTPException(502, "Could not fetch the file from its source." + (f" ({exc})" if config.DEBUG else ""))
    if embed and mime.startswith("image/"):
        return HTMLResponse(
            '<!doctype html><html><head><meta name="viewport" content="width=device-width"></head>'
            '<body style="margin:0;padding:12px;background:#ecebe6;display:flex;justify-content:center;align-items:flex-start">'
            f'<img src="/api/documents/{doc_id}/preview" alt="" style="max-width:100%;height:auto;display:block;box-shadow:0 2px 14px rgba(0,0,0,.18)"></body></html>'
        )
    if mime == "text/plain":
        mime = "text/plain; charset=utf-8"
    safe = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in (row["name"] or "document"))[:120]
    return Response(content=data, media_type=mime, headers={"Content-Disposition": f'inline; filename="{safe}"'})


class DocumentPatch(BaseModel):
    doc_type: str | None = Field(None, max_length=40)
    direction: str | None = Field(None, max_length=20)
    counterparty: str | None = Field(None, max_length=200)
    counterparty_role: str | None = Field(None, max_length=20)
    document_number: str | None = Field(None, max_length=100)
    issue_date: str | None = Field(None, max_length=10)
    due_date: str | None = Field(None, max_length=10)
    currency: str | None = Field(None, max_length=3)
    subtotal: float | None = None
    vat_amount: float | None = None
    total: float | None = None
    amount_paid: float | None = None
    payment_status: str | None = Field(None, max_length=20)
    category: str | None = Field(None, max_length=40)
    project: str | None = Field(None, max_length=200)
    summary: str | None = Field(None, max_length=1000)


@app.patch("/api/documents/{doc_id}", dependencies=[Depends(require_login)])
def patch_document(doc_id: int, patch: DocumentPatch) -> dict[str, Any]:
    """Hand correction: stored with overridden=1 so syncs never overwrite it."""
    row = db.get_document(doc_id)
    if row is None:
        raise HTTPException(404, "document not found")
    changes = patch.model_dump(exclude_unset=True)
    if "doc_type" in changes and changes["doc_type"] not in extract.DOC_TYPES:
        raise HTTPException(400, "unknown document type")
    if "direction" in changes and changes["direction"] not in extract.DIRECTIONS:
        raise HTTPException(400, "unknown direction")
    if "payment_status" in changes and changes["payment_status"] not in extract.PAYMENT_STATUSES:
        raise HTTPException(400, "unknown payment status")
    derived = extract.derive({**row, **changes})
    db.update_document(doc_id, changes, {k: derived[k] for k in ("treatment", "sign", "net_amount")})
    return db.get_document(doc_id) or {}


@app.post("/api/documents/{doc_id}/reprocess", dependencies=[Depends(require_login)])
def reprocess_document(doc_id: int) -> dict[str, Any]:
    _require_api_key()
    try:
        return sync.reprocess(doc_id)
    except KeyError:
        raise HTTPException(404, "document not found")
    except Exception as exc:
        log.warning("reprocess of %s failed: %s", doc_id, exc)
        raise HTTPException(500, f"Could not re-read the document." + (f" ({exc})" if config.DEBUG else ""))


@app.post("/api/sync", dependencies=[Depends(require_login)])
def start_sync(force: bool = False) -> dict[str, Any]:
    _require_api_key()
    return {"started": sync.start(force=force, trigger="manual"), "status": sync.status()}


@app.get("/api/sync/status", dependencies=[Depends(require_login)])
def sync_status() -> dict[str, Any]:
    return sync.status()


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    date_from: str | None = Field(None, max_length=10)
    date_to: str | None = Field(None, max_length=10)


@app.post("/api/ask", dependencies=[Depends(require_login)])
def ask_question(q: Question) -> dict[str, Any]:
    _require_api_key()
    docs = db.all_done_documents()
    summ = analytics.compute(docs, q.date_from or None, q.date_to or None)
    in_period = [d for d in docs if analytics._eff_date(d) and (not q.date_from or analytics._eff_date(d) >= q.date_from)
                 and (not q.date_to or analytics._eff_date(d) <= q.date_to)]
    try:
        return {"answer": ask.answer(q.question.strip(), summ, in_period)}
    except Exception as exc:
        log.warning("ask failed: %s", exc)
        raise HTTPException(500, "The question could not be answered." + (f" ({exc})" if config.DEBUG else ""))


# ---------------------------------------------------------------------- uploads (app -> Drive / folder)


@app.get("/api/upload/targets", dependencies=[Depends(require_login)])
def upload_targets() -> list[dict[str, Any]]:
    """Sources that can receive uploads right now."""
    out = []
    for s in db.list_sources(include_secrets=True):
        if not s["enabled"] or not s["connected"]:
            continue
        if s["kind"] == "m365":
            folder = microsoft.location_of(s).get("label") or "OneDrive / Ledger Uploads"
        else:
            folder = s.get("drive_folder_id") or s.get("local_folder") or "Ledger Uploads"
        out.append({"id": s["id"], "name": s["name"], "kind": s["kind"], "can_upload": sources.can_upload(s), "folder": folder,
                    "reason": None if sources.can_upload(s) else sources.upload_block_reason(s)})
    return out


@app.post("/api/upload", dependencies=[Depends(require_login)])
async def upload(source_id: int = Form(...), files: list[UploadFile] = File(...)) -> dict[str, Any]:
    """Upload invoices/receipts: they are stored in the chosen Drive or folder and read immediately."""
    src = db.get_source(source_id)
    if src is None or not src["enabled"]:
        raise HTTPException(404, "source not found or disabled")
    if not (src["kind"] == "local" or src.get("token_json")):
        raise HTTPException(400, "this source is not connected")
    if not sources.can_upload(src):
        raise HTTPException(400, sources.upload_block_reason(src))
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(400, f"upload at most {MAX_UPLOAD_FILES} files at a time")
    max_bytes = settings.get_float("max_file_mb", 25) * 1024 * 1024
    payload: list[tuple[str, bytes, str]] = []
    rejected: list[dict[str, Any]] = []
    for f in files:
        name = Path(f.filename or "upload").name
        mime = f.content_type if f.content_type in sources.UPLOADABLE_MIMES else sources.guess_mime(name)
        if mime not in sources.UPLOADABLE_MIMES:
            rejected.append({"name": name, "ok": False, "error": "unsupported file type"})
            continue
        data = await f.read()
        if len(data) > max_bytes:
            rejected.append({"name": name, "ok": False, "error": f"larger than {settings.get('max_file_mb')} MB"})
            continue
        if not data:
            rejected.append({"name": name, "ok": False, "error": "empty file"})
            continue
        payload.append((name, data, mime))
    results = sync.process_uploaded(src, payload) if payload else []
    return {"results": results + rejected, "version": db.ledger_version()}


# ====================================================================== admin: auth


@app.get("/api/admin/me")
def admin_me(request: Request) -> dict[str, Any]:
    ident = current_identity(request)
    return {"authenticated": bool(ident and ident.role == "admin"), "role": ident.role if ident else None,
            "user": ident.public() if ident else None, "needs_setup": not settings.get("admin_password_hash"),
            "email_required": bool(config.ADMIN_EMAIL)}


@app.post("/api/admin/setup")
def admin_setup(body: Password, request: Request, response: Response) -> dict[str, Any]:
    """First run only: create the admin password."""
    if settings.get("admin_password_hash"):
        raise HTTPException(400, "an admin password is already set")
    if len(body.password) < settings.MIN_PASSWORD_LENGTH:
        raise HTTPException(400, f"password must be at least {settings.MIN_PASSWORD_LENGTH} characters")
    settings.set_password(body.password, "admin")
    _set_session(response, request, settings.Identity.builtin_admin())
    return {"ok": True}


@app.post("/api/admin/login")
def admin_login(body: Login, request: Request, response: Response) -> dict[str, Any]:
    """Admin login: the admin password (empty email) or a user account with the admin role."""
    ident = _login(body, request)
    if ident.role != "admin":
        raise HTTPException(403, "this account can use the dashboard but not the admin area")
    _set_session(response, request, ident)
    return {"ok": True, "user": ident.public()}


@app.post("/api/admin/logout")
def admin_logout(response: Response) -> dict[str, Any]:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.post("/api/admin/password", dependencies=[Depends(require_admin)])
def admin_change_password(body: PasswordChange, request: Request, response: Response) -> dict[str, Any]:
    """Change the built-in admin password. Rotating the signing secret logs every browser out."""
    if settings.admin_from_env():
        raise HTTPException(400, "the admin password is set by ADMIN_PASSWORD in the server environment; change it there and restart")
    if not settings.check_password(body.current, "admin"):
        raise HTTPException(400, "current password is wrong")
    if len(body.new) < settings.MIN_PASSWORD_LENGTH:
        raise HTTPException(400, f"new password must be at least {settings.MIN_PASSWORD_LENGTH} characters")
    settings.set_password(body.new, "admin")
    settings.rotate_session_secret()  # log every other browser out
    _set_session(response, request, current_identity(request))  # type: ignore[arg-type]
    return {"ok": True}


# ====================================================================== admin: user accounts

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class UserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    name: str = Field("", max_length=100)
    role: str = Field("user", max_length=10)
    password: str = Field(min_length=1, max_length=200)


class UserPatch(BaseModel):
    name: str | None = Field(None, max_length=100)
    role: str | None = Field(None, max_length=10)
    active: bool | None = None


def _check_user_fields(email: str | None = None, role: str | None = None, password: str | None = None) -> None:
    if email is not None and not _EMAIL_RE.match(email.strip()):
        raise HTTPException(400, "enter a valid email address")
    if role is not None and role not in db.USER_ROLES:
        raise HTTPException(400, "role must be 'user' or 'admin'")
    if password is not None and len(password) < settings.MIN_PASSWORD_LENGTH:
        raise HTTPException(400, f"password must be at least {settings.MIN_PASSWORD_LENGTH} characters")


def _get_user_or_404(user_id: int) -> dict[str, Any]:
    user = db.get_user(user_id)
    if user is None:
        raise HTTPException(404, "user not found")
    return user


@app.get("/api/admin/users", dependencies=[Depends(require_admin)])
def admin_list_users() -> dict[str, Any]:
    return {"users": db.list_users()}


@app.post("/api/admin/users", dependencies=[Depends(require_admin)])
def admin_create_user(body: UserCreate) -> dict[str, Any]:
    """Create an account. The admin tells the person their email and the initial password; they can
    change the password themselves from the dashboard afterwards."""
    _check_user_fields(email=body.email, role=body.role, password=body.password)
    try:
        uid = db.create_user(body.email, body.name or body.email.split("@")[0], body.role, settings.hash_password(body.password))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    log.info("user account created: %s (%s)", body.email.strip().lower(), body.role)
    return {"ok": True, "user": db.get_user(uid)}


@app.patch("/api/admin/users/{user_id}", dependencies=[Depends(require_admin)])
def admin_update_user(user_id: int, body: UserPatch, request: Request) -> dict[str, Any]:
    """Rename, change role, enable/disable. An admin cannot lock themselves out."""
    _get_user_or_404(user_id)
    _check_user_fields(role=body.role)
    me = current_identity(request)
    fields: dict[str, Any] = {}
    if body.name is not None:
        fields["name"] = body.name.strip()
    if body.role is not None:
        fields["role"] = body.role
    if body.active is not None:
        fields["active"] = body.active
    if me and me.uid == user_id and (fields.get("role") == "user" or fields.get("active") is False):
        raise HTTPException(400, "you cannot remove your own admin access; ask another admin")
    db.update_user(user_id, **fields)
    if fields.get("active") is False or fields.get("role"):
        db.bump_user_token(user_id)  # take effect in every browser immediately
    return {"ok": True, "user": db.get_user(user_id)}


@app.post("/api/admin/users/{user_id}/password", dependencies=[Depends(require_admin)])
def admin_reset_user_password(user_id: int, body: Password) -> dict[str, Any]:
    """Set a new password for someone who forgot theirs; logs them out everywhere."""
    _get_user_or_404(user_id)
    _check_user_fields(password=body.password)
    db.update_user(user_id, password_hash=settings.hash_password(body.password))
    db.bump_user_token(user_id)
    return {"ok": True}


@app.delete("/api/admin/users/{user_id}", dependencies=[Depends(require_admin)])
def admin_delete_user(user_id: int, request: Request) -> dict[str, Any]:
    _get_user_or_404(user_id)
    me = current_identity(request)
    if me and me.uid == user_id:
        raise HTTPException(400, "you cannot delete the account you are signed in with")
    db.delete_user(user_id)
    return {"ok": True}


# ====================================================================== admin: overview & settings


@app.get("/api/admin/overview", dependencies=[Depends(require_admin)])
def admin_overview() -> dict[str, Any]:
    srcs = db.list_sources(include_secrets=True)
    for s in srcs:
        s["can_upload"] = s["connected"] and sources.can_upload(s)
        s.pop("token_json", None)
    pub = settings.public()
    pub["watch_interval_seconds"] = settings.get_int("watch_interval_seconds", 120)
    return {"settings": pub, "sources": srcs, "sync": sync.status(), "counts": db.status_counts(),
            "demo_loaded": demo.count() > 0, "models": [{"id": m, "label": l} for m, l in extract.MODELS],
            "production": config.IS_PRODUCTION, "users": db.list_users(), "previews": previews.stats()}


@app.post("/api/admin/previews/clear", dependencies=[Depends(require_admin)])
def admin_clear_previews() -> dict[str, Any]:
    """Delete every stored copy of a document file (they are re-fetched from the source when viewed)."""
    return {"ok": True, "removed": previews.clear(), "previews": previews.stats()}


class SettingsPatch(BaseModel):
    company_name: str | None = Field(None, max_length=200)
    company_address: str | None = Field(None, max_length=500)
    company_vat: str | None = Field(None, max_length=40)
    base_currency: str | None = Field(None, max_length=3)
    country: str | None = Field(None, max_length=60)
    extract_model: str | None = Field(None, max_length=60)
    extract_effort: str | None = Field(None, max_length=10)
    ask_model: str | None = Field(None, max_length=60)
    concurrency: int | None = Field(None, ge=1, le=10)
    max_file_mb: float | None = Field(None, ge=1, le=100)
    auto_sync_minutes: int | None = Field(None, ge=0, le=10080)
    watch_interval_seconds: int | None = Field(None, ge=0, le=86400)
    anthropic_api_key: str | None = Field(None, max_length=300)
    cache_previews: bool | None = None  # keep viewable copies of files on the server (default off)
    llm_provider: str | None = Field(None, max_length=10)  # ollama | claude (default from .env)
    llm_fallback: bool | None = None  # Ollama → Claude for photos / scans / while Ollama is unreachable


@app.put("/api/admin/settings", dependencies=[Depends(require_admin)])
def admin_update_settings(body: SettingsPatch) -> dict[str, Any]:
    changes = body.model_dump(exclude_unset=True)
    if "cache_previews" in changes:
        changes["cache_previews"] = "1" if changes["cache_previews"] else "0"
        if changes["cache_previews"] == "0":
            previews.clear()  # turning it off also removes what was stored so far
    if "anthropic_api_key" in changes:
        key = (changes["anthropic_api_key"] or "").strip()
        if key and not settings.looks_like_key(key):
            raise HTTPException(400, "that does not look like a Claude API key")
        changes["anthropic_api_key"] = key
    if "llm_provider" in changes:
        changes["llm_provider"] = (changes["llm_provider"] or "").strip().lower()
        if changes["llm_provider"] not in ("ollama", "claude"):
            raise HTTPException(400, "the document reader must be 'ollama' or 'claude'")
    if "llm_fallback" in changes:
        changes["llm_fallback"] = "1" if changes["llm_fallback"] else "0"
    if "extract_effort" in changes and changes["extract_effort"] not in ("low", "medium", "high"):
        raise HTTPException(400, "effort must be low, medium or high")
    if "extract_model" in changes and changes["extract_model"] not in {m for m, _ in extract.MODELS}:
        raise HTTPException(400, "unknown model")
    if "ask_model" in changes and changes["ask_model"] and changes["ask_model"] not in {m for m, _ in extract.MODELS}:
        raise HTTPException(400, "unknown model")
    if "base_currency" in changes and changes["base_currency"]:
        changes["base_currency"] = changes["base_currency"].strip().upper()[:3]
    if "watch_interval_seconds" in changes and 0 < changes["watch_interval_seconds"] < 30:
        changes["watch_interval_seconds"] = 30  # keep Drive API usage reasonable
    settings.set_many(changes)
    if {"llm_provider", "llm_fallback", "anthropic_api_key"} & changes.keys():
        llm.refresh_in_background()  # the badge should reflect the new reader within seconds
    pub = settings.public()
    pub["watch_interval_seconds"] = settings.get_int("watch_interval_seconds", 120)
    return pub


class KeyTest(BaseModel):
    key: str | None = Field(None, max_length=300)
    model: str | None = Field(None, max_length=60)


@app.post("/api/admin/test-key", dependencies=[Depends(require_admin)])
def admin_test_key(body: KeyTest) -> dict[str, Any]:
    key = (body.key or "").strip() or settings.get("anthropic_api_key").strip()
    if not settings.looks_like_key(key):
        return {"ok": False, "message": "No key to test."}
    return extract.test_api_key(key, body.model or None)


@app.post("/api/admin/llm-check", dependencies=[Depends(require_admin)])
def admin_llm_check() -> dict[str, Any]:
    """Live check of the configured document reader (Ollama: version, models, probe; Claude: key test)."""
    return llm.health()


class GoogleClient(BaseModel):
    json_text: str = Field(max_length=50_000)


@app.post("/api/admin/google-client", dependencies=[Depends(require_admin)])
def admin_set_google_client(body: GoogleClient) -> dict[str, Any]:
    try:
        cfg = json.loads(body.json_text)
    except ValueError:
        raise HTTPException(400, "that is not valid JSON")
    inner = cfg.get("installed") or cfg.get("web") if isinstance(cfg, dict) else None
    if not inner or not inner.get("client_id") or not inner.get("client_secret"):
        raise HTTPException(400, "expected a Google OAuth client JSON with 'installed' or 'web' containing client_id and client_secret")
    settings.set_value("google_client_config", json.dumps(cfg))
    return settings.public()


@app.delete("/api/admin/google-client", dependencies=[Depends(require_admin)])
def admin_remove_google_client() -> dict[str, Any]:
    settings.set_value("google_client_config", "")
    return settings.public()


class MicrosoftClient(BaseModel):
    """Microsoft Entra app registration (Admin → Microsoft setup)."""
    client_id: str = Field(min_length=8, max_length=200)
    client_secret: str | None = Field(None, max_length=500)  # blank keeps the saved one; "-" clears it
    tenant: str | None = Field(None, max_length=200)  # "common", a domain (vistaroofing.ie) or a tenant id


@app.post("/api/admin/ms-client", dependencies=[Depends(require_admin)])
def admin_set_ms_client(body: MicrosoftClient) -> dict[str, Any]:
    cid = body.client_id.strip()
    if not all(ch.isalnum() or ch in "-_." for ch in cid):
        raise HTTPException(400, "that does not look like an application (client) id")
    tenant = (body.tenant or "common").strip() or "common"
    if not all(ch.isalnum() or ch in "-_." for ch in tenant):
        raise HTTPException(400, "tenant must be 'common', a domain name or a tenant id")
    current = microsoft.client_config() or {}
    secret = current.get("client_secret", "")
    if body.client_secret is not None and body.client_secret.strip():
        secret = "" if body.client_secret.strip() == "-" else body.client_secret.strip()
    settings.set_value("ms_client_config", json.dumps({"client_id": cid, "client_secret": secret, "tenant": tenant}))
    return settings.public()


@app.delete("/api/admin/ms-client", dependencies=[Depends(require_admin)])
def admin_remove_ms_client() -> dict[str, Any]:
    settings.set_value("ms_client_config", "")
    return settings.public()


# ====================================================================== admin: sources


class SourceCreate(BaseModel):
    kind: str = Field(max_length=10)
    name: str = Field(min_length=1, max_length=100)
    drive_folder_id: str | None = Field(None, max_length=300)
    local_folder: str | None = Field(None, max_length=500)
    # Microsoft 365: a SharePoint/OneDrive folder link ("shared" = everything shared with the account), tenant override, uploads on/off
    ms_link: str | None = Field(None, max_length=2000)
    ms_tenant: str | None = Field(None, max_length=200)
    ms_upload: bool = True


class SourcePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    enabled: bool | None = None
    drive_folder_id: str | None = Field(None, max_length=300)
    local_folder: str | None = Field(None, max_length=500)
    ms_link: str | None = Field(None, max_length=2000)
    ms_tenant: str | None = Field(None, max_length=200)
    ms_upload: bool | None = None


def _check_ms_link(value: str | None) -> str | None:
    v = (value or "").strip()
    if not v:
        return None
    if v.lower() == microsoft.SHARED_KEYWORD:
        return microsoft.SHARED_KEYWORD
    if v.startswith("https://") or (":" in v and "://" not in v and " " not in v):
        return v
    raise HTTPException(400, "paste an https:// link to a SharePoint or OneDrive folder, the word 'shared', or leave it blank")


def _check_tenant(value: str | None) -> str | None:
    v = (value or "").strip()
    if not v:
        return None
    if not all(ch.isalnum() or ch in "-_." for ch in v):
        raise HTTPException(400, "tenant must be a domain name (e.g. vistaroofing.ie) or a tenant id")
    return v


def _folder_id_from(value: str | None) -> str | None:
    """Accept a folder id or a Drive folder URL."""
    v = (value or "").strip()
    if not v:
        return None
    if "/folders/" in v:
        v = v.split("/folders/", 1)[1].split("?", 1)[0].split("/", 1)[0]
    if not all(ch.isalnum() or ch in "-_" for ch in v):
        raise HTTPException(400, "that does not look like a Drive folder id or link")
    return v


def _public_source(source_id: int) -> dict[str, Any]:
    src = db.get_source(source_id) or {}
    src["can_upload"] = bool(src.get("token_json") or src.get("kind") == "local") and sources.can_upload(src)
    src.pop("token_json", None)
    return src


@app.get("/api/admin/sources", dependencies=[Depends(require_admin)])
def admin_sources() -> list[dict[str, Any]]:
    srcs = db.list_sources(include_secrets=True)
    for s in srcs:
        s["can_upload"] = s["connected"] and sources.can_upload(s)
        s.pop("token_json", None)
    return srcs


@app.post("/api/admin/sources", dependencies=[Depends(require_admin)])
def admin_create_source(body: SourceCreate) -> dict[str, Any]:
    if body.kind not in ("drive", "local", "m365"):
        raise HTTPException(400, "kind must be 'drive', 'm365' or 'local'")
    name = body.name.strip()
    if body.kind == "local":
        try:
            folder = sources.check_local_folder(body.local_folder or "")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        sid = db.create_source("local", name, local_folder=str(folder))
    elif body.kind == "m365":
        if microsoft.client_config() is None:
            raise HTTPException(400, "fill in the Microsoft app registration first (Admin → Microsoft setup)")
        sid = db.create_source("m365", name, ms_link=_check_ms_link(body.ms_link), ms_tenant=_check_tenant(body.ms_tenant), ms_upload=body.ms_upload)
    else:
        if settings.google_client_config() is None:
            raise HTTPException(400, "upload the Google OAuth client JSON first (Admin → Google)")
        sid = db.create_source("drive", name, drive_folder_id=_folder_id_from(body.drive_folder_id))
    return _public_source(sid)


@app.patch("/api/admin/sources/{source_id}", dependencies=[Depends(require_admin)])
def admin_patch_source(source_id: int, body: SourcePatch) -> dict[str, Any]:
    src = db.get_source(source_id)
    if src is None:
        raise HTTPException(404, "source not found")
    changes = body.model_dump(exclude_unset=True)
    if "enabled" in changes:
        changes["enabled"] = 1 if changes["enabled"] else 0
    if "drive_folder_id" in changes:
        changes["drive_folder_id"] = _folder_id_from(changes["drive_folder_id"])
        changes["drive_page_token"] = None  # scope changed: restart the change feed
    if "local_folder" in changes and changes["local_folder"] is not None:
        try:
            changes["local_folder"] = str(sources.check_local_folder(changes["local_folder"]))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    if "ms_tenant" in changes:
        changes["ms_tenant"] = _check_tenant(changes["ms_tenant"])
    if "ms_upload" in changes:
        changes["ms_upload"] = 1 if changes["ms_upload"] else 0  # takes effect at the next Connect (scopes are granted at sign-in)
    if "ms_link" in changes:
        changes["ms_link"] = _check_ms_link(changes["ms_link"])
        changes["drive_page_token"] = None
        changes["ms_location"] = None
        if src.get("token_json") and src["kind"] == "m365":
            # Already signed in: resolve the new link right away so the next sync uses it.
            try:
                loc = microsoft.resolve_link(microsoft.Graph(src), changes["ms_link"]) if changes["ms_link"] else {"mode": "onedrive", "label": "OneDrive"}
                changes["ms_location"] = json.dumps(loc)
                changes["last_error"] = None
            except Exception as exc:
                raise HTTPException(400, f"could not open that link with the connected account: {exc}")
    db.update_source(source_id, **changes)
    return _public_source(source_id)


@app.delete("/api/admin/sources/{source_id}", dependencies=[Depends(require_admin)])
def admin_delete_source(source_id: int) -> dict[str, Any]:
    if db.get_source(source_id) is None:
        raise HTTPException(404, "source not found")
    if sync.status()["running"]:
        raise HTTPException(409, "a sync is running: press Stop (or wait for it to finish) before removing a source")
    return {"ok": True, "documents_removed": db.delete_source(source_id)}


@app.post("/api/admin/sources/{source_id}/sync", dependencies=[Depends(require_admin)])
def admin_sync_source(source_id: int, force: bool = False) -> dict[str, Any]:
    _require_api_key()
    src = db.get_source(source_id)
    if src is None:
        raise HTTPException(404, "source not found")
    if src["kind"] in sources.CLOUD_KINDS and not src.get("token_json"):
        raise HTTPException(400, f"connect this {sources.KIND_LABELS[src['kind']]} source first")
    return {"started": sync.start([source_id], force=force, trigger="manual"), "status": sync.status()}


@app.post("/api/admin/sync-all", dependencies=[Depends(require_admin)])
def admin_sync_all(force: bool = False) -> dict[str, Any]:
    _require_api_key()
    return {"started": sync.start(force=force, trigger="manual"), "status": sync.status()}


@app.post("/api/admin/sync-stop", dependencies=[Depends(require_admin)])
def admin_sync_stop() -> dict[str, Any]:
    """Stop the running sync after the files being read now. Nothing already stored is lost."""
    return {"stopped": sync.stop(), "status": sync.status()}


@app.post("/api/admin/watch-now", dependencies=[Depends(require_admin)])
def admin_watch_now() -> dict[str, Any]:
    """Run one watcher pass immediately (useful for testing the change feed)."""
    _require_api_key()
    changed = sync.check_for_changes()
    started = sync.start(changed, trigger="watch") if changed else False
    return {"changed_sources": changed, "started": started}


@app.post("/api/admin/sources/{source_id}/disconnect", dependencies=[Depends(require_admin)])
def admin_disconnect_source(source_id: int) -> dict[str, Any]:
    if db.get_source(source_id) is None:
        raise HTTPException(404, "source not found")
    sources.save_token(source_id, None)  # token_json is shared by Google and Microsoft sources
    db.update_source(source_id, account_email=None, last_status="not_connected", drive_page_token=None)
    return {"ok": True}


# ---------------------------------------------------------------------- Google OAuth

_oauth_states: dict[str, tuple[int, float]] = {}


def _redirect_uri(request: Request) -> str:
    return _base_url(request) + "/admin/oauth/callback"


@app.get("/admin/sources/{source_id}/connect")
def oauth_connect(source_id: int, request: Request):
    """Send the admin to Google's or Microsoft's sign-in screen for this source."""
    if current_role(request) != "admin":
        return RedirectResponse("/admin", status_code=303)
    src = db.get_source(source_id)
    if src is None or src["kind"] not in sources.CLOUD_KINDS:
        raise HTTPException(404, "cloud source not found")
    now = time.time()
    for k in [k for k, (_, ts) in _oauth_states.items() if now - ts > 600]:
        _oauth_states.pop(k, None)
    for k in [k for k, (_, ts, _v) in _ms_states.items() if now - ts > 600]:
        _ms_states.pop(k, None)
    state = secrets.token_urlsafe(24)
    try:
        if src["kind"] == "m365":
            verifier, challenge = microsoft.pkce_pair()
            _ms_states[state] = (source_id, now, verifier)
            url = microsoft.authorization_url(src, _ms_redirect_uri(request), state, challenge)
        else:
            _oauth_states[state] = (source_id, now)
            url = sources.authorization_url(_redirect_uri(request), state)
    except Exception as exc:
        return RedirectResponse(f"/admin?error={quote(str(exc))}#sources", status_code=303)
    return RedirectResponse(url, status_code=303)


# ---------------------------------------------------------------------- Microsoft OAuth

_ms_states: dict[str, tuple[int, float, str]] = {}  # state -> (source id, created, PKCE verifier)


def _ms_redirect_uri(request: Request) -> str:
    return _base_url(request) + "/admin/oauth/ms-callback"


@app.get("/admin/oauth/ms-callback", name="ms_oauth_callback")
def ms_oauth_callback(request: Request, state: str = "", code: str = "", error: str = "", error_description: str = ""):
    """Microsoft sends the admin back here: exchange the code, resolve the folder link, store the token, start syncing."""
    entry = _ms_states.pop(state, None)
    if entry is None:
        return RedirectResponse("/admin?error=" + quote("Sign-in session expired or invalid. Press Connect again.") + "#sources", status_code=303)
    source_id, _, verifier = entry
    src = db.get_source(source_id)
    if src is None:
        return RedirectResponse("/admin?error=" + quote("That source no longer exists.") + "#sources", status_code=303)
    if error or not code:
        why = error_description or error or "no code returned"
        db.update_source(source_id, last_status="not_connected", last_error=f"Microsoft sign-in cancelled: {why[:200]}")
        return RedirectResponse("/admin?error=" + quote(f"Microsoft sign-in was cancelled ({why[:200]}).") + "#sources", status_code=303)
    try:
        token, email = microsoft.exchange_code(src, _ms_redirect_uri(request), code, verifier)
    except Exception as exc:
        log.warning("microsoft oauth exchange failed for source %s: %s", source_id, exc)
        db.update_source(source_id, last_status="not_connected", last_error=f"{type(exc).__name__}: {str(exc)[:300]}")
        return RedirectResponse("/admin?error=" + quote(f"Microsoft sign-in failed: {str(exc)[:300]}") + "#sources", status_code=303)
    microsoft.save_token(source_id, token)
    # Resolve the pasted link with the account that just signed in (it decides what the account can see).
    location_err = None
    try:
        link = (src.get("ms_link") or "").strip()
        loc = microsoft.resolve_link(microsoft.Graph(src, token), link) if link else {"mode": "onedrive", "label": "OneDrive"}
        db.update_source(source_id, ms_location=json.dumps(loc))
    except Exception as exc:
        location_err = str(exc)[:300]
        log.warning("could not resolve link for source %s: %s", source_id, exc)
    db.update_source(source_id, account_email=email, last_status="connected" if not location_err else "failed",
                     last_error=location_err, enabled=1, drive_page_token=None)
    if location_err:
        return RedirectResponse("/admin?error=" + quote(f"Connected {email}, but the folder link could not be opened: {location_err}") + "#sources", status_code=303)
    started = settings.llm_ready() and sync.start([source_id], trigger="oauth")
    msg = f"Connected {email or 'Microsoft 365'}." + (" Sync started." if started else " Add a Claude API key to start syncing.")
    return RedirectResponse("/admin?connected=" + quote(msg) + "#sources", status_code=303)


@app.get("/admin/oauth/callback", name="oauth_callback")
def oauth_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    """Google sends the admin back here; exchange the code, store the token, start the first sync."""
    entry = _oauth_states.pop(state, None)
    if entry is None:
        return RedirectResponse("/admin?error=" + quote("Sign-in session expired or invalid. Press Connect again.") + "#sources", status_code=303)
    source_id, _ = entry
    if error or not code:
        db.update_source(source_id, last_status="not_connected", last_error=f"Google sign-in cancelled: {error or 'no code'}")
        return RedirectResponse("/admin?error=" + quote(f"Google sign-in was cancelled ({error or 'no code returned'}).") + "#sources", status_code=303)
    try:
        token_json, email = sources.exchange_code(_redirect_uri(request), code)
    except Exception as exc:
        log.warning("oauth exchange failed for source %s: %s", source_id, exc)
        db.update_source(source_id, last_status="not_connected", last_error=f"{type(exc).__name__}: {exc}")
        return RedirectResponse("/admin?error=" + quote(f"Google sign-in failed: {exc}") + "#sources", status_code=303)
    sources.save_token(source_id, token_json)
    db.update_source(source_id, account_email=email, last_status="connected", last_error=None, enabled=1, drive_page_token=None)
    started = settings.llm_ready() and sync.start([source_id], trigger="oauth")
    msg = f"Connected {email or 'Google Drive'}." + (" Sync started." if started else " Add a Claude API key to start syncing.")
    return RedirectResponse("/admin?connected=" + quote(msg) + "#sources", status_code=303)


# ====================================================================== admin: data


@app.get("/api/admin/runs", dependencies=[Depends(require_admin)])
def admin_runs(limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
    return db.last_runs(limit)


@app.get("/api/admin/backup", dependencies=[Depends(require_admin)])
def admin_backup():
    """Download a consistent copy of the database (secrets inside stay encrypted when APP_SECRET_KEY is set)."""
    tmp = tempfile.NamedTemporaryFile(prefix="ledger-backup-", suffix=".db", dir=config.DATA_DIR, delete=False)
    tmp.close()
    db.backup_to(tmp.name)
    stamp = time.strftime("%Y%m%d-%H%M")
    return FileResponse(tmp.name, media_type="application/octet-stream", filename=f"ledger-{stamp}.db")


@app.post("/api/admin/clear-documents", dependencies=[Depends(require_admin)])
def admin_clear_documents() -> dict[str, Any]:
    if sync.status()["running"]:
        raise HTTPException(409, "wait for the current sync to finish")
    db.clear_all_documents()
    return {"ok": True}


@app.post("/api/admin/demo/load", dependencies=[Depends(require_admin)])
def admin_demo_load() -> dict[str, Any]:
    return {"loaded": demo.load()}


@app.post("/api/admin/demo/clear", dependencies=[Depends(require_admin)])
def admin_demo_clear() -> dict[str, Any]:
    demo.clear()
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC), name="static")
