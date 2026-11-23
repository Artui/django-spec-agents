from __future__ import annotations

from argparse import ArgumentParser
from typing import Any

from asgiref.sync import async_to_sync
from django.core.management.base import BaseCommand
from django.utils import timezone

from django_spec_agents.contrib.store.default_run_store import DefaultRunStore
from django_spec_agents.contrib.store.utils import parse_age, stale_run_logs

_DEFAULT_AGE = "24h"


class Command(BaseCommand):
    """Delete the event logs of runs that ended longer ago than the retention window."""

    help = "Delete run logs whose run ended over a window ago (default 24h); never a live run's."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--older-than",
            default=_DEFAULT_AGE,
            help=(
                "How long a log outlives its run, as a number with a unit: 30m, 24h, "
                "7d. A bare number means hours. Pass the store's log_retention. "
                f"Default {_DEFAULT_AGE}."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many logs would be deleted, and delete nothing.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        retention = parse_age(options["older_than"])
        if options["dry_run"]:
            count = stale_run_logs(timezone.now(), retention).count()
            self.stdout.write(f"Would delete {count} run log(s).")
            return
        count = async_to_sync(DefaultRunStore(log_retention=retention).prune)()
        self.stdout.write(f"Deleted {count} run log(s).")


__all__ = ["Command"]
