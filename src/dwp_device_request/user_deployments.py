"""Shared model and runner for CLIs that submit one user request per device."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable

from . import automate_device_request as dwp


USER_STATUSES: tuple[tuple[str, str], ...] = (
    ("Used stock", "Deployed - Existing Stock"),
    ("New stock", "Deployed - New Stock"),
    ("Loan", "Loan"),
    ("Pending return", "Pending Return"),
)


@dataclass(frozen=True)
class UserDeployment:
    serial: str
    username: str
    status: str
    group: str = "Deployments"
    source: str | None = None


@dataclass(frozen=True)
class DeploymentOutcome:
    deployment: UserDeployment
    request_id: str | None
    order_id: str | None
    error: str | None = None
    submitted: bool = False
    not_submitted_reason: str | None = None


class UserDeploymentRunner:
    def __init__(
        self,
        client: Any,
        request_for: str,
        *,
        manual_review: bool = False,
    ) -> None:
        self.client = client
        self.request_for = request_for
        self.manual_review = manual_review

    def run(self, deployments: Iterable[UserDeployment]) -> list[DeploymentOutcome]:
        jobs = list(deployments)
        outcomes: list[DeploymentOutcome] = []
        print(f"\nSubmitting {len(jobs)} request{'s' if len(jobs) != 1 else ''}...")
        for index, job in enumerate(jobs, 1):
            dwp.verbose_detail(
                self.client,
                f"[{index}/{len(jobs)}] {job.serial} → {job.username} ({job.status})",
            )
            try:
                result = dwp.deploy_device_to_user(
                    self.client,
                    serial=job.serial,
                    request_for=self.request_for,
                    deployed_to=job.username,
                    status=job.status,
                    submit=True,
                    manual_review_enabled=self.manual_review,
                )
                outcomes.append(
                    DeploymentOutcome(
                        deployment=replace(
                            job,
                            serial=result.resolved_serial or job.serial,
                            username=result.resolved_username or job.username,
                        ),
                        request_id=result.request_id,
                        order_id=result.order_id,
                        submitted=result.submitted,
                        not_submitted_reason=result.not_submitted_reason,
                    )
                )
            except dwp.DWPError as exc:
                request_id = exc.request_id if isinstance(exc, dwp.DeploymentExecutionError) else None
                outcomes.append(
                    DeploymentOutcome(job, request_id, None, error=str(exc))
                )
                print(f"Could not deploy {job.serial}: {exc}")
        return outcomes


def print_grouped_results(
    outcomes: list[DeploymentOutcome], groups: Iterable[str] | None = None
) -> None:
    print("\nResults")
    group_names = list(groups or dict.fromkeys(outcome.deployment.group for outcome in outcomes))
    for group in group_names:
        selected = [outcome for outcome in outcomes if outcome.deployment.group == group]
        print(f"\n{group}")
        if not selected:
            print("  None")
        for outcome in selected:
            serial = outcome.deployment.serial
            if outcome.error:
                request = f" (request {outcome.request_id})" if outcome.request_id else ""
                print(f"  {serial}: FAILED{request} — {outcome.error}")
            elif not outcome.submitted:
                request = f"request {outcome.request_id}" if outcome.request_id else "no request ID"
                reason = outcome.not_submitted_reason or "not submitted"
                print(f"  {serial}: NOT SUBMITTED — {request}; {reason}")
            else:
                order = f" (order {outcome.order_id})" if outcome.order_id else ""
                print(f"  {serial}: request {outcome.request_id}{order}")
    failed = sum(bool(outcome.error) for outcome in outcomes)
    held = sum(not outcome.error and not outcome.submitted for outcome in outcomes)
    submitted = len(outcomes) - failed - held
    print(f"\nCompleted: {submitted} submitted, {held} not submitted, {failed} failed, {len(outcomes)} total.")
