"""Background service controls and Git update handling for the local web UI."""

from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import threading
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CONTROL_FILE = ROOT / "results" / "auto-eudm-service-control.json"
SERVICE_LABEL = "com.ryankontos.auto-eudm"
UPDATE_INTERVAL_SECONDS = 60
GIT_TIMEOUT_SECONDS = 30
UPDATE_NOTES_DIRECTORY = "update-notes"
UPDATE_NOTES_MAX_COUNT = 20
UPDATE_NOTE_MAX_CHARACTERS = 6000


def branch_for_channel(channel: object) -> str:
    return "main" if str(channel or "").strip().casefold() == "development" else "stable"


def _update_notes_since(base_commit: str, target_ref: str) -> list[dict[str, str]]:
    """Read concise Markdown notes added or changed by incoming commits."""
    changed = _git(
        "diff", "--name-only", "--diff-filter=ACMR",
        f"{base_commit}..{target_ref}", "--", UPDATE_NOTES_DIRECTORY,
    )
    if changed.returncode != 0:
        return []
    paths = sorted(set(changed.stdout.splitlines()), reverse=True)[:UPDATE_NOTES_MAX_COUNT]
    notes: list[dict[str, str]] = []
    for path in paths:
        parts = path.split("/")
        if (
            len(parts) != 2
            or parts[0] != UPDATE_NOTES_DIRECTORY
            or not parts[1].lower().endswith(".md")
            or parts[1] in {"", ".", ".."}
        ):
            continue
        result = _git("show", f"{target_ref}:{path}", timeout=5)
        if result.returncode != 0:
            continue
        markdown = result.stdout.strip()
        if markdown:
            notes.append({
                "file": parts[1],
                "markdown": markdown[:UPDATE_NOTE_MAX_CHARACTERS],
            })
    return notes


def control_file_for_port(port: int) -> Path:
    return ROOT / "results" / f"auto-eudm-service-control-{int(port)}.json"


def _git(*arguments: str, timeout: int = GIT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(ROOT), *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def _git_text(*arguments: str, timeout: int = GIT_TIMEOUT_SECONDS) -> str:
    result = _git(*arguments, timeout=timeout)
    return result.stdout.strip() if result.returncode == 0 else ""


def current_branch() -> str:
    return _git_text("branch", "--show-current") or "main"


def valid_branch_name(branch: str) -> bool:
    value = str(branch or "").strip()
    if not value or value.startswith("-") or len(value) > 240:
        return False
    return _git("check-ref-format", "--branch", value, timeout=5).returncode == 0


def available_branches() -> list[str]:
    remote = _git_text("branch", "--remotes", "--format=%(refname:short)")
    branches = {
        line.removeprefix("origin/").strip()
        for line in remote.splitlines()
        if line.startswith("origin/") and line.strip() != "origin/HEAD"
    }
    branches = {branch for branch in branches if valid_branch_name(branch)}
    return sorted(branches, key=lambda branch: (branch != "main", branch.casefold()))


def write_control_action(action: str, path: Path | None = None) -> None:
    if action not in {"restart", "quit"}:
        raise ValueError("Unknown background service action.")
    target = path or CONTROL_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps({"action": action, "created_at": time.time()}) + "\n", encoding="utf-8")
    temporary.replace(target)


def consume_control_action(path: Path | None = None) -> str:
    target = path or CONTROL_FILE
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        target.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError):
        return ""
    action = payload.get("action", "") if isinstance(payload, dict) else ""
    return action if action in {"restart", "quit"} else ""


class LocalServiceManager:
    """Monitor a chosen origin branch and control the supervising launcher."""

    def __init__(self, app: Any, server: Any) -> None:
        self.app = app
        self.server = server
        self.supervised = bool(os.environ.get("AUTO_EUDM_SERVICE_CONTROL"))
        self.control_file = Path(os.environ["AUTO_EUDM_SERVICE_CONTROL"]) if self.supervised else CONTROL_FILE
        self._lock = threading.RLock()
        self._git_lock = threading.Lock()
        self._stop = threading.Event()
        self._check_thread: threading.Thread | None = None
        self._update_thread: threading.Thread | None = None
        self._state: dict[str, Any] = {
            "checking": False,
            "manual_check": False,
            "updating": False,
            "update_available": False,
            "update_error": "",
            "update_message": "Update status will appear here.",
            "remote_commit": "",
            "behind_count": 0,
            "update_notes": [],
        }
        self._poll_thread = threading.Thread(
            target=self._poll_updates,
            name="auto-eudm-update-monitor",
            daemon=True,
        )
        self._poll_thread.start()

    def _selected_branch(self) -> str:
        preferences = self.app.preferences_json()
        return branch_for_channel(preferences.get("update_channel"))

    def _login_agent_path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"

    def _login_agent_payload(self) -> dict[str, Any]:
        host = str(self.server.server_address[0])
        port = str(self.server.server_port)
        return {
            "Label": SERVICE_LABEL,
            "ProgramArguments": [
                sys.executable,
                str(ROOT / "start_auto_eudm.py"),
                "--service",
                "--no-open",
                "--host",
                host,
                "--port",
                port,
            ],
            "WorkingDirectory": str(ROOT),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ProcessType": "Background",
            "StandardOutPath": str(ROOT / "results" / "auto-eudm-service.log"),
            "StandardErrorPath": str(ROOT / "results" / "auto-eudm-service.log"),
        }

    def set_start_at_login(self, enabled: bool) -> dict[str, Any]:
        if sys.platform != "darwin":
            raise RuntimeError("Start at login is currently available on macOS.")
        (ROOT / "results").mkdir(parents=True, exist_ok=True)
        path = self._login_agent_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        previous_plist = path.read_bytes() if path.exists() else None
        if enabled:
            temporary = path.with_suffix(".tmp")
            temporary.write_bytes(plistlib.dumps(self._login_agent_payload(), fmt=plistlib.FMT_XML))
            temporary.replace(path)
            action = "enable"
        else:
            action = "disable"
        target = f"gui/{os.getuid()}/{SERVICE_LABEL}"
        result = subprocess.run(
            ["launchctl", action, target],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if result.returncode != 0:
            if enabled:
                if previous_plist is None:
                    path.unlink(missing_ok=True)
                else:
                    temporary = path.with_suffix(".tmp")
                    temporary.write_bytes(previous_plist)
                    temporary.replace(path)
            detail = " ".join((result.stderr or result.stdout).split())
            raise RuntimeError(detail[:240] or "macOS could not update the login setting.")
        return {"enabled": bool(enabled), "supported": True}

    def _poll_updates(self) -> None:
        while not self._stop.is_set():
            self._check_updates()
            self._stop.wait(UPDATE_INTERVAL_SECONDS)

    def request_check(self) -> dict[str, Any]:
        with self._lock:
            if self._state.get("updating"):
                return self.status()
            if self._state.get("checking"):
                self._state["manual_check"] = True
                self._state["update_message"] = "Checking for updates…"
                return self.status()
            if self._check_thread and self._check_thread.is_alive():
                self._state["manual_check"] = True
                self._state["update_message"] = "Checking for updates…"
                return self.status()
            self._check_thread = threading.Thread(
                target=self._check_updates,
                kwargs={"manual": True},
                name="auto-eudm-update-check",
                daemon=True,
            )
            self._check_thread.start()
        return self.status()

    def _check_updates(self, *, manual: bool = False) -> None:
        with self._lock:
            if self._state.get("updating"):
                return
            if self._state.get("checking"):
                if manual:
                    self._state["manual_check"] = True
                    self._state["update_message"] = "Checking for updates…"
                return
            manual = manual or bool(self._state.get("manual_check"))
            self._state["checking"] = True
            self._state["manual_check"] = manual
            if manual:
                self._state["update_error"] = ""
                self._state["update_message"] = "Checking for updates…"
        branch = self._selected_branch()
        try:
            if not valid_branch_name(branch):
                raise RuntimeError("Choose a valid Git branch in Settings.")
            with self._git_lock:
                remote = _git("remote", "get-url", "origin", timeout=5)
                if remote.returncode != 0:
                    raise RuntimeError("This project has no GitHub origin to check for updates.")
                refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
                fetched = _git("fetch", "--quiet", "origin", refspec, timeout=60)
                if fetched.returncode != 0:
                    raise RuntimeError("Could not check GitHub for updates. Check the repository connection.")
                remote_ref = f"refs/remotes/origin/{branch}"
                remote_commit = _git_text("rev-parse", "--verify", remote_ref)
                head = _git_text("rev-parse", "HEAD")
                checked_out_branch = current_branch()
                if not remote_commit or not head:
                    raise RuntimeError(f"The branch '{branch}' is not available on origin.")
                count_text = _git_text("rev-list", "--count", f"HEAD..{remote_ref}")
                behind_count = int(count_text or "0")
                changed_branch = branch != checked_out_branch
                available = behind_count > 0 or changed_branch
                local_changes = bool(_git_text("status", "--porcelain", "--untracked-files=all"))
                update_notes = _update_notes_since(head, remote_ref) if available else []
            with self._lock:
                self._state.update({
                    "branch": branch,
                    "current_branch": checked_out_branch,
                    "current_commit": head,
                    "remote_commit": remote_commit,
                    "behind_count": behind_count,
                    "update_available": available,
                    "working_tree_clean": not local_changes,
                    "update_notes": update_notes,
                    "update_message": (
                        f"An update is ready on {branch}."
                        if behind_count > 0
                        else f"Switch to the {branch} update channel."
                        if changed_branch
                        else f"AutoEUDM is up to date on {branch}."
                    ),
                    "update_error": "",
                    "last_checked": time.time(),
                })
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
            with self._lock:
                self._state.update({
                    "branch": branch,
                    "update_available": False,
                    "update_notes": [],
                    "update_error": str(exc),
                    "update_message": str(exc),
                    "last_checked": time.time(),
                })
        finally:
            with self._lock:
                self._state["checking"] = False
                self._state["manual_check"] = False

    def request_update(self) -> dict[str, Any]:
        with self._lock:
            if self._state.get("updating"):
                return self.status()
            self._state["updating"] = True
            self._state["update_error"] = ""
            self._state["update_message"] = "Preparing the update…"
            self._update_thread = threading.Thread(
                target=self._apply_update,
                name="auto-eudm-git-update",
                daemon=True,
            )
            self._update_thread.start()
        return self.status()

    def _apply_update(self) -> None:
        branch = self._selected_branch()
        try:
            with self._git_lock:
                if not valid_branch_name(branch):
                    raise RuntimeError("Choose a valid Git branch in Settings.")
                if _git_text("status", "--porcelain", "--untracked-files=all"):
                    raise RuntimeError("Commit or move local project changes before updating AutoEUDM.")
                with self._lock:
                    self._state["update_message"] = f"Downloading {branch}…"
                remote_ref = f"refs/remotes/origin/{branch}"
                fetched = _git(
                    "fetch", "--quiet", "origin",
                    f"+refs/heads/{branch}:{remote_ref}",
                    timeout=60,
                )
                if fetched.returncode != 0:
                    raise RuntimeError("Could not download the selected branch from GitHub.")
                remote_commit = _git_text("rev-parse", "--verify", remote_ref)
                if not remote_commit:
                    raise RuntimeError(f"The branch '{branch}' is not available on origin.")
                head = _git_text("rev-parse", "HEAD")
                checked_out_branch = current_branch()
                ahead_of_remote = int(_git_text("rev-list", "--count", f"HEAD..{remote_ref}") or "0")
                if branch == checked_out_branch and ahead_of_remote == 0:
                    with self._lock:
                        self._state.update({
                            "update_available": False,
                            "remote_commit": remote_commit,
                            "current_commit": head,
                            "update_notes": [],
                            "update_message": f"AutoEUDM is up to date on {branch}.",
                        })
                    return
                local_branch = _git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
                if local_branch.returncode == 0:
                    switched = _git("switch", branch)
                else:
                    switched = _git("switch", "--track", "-c", branch, f"origin/{branch}")
                if switched.returncode != 0:
                    raise RuntimeError("Could not switch to the selected branch. Check for local Git changes.")
                with self._lock:
                    self._state["update_message"] = "Installing the latest version…"
                pulled = _git("pull", "--ff-only", "origin", branch, timeout=120)
                if pulled.returncode != 0:
                    raise RuntimeError("The update could not be applied as a fast-forward. Check the selected branch.")
                new_commit = _git_text("rev-parse", "HEAD")
                if not new_commit:
                    raise RuntimeError("Git updated the files, but AutoEUDM could not confirm the new version.")
                if new_commit == head:
                    with self._lock:
                        self._state.update({
                            "branch": branch,
                            "current_branch": branch,
                            "current_commit": new_commit,
                            "remote_commit": remote_commit,
                            "behind_count": 0,
                            "update_available": False,
                            "update_notes": [],
                            "update_message": f"Switched to the {branch} update channel.",
                            "update_error": "",
                        })
                    return
            with self._lock:
                self._state.update({
                    "branch": branch,
                    "current_branch": branch,
                    "current_commit": new_commit,
                    "remote_commit": new_commit,
                    "behind_count": 0,
                    "update_available": False,
                    "update_notes": [],
                    "update_message": "Restarting with the latest version…",
                    "update_error": "",
                })
            if self.supervised:
                write_control_action("restart", self.control_file)
                self._shutdown_server()
            else:
                self.server.restart_requested = True
                self._shutdown_server()
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
            with self._lock:
                self._state.update({
                    "update_error": str(exc),
                    "update_message": str(exc),
                })
        finally:
            with self._lock:
                self._state["updating"] = False

    def request_quit(self) -> dict[str, Any]:
        if self.supervised:
            write_control_action("quit", self.control_file)
            self._shutdown_server()
        else:
            self._shutdown_server()
        return {"stopping": True}

    def _shutdown_server(self) -> None:
        timer = threading.Timer(0.4, self.server.shutdown)
        timer.name = "auto-eudm-service-shutdown"
        timer.daemon = True
        timer.start()

    def status(self) -> dict[str, Any]:
        with self._lock:
            values = dict(self._state)
        preferences = self.app.preferences_json()
        channel = str(preferences.get("update_channel") or "stable").strip().casefold()
        branch = branch_for_channel(channel)
        values["branch"] = branch
        values["update_channel"] = channel
        values["branches"] = available_branches()
        values["current_branch"] = current_branch()
        values["current_commit"] = values.get("current_commit") or _git_text("rev-parse", "HEAD")
        values["background"] = self.supervised
        values["start_at_login_supported"] = sys.platform == "darwin"
        values["start_at_login"] = bool(preferences.get("start_at_login"))
        values["active_submissions"] = self.app.jobs.active_job_count()
        return values


__all__ = [
    "CONTROL_FILE",
    "LocalServiceManager",
    "available_branches",
    "consume_control_action",
    "control_file_for_port",
    "current_branch",
    "valid_branch_name",
    "write_control_action",
]
