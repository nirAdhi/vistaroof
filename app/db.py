"""SQLite storage: settings, sources, documents and sync runs.

Tables
------
settings    key/value rows edited from the Admin screen (see settings.py).
sources     one row per Google Drive connection or local folder. ``token_json`` holds the (encrypted)
            Google OAuth token; ``drive_page_token`` is the cursor of Google's change feed used by
            the watcher for near-real-time sync.
documents   one row per file seen in a source. Lifecycle of ``status``:
              pending -> processing -> done | skipped | error, and ``removed`` once the file is no
              longer in the source. Only ``done`` rows take part in the dashboard numbers.
            ``overridden=1`` marks hand edits that must never be overwritten by a re-read.
sync_runs   one row per sync of one source, for the Sync log.
users       named sign-in accounts for the dashboard (and, with ``role='admin'``, the admin area).
            ``token_version`` is embedded in every session token; bumping it logs that person out of
            every browser (used when an admin resets their password or disables the account).

Concurrency
-----------
SQLite in WAL mode with a 30 s busy timeout. Every function opens its own short-lived connection
(``connect()``), which is what makes the thread pool in sync.py safe. Keep transactions short.

Debugging tips
--------------
* ``sqlite3 data/ledger.db "SELECT status, COUNT(*) FROM documents GROUP BY status"``
* A document stuck in ``processing`` means the process died mid-sync; the next sync picks it up again
  because ``needs_processing()`` only skips ``done``/``skipped`` rows.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,              -- drive | local
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    drive_folder_id TEXT,            -- limit a Drive source to one folder (and its sub-folders)
    local_folder TEXT,
    token_json TEXT,                 -- Google OAuth token (encrypted when APP_SECRET_KEY is set)
    account_email TEXT,
    created_at TEXT,
    last_sync_at TEXT,
    last_status TEXT,
    last_error TEXT,
    drive_page_token TEXT,           -- Google Drive changes.list cursor (watcher)
    last_change_check TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,            -- drive | local | demo
    source_ref INTEGER,              -- sources.id (NULL for demo)
    source_id TEXT NOT NULL,         -- Drive file id or local path
    name TEXT,
    mime TEXT,
    folder TEXT,
    web_link TEXT,
    size INTEGER,
    modified_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    processed_at TEXT,
    processed_modified_at TEXT,      -- modified_at at the time of the last read; differs => re-read
    model TEXT,
    doc_type TEXT,
    direction TEXT,
    treatment TEXT,
    sign INTEGER DEFAULT 0,
    counterparty TEXT,
    counterparty_role TEXT,
    document_number TEXT,
    issue_date TEXT,
    due_date TEXT,
    currency TEXT,
    subtotal REAL,
    vat_amount REAL,
    total REAL,
    net_amount REAL,
    amount_paid REAL,
    payment_status TEXT,
    category TEXT,
    project TEXT,
    summary TEXT,
    confidence REAL,
    flags TEXT,                      -- JSON list
    line_items TEXT,                 -- JSON list
    raw TEXT,                        -- JSON: the model's full answer
    overridden INTEGER NOT NULL DEFAULT 0,
    uploaded_by TEXT,                -- 'app' when the file was uploaded through the dashboard
    UNIQUE (source_ref, source_id)
);
CREATE INDEX IF NOT EXISTS idx_documents_issue_date ON documents(issue_date);
CREATE INDEX IF NOT EXISTS idx_documents_treatment ON documents(treatment);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_documents_source_ref ON documents(source_ref);
CREATE INDEX IF NOT EXISTS idx_documents_counterparty ON documents(counterparty);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT,
    finished_at TEXT,
    source TEXT,
    source_ref INTEGER,
    source_name TEXT,
    force INTEGER DEFAULT 0,
    total INTEGER DEFAULT 0,
    processed INTEGER DEFAULT 0,
    skipped INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    removed INTEGER DEFAULT 0,
    status TEXT,
    message TEXT,
    trigger TEXT                     -- manual | watch | schedule | upload | oauth
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,      -- lower-cased sign-in name
    name TEXT,
    role TEXT NOT NULL DEFAULT 'user',   -- user (dashboard) | admin (dashboard + admin area)
    password_hash TEXT NOT NULL,     -- <salt>$<pbkdf2>, see settings.hash_password()
    active INTEGER NOT NULL DEFAULT 1,
    token_version INTEGER NOT NULL DEFAULT 1,  -- bump to invalidate this user's sessions
    created_at TEXT,
    last_login_at TEXT
);
"""

DOC_COLUMNS = [
    "id", "source", "source_ref", "source_id", "name", "mime", "folder", "web_link", "size", "modified_at", "status",
    "error", "processed_at", "processed_modified_at", "model", "doc_type", "direction", "treatment", "sign",
    "counterparty", "counterparty_role", "document_number", "issue_date", "due_date", "currency", "subtotal",
    "vat_amount", "total", "net_amount", "amount_paid", "payment_status", "category", "project", "summary",
    "confidence", "flags", "line_items", "raw", "overridden", "uploaded_by",
]

# Columns added after the first release; init_db() adds any that are missing (SQLite has no ADD COLUMN IF NOT EXISTS).
_MIGRATIONS = {
    "sources": [("drive_page_token", "TEXT"), ("last_change_check", "TEXT"),
                # Microsoft 365 sources: the pasted link, its resolved location (JSON), tenant override, upload flag
                ("ms_link", "TEXT"), ("ms_location", "TEXT"), ("ms_tenant", "TEXT"), ("ms_upload", "INTEGER DEFAULT 1")],
    "documents": [("uploaded_by", "TEXT"), ("attempts", "INTEGER NOT NULL DEFAULT 0")],
    "sync_runs": [("source_ref", "INTEGER"), ("source_name", "TEXT"), ("force", "INTEGER DEFAULT 0"), ("message", "TEXT"),
                  ("removed", "INTEGER DEFAULT 0"), ("trigger", "TEXT"), ("files", "TEXT")],
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Short-lived connection with WAL + busy timeout; commits on success, rolls back on error."""
    conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_TABLES = {"settings", "sources", "documents", "sync_runs", "documents_v1"}
# Columns that may be written through the **fields helpers. Anything else raises, so a bug can never
# turn a dict key into SQL.
_SOURCE_COLUMNS = {"name", "enabled", "drive_folder_id", "local_folder", "token_json", "account_email", "last_sync_at",
                   "last_status", "last_error", "drive_page_token", "last_change_check",
                   "ms_link", "ms_location", "ms_tenant", "ms_upload"}
_RUN_COLUMNS = {"finished_at", "total", "processed", "skipped", "errors", "removed", "status", "message", "files"}
_USER_COLUMNS = {"name", "role", "active", "password_hash"}
USER_ROLES = ("user", "admin")


def _assert_columns(fields: dict[str, Any], allowed: set[str]) -> None:
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"refusing to write unknown column(s): {sorted(bad)}")


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    if table not in _TABLES:
        raise ValueError(f"unknown table {table}")
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]  # nosec B608 - table whitelisted above


def init_db() -> None:
    """Create tables and apply in-place migrations. Safe to run at every start."""
    with connect() as conn:
        # v1 (single source, UNIQUE(source_id)) -> multi-source layout: rebuild the documents table.
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='documents'").fetchone():
            cols = _columns(conn, "documents")
            if "source_ref" not in cols:
                conn.execute("ALTER TABLE documents RENAME TO documents_v1")
                conn.executescript(SCHEMA)
                keep = [c for c in DOC_COLUMNS if c in cols]  # intersection of two code-defined lists
                conn.execute(f"INSERT INTO documents ({', '.join(keep)}) SELECT {', '.join(keep)} FROM documents_v1")  # nosec B608
                conn.execute("DROP TABLE documents_v1")
        conn.executescript(SCHEMA)
        for table, additions in _MIGRATIONS.items():  # table/column names are literals in _MIGRATIONS
            existing = _columns(conn, table)
            for col, decl in additions:
                if col not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")  # nosec B608


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """sqlite3.Row -> dict, decoding the JSON columns."""
    if row is None:
        return None
    d = dict(row)
    for key in ("flags", "line_items", "raw"):
        if d.get(key):
            try:
                d[key] = json.loads(d[key])
            except (TypeError, ValueError):
                pass
    return d


# ---------------------------------------------------------------- sources


def create_source(kind: str, name: str, drive_folder_id: str | None = None, local_folder: str | None = None,
                  ms_link: str | None = None, ms_tenant: str | None = None, ms_upload: bool = True) -> int:
    """Insert a source row. Cloud kinds (drive, m365) start as ``not_connected`` until OAuth completes."""
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO sources (kind, name, enabled, drive_folder_id, local_folder, ms_link, ms_tenant, ms_upload, created_at, last_status)
               VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)""",
            (kind, name, drive_folder_id or None, local_folder or None, ms_link or None, ms_tenant or None, 1 if ms_upload else 0,
             now_iso(), "ready" if kind == "local" else "not_connected"),
        )
        return int(cur.lastrowid)


def update_source(source_id: int, **fields: Any) -> None:
    """Update whitelisted columns; callers pass already-encrypted token_json."""
    if not fields:
        return
    _assert_columns(fields, _SOURCE_COLUMNS)
    sets = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE sources SET {sets} WHERE id=?", (*fields.values(), source_id))  # nosec B608 - names whitelisted, values bound


def get_source(source_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    return dict(row) if row else None


def list_sources(include_secrets: bool = False) -> list[dict[str, Any]]:
    """Sources with document counts. ``connected`` = local, or a Drive with a stored token."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT s.*,
                      (SELECT COUNT(*) FROM documents d WHERE d.source_ref = s.id AND d.status != 'removed') AS doc_count,
                      (SELECT COUNT(*) FROM documents d WHERE d.source_ref = s.id AND d.status='done') AS done_count,
                      (SELECT COUNT(*) FROM documents d WHERE d.source_ref = s.id AND d.status='error') AS error_count,
                      (SELECT COUNT(*) FROM documents d WHERE d.source_ref = s.id AND d.status='skipped') AS skipped_count,
                      (SELECT COUNT(*) FROM documents d WHERE d.source_ref = s.id AND d.status IN ('pending', 'processing')) AS waiting_count
               FROM sources s ORDER BY s.id"""
        ).fetchall()
        # the last run of each source, for the card's "100% complete · N read · finished at" line
        last = {r["source_ref"]: dict(r) for r in conn.execute(
            "SELECT * FROM sync_runs WHERE id IN (SELECT MAX(id) FROM sync_runs WHERE source_ref IS NOT NULL GROUP BY source_ref)"
        ).fetchall()}
    out = []
    for r in rows:
        d = dict(r)
        d["connected"] = d["kind"] == "local" or bool(d.get("token_json"))
        if not include_secrets:
            d.pop("token_json", None)
        run = last.get(d["id"])
        if run:
            try:
                files = json.loads(run.get("files") or "[]")
            except ValueError:
                files = []
            d["last_run"] = {k: run.get(k) for k in ("started_at", "finished_at", "status", "trigger", "total", "processed", "skipped", "errors", "removed", "message")}
            d["last_run"]["files"] = files
        else:
            d["last_run"] = None
        out.append(d)
    return out


def delete_source(source_id: int) -> int:
    """Remove a source and all of its documents; returns the number of documents deleted."""
    with connect() as conn:
        n = conn.execute("DELETE FROM documents WHERE source_ref=?", (source_id,)).rowcount
        conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
    _touch()
    return n


# ---------------------------------------------------------------- users


def _user_public(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Row -> dict without the password hash (what the API returns)."""
    if row is None:
        return None
    d = dict(row)
    d.pop("password_hash", None)
    d["active"] = bool(d.get("active"))
    return d


def create_user(email: str, name: str, role: str, password_hash: str) -> int:
    """Insert an account. Raises ValueError when the email is already taken or the role is unknown."""
    if role not in USER_ROLES:
        raise ValueError(f"unknown role {role!r}")
    try:
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO users (email, name, role, password_hash, active, token_version, created_at) VALUES (?, ?, ?, ?, 1, 1, ?)",
                (email.strip().lower(), name.strip(), role, password_hash, now_iso()),
            )
            return int(cur.lastrowid)
    except sqlite3.IntegrityError as exc:
        raise ValueError("an account with this email already exists") from exc


def get_user(user_id: int, with_hash: bool = False) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return (dict(row) if row else None) if with_hash else _user_public(row)


def get_user_by_email(email: str) -> dict[str, Any] | None:
    """Full row (including ``password_hash``) for the login check."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    return dict(row) if row else None


def list_users() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY role DESC, lower(name), email").fetchall()
    return [_user_public(r) for r in rows]  # type: ignore[misc]


def update_user(user_id: int, **fields: Any) -> None:
    """Update whitelisted columns (name, role, active, password_hash)."""
    if not fields:
        return
    _assert_columns(fields, _USER_COLUMNS)
    if "role" in fields and fields["role"] not in USER_ROLES:
        raise ValueError(f"unknown role {fields['role']!r}")
    if "active" in fields:
        fields["active"] = 1 if fields["active"] else 0
    sets = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE users SET {sets} WHERE id=?", (*fields.values(), user_id))  # nosec B608 - names whitelisted


def bump_user_token(user_id: int) -> None:
    """Invalidate every session of this user (password reset, disable)."""
    with connect() as conn:
        conn.execute("UPDATE users SET token_version = token_version + 1 WHERE id=?", (user_id,))


def touch_user_login(user_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (now_iso(), user_id))


def delete_user(user_id: int) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM users WHERE id=?", (user_id,)).rowcount > 0


def count_users() -> int:
    with connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


# ---------------------------------------------------------------- documents


def upsert_file_metadata(meta: dict[str, Any]) -> int:
    """Insert a new file row or refresh name/size/modified_at of a known one. Returns the document id.
    A file that re-appears after being marked ``removed`` goes back to ``pending``."""
    with connect() as conn:
        row = conn.execute(
            "SELECT id, status FROM documents WHERE source_id = ? AND source_ref IS ?", (meta["source_id"], meta.get("source_ref")),
        ).fetchone()
        if row is None:
            cur = conn.execute(
                """INSERT INTO documents (source, source_ref, source_id, name, mime, folder, web_link, size, modified_at, status, uploaded_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (
                    meta["source"], meta.get("source_ref"), meta["source_id"], meta["name"], meta["mime"],
                    meta.get("folder"), meta.get("web_link"), meta.get("size"), meta.get("modified_at"), meta.get("uploaded_by"),
                ),
            )
            return int(cur.lastrowid)
        conn.execute(
            "UPDATE documents SET name=?, mime=?, folder=?, web_link=?, size=?, modified_at=?, status=CASE WHEN status='removed' THEN 'pending' ELSE status END WHERE id=?",
            (meta["name"], meta["mime"], meta.get("folder"), meta.get("web_link"), meta.get("size"), meta.get("modified_at"), row["id"]),
        )
        return int(row["id"])


def mark_missing(source_ref: int, present_source_ids: set[str]) -> int:
    """After a full listing: flag documents of this source that are no longer in it. Returns the count."""
    with connect() as conn:
        rows = conn.execute("SELECT id, source_id FROM documents WHERE source_ref=? AND status != 'removed'", (source_ref,)).fetchall()
        # a sheet document ("<file>#sheet:<name>") lives as long as its workbook is listed
        gone = [r["id"] for r in rows if r["source_id"].partition(SHEET_SEP)[0] not in present_source_ids]
        for doc_id in gone:
            conn.execute("UPDATE documents SET status='removed', error='file no longer in source' WHERE id=?", (doc_id,))
    if gone:
        _touch()
    return len(gone)


def remove_missing_sheets(source_ref: int, file_source_id: str, present_sheet_ids: set[str]) -> int:
    """Sheet documents of a workbook whose sheet is no longer in it are marked removed. Returns the count."""
    with connect() as conn:
        rows = conn.execute("SELECT id, source_id FROM documents WHERE source_ref=? AND source_id LIKE ? AND status != 'removed'",
                            (source_ref, file_source_id + SHEET_SEP + "%")).fetchall()
        gone = [r["id"] for r in rows if r["source_id"] not in present_sheet_ids]
        for doc_id in gone:
            conn.execute("UPDATE documents SET status='removed', error='sheet no longer in the workbook' WHERE id=?", (doc_id,))
    if gone:
        _touch()
    return len(gone)


def mark_split(doc_id: int, sheets: int) -> None:
    """The workbook row of a file read sheet by sheet: holds no figures itself, points at its sheet documents."""
    with connect() as conn:
        conn.execute(
            "UPDATE documents SET status='split', error=?, attempts=0, processed_at=?, processed_modified_at=modified_at WHERE id=?",
            (f"workbook read as {sheets} separate sheets (one document each, listed under this file's name)", now_iso(), doc_id),
        )
    _touch()


def needs_processing(doc_id: int, force: bool = False, retry_prefix: str | tuple[str, ...] | None = None) -> bool:
    """Decide whether a file must be (re-)read. Hand-edited documents are never re-read automatically.

    ``retry_prefix``: a skipped file whose reason starts with one of these is read again (photos / scans
    skipped by a text-only reader once vision is available; types the app has since learnt to read)."""
    if isinstance(retry_prefix, str):
        retry_prefix = (retry_prefix,)
    with connect() as conn:
        row = conn.execute(
            "SELECT status, error, modified_at, processed_modified_at, overridden, attempts FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
    if row is None:
        return False
    if row["overridden"]:
        return False
    if force:
        return True
    if row["status"] == "skipped" and retry_prefix and (row["error"] or "").startswith(tuple(retry_prefix)):
        return True
    if row["status"] in ("done", "skipped", "split") and row["processed_modified_at"] == row["modified_at"]:
        return False
    if row["status"] == "error" and (row["attempts"] or 0) >= MAX_ATTEMPTS and row["processed_modified_at"] == row["modified_at"]:
        return False  # tried enough: rests as an error until the file changes, a Full re-read, Re-read or a hand entry
    return True


MAX_ATTEMPTS = 3  # automatic reads of a failing file before the sync leaves it alone


def set_status(doc_id: int, status: str, error: str | None = None) -> None:
    with connect() as conn:
        conn.execute("UPDATE documents SET status=?, error=? WHERE id=?", (status, error, doc_id))


def mark_error(doc_id: int, message: str) -> int:
    """Record a failed read and count the attempt. From MAX_ATTEMPTS on the message says the sync has given up on
    it (until the file changes or someone acts on it) so the same file cannot cost every run minutes. Returns attempts."""
    with connect() as conn:
        attempts = (conn.execute("SELECT attempts FROM documents WHERE id=?", (doc_id,)).fetchone() or {"attempts": 0})["attempts"] or 0
        attempts += 1
        if attempts >= MAX_ATTEMPTS:
            message = f"{message[:380]} — gave up after {attempts} attempts; enter the figures by hand or use Re-read"
        conn.execute(
            "UPDATE documents SET status='error', error=?, attempts=?, processed_at=?, processed_modified_at=modified_at WHERE id=?",
            (message[:500], attempts, now_iso(), doc_id),
        )
    return attempts


def reset_stuck_processing() -> int:
    """At start-up: rows left 'processing' by a restart mid-sync go back to pending (the next sync reads them)."""
    with connect() as conn:
        n = conn.execute("UPDATE documents SET status='pending' WHERE status='processing'").rowcount
    return n


def mark_skipped(doc_id: int, reason: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE documents SET status='skipped', error=?, processed_at=?, processed_modified_at=modified_at WHERE id=?",
            (reason, now_iso(), doc_id),
        )


def save_extraction(doc_id: int, fields: dict[str, Any], model: str) -> None:
    """Persist the model's answer (already passed through extract.derive)."""
    with connect() as conn:
        conn.execute(
            """UPDATE documents SET
                status='done', error=NULL, attempts=0, processed_at=?, processed_modified_at=modified_at, model=?,
                doc_type=?, direction=?, treatment=?, sign=?, counterparty=?, counterparty_role=?,
                document_number=?, issue_date=?, due_date=?, currency=?, subtotal=?, vat_amount=?, total=?,
                net_amount=?, amount_paid=?, payment_status=?, category=?, project=?, summary=?, confidence=?,
                flags=?, line_items=?, raw=?
               WHERE id=?""",
            (
                now_iso(), model,
                fields.get("doc_type"), fields.get("direction"), fields.get("treatment"), fields.get("sign", 0),
                fields.get("counterparty"), fields.get("counterparty_role"), fields.get("document_number"),
                fields.get("issue_date"), fields.get("due_date"), fields.get("currency"), fields.get("subtotal"),
                fields.get("vat_amount"), fields.get("total"), fields.get("net_amount"), fields.get("amount_paid"),
                fields.get("payment_status"), fields.get("category"), fields.get("project"), fields.get("summary"),
                fields.get("confidence"), json.dumps(fields.get("flags") or []),
                json.dumps(fields.get("line_items") or []), json.dumps(fields.get("raw") or {}),
                doc_id,
            ),
        )


EDITABLE_FIELDS = {
    "doc_type", "direction", "counterparty", "counterparty_role", "document_number", "issue_date", "due_date",
    "currency", "subtotal", "vat_amount", "total", "amount_paid", "payment_status", "category", "project", "summary",
}


MANUAL_MODEL = "manual"  # ``documents.model`` for figures typed in by a person (a file no reader could read)
SHEET_SEP = "#sheet:"  # source_id of a sheet document = "<file id>#sheet:<sheet name>" (textract.sheet_ref / split_ref)


def update_document(doc_id: int, patch: dict[str, Any], derived: dict[str, Any]) -> None:
    """Apply a hand edit. Only EDITABLE_FIELDS are accepted; treatment/sign/net are recomputed by the caller.

    Works on a read document (a correction) and on a skipped / failed / pending one (the figures are
    entered by hand, e.g. a handwritten receipt no model can read). Either way the row ends up
    ``done`` with ``overridden=1`` so no sync re-reads it; a row that was never read gets
    ``model='manual'`` and loses its error text, so it leaves the Skipped list and counts in the
    dashboard immediately."""
    sets, vals = [], []
    for k, v in patch.items():
        if k in EDITABLE_FIELDS:  # whitelist: unknown keys are silently ignored
            sets.append(f"{k}=?")
            vals.append(v)
    for k in ("treatment", "sign", "net_amount"):
        if k in derived:
            sets.append(f"{k}=?")
            vals.append(derived[k])
    sets += ["overridden=1", "status='done'", "error=NULL", "processed_modified_at=modified_at",
             "processed_at=COALESCE(processed_at, ?)", "model=COALESCE(model, ?)",
             "confidence=CASE WHEN model IS NULL THEN 1.0 ELSE confidence END",
             "flags=CASE WHEN model IS NULL THEN '[]' ELSE flags END"]
    vals += [now_iso(), MANUAL_MODEL, doc_id]
    with connect() as conn:
        conn.execute(f"UPDATE documents SET {', '.join(sets)} WHERE id=?", vals)  # nosec B608 - names whitelisted, values bound
    _touch()


def reset_document(doc_id: int) -> None:
    """Forget the hand edit and the last read so the next sync / re-read processes the file again."""
    with connect() as conn:
        conn.execute("UPDATE documents SET status='pending', overridden=0, attempts=0, processed_modified_at=NULL WHERE id=?", (doc_id,))


def undated_documents_with_raw() -> list[dict[str, Any]]:
    """Read documents missing a date whose stored model answer might still hold one (see app/repair.py)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, issue_date, due_date, raw FROM documents WHERE status='done' AND overridden=0 AND source!='demo' "
            "AND raw IS NOT NULL AND (issue_date IS NULL OR due_date IS NULL)"
        ).fetchall()
    return [dict(r) for r in rows]


def set_dates(doc_id: int, issue_date: str | None, due_date: str | None) -> None:
    with connect() as conn:
        conn.execute("UPDATE documents SET issue_date=?, due_date=? WHERE id=?", (issue_date, due_date, doc_id))
    _touch()


_SOURCE_NAME_SQL = "COALESCE(s.name, CASE WHEN d.source='demo' THEN 'Demo data' END) AS source_name"
_DOC_LIST_COLUMNS = ", ".join(
    "d." + c for c in (
        "id", "source", "source_ref", "source_id", "name", "mime", "folder", "web_link", "size", "modified_at", "status", "error",
        "processed_at", "doc_type", "direction", "treatment", "sign", "counterparty", "counterparty_role", "document_number",
        "issue_date", "due_date", "currency", "subtotal", "vat_amount", "total", "net_amount", "amount_paid", "payment_status",
        "category", "project", "summary", "confidence", "flags", "overridden", "uploaded_by", "model",
    )
)


def get_document(doc_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT d.*, " + _SOURCE_NAME_SQL + " FROM documents d LEFT JOIN sources s ON s.id = d.source_ref WHERE d.id=?", (doc_id,)  # nosec B608 - constant fragment
        ).fetchone()
    return row_to_dict(row)


def list_documents(filters: dict[str, Any]) -> list[dict[str, Any]]:
    """Filtered document list for the dashboard. ``removed`` rows are hidden unless asked for explicitly."""
    where, vals = ["1=1"], []
    # Same rule as analytics._eff_date: an undated document is placed by its file date, so the list
    # and the dashboard figures agree on what "in this period" means.
    if filters.get("date_from"):
        where.append("COALESCE(d.issue_date, substr(d.modified_at, 1, 10)) >= ?"); vals.append(filters["date_from"])
    if filters.get("date_to"):
        where.append("COALESCE(d.issue_date, substr(d.modified_at, 1, 10)) <= ?"); vals.append(filters["date_to"])
    for key in ("doc_type", "treatment", "status", "payment_status", "counterparty", "project"):  # fixed column names
        if filters.get(key):
            where.append(f"d.{key} = ?"); vals.append(filters[key])  # nosec B608 - value is a bound parameter
    if not filters.get("status"):
        where.append("d.status != 'removed'")
    if filters.get("source_ref"):
        where.append("d.source_ref = ?"); vals.append(int(filters["source_ref"]))
    if filters.get("q"):
        q = f"%{filters['q']}%"
        where.append("(d.name LIKE ? OR d.counterparty LIKE ? OR d.document_number LIKE ? OR d.summary LIKE ? OR d.project LIKE ? OR d.folder LIKE ?)")
        vals.extend([q] * 6)
    sql = "SELECT " + _DOC_LIST_COLUMNS + ", " + _SOURCE_NAME_SQL + " FROM documents d LEFT JOIN sources s ON s.id = d.source_ref WHERE " + " AND ".join(where) + " ORDER BY COALESCE(d.issue_date, d.modified_at) DESC, d.id DESC LIMIT ?"  # nosec B608 - fixed fragments, bound values
    vals.append(min(int(filters.get("limit", 2000)), 10000))
    with connect() as conn:
        return [row_to_dict(r) for r in conn.execute(sql, vals).fetchall()]  # nosec B608 - WHERE built from fixed fragments + bound values


def all_done_documents() -> list[dict[str, Any]]:
    """Everything the analytics need, for successfully read documents only."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, source_ref, name, folder, web_link, doc_type, direction, treatment, sign, counterparty,
                      counterparty_role, document_number, issue_date, due_date, currency, subtotal, vat_amount, total,
                      net_amount, amount_paid, payment_status, category, project, summary, confidence, flags, modified_at
               FROM documents WHERE status='done'"""
        ).fetchall()
        return [row_to_dict(r) for r in rows]


def status_counts() -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM documents GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def model_counts() -> dict[str, int]:
    """How many real documents each model has read (``ollama/qwen2.5:3b``: 12, ``claude-opus-5-5``: 300)."""
    with connect() as conn:
        rows = conn.execute("SELECT model, COUNT(*) AS n FROM documents WHERE status='done' AND source!='demo' AND model IS NOT NULL "
                            "GROUP BY model ORDER BY n DESC").fetchall()
    return {r["model"]: r["n"] for r in rows}


_rev = 0  # bumped on every write that changes what the dashboard shows (single-process app)


def _touch() -> None:
    global _rev
    _rev += 1


def ledger_version() -> str:
    """Cheap change marker the dashboard polls to know when to refresh. Combines an in-memory
    revision (hand edits, removals) with database facts (survives restarts)."""
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, SUM(status='done') AS d, SUM(status='removed') AS r, MAX(processed_at) AS p, MAX(id) AS m FROM documents"
        ).fetchone()
    return f"{_rev}:{row['n']}:{row['d'] or 0}:{row['r'] or 0}:{row['p']}:{row['m']}"


def clear_all_documents() -> None:
    with connect() as conn:
        conn.execute("DELETE FROM documents")
        conn.execute("DELETE FROM sync_runs")
    _touch()


# ---------------------------------------------------------------- sync runs


def start_run(source: str, source_ref: int | None, source_name: str, force: bool, trigger: str = "manual") -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO sync_runs (started_at, source, source_ref, source_name, force, status, trigger) VALUES (?, ?, ?, ?, ?, 'running', ?)",
            (now_iso(), source, source_ref, source_name, 1 if force else 0, trigger),
        )
        return int(cur.lastrowid)


def update_run(run_id: int, **fields: Any) -> None:
    if not fields:
        return
    _assert_columns(fields, _RUN_COLUMNS)
    sets = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE sync_runs SET {sets} WHERE id=?", (*fields.values(), run_id))  # nosec B608 - names whitelisted, values bound


def last_runs(limit: int = 10) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def backup_to(path: str) -> None:
    """Consistent online copy of the database (SQLite backup API), used by Admin → Download backup."""
    src = sqlite3.connect(config.DB_PATH, timeout=30)
    dst = sqlite3.connect(path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
