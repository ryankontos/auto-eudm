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


if __name__ == "__main__":
    unittest.main()
