"""Small terminal presentation helpers with a plain-text fallback."""

from __future__ import annotations

import os
import sys
from typing import Iterable


_COLOUR = sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOUR else text


def title(text: str) -> None:
    print("\n" + _paint(f"━━ {text} ━━", "1;36"))


def success(text: str) -> str:
    return _paint(f"✓ {text}", "32")


def failure(text: str) -> str:
    return _paint(f"✗ {text}", "31")


def held(text: str) -> str:
    return _paint(f"• {text}", "33")


def working(text: str) -> str:
    return _paint(f"… {text}", "36")


def summary(title_text: str, rows: Iterable[tuple[str, str]]) -> None:
    """Print status rows as (success|failure|held, text)."""
    title(title_text)
    for state, text in rows:
        if state == "success":
            print(success(text))
        elif state == "failure":
            print(failure(text))
        else:
            print(held(text))
