"""Log redaction: SSIDs and session values must never reach a log line."""
from __future__ import annotations

import logging
import re

_PATTERNS = [
    re.compile(r'42\["auth".*?\]\)?', re.S),
    re.compile(r'("session"\s*:\s*")[^"]+(")', re.I),
    re.compile(r"(po_session=)[^;\s]+", re.I),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.I),
]


def redact(text: str) -> str:
    text = _PATTERNS[0].sub("<ssid redacted>", text)
    text = _PATTERNS[1].sub(r"\1<redacted>\2", text)
    text = _PATTERNS[2].sub(r"\1<redacted>", text)
    text = _PATTERNS[3].sub(r"\1<redacted>", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
        except Exception:  # never break logging
            pass
        return True


def install_redaction() -> None:
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(RedactingFilter())
    root.addFilter(RedactingFilter())
