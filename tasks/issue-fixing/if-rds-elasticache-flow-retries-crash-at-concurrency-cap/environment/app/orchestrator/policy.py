"""Flow-run orchestration rules.

A proposed state goes through the rules of a policy in priority order. Each rule that applies to
the transition (initial type -> proposed type) gets `before_transition` before the new state is
written and `after_transition` after it, inside the same Postgres transaction. A rule may reject
the proposal and substitute a state of its own (the run then takes that state and the client is
told REJECT) or abort it (nothing is written). Universal transforms run for every transition.

`force=True` proposals (operators, sweepers) skip the core rules and go through MINIMAL_POLICY.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

import psycopg

from . import slots
from .config import Settings
from .leases import LeaseStorage
from .pause import pause_point
from .states import (
    CANCELLED,
    CANCELLING,
    FAILED,
    PENDING,
    RUNNING,
    SCHEDULED,
    TERMINAL_TYPES,
    ALL_TYPES,
    State,
    utcnow,
)

logger = logging.getLogger("orchestrator.policy")

ACCEPT = "ACCEPT"
REJECT = "REJECT"
ABORT = "ABORT"


class OrchestrationContext:
    def __init__(
        self,
        conn: psycopg.Connection,
        leases: LeaseStorage,
        settings: Settings,
        run: dict[str, Any],
        initial_state: State | None,
        proposed_state: State,
    ):
        self.conn = conn
        self.leases = leases
        self.settings = settings
        self.run = run
        self.initial_state = initial_state
        self.proposed_state: State | None = proposed_state
        self.response_status = ACCEPT
        self.reason: str | None = None
        # column -> value updates for flow_run written with the validated state
        self.run_updates: dict[str, Any] = {}

    @property
    def initial_state_type(self) -> str | None:
        return self.initial_state.type if self.initial_state else None

    @property
    def proposed_state_type(self) -> str | None:
        return self.proposed_state.type if self.proposed_state else None

    def deployment(self) -> dict[str, Any] | None:
        if not self.run.get("deployment_id"):
            return None
        return self.conn.execute(
            """
            SELECT d.id, d.concurrency_limit_id, d.collision_strategy, c."limit" AS lim
              FROM deployment d LEFT JOIN concurrency_limit_v2 c ON c.id = d.concurrency_limit_id
             WHERE d.id = %s
            """,
            (self.run["deployment_id"],),
        ).fetchone()


class BaseRule:
    FROM_STATES: set[str | None] = ALL_TYPES | {None}
    TO_STATES: set[str | None] = ALL_TYPES
    universal = False

    def __init__(self, context: OrchestrationContext, from_type: str | None, to_type: str | None):
        self.context = context
        self.from_type = from_type
        self.to_type = to_type

    def applies(self) -> bool:
        return self.universal or (self.from_type in self.FROM_STATES and self.to_type in self.TO_STATES)

    def nullified_transition(self) -> bool:
        return self.context.response_status == ABORT or self.context.proposed_state is None

    def reject_transition(self, state: State, reason: str) -> None:
        self.context.proposed_state = state
        self.context.response_status = REJECT
        self.context.reason = reason

    def abort_transition(self, reason: str) -> None:
        self.context.proposed_state = None
        self.context.response_status = ABORT
        self.context.reason = reason

    def before_transition(self) -> None:
        pass

    def after_transition(self) -> None:
        pass


class PreserveDeploymentConcurrencyLeaseId(BaseRule):
    """Carry the deployment concurrency lease id forward when the proposal does not name one.

    Clients send lease_id: null on most proposals; without this the id would be dropped from the
    run's state on the next transition.
    """

    universal = True

    def before_transition(self) -> None:
        ctx = self.context
        if ctx.initial_state is None or ctx.proposed_state is None:
            return
        if ctx.initial_state.lease_id is not None and ctx.proposed_state.lease_id is None:
            ctx.proposed_state.lease_id = ctx.initial_state.lease_id


class HandleFlowTerminalStateTransitions(BaseRule):
    """A run that reached a terminal state stays there unless the proposal is forced."""

    FROM_STATES = set(TERMINAL_TYPES)
    TO_STATES = set(ALL_TYPES)

    def before_transition(self) -> None:
        self.abort_transition(f"This run is already {self.context.initial_state.name}.")


class PreventPendingTransitions(BaseRule):
    """Only one worker may move a run to PENDING; a run already past SCHEDULED is not resubmitted."""

    FROM_STATES = {PENDING, RUNNING, CANCELLING}
    TO_STATES = {PENDING}

    def before_transition(self) -> None:
        self.abort_transition(f"This run is already {self.context.initial_state.name}.")


class SecureFlowConcurrencySlots(BaseRule):
    """Enforce deployment concurrency limits.

    The slot is taken on the transition into PENDING, so a worker secures it before it provisions
    infrastructure. A lease is created for the slot and its id stamped on the PENDING state; the
    run's process keeps the lease alive by renewing it.
    """

    FROM_STATES = (ALL_TYPES | {None}) - {PENDING, RUNNING, CANCELLING}
    TO_STATES = {PENDING}

    def before_transition(self) -> None:
        ctx = self.context
        deployment = ctx.deployment()
        if ctx.run.get("deployment_id") and deployment is None:
            self.abort_transition("Deployment not found.")
            return
        if not deployment or not deployment["concurrency_limit_id"]:
            return
        if deployment["lim"] == 0:
            self.abort_transition(
                "The deployment concurrency limit is 0. The flow will deadlock if submitted again."
            )
            return

        limit_id = deployment["concurrency_limit_id"]
        if slots.bulk_increment_active_slots(ctx.conn, [limit_id], 1):
            lease = ctx.leases.create_lease(
                resource_ids=[limit_id],
                ttl=timedelta(seconds=ctx.settings.lease_ttl_s),
                slots=1,
            )
            ctx.proposed_state.lease_id = lease.id
            return

        if deployment["collision_strategy"] == "CANCEL_NEW":
            self.reject_transition(
                State(CANCELLED, "Cancelled", message="Deployment concurrency limit reached."),
                reason="Deployment concurrency limit reached.",
            )
        else:
            self.reject_transition(
                State(
                    SCHEDULED,
                    "AwaitingConcurrencySlot",
                    scheduled_time=utcnow() + timedelta(seconds=ctx.settings.slot_wait_s),
                ),
                reason="Deployment concurrency limit reached.",
            )


class RetryFailedFlows(BaseRule):
    """Reject a failure into AwaitingRetry while the run has retries left.

    The engine that proposed the failure retries in the same process: it waits until the
    AwaitingRetry state's scheduled time and proposes RUNNING again.
    """

    FROM_STATES = {RUNNING}
    TO_STATES = {FAILED}

    def before_transition(self) -> None:
        ctx = self.context
        run = ctx.run
        if run["retries"] is None or run["run_count"] > run["retries"]:
            # Clear the retry type so later infrastructure-level retries start clean.
            ctx.run_updates["retry_type"] = None
            return

        ctx.run_updates["retry_type"] = "in_process"
        retry_state = State(
            SCHEDULED,
            "AwaitingRetry",
            scheduled_time=utcnow() + timedelta(seconds=run["retry_delay_s"] or 0),
            message=ctx.proposed_state.message,
        )
        self.reject_transition(retry_state, reason="Retrying")


def _release_concurrency_lease(
    conn: psycopg.Connection,
    leases: LeaseStorage,
    lease_id: uuid.UUID,
    fallback_concurrency_limit_ids: list[uuid.UUID] | None = None,
    fallback_slots: int = 1,
) -> None:
    lease = leases.read_lease(lease_id)
    if not lease:
        logger.warning("Lease %s not found during release", lease_id)
        if fallback_concurrency_limit_ids:
            if not slots.bulk_decrement_active_slots(conn, fallback_concurrency_limit_ids, fallback_slots):
                raise RuntimeError(f"Failed to release concurrency slots for lease {lease_id}")
        return

    if not slots.bulk_decrement_active_slots(conn, lease.resource_ids, lease.slots):
        raise RuntimeError(f"Failed to release concurrency slots for lease {lease_id}")
    leases.revoke_lease(lease.id)
    pause_point("release", lease_id=str(lease_id))


class ReleaseFlowConcurrencySlots(BaseRule):
    """Release the deployment concurrency slot held by a flow run.

    Runs after any transition out of RUNNING, CANCELLING or PENDING into another kind of state.
    """

    universal = True

    def after_transition(self) -> None:
        if self.nullified_transition():
            return
        ctx = self.context
        if not (
            ctx.initial_state_type in {RUNNING, CANCELLING, PENDING}
            and ctx.proposed_state_type not in {PENDING, RUNNING, CANCELLING}
        ):
            return
        if not ctx.run.get("deployment_id"):
            return

        if ctx.initial_state and ctx.initial_state.lease_id:
            deployment = ctx.deployment()
            fallback = None
            if deployment and deployment["concurrency_limit_id"]:
                fallback = [deployment["concurrency_limit_id"]]
            _release_concurrency_lease(
                ctx.conn, ctx.leases, ctx.initial_state.lease_id, fallback_concurrency_limit_ids=fallback
            )
            return

        deployment = ctx.deployment()
        if not deployment or not deployment["concurrency_limit_id"]:
            return
        if not slots.bulk_decrement_active_slots(ctx.conn, [deployment["concurrency_limit_id"]], 1):
            raise RuntimeError("Failed to release deployment concurrency slots")


CORE_POLICY: list[type[BaseRule]] = [
    PreserveDeploymentConcurrencyLeaseId,
    HandleFlowTerminalStateTransitions,
    PreventPendingTransitions,
    SecureFlowConcurrencySlots,
    RetryFailedFlows,
    ReleaseFlowConcurrencySlots,
]

MINIMAL_POLICY: list[type[BaseRule]] = [
    ReleaseFlowConcurrencySlots,
]


def orchestrate(context: OrchestrationContext, policy: list[type[BaseRule]], write_state) -> None:
    """Run `policy` over the context's transition; `write_state(context)` persists the result."""
    from_type = context.initial_state_type
    to_type = context.proposed_state_type
    entered: list[BaseRule] = []
    for rule_type in policy:
        rule = rule_type(context, from_type, to_type)
        if not rule.applies():
            continue
        # Once a transition is rejected or aborted only universal transforms still see it.
        if context.response_status != ACCEPT and not rule.universal:
            continue
        rule.before_transition()
        entered.append(rule)

    if context.response_status != ABORT and context.proposed_state is not None:
        write_state(context)

    for rule in entered:
        rule.after_transition()
