from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from auto_eudm import local_service
from auto_eudm.web_runtime import Application


class UpdateChannelTests(unittest.TestCase):
    def test_update_restarts_a_server_running_an_older_checkout(self) -> None:
        manager = local_service.LocalServiceManager.__new__(local_service.LocalServiceManager)
        manager._lock = threading.RLock()
        manager._git_lock = threading.Lock()
        manager._state = {"updating": True}
        manager.supervised = False
        manager.server = SimpleNamespace(commit_id="old-commit", restart_requested=False)
        manager._selected_branch = lambda: "main"
        manager._shutdown_server = mock.Mock()

        def git_text(*arguments: str) -> str:
            if arguments[:2] == ("status", "--porcelain"):
                return ""
            if arguments[:2] == ("rev-parse", "--verify"):
                return "new-commit"
            if arguments[:2] == ("rev-parse", "HEAD"):
                return "new-commit"
            if arguments[:2] == ("rev-list", "--count"):
                return "0"
            raise AssertionError(arguments)

        with mock.patch.object(local_service, "_git_text", side_effect=git_text), \
             mock.patch.object(local_service, "_git", return_value=SimpleNamespace(returncode=0)), \
             mock.patch.object(local_service, "current_branch", return_value="main"):
            manager._apply_update()

        self.assertTrue(manager.server.restart_requested)
        manager._shutdown_server.assert_called_once()
        self.assertEqual(manager._state["update_message"], "Restarting with the latest version…")

    def test_update_does_not_restart_an_already_current_server(self) -> None:
        manager = local_service.LocalServiceManager.__new__(local_service.LocalServiceManager)
        manager._lock = threading.RLock()
        manager._git_lock = threading.Lock()
        manager._state = {"updating": True}
        manager.supervised = False
        manager.server = SimpleNamespace(commit_id="new-commit", restart_requested=False)
        manager._selected_branch = lambda: "main"
        manager._shutdown_server = mock.Mock()

        def git_text(*arguments: str) -> str:
            if arguments[:2] == ("status", "--porcelain"):
                return ""
            if arguments[:2] in {("rev-parse", "--verify"), ("rev-parse", "HEAD")}:
                return "new-commit"
            if arguments[:2] == ("rev-list", "--count"):
                return "0"
            raise AssertionError(arguments)

        with mock.patch.object(local_service, "_git_text", side_effect=git_text), \
             mock.patch.object(local_service, "_git", return_value=SimpleNamespace(returncode=0)), \
             mock.patch.object(local_service, "current_branch", return_value="main"):
            manager._apply_update()

        self.assertFalse(manager.server.restart_requested)
        manager._shutdown_server.assert_not_called()
        self.assertEqual(manager._state["update_message"], "Deployments is up to date on main.")

    def test_channel_names_map_to_stable_and_development_branches(self) -> None:
        self.assertEqual(local_service.branch_for_channel("stable"), "stable")
        self.assertEqual(local_service.branch_for_channel("development"), "main")
        self.assertEqual(local_service.branch_for_channel(None), "stable")

    def test_preferences_default_to_stable_and_discard_legacy_branch_choice(self) -> None:
        app = Application.__new__(Application)
        app.config = SimpleNamespace(concurrency=1)
        defaults = app._preference_defaults()

        self.assertEqual(defaults["update_channel"], "stable")
        migrated = app._normalise_preferences({"update_branch": "main"}, base=defaults)
        self.assertEqual(migrated["update_channel"], "stable")
        self.assertNotIn("update_branch", migrated)

    def test_reads_markdown_notes_from_the_incoming_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)

            def git(*arguments: str) -> str:
                result = subprocess.run(
                    ["git", "-C", str(repository), *arguments],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                return result.stdout.strip()

            git("init", "-q")
            git("config", "user.name", "AutoEUDM tests")
            git("config", "user.email", "tests@example.invalid")
            (repository / "README.md").write_text("Initial\n", encoding="utf-8")
            git("add", "README.md")
            git("commit", "-m", "Initial")
            base = git("rev-parse", "HEAD")

            notes_directory = repository / "update-notes"
            notes_directory.mkdir()
            note = notes_directory / "2026-09-23-example.md"
            note.write_text("# Example update\n\n- A brief user-facing change.\n", encoding="utf-8")
            (notes_directory / "internal.txt").write_text("Not a release note.\n", encoding="utf-8")
            git("add", "update-notes")
            git("commit", "-m", "Add notes")

            with mock.patch.object(local_service, "ROOT", repository):
                notes = local_service._update_notes_since(base, "HEAD")

        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["file"], "2026-09-23-example.md")
        self.assertIn("A brief user-facing change", notes[0]["markdown"])


if __name__ == "__main__":
    unittest.main()
