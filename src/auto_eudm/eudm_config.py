"""Shared configuration loaded from .env and process environment variables."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


# eudm_config.py lives under src/auto_eudm; shared .env belongs at
# the repository root alongside README.md and the launchers.
PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = PROJECT_DIR / ".env"


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def load_env_file(path: Path | None = None) -> Path:
    """Load KEY=VALUE entries without overwriting the real process environment."""
    selected = path or Path(os.getenv("EUDM_ENV_FILE", str(DEFAULT_ENV_FILE))).expanduser()
    if not selected.exists():
        return selected
    for number, raw in enumerate(selected.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"Invalid .env entry on line {number}: expected NAME=VALUE")
        name, value = line.split("=", 1)
        name = name.strip()
        if not name or not name.replace("_", "").isalnum() or name[0].isdigit():
            raise ValueError(f"Invalid .env variable name on line {number}")
        os.environ.setdefault(name, _unquote(value.strip()))
    return selected


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().casefold()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be true/false, yes/no, on/off, or 1/0")


@dataclass(frozen=True)
class AppConfig:
    env_file: Path
    base: str
    browser_profile: str | None
    browser_debug_port: int
    browser_headless: bool
    request_for: str | None
    city: str | None
    building: str | None
    floor: str | None
    room: str | None
    cabinet: str | None
    default_user_status: str
    default_location_status: str
    simulate: bool
    verbose: bool
    logging: bool
    concurrency: int
    manual_review: bool
    spreadsheet_import_enabled: bool

    @classmethod
    def load(cls) -> "AppConfig":
        env_file = load_env_file()

        def optional(name: str) -> str | None:
            value = os.getenv(name, "").strip()
            return value or None

        raw_concurrency = os.getenv("EUDM_CONCURRENCY", "1").strip()
        if not raw_concurrency.isdigit() or int(raw_concurrency) < 1 or int(raw_concurrency) > 50:
            raise ValueError("EUDM_CONCURRENCY must be a whole number between 1 and 50")
        raw_debug_port = os.getenv("EUDM_BROWSER_DEBUG_PORT", "9222").strip()
        if not raw_debug_port.isdigit() or not 1024 <= int(raw_debug_port) <= 65535:
            raise ValueError("EUDM_BROWSER_DEBUG_PORT must be a whole number between 1024 and 65535")

        return cls(
            env_file=env_file,
            base=os.getenv("EUDM_BASE", "https://macquarie-dwp.onbmc.com/dwp/rest").strip(),
            browser_profile=optional("EUDM_BROWSER_PROFILE") or "~/.auto-eudm-chrome",
            browser_debug_port=int(raw_debug_port),
            browser_headless=env_bool("EUDM_BROWSER_HEADLESS"),
            request_for=optional("EUDM_REQUEST_FOR"),
            city=optional("EUDM_CITY"),
            building=optional("EUDM_BUILDING"),
            floor=optional("EUDM_FLOOR"),
            room=optional("EUDM_ROOM"),
            cabinet=optional("EUDM_CABINET"),
            default_user_status=os.getenv(
                "EUDM_DEFAULT_USER_STATUS", "Deployed - Existing Stock"
            ).strip(),
            default_location_status=os.getenv(
                "EUDM_DEFAULT_LOCATION_STATUS", "Used Stock"
            ).strip(),
            simulate=env_bool("EUDM_SIMULATE"),
            verbose=env_bool("EUDM_VERBOSE"),
            logging=env_bool("EUDM_LOGGING"),
            concurrency=int(raw_concurrency),
            manual_review=env_bool("EUDM_MANUAL_REVIEW"),
            spreadsheet_import_enabled=env_bool("EUDM_ENABLE_SPREADSHEET_IMPORT"),
        )
