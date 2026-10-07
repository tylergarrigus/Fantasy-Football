"""Logging with mandatory secret redaction.

Every secret read through `ff.config` is registered, and this filter scrubs it
from any log record -- message, args, or formatted output. A cookie that leaks
into an Actions log is a leaked cookie: the logs are public on a public repo.
"""

from __future__ import annotations

import logging
import re
import sys

from ff.config import registered_secrets

REDACTED = "***REDACTED***"

# Belt-and-braces: catch credential shapes even if they were never registered
# (e.g. pulled straight from os.environ by a third-party library).
_PATTERNS = [
    re.compile(r"espn_s2=[^;\s&\"']+", re.I),
    re.compile(r"SWID=\{?[0-9A-Fa-f-]{8,}\}?", re.I),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\b[A-Za-z0-9_\-]{0,10}(?:token|apikey|api_key)=[^;\s&\"']+", re.I),
]


def scrub(text: str) -> str:
    """Remove known secrets from a string. Safe to call on anything."""
    if not text:
        return text
    for secret in registered_secrets():
        if secret in text:
            text = text.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = scrub(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: _scrub_any(v) for k, v in record.args.items()}
            else:
                record.args = tuple(_scrub_any(a) for a in record.args)
        return True


def _scrub_any(value: object) -> object:
    return scrub(value) if isinstance(value, str) else value


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)-22s %(message)s", "%H:%M:%S")
    )
    handler.addFilter(RedactingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # These are chatty and tell us nothing we want.
    for noisy in ("urllib3", "requests", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
