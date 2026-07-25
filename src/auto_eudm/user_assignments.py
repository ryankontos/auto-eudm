"""Shared model and runner for CLIs that submit one user request per device."""

from __future__ import annotations

from dataclasses import dataclass, replace
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from typing import Any, Iterable

from . import eudm_request as eudm
from . import run_reporting
from . import presentation


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
        concurrency: int = 1,
    ) -> None:
        self.client = client
        self.request_for = request_for
        self.manual_review = manual_review
        self.concurrency = concurrency

    def run(self, deployments: Iterable[UserDeployment]) -> list[DeploymentOutcome]:
        jobs = list(deployments)
        print(f"\nSubmitting {len(jobs)} request{'s' if len(jobs) != 1 else ''}...")
        workers = self.concurrency
        if self.manual_review and workers > 1:
            print("  Manual review is on, so requests will run one at a time.")
            workers = 1
        if workers > 1:
            print(f"  Using up to {workers} parallel requests. Request IDs will appear as they are created.")
            clients = self.client.parallel_clients(workers)
            outcomes_by_index: dict[int, DeploymentOutcome] = {}
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(self._run_one, clients[index % workers], job, index, len(jobs)): index
                    for index, job in enumerate(jobs, 1)
                }
                for future in as_completed(futures):
                    outcomes_by_index[futures[future]] = future.result()
            return [outcomes_by_index[index] for index in range(1, len(jobs) + 1)]

        outcomes: list[DeploymentOutcome] = []
        for index, job in enumerate(jobs, 1):
            outcomes.append(self._run_one(self.client, job, index, len(jobs)))
        return outcomes

    def _run_one(
        self, client: Any, job: UserDeployment, index: int, total: int
    ) -> DeploymentOutcome:
        print(f"  [{index}/{total}] {presentation.working(f'Working on {job.serial} → {job.username}...')}", flush=True)
        started = time.monotonic()
        eudm.verbose_detail(client, f"[{index}/{total}] {job.serial} → {job.username} ({job.status})")
        try:
            result = eudm.deploy_device_to_user(
                client,
                serial=job.serial,
                request_for=self.request_for,
                deployed_to=job.username,
                status=job.status,
                submit=True,
                manual_review_enabled=self.manual_review,
                on_request_created=lambda request_id: print(
                    f"  [{index}/{total}] {presentation.working(f'Created request {request_id}; completing it...')}",
                    flush=True,
                ),
            )
            outcome = DeploymentOutcome(
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
            request_text = result.request_id or "no request ID"
            state = "Done" if result.submitted else "Held"
            formatter = presentation.success if result.submitted else presentation.held
            print(
                f"  [{index}/{total}] {formatter(f'{state} — request {request_text} ({time.monotonic() - started:.0f}s).')}",
                flush=True,
            )
            return outcome
        except eudm.EUDMError as exc:
            request_id = exc.request_id if isinstance(exc, eudm.DeploymentExecutionError) else None
            print(presentation.failure(f"Could not deploy {job.serial} after {time.monotonic() - started:.0f}s: {exc}"), flush=True)
            return DeploymentOutcome(job, request_id, None, error=str(exc))


def print_grouped_results(
    outcomes: list[DeploymentOutcome], groups: Iterable[str] | None = None, *, command: str | None = None
) -> None:
    presentation.title("Summary")
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
                print("  " + presentation.failure(f"{serial}: FAILED{request} — {outcome.error}"))
            elif not outcome.submitted:
                request = f"request {outcome.request_id}" if outcome.request_id else "no request ID"
                reason = outcome.not_submitted_reason or "not submitted"
                print("  " + presentation.held(f"{serial}: NOT SUBMITTED — {request}; {reason}"))
            else:
                order = f" (order {outcome.order_id})" if outcome.order_id else ""
                print("  " + presentation.success(f"{serial}: request {outcome.request_id}{order}"))
    failed = sum(bool(outcome.error) for outcome in outcomes)
    held = sum(not outcome.error and not outcome.submitted for outcome in outcomes)
    submitted = len(outcomes) - failed - held
    print(f"\nCompleted: {submitted} submitted, {held} not submitted, {failed} failed, {len(outcomes)} total.")
    if command:
        lines: list[str] = []
        for outcome in outcomes:
            job = outcome.deployment
            result = "FAILED" if outcome.error else "SUBMITTED" if outcome.submitted else "NOT SUBMITTED"
            lines.append(
                " | ".join(
                    (
                        result,
                        f"serial={job.serial}",
                        f"username={job.username}",
                        f"status={job.status}",
                        f"request={outcome.request_id or '-'}",
                        f"order={outcome.order_id or '-'}",
                        f"detail={outcome.error or outcome.not_submitted_reason or '-'}",
                    )
                )
            )
        run_reporting.write_result_file(command, lines)
