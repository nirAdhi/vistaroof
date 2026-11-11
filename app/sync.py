"""Sync pipeline: list a source, read every new/changed file with Claude, store the result.

Three ways a sync starts
------------------------
1. Manual: the Sync buttons (owner dashboard / Admin) call ``start()``.
2. Watcher (near real time): ``start_scheduler()`` runs a background thread that, every
   ``watch_interval_seconds``, asks each connected Drive whether anything changed (Drive changes feed)
   and re-scans local folders; when something changed it starts a sync for that source only.
   Every ``auto_sync_minutes`` a full sync of everything runs as a safety net.
3. Upload: ``process_uploaded()`` reads files the owner just uploaded, synchronously, so the result
   can be shown immediately.

Only one sync runs at a time (``_state["running"]``). Requests that arrive meanwhile are queued in
``_rerun`` and run when the current one finishes. Progress is in ``_state`` and served by
``status()`` for the UI. ``stop()`` (Admin → Stop) ends the run after the files in flight: the run
and the source are marked ``stopped``, nothing stored is lost, unread files wait for the next sync.
The same path is taken automatically (``_abort``) when the Claude API is unusable — invalid key, no
credit (``extract.ApiUnavailable``) — so one clear message replaces thousands of identical error rows;
the files touched go back to ``pending``.

Per file: ``_process_one()`` → metadata upsert → skip if unchanged / hand-edited → download →
``extract.extract()`` → ``db.save_extraction()``. Any exception is recorded on the document
(status ``error``) and never aborts the run.

Debugging tips
--------------
* ``status()["recent_errors"]`` lists the last failures of the current run; the Sync log keeps runs.
* Set ``APP_DEBUG=1`` to see per-file log lines.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from . import db, extract, llm, notify, settings
from .sources import SUPPORTED_MIMES, FileMeta, open_source

log = logging.getLogger(__name__)

_lock = threading.Lock()
_state: dict[str, Any] = {
    "running": False, "phase": "idle", "message": "", "trigger": None, "current_source": None, "current_file": None,
    "queue": [], "total": 0, "processed": 0, "skipped": 0, "errors": 0, "removed": 0, "recent_errors": [],
    "last_finished_at": None, "last_watch_at": None, "stop_requested": False, "stop_reason": None,
}
ADMIN_STOP = "stopped by the admin"
_rerun: dict[int | None, bool] = {}  # source id -> force; None = all enabled sources
# Why a run started, for the status line: a sync that starts "by itself" is the watcher reacting to a change
# in SharePoint / Drive, a request queued behind the previous run, the auto-sync timer, or a fresh sign-in.
TRIGGER_WHY = {
    "watch": "SharePoint / Drive reported a change", "queued": "requested while the previous sync was running",
    "schedule": "automatic sync timer", "oauth": "just connected",
}


def status() -> dict[str, Any]:
    """Snapshot for the UI (adds document counts and the last runs)."""
    with _lock:
        snap = dict(_state)
        snap["recent_errors"] = list(_state["recent_errors"])[-10:]
        snap["queue"] = list(_state["queue"])
    snap["counts"] = db.status_counts()
    snap["runs"] = db.last_runs(8)
    snap["version"] = db.ledger_version()
    snap["llm"] = llm.status()          # is the document reader (Ollama / Claude) usable right now?
    snap["read_by"] = db.model_counts()  # which model produced the figures
    return snap


def _set(**kw: Any) -> None:
    with _lock:
        _state.update(kw)


def _bump(key: str, n: int = 1) -> None:
    with _lock:
        _state[key] += n


def _note_error(name: str, err: str) -> None:
    with _lock:
        _state["errors"] += 1
        _state["recent_errors"].append({"file": name, "error": err})
        _state["recent_errors"] = _state["recent_errors"][-50:]


# ------------------------------------------------------------------ starting syncs


def start(source_ids: list[int] | None = None, force: bool = False, wait: bool = False, trigger: str = "manual") -> bool:
    """Sync the given sources (default: all enabled). Returns False (and queues the request) if busy."""
    sources = db.list_sources(include_secrets=True)
    if source_ids is None:
        sources = [s for s in sources if s["enabled"]]
    else:
        sources = [s for s in sources if s["id"] in source_ids]
    with _lock:
        if _state["running"]:
            for s in (sources or [None]):
                key = s["id"] if s else None
                _rerun[key] = _rerun.get(key, False) or force
            return False
        _state.update({
            "running": True, "phase": "starting", "message": "Starting…", "trigger": trigger, "current_source": None,
            "current_file": None, "queue": [s["name"] for s in sources], "total": 0, "processed": 0, "skipped": 0, "errors": 0,
            "removed": 0, "recent_errors": [], "stop_requested": False, "stop_reason": None,
        })
    if not sources:
        _set(running=False, phase="done", message="Nothing to sync: no enabled sources", last_finished_at=db.now_iso())
        return True
    t = threading.Thread(target=_run_all, args=(sources, force, trigger), daemon=True, name="sync")
    t.start()
    if wait:
        t.join()
    return True


def stop() -> bool:
    """Ask the running sync to stop once the files being read right now are finished.

    Everything stored so far stays; files not reached are read by the next sync. Requests queued
    behind the run are dropped too (the admin pressed Stop for a reason). Returns False when idle."""
    with _lock:
        if not _state["running"]:
            return False
        _state["stop_requested"] = True
        _state["stop_reason"] = ADMIN_STOP
        _state["message"] = "Stopping after the files being read now…"
        _rerun.clear()
    log.info("sync stop requested")
    return True


def _abort(reason: str) -> None:
    """Stop the run because the API is unusable (bad key, no credit): same path as Stop, reason kept."""
    first = False
    with _lock:
        if not _state["stop_requested"]:
            _state["stop_requested"] = True
            _state["stop_reason"] = reason
            _state["message"] = f"Stopping: {reason}"
            log.error("sync aborted: %s", reason)
            first = True
        _rerun.clear()
    if first:
        notify.sync_aborted(reason)  # email the admin (once per hour at most)


def _run_all(sources: list[dict[str, Any]], force: bool, trigger: str) -> None:
    try:
        for src in sources:
            if _state["stop_requested"]:
                break
            with _lock:
                _state["queue"] = [n for n in _state["queue"] if n != src["name"]]
            _run_source(src, force, trigger)
        reason = _state["stop_reason"]
        message = "Sync complete" if not _state["stop_requested"] else "Sync stopped" if reason == ADMIN_STOP else f"Sync stopped: {reason}"
        _set(phase="done", message=message, current_source=None, current_file=None)
    except Exception as exc:  # pragma: no cover - defensive
        _set(phase="failed", message=f"Sync failed: {exc}")
        log.exception("sync failed")
    finally:
        _set(running=False, last_finished_at=db.now_iso(), stop_requested=False)  # uploads reuse _process_one: never leave the flag set
        _drain_rerun()


def _drain_rerun() -> None:
    """Start any sync that was requested while we were busy."""
    with _lock:
        pending = dict(_rerun)
        _rerun.clear()
    if not pending:
        return
    if None in pending:
        start(None, force=pending[None], trigger="queued")
    else:
        start([k for k in pending], force=any(pending.values()), trigger="queued")


def _run_source(src: dict[str, Any], force: bool, trigger: str) -> None:
    """Full listing + processing of one source, recorded as one sync_runs row."""
    run_id = db.start_run(src["kind"], src["id"], src["name"], force, trigger)
    why = TRIGGER_WHY.get(trigger)
    _set(phase="listing", current_source=src["name"], message=f"Listing files in {src['name']}…" + (f" ({why})" if why else ""))
    db.update_source(src["id"], last_status="syncing", last_error=None)
    try:
        source = open_source(src)
        files = list(source.list_files())
        _bump("total", len(files))
        _set(phase="processing", message=f"{len(files)} files in {src['name']}")
        db.update_run(run_id, total=len(files))
        base = dict(processed=_state["processed"], skipped=_state["skipped"], errors=_state["errors"])
        # Skipped files worth another look: photos / scans once a vision reader exists, and file types the app has
        # since learnt to read (a still-unsupported type is skipped again in a millisecond, no download involved).
        retry = ("unsupported file type", "text file too large") + ((extract.NEEDS_VISION_PREFIX,) if extract.vision_available() else ())
        # ("text file too large": the wording before spreadsheet rows were trimmed; such files may fit now)
        with ThreadPoolExecutor(max_workers=max(1, settings.get_int("concurrency", 3))) as pool:
            futures = {pool.submit(_process_one, source, meta, force, retry): meta for meta in files}
            for fut in as_completed(futures):
                if fut.cancelled():
                    continue
                meta = futures[fut]
                try:
                    fut.result()
                except Exception as exc:  # pragma: no cover - _process_one already catches
                    _note_error(meta.name, str(exc))
                db.update_run(
                    run_id, processed=_state["processed"] - base["processed"], skipped=_state["skipped"] - base["skipped"],
                    errors=_state["errors"] - base["errors"],
                )
                if _state["stop_requested"]:
                    for f in futures:
                        f.cancel()  # files not started yet; the ones in flight finish normally
                    break
        if _state["stop_requested"]:  # the pool has drained: count the files that were in flight too
            reason = _state["stop_reason"] or ADMIN_STOP
            by_admin = reason == ADMIN_STOP
            db.update_run(run_id, finished_at=db.now_iso(), status="stopped", message=reason,
                          processed=_state["processed"] - base["processed"], skipped=_state["skipped"] - base["skipped"],
                          errors=_state["errors"] - base["errors"])
            db.update_source(src["id"], last_sync_at=db.now_iso(), last_status="stopped" if by_admin else "failed",
                             last_error=None if by_admin else reason)
            log.info("%s: sync stopped (%s) after %d file(s)", src["name"], reason, _state["processed"] - base["processed"])
            return
        removed = db.mark_missing(src["id"], {m.source_id for m in files})
        if removed:
            _bump("removed", removed)
            log.info("%s: %d file(s) no longer in source, hidden from the ledger", src["name"], removed)
        n_err = _state["errors"] - base["errors"]
        db.update_run(run_id, finished_at=db.now_iso(), status="done", removed=removed)
        db.update_source(src["id"], last_sync_at=db.now_iso(), last_status="ok" if not n_err else "errors",
                         last_error=None if not n_err else f"{n_err} file(s) failed")
    except Exception as exc:
        msg = f"{type(exc).__name__}: {str(exc)[:400]}"
        db.update_run(run_id, finished_at=db.now_iso(), status="failed", message=msg)
        db.update_source(src["id"], last_sync_at=db.now_iso(), last_status="failed", last_error=msg)
        _note_error(src["name"], msg)
        log.exception("sync of %s failed", src["name"])


def _process_one(source, meta: FileMeta, force: bool, retry_prefix: str | tuple[str, ...] | None = None) -> str:
    """Read one file if needed. Returns the resulting status."""
    if _state["stop_requested"]:
        return "stopped"  # not even recorded: the next sync sees it as a new file
    doc_id = db.upsert_file_metadata(meta.as_dict())
    if not db.needs_processing(doc_id, force=force, retry_prefix=retry_prefix):
        _bump("skipped")
        return "unchanged"
    _set(current_file=meta.name)
    try:
        if meta.mime not in SUPPORTED_MIMES:
            db.mark_skipped(doc_id, f"unsupported file type ({meta.mime})")
            _bump("skipped")
            return "skipped"
        max_mb = settings.get_float("max_file_mb", 25)
        if meta.size and meta.size > max_mb * 1024 * 1024:
            db.mark_skipped(doc_id, f"file larger than {max_mb:g} MB")
            _bump("skipped")
            return "skipped"
        db.set_status(doc_id, "processing")
        content = source.fetch(meta)
        fields, model = extract.extract(meta, content)
        db.save_extraction(doc_id, fields, model)
        _bump("processed")
        log.debug("read %s -> %s %s", meta.name, fields.get("doc_type"), fields.get("total"))
        return "done"
    except ValueError as exc:  # unsupported / too large / unreadable
        db.mark_skipped(doc_id, str(exc))
        _bump("skipped")
        return "skipped"
    except extract.ApiUnavailable as exc:  # bad key / no credit: not this file's fault, stop the whole run
        db.set_status(doc_id, "pending")  # read on the next sync, no error row
        _note_error(meta.name, str(exc))
        _abort(str(exc))
        return "error"
    except Exception as exc:
        msg = f"{type(exc).__name__}: {str(exc)[:400]}"
        attempts = db.mark_error(doc_id, msg)  # counted: after MAX_ATTEMPTS the sync stops retrying this file
        _note_error(meta.name, msg)
        log.warning("failed to read %s (attempt %d): %s", meta.name, attempts, msg)
        return "error"


def reprocess(doc_id: int, provider: str | None = None) -> dict[str, Any]:
    """Re-run extraction for a single stored document (synchronously), discarding hand edits.
    ``provider`` forces a particular reader for this file (e.g. Gemini for a photo Ollama skipped)."""
    row = db.get_document(doc_id)
    if row is None:
        raise KeyError(doc_id)
    if row["source"] == "demo" or not row.get("source_ref"):
        raise RuntimeError("demo documents have no file behind them")
    src = db.get_source(row["source_ref"])
    if src is None:
        raise RuntimeError("the source for this document has been removed")
    source = open_source(src)
    meta = FileMeta(
        source=row["source"], source_ref=row["source_ref"], source_id=row["source_id"], name=row["name"], mime=row["mime"],
        folder=row["folder"], web_link=row["web_link"], size=row["size"], modified_at=row["modified_at"],
    )
    db.reset_document(doc_id)
    db.set_status(doc_id, "processing")
    try:
        content = source.fetch(meta)
        fields, model = extract.extract_with(provider, meta, content) if provider else extract.extract(meta, content)
        db.save_extraction(doc_id, fields, model)
    except ValueError as exc:
        db.mark_skipped(doc_id, str(exc))
    except Exception as exc:
        db.mark_error(doc_id, f"{type(exc).__name__}: {str(exc)[:400]}")
        raise
    return db.get_document(doc_id) or {}


# ------------------------------------------------------------------ uploads


def process_uploaded(src: dict[str, Any], files: list[tuple[str, bytes, str]]) -> list[dict[str, Any]]:
    """Store uploaded files in the source (Drive or folder) and read them right away.
    ``files`` = [(filename, bytes, mime)]. Returns one result dict per file."""
    source = open_source(src)
    results = []
    for filename, data, mime in files:
        try:
            meta = source.upload(filename, data, mime)
            status = _process_one(source, meta, force=False) if settings.llm_ready() else "pending"
            doc = db.get_document(db.upsert_file_metadata(meta.as_dict())) or {}
            results.append({"name": filename, "ok": True, "doc_id": doc.get("id"), "status": doc.get("status", status),
                            "doc_type": doc.get("doc_type"), "total": doc.get("total"), "counterparty": doc.get("counterparty"),
                            "error": doc.get("error"), "web_link": doc.get("web_link")})
        except Exception as exc:
            log.exception("upload of %s failed", filename)
            results.append({"name": filename, "ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
    return results


# ------------------------------------------------------------------ watcher / scheduler

_scheduler_started = False
LLM_STATUS_EVERY = 60  # seconds between checks of the document reader (Ollama reachability)
MIN_WATCH_SECONDS = 10  # floor for watch_interval_seconds: one delta call per source per interval is cheap for us, not for Graph if faster


def check_for_changes() -> list[int]:
    """One watcher pass: returns the ids of sources that have something new. Drive sources are asked
    through the changes feed; local folders are always re-scanned (cheap, incremental)."""
    changed: list[int] = []
    for src in db.list_sources(include_secrets=True):
        if not src["enabled"]:
            continue
        if src["kind"] == "local":
            changed.append(src["id"])
            continue
        if not src.get("token_json"):
            continue
        try:
            # Google Drive and Microsoft 365 both expose changes_since(cursor) -> (changed, new_cursor)
            cloud = open_source(src)
            has_changes, token = cloud.changes_since(src.get("drive_page_token"))
            db.update_source(src["id"], drive_page_token=token, last_change_check=db.now_iso())
            if has_changes:
                changed.append(src["id"])
        except Exception as exc:
            log.warning("change check for %s failed: %s", src["name"], exc)
            db.update_source(src["id"], last_error=f"watch: {type(exc).__name__}: {str(exc)[:200]}")
    _set(last_watch_at=db.now_iso())
    return changed


def start_scheduler() -> None:
    """Background thread: watcher every ``watch_interval_seconds`` (0 = off) and a full sync every
    ``auto_sync_minutes`` (0 = off). Both need a Claude key."""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    def loop() -> None:
        last_watch = last_full = time.monotonic()
        last_llm = -LLM_STATUS_EVERY  # first check right away
        while True:
            time.sleep(15)
            try:
                if time.monotonic() - last_llm >= LLM_STATUS_EVERY:
                    last_llm = time.monotonic()
                    llm.refresh_status()  # keeps the "Ollama connected / unreachable" badge current
                if not settings.llm_ready() or not llm.status().get("ok", True):
                    continue  # nothing to read with; the watcher would only produce failed runs
                watch_every = settings.get_int("watch_interval_seconds", 120)
                if watch_every > 0 and time.monotonic() - last_watch >= watch_every and not _state["running"]:
                    last_watch = time.monotonic()
                    changed = check_for_changes()
                    if changed:
                        start(changed, trigger="watch")
                full_every = settings.get_int("auto_sync_minutes", 0)
                if full_every > 0 and time.monotonic() - last_full >= full_every * 60:
                    last_full = time.monotonic()
                    start(trigger="schedule")
            except Exception:  # pragma: no cover - keep the thread alive
                log.exception("scheduler error")

    threading.Thread(target=loop, daemon=True, name="watcher").start()
