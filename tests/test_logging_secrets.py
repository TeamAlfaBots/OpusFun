"""Secrets must never reach the logs — through any path."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from core.logger import RedactingFormatter, redact, setup_logging

TOKEN = "7812345678:AAH1eXampleT0kenValueThatMustNeverAppearInLogs"
API_HASH = "0123456789abcdef0123456789abcdef"
MONGO = "mongodb+srv://dbuser:SuperSecretPw@cluster0.abcd.mongodb.net/db"
# Values logged verbatim, each paired with the fragment that must not survive.
# A bare password is deliberately excluded: with no surrounding URI context it
# is indistinguishable from an ordinary word, so it is covered by the
# connection-string test below instead.
LOGGED_SECRETS = (
    (TOKEN, TOKEN),
    (API_HASH, API_HASH),
    (MONGO, "SuperSecretPw"),
)


@pytest.fixture()
def log_file(tmp_path: Path):
    path = tmp_path / "opusfun.log"
    setup_logging("DEBUG", path)
    yield path
    logging.shutdown()
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)


def _read(path: Path) -> str:
    for handler in logging.getLogger().handlers:
        handler.flush()
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("logged,forbidden", LOGGED_SECRETS)
def test_secrets_are_redacted_from_plain_messages(log_file: Path, logged: str, forbidden: str):
    logging.getLogger("opusfun.t").info("value=%s", logged)
    assert forbidden not in _read(log_file)


def test_secret_in_exception_traceback_is_redacted(log_file: Path):
    """The nastiest path: a token embedded in an exception message."""
    log = logging.getLogger("opusfun.t")
    try:
        raise ValueError(f"request failed using {TOKEN}")
    except ValueError:
        log.exception("operation failed")

    assert TOKEN not in _read(log_file)


def test_mongo_password_is_redacted_but_host_is_kept(log_file: Path):
    logging.getLogger("opusfun.t").error("cannot reach %s", MONGO)
    content = _read(log_file)
    assert "SuperSecretPw" not in content
    assert "dbuser" not in content
    # The host stays visible: it is what makes the error diagnosable.
    assert "cluster0.abcd.mongodb.net" in content


def test_redaction_survives_third_party_preformatted_records(log_file: Path):
    logging.getLogger("pyrogram.session").warning("auth with %s", TOKEN)
    assert TOKEN not in _read(log_file)


def test_normal_messages_are_untouched(log_file: Path):
    logging.getLogger("opusfun.t").info("/slap used by 12345 in chat -100999")
    content = _read(log_file)
    assert "/slap used by 12345 in chat -100999" in content


def test_redact_helper_is_idempotent():
    once = redact(f"t={TOKEN} m={MONGO}")
    assert redact(once) == once


def test_formatter_redacts_without_a_filter():
    """The formatter alone must be sufficient (defence in depth)."""
    formatter = RedactingFormatter("%(message)s")
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "tok=%s", (TOKEN,), None)
    assert TOKEN not in formatter.format(record)
