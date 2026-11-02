"""Logging configuration: console + rotating file in ``LOG_DIR``.

Every module logs through ``logging.getLogger(__name__)``. In production read
``<DATA_DIR>/logs/ledger.log`` (rotates at 5 MB, 5 files kept). Set ``APP_DEBUG=1`` for DEBUG level.
"""
from __future__ import annotations

import logging
import logging.handlers

from . import config

_configured = False


def setup() -> None:
    """Idempotent: safe to call from the app lifespan, tests and scripts."""
    global _configured
    if _configured:
        return
    _configured = True
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if config.DEBUG else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    file_handler = logging.handlers.RotatingFileHandler(
        config.LOG_DIR / "ledger.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in root.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        root.addHandler(stream)
    for noisy in ("googleapiclient.discovery_cache", "httpx2", "httpcore", "anthropic._base_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
