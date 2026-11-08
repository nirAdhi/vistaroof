"""Backups to a folder on the server (typically a mounted USB / external drive) and as downloads.

Two kinds, both written under ``backup_dir`` (Admin → Data; default ``<DATA_DIR>/backups``):

* **application** — ``ledger-app-<stamp>.zip``: a consistent copy of the database (every figure,
  setting, source and user; secrets stay encrypted), ``documents.csv`` with one row per document,
  ``summary.json`` with the all-time totals, and ``manifest.json``. Seconds to make. Can also run on
  a schedule (``backup_every_hours``) with retention (``backup_keep`` of each kind).
* **full** — ``ledger-full-<stamp>/``: the same application zip under ``app/`` plus every document
  file fetched again from its source (SharePoint / OneDrive / Google Drive / local folder) under
  ``files/<source>/<folder>/<name>``. Read-only on the sources, runs in a background thread with
  progress and a Stop button, keeps going when one file fails (listed in the manifest).

``download_path(name)`` turns either into a single zip for the browser, so the backup can also be
saved straight to a USB drive on the admin's own PC.

Safety: names are validated against ``NAME_RE`` before any path is touched, ``backup_dir`` must
exist and be writable (and lie inside ``BACKUP_ROOTS`` when that is set), retention only deletes
files/folders matching our own naming pattern, and nothing is ever written to a source.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import analytics, config, db, settings
from .sources import FileMeta, open_source

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^ledger-(app|full)-\d{8}-\d{6}(\.zip)?$")
_BAD = re.compile(r'[<>:"|?*\x00-\x1f\\/]+')
CSV_COLUMNS = ["id", "issue_date", "due_date", "doc_type", "direction", "treatment", "counterparty", "counterparty_role",
               "document_number", "project", "category", "currency", "subtotal", "vat_amount", "total", "net_amount",
               "amount_paid", "payment_status", "confidence", "status", "name", "folder", "source_name", "web_link", "summary"]

_lock = threading.Lock()
_state: dict[str, Any] = {"running": False, "kind": None, "phase": "idle", "message": "", "total": 0, "done": 0, "bytes": 0,
                          "errors": [], "started_at": None, "finished_at": None, "last_name": None, "stop": False}
_scheduler_started = False


# ------------------------------------------------------------------ destination


def backup_dir() -> Path:
    v = (settings.get("backup_dir") or "").strip()
    return Path(v).expanduser() if v else config.DATA_DIR / "backups"


def check_dir(path: str) -> Path:
    """Validate a folder the admin wants backups written to: exists, writable, inside BACKUP_ROOTS if set."""
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise ValueError(f"folder not found: {path} (plug in / mount the drive and create the folder first)")
    if config.BACKUP_ROOTS and not any(p == root or root in p.parents for root in config.BACKUP_ROOTS):
        raise ValueError("that folder is outside the allowed BACKUP_ROOTS for this server")
    try:
        probe = p / ".ledger-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise ValueError(f"folder is not writable: {exc}") from exc
    return p


def dir_info() -> dict[str, Any]:
    p = backup_dir()
    info: dict[str, Any] = {"path": str(p), "default": not (settings.get("backup_dir") or "").strip(), "exists": p.is_dir(), "writable": False, "free_bytes": None}
    if p.is_dir():
        try:
            check_dir(str(p))
            info["writable"] = True
        except ValueError as exc:
            info["problem"] = str(exc)
        try:
            info["free_bytes"] = shutil.disk_usage(p).free
        except OSError:
            pass
    else:
        info["problem"] = "folder does not exist"
    return info


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _free_name(dest: Path, kind: str, suffix: str = "") -> str:
    """``ledger-<kind>-<stamp><suffix>`` that does not exist yet (stamps have second resolution: wait for the next one)."""
    for _ in range(40):
        name = f"ledger-{kind}-{_stamp()}{suffix}"
        if not (dest / name).exists():
            return name
        time.sleep(0.25)
    raise RuntimeError("could not find a free backup name")


def _size_of(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def list_backups() -> list[dict[str, Any]]:
    p = backup_dir()
    if not p.is_dir():
        return []
    out = []
    for child in p.iterdir():
        if not NAME_RE.match(child.name):
            continue
        kind = "app" if child.name.startswith("ledger-app-") else "full"
        if (kind == "app") != child.is_file():
            continue
        try:
            out.append({"name": child.name, "kind": kind, "size": _size_of(child), "is_dir": child.is_dir(),
                        "created": datetime.fromtimestamp(child.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
                        "complete": kind == "app" or (child / "manifest.json").exists()})
        except OSError:
            continue
    out.sort(key=lambda b: b["name"], reverse=True)
    return out


def resolve_name(name: str) -> Path:
    """A backup the admin referred to by name → its path, or ValueError (names are validated first)."""
    if not NAME_RE.match(name or ""):
        raise ValueError("not a backup name")
    p = backup_dir() / name
    if not p.exists():
        raise FileNotFoundError(name)
    return p


# ------------------------------------------------------------------ application backup


def _documents_csv() -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for d in db.list_documents({"limit": 10000}):
        if d.get("source") == "demo":
            continue
        w.writerow({k: ("" if d.get(k) is None else d.get(k)) for k in CSV_COLUMNS})
    return buf.getvalue()


def _manifest(kind: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    counts = db.status_counts()
    return {"kind": kind, "app": "Roofing Ledger", "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "company": settings.get("company_name"), "documents": counts, "sources": [{"id": s["id"], "kind": s["kind"], "name": s["name"]} for s in db.list_sources()],
            "restore": "Stop the app, replace data/ledger.db with ledger.db from this backup, start the app. documents.csv and summary.json are for reading; the files folder (full backups) mirrors the source folders.",
            **(extra or {})}


def _write_app_zip(zip_path: Path) -> None:
    tmp = tempfile.NamedTemporaryFile(prefix="ledger-backup-", suffix=".db", dir=config.DATA_DIR, delete=False)
    tmp.close()
    try:
        db.backup_to(tmp.name)
        docs = [d for d in db.all_done_documents()]
        summary = analytics.compute(docs, None, None)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(tmp.name, "ledger.db")
            z.writestr("documents.csv", _documents_csv())
            z.writestr("summary.json", json.dumps(summary, indent=2, default=str))
            z.writestr("manifest.json", json.dumps(_manifest("app"), indent=2, default=str))
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def app_backup(dest: Path | None = None) -> dict[str, Any]:
    """Write ``ledger-app-<stamp>.zip`` into the backup folder (or ``dest``). Returns the listing entry."""
    if dest is None:
        dest = check_dir(str(backup_dir())) if backup_dir().is_dir() else _ensure_default()
    name = _free_name(dest, "app", ".zip")
    path = dest / name
    _write_app_zip(path)
    _retain(dest, "app")
    log.info("application backup written: %s (%d bytes)", path, path.stat().st_size)
    return {"name": name, "kind": "app", "size": path.stat().st_size, "path": str(path)}


def _ensure_default() -> Path:
    p = backup_dir()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _retain(dest: Path, kind: str) -> int:
    """Keep the newest ``backup_keep`` backups of a kind; delete only names matching our pattern."""
    keep = max(1, settings.get_int("backup_keep", 10))
    items = sorted([c for c in dest.iterdir() if NAME_RE.match(c.name) and c.name.startswith(f"ledger-{kind}-")], key=lambda c: c.name)
    removed = 0
    for old in items[:-keep] if len(items) > keep else []:
        try:
            shutil.rmtree(old) if old.is_dir() else old.unlink()
            removed += 1
        except OSError as exc:
            log.warning("could not remove old backup %s: %s", old, exc)
    return removed


# ------------------------------------------------------------------ full backup (files included)


def status() -> dict[str, Any]:
    with _lock:
        s = dict(_state)
        s["errors"] = list(_state["errors"])[-20:]
        s["error_count"] = len(_state["errors"])
        return s


def _set(**kw: Any) -> None:
    with _lock:
        _state.update(kw)


def _safe_part(s: str) -> str:
    s = _BAD.sub("_", (s or "").strip()).strip(". ")
    return s or "_"


def _file_path(root: Path, doc: dict[str, Any]) -> Path:
    parts = [_safe_part(x) for x in (doc.get("folder") or doc.get("source_name") or "files").replace("\\", "/").split("/") if x.strip() and x.strip() not in (".", "..")]
    p = root.joinpath(*parts) / _safe_part(doc["name"])
    if p.exists():
        p = p.with_name(f"{p.stem}-{doc['id']}{p.suffix}")
    return p


def full_backup(dest: Path | None = None) -> dict[str, Any]:
    """Application zip + every document file, fetched from its source. Synchronous; ``start_full`` threads it."""
    name = f"ledger-full-{_stamp()}"
    _set(running=True, kind="full", phase="app", message="Copying the database…", total=0, done=0, bytes=0, errors=[],
         started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), finished_at=None, last_name=name, stop=False)
    written: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    root: Path | None = None
    try:
        dest = dest or (check_dir(str(backup_dir())) if backup_dir().is_dir() else _ensure_default())
        name = _free_name(dest, "full")
        _set(last_name=name)
        root = dest / name
        (root / "app").mkdir(parents=True)
        (root / "files").mkdir()
        app_zip = root / "app" / f"ledger-app-{name[12:]}.zip"
        _write_app_zip(app_zip)
        docs = [d for d in db.list_documents({"limit": 10000}) if d.get("source") != "demo" and d.get("source_ref")]
        _set(phase="files", total=len(docs), message=f"Fetching {len(docs)} files from the sources…")
        by_source: dict[int, list[dict[str, Any]]] = {}
        for d in docs:
            by_source.setdefault(int(d["source_ref"]), []).append(d)
        for sid, items in by_source.items():
            src_row = db.get_source(sid)
            if src_row is None:
                for d in items:
                    skipped.append({"name": d["name"], "reason": "source no longer exists"})
                continue
            try:
                source = open_source(src_row)
            except Exception as exc:
                for d in items:
                    skipped.append({"name": d["name"], "reason": f"source cannot be opened: {str(exc)[:160]}"})
                _set(errors=_state["errors"] + [f"{src_row['name']}: {str(exc)[:160]}"], done=_state["done"] + len(items))
                continue
            for d in items:
                if _state["stop"]:
                    raise InterruptedError("stopped by the admin")
                _set(message=f"{src_row['name']}: {d['name']}")
                try:
                    meta = FileMeta(source=d["source"], source_ref=d["source_ref"], source_id=d["source_id"], name=d["name"], mime=d["mime"],
                                    folder=d.get("folder") or "", web_link=d.get("web_link") or "", size=d.get("size"), modified_at=d.get("modified_at") or "")
                    content = source.fetch(meta)
                    data = content.data if content.data is not None else (content.text or "").encode("utf-8")
                    target = _file_path(root / "files", {**d, "source_name": src_row["name"]})
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    written.append({"name": d["name"], "path": str(target.relative_to(root)), "bytes": len(data), "source": src_row["name"]})
                    _set(bytes=_state["bytes"] + len(data))
                except Exception as exc:
                    skipped.append({"name": d["name"], "reason": f"{type(exc).__name__}: {str(exc)[:200]}"})
                    _set(errors=_state["errors"] + [f"{d['name']}: {type(exc).__name__}: {str(exc)[:120]}"])
                finally:
                    _set(done=_state["done"] + 1)
        (root / "manifest.json").write_text(json.dumps(_manifest("full", {"files_written": len(written), "files_skipped": len(skipped), "bytes": _state["bytes"],
                                                                        "files": written, "skipped": skipped}), indent=2, default=str), encoding="utf-8")
        _retain(dest, "full")
        _set(phase="done", message=f"Done: {len(written)} files, {len(skipped)} skipped, {_state['bytes'] / 1048576:.1f} MB")
        log.info("full backup written: %s (%d files, %d skipped)", root, len(written), len(skipped))
        return {"name": name, "kind": "full", "files": len(written), "skipped": len(skipped), "bytes": _state["bytes"], "path": str(root)}
    except InterruptedError:
        _set(phase="stopped", message=f"Stopped after {len(written)} files; the partial folder {name} was removed")
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)
        return {"name": None, "kind": "full", "stopped": True}
    except Exception as exc:
        _set(phase="failed", message=f"Backup failed: {type(exc).__name__}: {str(exc)[:200]}")
        log.exception("full backup failed")
        raise
    finally:
        _set(running=False, finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))


def start_full() -> bool:
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, phase="starting", message="Starting…", stop=False)
    threading.Thread(target=lambda: _quiet(full_backup), daemon=True, name="backup").start()
    return True


def stop() -> bool:
    with _lock:
        if not _state["running"]:
            return False
        _state["stop"] = True
        _state["message"] = "Stopping…"
    return True


def _quiet(fn) -> None:
    try:
        fn()
    except Exception:
        log.exception("backup thread failed")  # the state already carries the message for the UI


# ------------------------------------------------------------------ download + delete


def download_path(name: str) -> tuple[Path, str, bool]:
    """(path to send, filename, is_temporary). A full backup folder is zipped into a temp file first."""
    p = resolve_name(name)
    if p.is_file():
        return p, p.name, False
    tmp = tempfile.NamedTemporaryFile(prefix="ledger-download-", suffix=".zip", dir=config.DATA_DIR, delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(p.rglob("*")):
            if f.is_file():
                z.write(f, f"{p.name}/{f.relative_to(p).as_posix()}")
    return Path(tmp.name), f"{p.name}.zip", True


def delete(name: str) -> None:
    p = resolve_name(name)
    shutil.rmtree(p) if p.is_dir() else p.unlink()
    log.info("backup deleted by the admin: %s", p)


# ------------------------------------------------------------------ schedule


def due(now: float | None = None) -> bool:
    """Is an automatic application backup due? (``backup_every_hours`` > 0 and the newest app backup is older.)"""
    every = settings.get_int("backup_every_hours", 0)
    if every <= 0:
        return False
    now = now or time.time()
    p = backup_dir()
    if not p.is_dir():
        return True
    newest = max((c.stat().st_mtime for c in p.iterdir() if NAME_RE.match(c.name) and c.name.startswith("ledger-app-")), default=0)
    return now - newest >= every * 3600


def run_scheduled(now: float | None = None) -> dict[str, Any] | None:
    if not due(now):
        return None
    try:
        return app_backup()
    except Exception as exc:
        log.error("scheduled backup failed: %s", exc)
        return None


def start_scheduler() -> None:
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    def loop() -> None:
        while True:
            time.sleep(60)
            try:
                run_scheduled()
            except Exception:  # pragma: no cover
                log.exception("backup scheduler error")

    threading.Thread(target=loop, daemon=True, name="backup-scheduler").start()
