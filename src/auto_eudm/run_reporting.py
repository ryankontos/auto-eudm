"""Small, privacy-conscious run logs and human-readable result files."""

from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path
from typing import Iterable


PROJECT_DIR = Path(__file__).resolve().parents[2]
LOGGER = logging.getLogger("auto_eudm")


def configure_logging(*, enabled: bool, command: str) -> Path | None:
    """Start an optional log without recording cookies or response bodies."""
    for handler in list(LOGGER.handlers):
        LOGGER.removeHandler(handler)
        handler.close()
    LOGGER.setLevel(logging.DEBUG)
    LOGGER.propagate = False
    if not enabled:
        LOGGER.addHandler(logging.NullHandler())
        return None
    folder = PROJECT_DIR / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.now():%Y%m%d-%H%M%S-%f}-{command}.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.info("Started %s", command)
    print(f"Detailed activity log: {path}")
    return path


def event(message: str, *args: object) -> None:
    LOGGER.info(message, *args)


def exception(message: str, *args: object) -> None:
    """Record the active exception without exposing it in the browser response."""
    LOGGER.exception(message, *args)


def network(
    method: str,
    path: str,
    *,
    status: int | None = None,
    duration_ms: int | None = None,
    transport: str,
    error: str | None = None,
) -> None:
    details = [transport, method, path]
    if status is not None:
        details.append(f"status={status}")
    if duration_ms is not None:
        details.append(f"duration_ms={duration_ms}")
    if error:
        details.append(f"error={error}")
    LOGGER.info("NETWORK %s", " ".join(details))


def write_result_file(command: str, lines: Iterable[str]) -> Path:
    folder = PROJECT_DIR / "results"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.now():%Y%m%d-%H%M%S-%f}-{command}.txt"
    content = [f"AutoEUDM results — {command}", f"Generated: {datetime.now():%Y-%m-%d %H:%M:%S}", ""]
    content.extend(lines)
    path.write_text("\n".join(content).rstrip() + "\n", encoding="utf-8")
    print(f"Results saved: {path}")
    event("Results written to %s", path)
    return path
