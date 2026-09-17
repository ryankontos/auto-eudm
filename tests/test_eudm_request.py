from __future__ import annotations

import unittest

from auto_eudm import eudm_request as eudm


class HelixRequestHeaderTests(unittest.TestCase):
    def test_post_headers_match_the_live_helix_site_context(self) -> None:
        headers = eudm.helix_request_headers(
            "https://macquarie-dwp.onbmc.com/dwp/rest",
            "POST",
            has_body=True,
            user_agent="Chrome test",
        )

        self.assertEqual(headers["Origin"], "https://macquarie-dwp.onbmc.com")
        self.assertEqual(headers["Referer"], "https://macquarie-dwp.onbmc.com/")
        self.assertEqual(headers["X-Requested-By"], "XMLHttpRequest")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["User-Agent"], "Chrome test")

    def test_get_headers_do_not_send_an_origin(self) -> None:
        headers = eudm.helix_request_headers(
            "https://macquarie-dwp.onbmc.com/dwp/rest",
            "GET",
            has_body=False,
            user_agent="Chrome test",
        )

        self.assertNotIn("Origin", headers)
        self.assertNotIn("Content-Type", headers)

    def test_redirects_and_unauthorised_responses_require_reauthentication(self) -> None:
        class RecordingClient(eudm.Client):
            status = 302

            def _curl_request(self, method, url, body, headers):
                return self.status, ""

        client = RecordingClient(
            "https://macquarie-dwp.onbmc.com/dwp/rest",
            "session=cookie",
        )
        with self.assertRaises(eudm.SSOExpiredError):
            client.request("GET", "v2/carts")

        client.status = 401
        with self.assertRaises(eudm.SSOExpiredError):
            client.request("GET", "v2/carts")

    def test_browser_handoff_uses_api_scoped_cookies_and_browser_identity(self) -> None:
        class Context:
            def __init__(self) -> None:
                self.urls = None

            def cookies(self, urls):
                self.urls = urls
                return [
                    {"name": "root", "value": "one", "path": "/"},
                    {"name": "api", "value": "two", "path": "/dwp/rest"},
                ]

        context = Context()
        browser = eudm.BrowserClient(
            "https://macquarie-dwp.onbmc.com/dwp/rest",
            context,
            user_agent="Chrome test",
        )

        clients = browser.parallel_clients(2)

        self.assertEqual(
            context.urls,
            ["https://macquarie-dwp.onbmc.com/dwp/rest/v2/carts"],
        )
        self.assertEqual(len(clients), 2)
        self.assertEqual(clients[0].cookie, "api=two; root=one")
        self.assertEqual(clients[0].user_agent, "Chrome test")


class BrowserAuthenticationPageTests(unittest.TestCase):
    class Page:
        def __init__(self, *, navigates: bool) -> None:
            self.url = "about:blank"
            self.navigates = navigates
            self.closed = False
            self.goto_calls: list[str] = []

        def on(self, _event, _handler) -> None:
            return None

        def goto(self, url, **_kwargs) -> None:
            self.goto_calls.append(url)
            if self.navigates:
                self.url = url

        def is_closed(self) -> bool:
            return self.closed

        def wait_for_timeout(self, _milliseconds) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    class Context:
        def __init__(self, first_page) -> None:
            self.pages = [first_page]
            self.created_pages: list[BrowserAuthenticationPageTests.Page] = []

        def new_page(self):
            page = BrowserAuthenticationPageTests.Page(navigates=True)
            self.pages.append(page)
            self.created_pages.append(page)
            return page

    def test_reuses_chromes_existing_startup_page(self) -> None:
        startup = self.Page(navigates=True)
        context = self.Context(startup)

        active = eudm.open_helix_auth_page(
            context,
            "https://macquarie-dwp.onbmc.com/dwp/app/",
        )

        self.assertIs(active, startup)
        self.assertEqual(context.created_pages, [])
        self.assertEqual(startup.goto_calls, ["https://macquarie-dwp.onbmc.com/dwp/app/"])

    def test_retries_in_a_new_tab_if_startup_page_stays_blank(self) -> None:
        startup = self.Page(navigates=False)
        context = self.Context(startup)

        active = eudm.open_helix_auth_page(
            context,
            "https://macquarie-dwp.onbmc.com/dwp/app/",
        )

        self.assertIs(active, context.created_pages[0])
        self.assertTrue(startup.closed)
        self.assertEqual(active.url, "https://macquarie-dwp.onbmc.com/dwp/app/")


if __name__ == "__main__":
    unittest.main()
