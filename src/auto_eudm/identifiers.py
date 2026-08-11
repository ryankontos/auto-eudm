"""Shared validation rules for exact EUDM identifiers."""

from __future__ import annotations

import re
from typing import Any


SERIAL_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
LOGIN_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9._-]*")
MIN_SERIAL_LENGTH = 6


def is_serial(value: Any) -> bool:
    """Return whether *value* is a complete hostname or serial identifier."""
    return bool(
        isinstance(value, str)
        and len(value) >= MIN_SERIAL_LENGTH
        and SERIAL_PATTERN.fullmatch(value)
    )


def is_login_id(value: Any) -> bool:
    """Return whether *value* is a login ID, rather than a name or email."""
    return bool(isinstance(value, str) and LOGIN_PATTERN.fullmatch(value))
