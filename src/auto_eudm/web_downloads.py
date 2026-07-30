"""ALM Workbook URL validation and authenticated SharePoint download support."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
import threading
import tempfile
import time
import traceback
from typing import Any
import urllib.parse

from . import eudm_request as eudm


ROOT = Path(__file__).resolve().parents[2]
MAX_WORKBOOK_DOWNLOAD = 100 * 1024 * 1024
WORKBOOK_DIAGNOSTIC_HTML_LIMIT = 350_000

class WorkbookDownloadDiagnostics:
    """Full-fidelity diagnostics for one ALM Workbook download."""

    def __init__(self, url: str) -> None:
        folder = ROOT / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / f"{datetime.now():%Y%m%d-%H%M%S-%f}-alm-workbook-download.log"
        self._lock = threading.Lock()
        self.event(
            "diagnostic.started",
            source_url=url,
            mode="visible_excel_menu",
            python_version=__import__("sys").version,
        )

    def event(self, name: str, **details: Any) -> None:
        record = {
            "time": datetime.now().isoformat(timespec="milliseconds"),
            "event": name,
            **details,
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def page_snapshot(self, label: str, page: Any) -> None:
        """Capture the current page HTML for diagnosing a browser download flow."""
        try:
            content = page.content()
            truncated = len(content) > WORKBOOK_DIAGNOSTIC_HTML_LIMIT
            if truncated:
                content = content[:WORKBOOK_DIAGNOSTIC_HTML_LIMIT]
            self.event(
                "browser.page_snapshot",
                label=label,
                url=str(page.url),
                title=page.title(),
                html=content,
                html_truncated=truncated,
            )
        except Exception as exc:
            self.event("browser.page_snapshot_failed", label=label, error=repr(exc))


def sharepoint_download_url(raw_url: str) -> str:
    """Validate a SharePoint/OneDrive workbook URL for visible Chrome."""
    parsed = urllib.parse.urlsplit(raw_url.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        raise eudm.EUDMError("Enter a valid https SharePoint or OneDrive workbook link.")
    host = parsed.hostname or ""
    allowed = host in {"1drv.ms", "excel.cloud.microsoft"} or host.endswith((".sharepoint.com", ".onedrive.com", ".office.com", ".live.com"))
    if not allowed:
        raise eudm.EUDMError("Use a SharePoint, OneDrive, or Office workbook link.")
    return raw_url.strip()


def workbook_filename_from_url(url: str) -> str:
    candidate = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).name
    return (
        candidate
        if candidate.casefold().endswith((".xlsx", ".xlsm"))
        else "sharepoint-workbook.xlsx"
    )


def browser_is_waiting_for_sign_in(page: Any) -> bool:
    """Detect Microsoft authentication in any tab or embedded frame."""
    try:
        for open_page in page.context.pages:
            for frame in open_page.frames:
                parsed = urllib.parse.urlsplit(str(frame.url))
                host = (parsed.hostname or "").casefold()
                path = parsed.path.casefold()
                if host in {
                    "login.live.com",
                    "login.microsoft.com",
                    "login.microsoftonline.com",
                    "login.windows.net",
                    "account.live.com",
                }:
                    return True
                if host.endswith(".microsoftonline.com") and any(
                    marker in path for marker in ("/login", "/oauth2/", "/common/")
                ):
                    return True
                if frame.locator(
                    "input[name='loginfmt'], input[type='email'][autocomplete='username']"
                ).count() > 0:
                    return True
        return False
    except Exception:
        return False


def _download_workbook_with_browser_once(
    url: str,
    profile: str | None,
    *,
    job: Any | None = None,
    diagnostics: WorkbookDownloadDiagnostics | None = None,
    headless: bool = False,
) -> tuple[str, bytes]:
    """Run one Excel Online download attempt in either headless or visible Chrome."""
    if not profile:
        raise eudm.EUDMError(
            "A saved Chrome profile is required for this private ALM Workbook."
        )
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise eudm.EUDMError(
            "Playwright could not be installed for ALM Workbook download."
        ) from exc

    def update_job(message: str, *, waiting_for_login: bool = False) -> None:
        if job is not None:
            job.update(
                state="waiting_for_login" if waiting_for_login else "downloading",
                message=message,
            )

    playwright = None
    context = None
    page = None
    download_dir = tempfile.TemporaryDirectory(prefix="autoeudm-workbook-")
    try:
        if diagnostics:
            diagnostics.event(
                "browser.starting",
                source_url=url,
                mode="headless_excel_menu" if headless else "visible_excel_menu",
                profile=profile,
                profile_exists=Path(profile).expanduser().exists(),
            )
        playwright = sync_playwright().start()
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(Path(profile).expanduser()),
            channel="chrome",
            headless=headless,
            accept_downloads=True,
            downloads_path=download_dir.name,
        )
        page = context.pages[0] if context.pages else context.new_page()

        if diagnostics:
            diagnostics.event(
                "browser.started",
                initial_pages=[str(item.url) for item in context.pages],
                user_agent=page.evaluate("navigator.userAgent"),
            )
            page.on("framenavigated", lambda frame: diagnostics.event(
                "browser.navigation",
                is_main_frame=frame == page.main_frame,
                url=frame.url,
            ))
            page.on("download", lambda item: diagnostics.event(
                "browser.download_event",
                suggested_filename=item.suggested_filename,
            ))

        def find_control(pattern: str) -> Any | None:
            matcher = re.compile(pattern, flags=re.I)
            for frame in reversed(page.frames):
                for role in ("button", "menuitem", "tab", "link"):
                    try:
                        locator = frame.get_by_role(role, name=matcher).first
                        if locator.count() and locator.is_visible():
                            return locator
                    except Exception:
                        continue
                try:
                    locator = frame.get_by_text(matcher, exact=False).first
                    if locator.count() and locator.is_visible():
                        return locator
                except Exception:
                    continue
            return None

        update_job("Opening ALM Workbook in Chrome…")
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        if diagnostics:
            diagnostics.event("browser.workbook_opened", current_url=str(page.url))

        deadline = time.monotonic() + (60 if headless else 300)
        announced_login = False
        file_menu = None
        while time.monotonic() < deadline:
            if browser_is_waiting_for_sign_in(page):
                if headless:
                    raise eudm.EUDMError("SharePoint sign-in is needed for the background workbook download.")
                if not announced_login:
                    announced_login = True
                    update_job("Finish signing in in Chrome…", waiting_for_login=True)
                    if diagnostics:
                        diagnostics.event("browser.authentication_wait_started")
                page.wait_for_timeout(1_000)
                continue

            file_menu = find_control(r"^file$")
            if file_menu is not None:
                break
            update_job("Waiting for ALM Workbook to finish loading…")
            page.wait_for_timeout(1_000)
        if file_menu is None:
            if diagnostics:
                diagnostics.page_snapshot("excel_menu_not_ready", page)
            raise eudm.EUDMError(
                "Excel Online did not finish loading the ALM Workbook within five minutes."
            )

        # Excel can show the File tab before the workbook's commands are ready.
        # Give its last data and ribbon updates time to settle before opening it.
        update_job("Workbook loaded. Preparing Download a Copy…")
        page.wait_for_timeout(4_000)
        file_menu = find_control(r"^file$")
        if file_menu is None:
            raise eudm.EUDMError("Excel Online's File menu was no longer available.")
        file_menu.click(timeout=15_000)
        page.wait_for_timeout(600)

        # Office varies the accessible label between "Download a copy",
        # "Download a Copy", and labels containing a line break/shortcut.
        # Match the command text rather than requiring an exact whole label.
        download_pattern = r"download\s+(?:a\s+)?copy"
        download_control = find_control(download_pattern)
        if download_control is None:
            # Some Excel Online tenants put the workbook download behind a
            # second popover: File -> Create a Copy -> Download a Copy.
            create_copy = find_control(r"create\s+a\s+copy")
            if create_copy is not None:
                update_job("Opening Excel's copy options…")
                create_copy.click(timeout=15_000)
                copy_deadline = time.monotonic() + 15
                while time.monotonic() < copy_deadline and download_control is None:
                    page.wait_for_timeout(300)
                    download_control = find_control(download_pattern)

        if download_control is None:
            save_as = find_control(r"^save as$")
            if save_as is None:
                if diagnostics:
                    diagnostics.page_snapshot("excel_download_menu_missing", page)
                raise eudm.EUDMError(
                    "Excel Online did not show Download a Copy in its File menu."
                )
            save_as.click(timeout=15_000)
            page.wait_for_timeout(600)
            download_control = find_control(download_pattern)
        if download_control is None:
            if diagnostics:
                diagnostics.page_snapshot("excel_download_copy_missing", page)
                visible_commands: list[str] = []
                for frame in page.frames:
                    for role in ("button", "menuitem", "link"):
                        try:
                            visible_commands.extend(
                                text.strip()
                                for text in frame.get_by_role(role).all_text_contents()
                                if text.strip()
                            )
                        except Exception:
                            continue
                diagnostics.event(
                    "browser.visible_file_menu_commands",
                    commands=visible_commands,
                )
            raise eudm.EUDMError("Excel Online did not show Download a Copy.")

        update_job("Downloading workbook…")
        if diagnostics:
            diagnostics.event("browser.download_click", directory=download_dir.name)
        download = None
        try:
            with page.expect_download(timeout=15_000) as pending:
                download_control.click(timeout=15_000)
            download = pending.value
        except Exception as exc:
            # Excel can hand the download to Chrome without emitting a
            # Playwright download event. Watch the isolated directory too.
            if diagnostics:
                diagnostics.event("browser.download_event_unobserved", error=repr(exc))
            update_job("Waiting for Excel to finish the workbook download…")

        downloaded_path: Path | None = None
        suggested_filename = ""
        if download is not None:
            failure = download.failure()
            if failure:
                raise eudm.EUDMError(f"Excel Online could not download a copy: {failure}")
            downloaded_path = Path(download.path())
            suggested_filename = download.suggested_filename or ""
        else:
            deadline = time.monotonic() + 120
            last_size = -1
            stable_reads = 0
            while time.monotonic() < deadline:
                candidates = sorted(
                    (path for path in Path(download_dir.name).iterdir()
                     if path.is_file() and path.suffix.casefold() in {".xlsx", ".xlsm"}),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )
                if candidates:
                    candidate = candidates[0]
                    size = candidate.stat().st_size
                    if size > 0 and size == last_size:
                        stable_reads += 1
                    else:
                        stable_reads = 0
                    last_size = size
                    if stable_reads >= 2:
                        downloaded_path = candidate
                        suggested_filename = candidate.name
                        break
                page.wait_for_timeout(500)
            if downloaded_path is None:
                if diagnostics:
                    diagnostics.event(
                        "browser.download_file_missing",
                        directory=download_dir.name,
                        files=[path.name for path in Path(download_dir.name).iterdir()],
                    )
                raise eudm.EUDMError("Excel Online did not finish downloading the workbook within two minutes.")

        data = downloaded_path.read_bytes()
        if len(data) > MAX_WORKBOOK_DOWNLOAD or not data.startswith(b"PK\x03\x04"):
            raise eudm.EUDMError("Excel Online did not return an Excel workbook.")
        suggested_filename = suggested_filename or workbook_filename_from_url(url)
        filename = Path(suggested_filename).name
        if not filename.lower().endswith((".xlsx", ".xlsm")):
            filename = workbook_filename_from_url(url)
        if diagnostics:
            diagnostics.event(
                "browser.download_complete",
                filename=filename,
                byte_count=len(data),
                zip_signature=data[:4].hex(),
            )
        return filename, data
    except eudm.EUDMError:
        raise
    except Exception as exc:
        if diagnostics:
            diagnostics.event("browser.failed", error=repr(exc), traceback=traceback.format_exc())
            if page is not None:
                diagnostics.page_snapshot("browser_failure", page)
        raise eudm.EUDMError(
            "Chrome could not download a copy of the ALM Workbook."
        ) from exc
    finally:
        if context:
            try:
                if diagnostics:
                    diagnostics.event("browser.context_closing")
                context.close()
            except Exception:
                pass
        if playwright:
            try:
                playwright.stop()
            except Exception:
                pass
        download_dir.cleanup()
        if diagnostics:
            diagnostics.event("browser.stopped")


def download_workbook_with_browser(
    url: str,
    profile: str | None,
    *,
    job: Any | None = None,
    diagnostics: WorkbookDownloadDiagnostics | None = None,
    headless: bool = False,
) -> tuple[str, bytes]:
    """Download through Excel's menu with a few paced browser retries."""
    last_error: eudm.EUDMError | None = None
    for attempt in range(1, 4):
        attempt_headless = headless and attempt == 1
        if attempt > 1:
            time.sleep(3)
            if job is not None:
                job.update(
                    state="downloading",
                    message=f"Retrying workbook download ({attempt}/3)…",
                )
            if diagnostics:
                diagnostics.event(
                    "browser.retry_started",
                    attempt=attempt,
                    headless=attempt_headless,
                )
        try:
            return _download_workbook_with_browser_once(
                url,
                profile,
                job=job,
                diagnostics=diagnostics,
                headless=attempt_headless,
            )
        except eudm.EUDMError as exc:
            last_error = exc
            if diagnostics:
                diagnostics.event(
                    "browser.attempt_failed",
                    attempt=attempt,
                    headless=attempt_headless,
                    error=str(exc),
                )
            if attempt == 1 and headless and job is not None:
                job.update(
                    state="downloading",
                    message="Opening Chrome to finish the workbook download…",
                )
    assert last_error is not None
    raise last_error
