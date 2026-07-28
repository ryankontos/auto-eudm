"""ALM Workbook URL validation and authenticated SharePoint download support."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
import threading
import time
import traceback
from typing import Any
import urllib.parse
import urllib.request

from . import eudm_request as eudm


ROOT = Path(__file__).resolve().parents[2]
MAX_WORKBOOK_DOWNLOAD = 30 * 1024 * 1024
WORKBOOK_DOWNLOAD_TIMEOUT_MS = 20_000
WORKBOOK_DIAGNOSTIC_HTML_LIMIT = 350_000

def diagnostic_headers(headers: Any) -> dict[str, str]:
    """Only retain response metadata that helps identify a download response."""
    wanted = {"content-disposition", "content-length", "content-type", "location", "x-ms-request-id"}
    result: dict[str, str] = {}
    try:
        for key, value in dict(headers or {}).items():
            if str(key).casefold() in wanted:
                result[str(key)] = str(value)
    except Exception:
        pass
    return result


class WorkbookDownloadDiagnostics:
    """Full-fidelity diagnostics for one ALM Workbook download."""

    def __init__(self, url: str, *, headless: bool) -> None:
        folder = ROOT / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / f"{datetime.now():%Y%m%d-%H%M%S-%f}-alm-workbook-download.log"
        self._lock = threading.Lock()
        self.event(
            "diagnostic.started",
            source_url=url,
            headless_requested=headless,
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


class WorkbookDownloadRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Record each direct HTTP redirect before urllib follows it."""

    def __init__(self, diagnostics: WorkbookDownloadDiagnostics | None) -> None:
        super().__init__()
        self.diagnostics = diagnostics

    def redirect_request(
        self,
        request: Any,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> Any:
        if self.diagnostics:
            self.diagnostics.event(
                "direct_download.redirect",
                status=code,
                reason=message,
                from_url=request.full_url,
                to_url=new_url,
                headers=diagnostic_headers(headers),
            )
        return super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )


def sharepoint_download_url(raw_url: str) -> str:
    """Request a file download instead of the OneDrive/SharePoint web viewer."""
    parsed = urllib.parse.urlsplit(raw_url.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        raise eudm.EUDMError("Enter a valid https SharePoint or OneDrive workbook link.")
    host = parsed.hostname or ""
    allowed = host in {"1drv.ms", "excel.cloud.microsoft"} or host.endswith((".sharepoint.com", ".onedrive.com", ".office.com", ".live.com"))
    if not allowed:
        raise eudm.EUDMError("Use a SharePoint, OneDrive, or Office workbook link.")
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if not any(key.casefold() == "download" for key, _ in query):
        query.append(("download", "1"))
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), ""))


def wopi_file_credentials(request_url: str) -> tuple[str, str] | None:
    """Extract the authenticated file endpoint advertised to Excel Online."""
    try:
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(request_url).query,
            keep_blank_values=False,
        )
        folded = {
            str(key).casefold(): values[0]
            for key, values in query.items()
            if values
        }
        source = str(folded.get("wopisrc", "")).strip()
        token = str(folded.get("access_token", "")).strip()
        source_path = urllib.parse.urlsplit(source).path.casefold()
        if (
            source.startswith("https://")
            and "/wopi.ashx/files/" in source_path
            and token
        ):
            return source, token
    except Exception:
        pass
    return None


def workbook_filename_from_url(url: str) -> str:
    candidate = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).name
    return (
        candidate
        if candidate.casefold().endswith((".xlsx", ".xlsm"))
        else "sharepoint-workbook.xlsx"
    )


def filename_from_headers(headers: Any, fallback: str = "sharepoint-workbook.xlsx") -> str:
    disposition = str(headers.get("Content-Disposition", "")) if headers else ""
    match = re.search(r"filename\*?=(?:UTF-8''|\")?([^;\"]+)", disposition, flags=re.I)
    candidate = urllib.parse.unquote(match.group(1)).strip() if match else fallback
    candidate = Path(candidate).name or fallback
    return candidate if candidate.lower().endswith((".xlsx", ".xlsm")) else fallback


def read_workbook_response(response: Any) -> bytes:
    size = response.headers.get("Content-Length")
    if size and int(size) > MAX_WORKBOOK_DOWNLOAD:
        raise eudm.EUDMError("The linked workbook is larger than the 30 MB local limit.")
    data = response.read(MAX_WORKBOOK_DOWNLOAD + 1)
    if len(data) > MAX_WORKBOOK_DOWNLOAD:
        raise eudm.EUDMError("The linked workbook is larger than the 30 MB local limit.")
    if not data.startswith(b"PK\x03\x04"):
        raise eudm.EUDMError("The link did not return an Excel workbook.")
    return data


def download_workbook_direct(
    url: str,
    *,
    diagnostics: WorkbookDownloadDiagnostics | None = None,
) -> tuple[str, bytes]:
    download_url = sharepoint_download_url(url)
    if diagnostics:
        diagnostics.event(
            "direct_download.start",
            source_url=url,
            download_url=download_url,
            timeout_seconds=35,
        )
    request = urllib.request.Request(
        download_url,
        headers={"User-Agent": "AutoEUDM/1.0", "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*"},
    )
    started = time.monotonic()
    try:
        opener = urllib.request.build_opener(WorkbookDownloadRedirectHandler(diagnostics))
        with opener.open(request, timeout=35) as response:
            if diagnostics:
                diagnostics.event(
                    "direct_download.response",
                    final_url=response.geturl(),
                    status=getattr(response, "status", None),
                    headers=diagnostic_headers(response.headers),
                    duration_ms=round((time.monotonic() - started) * 1000),
                )
            data = read_workbook_response(response)
            filename = filename_from_headers(response.headers)
            if diagnostics:
                diagnostics.event(
                    "direct_download.complete",
                    filename=filename,
                    byte_count=len(data),
                    zip_signature=data[:4].hex(),
                )
            return filename, data
    except Exception as exc:
        if diagnostics:
            diagnostics.event(
                "direct_download.failed",
                error=repr(exc),
                traceback=traceback.format_exc(),
                duration_ms=round((time.monotonic() - started) * 1000),
            )
        raise


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


def download_workbook_with_browser(
    url: str,
    profile: str | None,
    *,
    job: Any | None = None,
    headless: bool = False,
    diagnostics: WorkbookDownloadDiagnostics | None = None,
) -> tuple[str, bytes]:
    """Download via Chrome, allowing sign-in when running visibly.

    The file is saved explicitly before closing the browser context.  This is
    important for larger workbooks: Playwright's temporary download disappears
    as soon as the context is closed.
    """
    if not profile:
        if diagnostics:
            diagnostics.event("browser.profile_missing")
        raise eudm.EUDMError("A saved browser profile is required for this private SharePoint workbook.")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        if diagnostics:
            diagnostics.event("browser.playwright_unavailable", error=repr(exc))
        raise eudm.EUDMError("Playwright could not be installed for SharePoint download.") from exc
    playwright = None
    context = None
    try:
        if diagnostics:
            diagnostics.event(
                "browser.starting",
                source_url=url,
                download_url=sharepoint_download_url(url),
                headless=headless,
                profile=profile,
                profile_exists=Path(profile).expanduser().exists(),
            )
        playwright = sync_playwright().start()
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(Path(profile).expanduser()),
            channel="chrome",
            headless=headless,
        )
        page = context.pages[0] if context.pages else context.new_page()
        wopi_credentials: dict[str, str] = {}
        attempted_wopi_tokens: set[str] = set()
        attempted_menu_downloads: set[str] = set()

        def observe_request(request: Any) -> None:
            candidate = wopi_file_credentials(request.url)
            if candidate:
                source, token = candidate
                if token != wopi_credentials.get("token"):
                    wopi_credentials.update(source=source, token=token)
                    if diagnostics:
                        diagnostics.event(
                            "browser.wopi_file_discovered",
                            source=source,
                            token_length=len(token),
                        )
            if diagnostics:
                diagnostics.event(
                    "browser.request",
                    method=request.method,
                    url=request.url,
                    resource_type=request.resource_type,
                    has_post_data=bool(request.post_data),
                )

        page.on("request", observe_request)
        if diagnostics:
            diagnostics.event(
                "browser.started",
                initial_pages=[str(item.url) for item in context.pages],
                user_agent=page.evaluate("navigator.userAgent"),
            )
            page.on("response", lambda response: diagnostics.event(
                "browser.response",
                status=response.status,
                status_text=response.status_text,
                url=response.url,
                headers=diagnostic_headers(response.headers),
            ))
            page.on("requestfailed", lambda request: diagnostics.event(
                "browser.request_failed",
                method=request.method,
                url=request.url,
                resource_type=request.resource_type,
                failure=str(request.failure),
            ))
            page.on("framenavigated", lambda frame: diagnostics.event(
                "browser.navigation",
                is_main_frame=frame == page.main_frame,
                url=frame.url,
            ))
            page.on("console", lambda message: diagnostics.event(
                "browser.console",
                level=message.type,
                text=message.text,
            ))
            page.on("pageerror", lambda error: diagnostics.event(
                "browser.page_error",
                error=repr(error),
            ))
            page.on("download", lambda item: diagnostics.event(
                "browser.download_event",
                suggested_filename=item.suggested_filename,
            ))

        def begin_download(timeout: int) -> Any:
            download_url = sharepoint_download_url(url)
            if diagnostics:
                diagnostics.event(
                    "browser.download_attempt",
                    download_url=download_url,
                    timeout_ms=timeout,
                    current_url=str(page.url),
                )
            with page.expect_download(timeout=timeout) as pending:
                try:
                    page.goto(
                        download_url,
                        wait_until="domcontentloaded",
                        timeout=timeout,
                    )
                except Exception as navigation_error:
                    if diagnostics:
                        diagnostics.event(
                            "browser.goto_exception",
                            error=repr(navigation_error),
                            current_url=str(page.url),
                        )
                        diagnostics.page_snapshot("after_navigation_exception", page)
                    # Chromium aborts a navigation once it turns into a file
                    # download. Playwright reports that normal transition as a
                    # goto error even though expect_download has captured it.
                    if "download is starting" not in str(navigation_error).casefold():
                        raise
            if diagnostics:
                diagnostics.event("browser.download_captured", current_url=str(page.url))
            return pending.value

        def download_from_wopi() -> Any | None:
            """Fetch the workbook using Excel Online's authenticated file token.

            The fetch deliberately runs in the SharePoint page. It therefore
            uses Chrome's working corporate trust store instead of Python TLS.
            Converting the response to a blob makes the completion observable
            as a normal Playwright download.
            """
            source = wopi_credentials.get("source")
            token = wopi_credentials.get("token")
            if not source or not token:
                if diagnostics:
                    diagnostics.event("browser.wopi_file_unavailable")
                return None
            if token in attempted_wopi_tokens:
                return None
            attempted_wopi_tokens.add(token)
            separator = "&" if urllib.parse.urlsplit(source).query else "?"
            contents_url = (
                f"{source.rstrip('/')}/contents{separator}"
                + urllib.parse.urlencode({"access_token": token})
            )
            filename = workbook_filename_from_url(url)
            if diagnostics:
                diagnostics.event(
                    "browser.wopi_download_attempt",
                    source=source,
                    filename=filename,
                    timeout_ms=60_000,
                )
            try:
                with page.expect_download(timeout=60_000) as pending:
                    result = page.evaluate(
                        """async ({url, filename, maxBytes}) => {
                            const response = await fetch(url, {
                                credentials: "include",
                                headers: {
                                    "Authorization": `Bearer ${new URL(url).searchParams.get("access_token")}`,
                                    "X-WOPI-MaxExpectedSize": String(maxBytes)
                                }
                            });
                            if (!response.ok) {
                                throw new Error(`WOPI file request returned HTTP ${response.status}`);
                            }
                            const blob = await response.blob();
                            if (blob.size > maxBytes) {
                                throw new Error("The linked workbook is larger than the local limit");
                            }
                            const objectUrl = URL.createObjectURL(blob);
                            const anchor = document.createElement("a");
                            anchor.href = objectUrl;
                            anchor.download = filename;
                            anchor.style.display = "none";
                            document.body.appendChild(anchor);
                            anchor.click();
                            anchor.remove();
                            setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
                            return {
                                status: response.status,
                                contentType: response.headers.get("content-type") || "",
                                byteCount: blob.size
                            };
                        }""",
                        {
                            "url": contents_url,
                            "filename": filename,
                            "maxBytes": MAX_WORKBOOK_DOWNLOAD,
                        },
                    )
                if diagnostics:
                    diagnostics.event("browser.wopi_download_captured", **result)
                return pending.value
            except Exception as exc:
                if diagnostics:
                    diagnostics.event(
                        "browser.wopi_download_failed",
                        error=repr(exc),
                        traceback=traceback.format_exc(),
                    )
                return None

        def download_from_excel_menu() -> Any | None:
            """Use Excel Online's visible Download a Copy control as a fallback."""
            attempt_key = wopi_credentials.get("token") or "before-wopi-token"
            if attempt_key in attempted_menu_downloads:
                return None
            attempted_menu_downloads.add(attempt_key)

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

            if diagnostics:
                diagnostics.event(
                    "browser.excel_menu_download_attempt",
                    current_url=str(page.url),
                    timeout_ms=60_000,
                )
            try:
                file_menu = find_control(r"^file$")
                if file_menu is None:
                    raise eudm.EUDMError("Excel Online's File menu was not available.")
                file_menu.click(timeout=10_000)
                page.wait_for_timeout(400)

                download_control = find_control(r"^download (a )?copy$")
                if download_control is None:
                    save_as = find_control(r"^save as$")
                    if save_as is None:
                        raise eudm.EUDMError(
                            "Excel Online's Download a Copy action was not available."
                        )
                    save_as.click(timeout=10_000)
                    page.wait_for_timeout(400)
                    download_control = find_control(r"^download (a )?copy$")
                if download_control is None:
                    raise eudm.EUDMError(
                        "Excel Online's Download a Copy action was not available."
                    )
                with page.expect_download(timeout=60_000) as pending:
                    download_control.click(timeout=10_000)
                if diagnostics:
                    diagnostics.event("browser.excel_menu_download_captured")
                return pending.value
            except Exception as exc:
                if diagnostics:
                    diagnostics.event(
                        "browser.excel_menu_download_failed",
                        error=repr(exc),
                        traceback=traceback.format_exc(),
                    )
                return None

        try:
            download = begin_download(WORKBOOK_DOWNLOAD_TIMEOUT_MS)
        except Exception as initial_error:
            if diagnostics:
                diagnostics.event(
                    "browser.initial_download_failed",
                    headless=headless,
                    error=repr(initial_error),
                    current_url=str(page.url),
                    sign_in_detected=browser_is_waiting_for_sign_in(page),
                )
                diagnostics.page_snapshot("after_initial_download_failure", page)
            download = download_from_wopi()
            if download is not None:
                initial_error = None
            if download is None and not headless:
                download = download_from_excel_menu()
                if download is not None:
                    initial_error = None
            if download is None and headless:
                raise eudm.EUDMError(
                    "The background ALM Workbook download needs Chrome."
                ) from initial_error
            if download is None and job is None:
                raise eudm.EUDMError(
                    "The ALM Workbook download did not start within 20 seconds."
                ) from initial_error
            if download is None:
                sign_in_detected = browser_is_waiting_for_sign_in(page)
                job.update(
                    state="waiting_for_login",
                    message=(
                        "Finish signing in in Chrome…"
                        if sign_in_detected
                        else "Waiting for workbook access in Chrome…"
                    ),
                    needs_login_confirmation=True,
                )
                if diagnostics:
                    diagnostics.event(
                        "browser.authentication_wait_started",
                        sign_in_detected=sign_in_detected,
                        timeout_seconds=300,
                    )
                deadline = time.monotonic() + 300
                confirmed = False
                while time.monotonic() < deadline:
                    page.wait_for_timeout(750)
                    if wopi_credentials:
                        download = download_from_wopi()
                        if download is not None:
                            if diagnostics:
                                diagnostics.event(
                                    "browser.authentication_completed_automatically"
                                )
                            break
                        download = download_from_excel_menu()
                        if download is not None:
                            if diagnostics:
                                diagnostics.event(
                                    "browser.authentication_completed_with_excel_menu"
                                )
                            break
                    if job.wait_for_login_confirmation(timeout=0):
                        confirmed = True
                        if diagnostics:
                            diagnostics.event(
                                "browser.login_confirmation_received",
                                current_url=str(page.url),
                            )
                            diagnostics.page_snapshot(
                                "after_login_confirmation", page
                            )
                        break
                else:
                    raise eudm.EUDMError(
                        "Workbook access was not completed within five minutes."
                    )
                job.update(
                    state="downloading",
                    message="Downloading workbook…",
                    needs_login_confirmation=False,
                )
                if confirmed:
                    # A pre-auth menu lookup can legitimately fail before the
                    # workbook UI exists; give it one fresh attempt after the
                    # user has completed sign-in.
                    attempted_menu_downloads.discard("before-wopi-token")
                    try:
                        download = begin_download(120_000)
                    except Exception:
                        download = download_from_wopi()
                        if download is None:
                            download = download_from_excel_menu()
                        if download is None:
                            raise

        # path() waits for Chromium to mark the download complete. Reading and
        # verifying that completed temporary file is the shutdown boundary.
        failure = download.failure()
        if failure:
            if diagnostics:
                diagnostics.event("browser.download_failed", failure=failure, current_url=str(page.url))
            raise eudm.EUDMError(f"The ALM Workbook download failed: {failure}")
        downloaded_path = Path(download.path())
        data = downloaded_path.read_bytes()
        if len(data) > MAX_WORKBOOK_DOWNLOAD or not data.startswith(b"PK\x03\x04"):
            if diagnostics:
                diagnostics.event(
                    "browser.download_invalid_file",
                    suggested_filename=download.suggested_filename,
                    byte_count=len(data),
                    first_bytes=data[:32].hex(),
                )
            raise eudm.EUDMError("SharePoint did not return an Excel workbook.")
        suggested = download.suggested_filename or "sharepoint-workbook.xlsx"
        filename = filename_from_headers({}, suggested)
        if diagnostics:
            diagnostics.event(
                "browser.download_complete",
                suggested_filename=suggested,
                filename=filename,
                byte_count=len(data),
                zip_signature=data[:4].hex(),
            )
        return filename, data
    except eudm.EUDMError as exc:
        if diagnostics:
            diagnostics.event("browser.failed", error=str(exc), traceback=traceback.format_exc())
        raise
    except Exception as exc:
        if diagnostics:
            diagnostics.event("browser.failed", error=repr(exc), traceback=traceback.format_exc())
        raise eudm.EUDMError("The workbook could not be downloaded with the current Chrome session.") from exc
    finally:
        # Close Chrome after every outcome. The captured file stays only in
        # Playwright's temporary download area until it has been read.
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
        if diagnostics:
            diagnostics.event("browser.stopped")


