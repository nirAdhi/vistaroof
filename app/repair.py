"""Repairs that need no model call: re-derive what the stored model answer already contains.

``repair_dates()``: a document whose issue / due date is empty, but whose stored raw answer holds a
date the parser did not accept at the time (``14/02/2026``, ``14 Feb 2026`` — small models write
what the document shows), gets the date now via ``extract._date``. Hand-edited and demo documents
are left alone. Runs once in the background at start-up and from Admin → Data → "Repair dates".
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any

from . import db, extract

log = logging.getLogger(__name__)


def repair_dates() -> dict[str, Any]:
    checked = updated = 0
    for row in db.undated_documents_with_raw():
        checked += 1
        try:
            raw = json.loads(row["raw"] or "{}")
        except ValueError:
            continue
        if not isinstance(raw, dict):
            continue
        issue = row["issue_date"] or extract._date(raw.get("issue_date"))
        due = row["due_date"] or extract._date(raw.get("due_date"))
        if issue != row["issue_date"] or due != row["due_date"]:
            db.set_dates(int(row["id"]), issue, due)
            updated += 1
    if updated:
        log.info("date repair: %d of %d undated documents now carry the date found in their stored answer", updated, checked)
    return {"checked": checked, "updated": updated}


def run_in_background() -> None:
    threading.Thread(target=lambda: _safe(repair_dates), daemon=True, name="repair-dates").start()


def _safe(fn) -> None:
    try:
        fn()
    except Exception:  # pragma: no cover - never take the app down for a repair
        log.exception("repair failed")
