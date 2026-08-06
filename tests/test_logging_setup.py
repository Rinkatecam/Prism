"""Durable file-logging regression test (council audit P0).

Logging was stdout-only, so post-incident forensics scrolled off the terminal
and the emitted X-Request-ID correlation was wasted. Importing app must now
attach a RotatingFileHandler writing to data/logs/prism.log.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import app


def test_rotating_file_handler_attached_to_root():
    handlers = logging.getLogger().handlers
    rfh = [h for h in handlers if isinstance(h, RotatingFileHandler)]
    assert rfh, "expected a durable RotatingFileHandler on the root logger"
    # rotation must be bounded, not unbounded growth
    assert rfh[0].maxBytes > 0
    assert rfh[0].backupCount >= 1


def test_log_file_created_under_data_logs():
    p = Path(app.__file__).parent / "data" / "logs" / "prism.log"
    assert p.exists(), f"expected durable log at {p}"
