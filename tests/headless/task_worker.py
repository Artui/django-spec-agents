from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.core.serializers.json import DjangoJSONEncoder
from django_service_specs import (
    NotPermitted,
    PrincipalUnavailable,
    build_call_context,
    check_call,
    dispatch,
    present,
    resolve_principal,
)

from django_spec_agents.headless.types.firing_state import FiringState

_LIVE = {"PENDING", "RUNNING"}
_LAPSED = {"ABANDONED", "AUTHORIZATION_LAPSED"}


@dataclass
class TaskRow:
    spec_name: str
    params: dict[str, Any]
    actor_pk: str | None
    state: str = "PENDING"
    attempts: int = 0
    lease_expires_at: datetime | None = None
    output: Any = None


@dataclass
class TaskWorker:
    """spec-tasks' enqueue, run_spec and sweep (baf4fad) as a FiringQueue, in their shape:
    checked at enqueue, JSON both ways, the actor as its pk, sync dispatch with no transaction,
    and only an Exception ends a run, so a BaseException leaves it RUNNING for the sweep."""

    specs: Mapping[str, Any]
    clock: Callable[[], datetime]
    lease: timedelta = timedelta(minutes=5)
    rows: dict[str, TaskRow] = field(default_factory=dict)

    def enqueue(self, spec_name: str, params: Mapping[str, Any], *, actor: Any) -> str:
        context = build_call_context("task", base={"carried": {}})
        check_call(self.specs[spec_name], principal=actor, arguments=params, call_context=context)
        run_id = str(len(self.rows) + 1)
        actor_pk = None if actor is None else str(actor.pk)
        self.rows[run_id] = TaskRow(spec_name, json.loads(json.dumps(dict(params))), actor_pk)
        return run_id

    def state(self, run_id: str) -> FiringState | None:
        row = self.rows.get(run_id)
        if row is None:
            return None
        if row.state in _LIVE:
            return FiringState.PENDING
        return FiringState.LAPSED if row.state in _LAPSED else FiringState.FINISHED

    def run(self, run_id: str) -> None:
        row = self.rows[run_id]
        if row.state != "PENDING":
            return
        row.state, row.attempts = "RUNNING", row.attempts + 1
        row.lease_expires_at = self.clock() + self.lease
        spec = self.specs[row.spec_name]
        try:
            principal = None if row.actor_pk is None else resolve_principal(row.actor_pk)
            result = dispatch(
                spec,
                principal=principal,
                arguments=row.params,
                call_context=build_call_context("task", base={"carried": {}}),
            )
            output = present(spec, result)
        except (NotPermitted, PrincipalUnavailable):
            row.state = "AUTHORIZATION_LAPSED"
            return
        except Exception:
            row.state = "FAILED"
            return
        row.state, row.output = "SUCCEEDED", json.loads(json.dumps(output, cls=DjangoJSONEncoder))

    def sweep(self) -> None:
        for row in self.rows.values():
            if row.state == "RUNNING" and row.lease_expires_at < self.clock():
                row.state = "ABANDONED"
