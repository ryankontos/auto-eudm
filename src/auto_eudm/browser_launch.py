"""Small, cross-platform helpers for opening AutoEUDM in a chosen Chrome profile."""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import webbrowser


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_PREFERENCES_PATH = PROJECT_ROOT / "results" / "web-settings.json"
OPEN_WEB_IN_EUDM_PROFILE = "open_web_in_eudm_profile"
DEFAULT_BROWSER_DEBUG_PORT = 9222


def preference_enabled(path: Path = WEB_PREFERENCES_PATH) -> bool:
    """Read the launcher preference without requiring the web server to run."""
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    return isinstance(values, dict) and values.get(OPEN_WEB_IN_EUDM_PROFILE) is True


def _first_executable(candidates: list[str | Path]) -> str | None:
    for candidate in candidates:
        value = str(candidate)
        found = shutil.which(value) if not Path(value).is_absolute() else value
        if found and Path(found).is_file():
            return found
    return None


def chrome_executable() -> str | None:
    """Find the installed Chrome executable used by the profile launcher."""
    system = platform.system()
    if system == "Darwin":
        return _first_executable(
            [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                "google-chrome",
            ]
        )
    if system == "Windows":
        candidates: list[str | Path] = ["chrome.exe", "chrome"]
        for variable in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            folder = os.getenv(variable)
            if folder:
                candidates.append(Path(folder) / "Google/Chrome/Application/chrome.exe")
        return _first_executable(candidates)
    return _first_executable(
        ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]
    )


def open_url(
    url: str,
    *,
    profile: str | None = None,
    use_profile: bool = False,
    debug_port: int = DEFAULT_BROWSER_DEBUG_PORT,
) -> bool:
    """Open a URL, optionally routing it through the configured EUDM profile.

    Passing a URL directly to Chrome lets an already-running instance of that
    profile receive it as a new tab. No ``--new-window`` flag is used, so the
    AutoEUDM tab and a temporary EUDM verification tab can share one window.
    """
    if use_profile and profile:
        executable = chrome_executable()
        if executable:
            try:
                subprocess.Popen(
                    [
                        executable,
                        f"--user-data-dir={Path(profile).expanduser()}",
                        f"--remote-debugging-port={int(debug_port)}",
                        url,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return True
            except OSError:
                pass
    return bool(webbrowser.open(url))
