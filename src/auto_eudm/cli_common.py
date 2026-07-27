"""Reusable console prompts and runtime options for the EUDM frontends."""

from __future__ import annotations

import argparse
import urllib.parse
from typing import Any, Sequence, TypeVar

from . import eudm_request as eudm
from .eudm_config import AppConfig
from . import run_reporting


T = TypeVar("T")


class Console:
    def text(self, label: str, *, default: str | None = None) -> str:
        while True:
            suffix = f" [{default}]" if default else ""
            value = input(f"{label}{suffix}: ").strip()
            if value:
                return value
            if default:
                return default
            print("A value is required.")

    def choose(self, label: str, choices: Sequence[tuple[str, T]]) -> tuple[str, T]:
        if not choices:
            raise eudm.EUDMError(f"No choices are available for {label}")
        print(f"\n{label}")
        for index, (display, _) in enumerate(choices, 1):
            print(f"  {index}. {display}")
        while True:
            raw = input(f"Choose 1-{len(choices)}: ").strip()
            if raw.isdigit() and 1 <= int(raw) <= len(choices):
                return choices[int(raw) - 1]
            print("Enter one of the listed numbers.")

    def choose_index(self, label: str, options: Sequence[str]) -> int:
        _, selected = self.choose(label, [(value, index) for index, value in enumerate(options)])
        return selected

    def yes_no(self, label: str, *, default: bool = False) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        while True:
            value = input(f"{label} {suffix}: ").strip().casefold()
            if not value:
                return default
            if value in ("y", "yes"):
                return True
            if value in ("n", "no"):
                return False
            print("Enter y or n.")


console = Console()


def add_runtime_arguments(
    parser: argparse.ArgumentParser,
    config: AppConfig,
    *,
    include_manual_review: bool = True,
) -> None:
    parser.add_argument(
        "--browser-profile",
        default=config.browser_profile,
        help="Dedicated installed-Chrome profile for SSO (default: EUDM_BROWSER_PROFILE).",
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=config.browser_headless,
        help="Run the dedicated Chrome profile without a visible window (default: EUDM_BROWSER_HEADLESS).",
    )
    parser.add_argument(
        "--cookie-mode",
        action="store_true",
        help="Use EUDM_COOKIE instead of opening Chrome. The cookie is never saved.",
    )
    parser.add_argument(
        "--simulate",
        action=argparse.BooleanOptionalAction,
        default=config.simulate,
        help="Use the local simulator; default can be set with EUDM_SIMULATE.",
    )
    if include_manual_review:
        parser.add_argument(
            "--manual-review",
            "--review",
            "--manual",
            action=argparse.BooleanOptionalAction,
            default=config.manual_review,
            help="Approve each populated request; default can be set with EUDM_MANUAL_REVIEW.",
        )
    parser.add_argument(
        "--verbose",
        action=argparse.BooleanOptionalAction,
        default=config.verbose,
        help="Show safe internal progress; default can be set with EUDM_VERBOSE.",
    )
    parser.add_argument(
        "--logging",
        action=argparse.BooleanOptionalAction,
        default=config.logging,
        help="Write safe API/authentication activity to logs/ (default: EUDM_LOGGING).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=config.concurrency,
        choices=range(1, 21),
        metavar="1-20",
        help="Parallel user requests (default: EUDM_CONCURRENCY; review mode stays sequential).",
    )
    parser.add_argument(
        "--base",
        default=config.base,
        help="EUDM REST URL (default: EUDM_BASE).",
    )


def validate_runtime_args(args: argparse.Namespace) -> None:
    if args.cookie_mode:
        args.browser_profile = None
    parsed = urllib.parse.urlparse(args.base)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not parsed.path.rstrip("/").endswith("/rest")
    ):
        raise eudm.EUDMError("--base must be an HTTPS EUDM REST URL ending in /rest")


def open_client(args: argparse.Namespace) -> Any:
    validate_runtime_args(args)
    return eudm.open_client(
        base=args.base,
        browser_profile=args.browser_profile,
        headless=args.headless,
        simulate=args.simulate,
        verbose=args.verbose,
    )


def start_run(args: argparse.Namespace, command: str) -> None:
    """Enable the optional safe activity log before authentication starts."""
    run_reporting.configure_logging(enabled=getattr(args, "logging", False), command=command)


def request_for(args: argparse.Namespace, config: AppConfig) -> str:
    value = (getattr(args, "request_for", None) or config.request_for or "").strip()
    if not value:
        value = console.text("Request-for login ID")
    if any(character.isspace() for character in value):
        raise eudm.EUDMError("The request-for login ID cannot contain whitespace.")
    return value
