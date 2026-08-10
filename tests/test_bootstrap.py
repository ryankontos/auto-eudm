from __future__ import annotations

import unittest

from auto_eudm.bootstrap import browser_runtime_required


class BrowserRuntimeTests(unittest.TestCase):
    def test_live_default_requires_browser_runtime(self) -> None:
        self.assertTrue(
            browser_runtime_required([], default_simulate=False)
        )

    def test_simulation_skips_browser_runtime(self) -> None:
        self.assertFalse(
            browser_runtime_required(["--simulate"], default_simulate=False)
        )

    def test_last_simulation_switch_wins_like_argparse(self) -> None:
        self.assertFalse(
            browser_runtime_required(
                ["--no-simulate", "--simulate"],
                default_simulate=False,
            )
        )
        self.assertTrue(
            browser_runtime_required(
                ["--simulate", "--no-simulate"],
                default_simulate=True,
            )
        )

    def test_dry_run_never_requires_browser_runtime(self) -> None:
        self.assertFalse(
            browser_runtime_required(
                ["--no-simulate", "--dry-run"],
                default_simulate=False,
                dry_run_flags=("--dry-run",),
            )
        )


if __name__ == "__main__":
    unittest.main()
