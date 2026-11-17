from __future__ import annotations

from typing import Any

from django.apps import apps
from django.core.management.base import BaseCommand, CommandParser

from django_spec_agents.routines.abstract_routine import AbstractRoutine
from django_spec_agents.routines.due_routines import due_routines


class Command(BaseCommand):
    help = (
        "Fire every due routine of each installed routine model whose routine_sweep() "
        "names a queue. Schedule it with your own scheduler, every minute or so."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--limit", type=int, default=100, help="Routines per model per run.")

    def handle(self, *args: Any, **options: Any) -> None:
        for model in apps.get_models():
            if not issubclass(model, AbstractRoutine) or (sweep := model.routine_sweep()) is None:
                continue
            result = due_routines(model, sweep, limit=options["limit"])
            self.stdout.write(
                f"{model._meta.label}: {len(result.fired)} fired, {len(result.missed)} missed, "
                f"{len(result.busy)} busy, {len(result.paused)} paused, "
                f"{len(result.failed)} failed"
            )
