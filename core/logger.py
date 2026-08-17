"""Structured logging setup.

A redaction filter strips anything that looks like a bot token, an API hash or
a MongoDB password before a record is emitted, so secrets can never leak into
log files even from third-party libraries.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Iterable

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Bot tokens: 123456789:AAE...
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), "<BOT_TOKEN_REDACTED>"),
    # MongoDB credentials inside a connection string.
    (re.compile(r"(mongodb(?:\+srv)?://)[^:/\s]+:[^@\s]+@"), r"\1<CREDENTIALS_REDACTED>@"),
    # Bare 32-char hex values (API hashes).
    (re.compile(r"\b[a-f0-9]{32}\b", re.IGNORECASE), "<API_HASH_REDACTED>"),
)

_NOISY_LOGGERS = (
    "pyrogram.session",
    "pyrogram.connection",
    "pyrogram.crypto",
    "pyrogram.session.session",
    "pyrogram.session.auth",
)


def redact(text: str) -> str:
    """Replace anything that looks like a credential with a marker."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RedactSecretsFilter(logging.Filter):
    """Scrub credentials from the message of every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed record
            return True

        redacted = redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


class RedactingFormatter(logging.Formatter):
    """Final safety net.

    The filter only sees ``record.getMessage()``; a secret can still reach the
    log through an exception message rendered inside a traceback, or through a
    third-party library's own formatting.  Redacting the fully formatted string
    covers every one of those paths.
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(
    level: str = "INFO",
    log_file: str | Path | None = "opusfun.log",
    *,
    quiet_loggers: Iterable[str] = _NOISY_LOGGERS,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 3,
) -> logging.Logger:
    """Configure root logging with console + rotating file handlers."""
    numeric_level = getattr(logging, str(level).upper(), logging.INFO)

    formatter = RedactingFormatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-28s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redactor = RedactSecretsFilter()

    root = logging.getLogger()
    root.setLevel(numeric_level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    console.addFilter(redactor)
    root.addHandler(console)

    if log_file:
        try:
            path = Path(log_file)
            if path.parent and not path.parent.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            file_handler.addFilter(redactor)
            root.addHandler(file_handler)
        except OSError as exc:
            root.warning("File logging disabled (%s)", exc)

    for name in quiet_loggers:
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("pymongo").setLevel(logging.WARNING)
    logging.getLogger("motor").setLevel(logging.WARNING)

    return logging.getLogger("opusfun")
