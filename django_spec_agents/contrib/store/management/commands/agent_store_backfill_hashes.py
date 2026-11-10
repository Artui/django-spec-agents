from __future__ import annotations

from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand

from django_spec_agents.contrib.store.models import StoredAttachment
from django_spec_agents.contrib.store.utils import hash_file


class Command(BaseCommand):
    """Fill in ``sha256`` for attachments stored before the column existed."""

    help = "Compute the missing sha256 content hashes for stored attachments."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many attachments need hashing, and change nothing.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        dry_run: bool = options["dry_run"]
        pending = StoredAttachment.objects.filter(sha256="").order_by("pk")
        if dry_run:
            self.stdout.write(f"{pending.count()} attachment(s) would be hashed.")
            return
        hashed = 0
        skipped = 0
        for attachment in pending.iterator():
            digest = self._digest(attachment)
            if digest is None:
                skipped += 1
                continue
            StoredAttachment.objects.filter(pk=attachment.pk).update(sha256=digest)
            hashed += 1
        self.stdout.write(f"Hashed {hashed} attachment(s), skipped {skipped} unreadable.")

    def _digest(self, attachment: StoredAttachment) -> str | None:
        """The row's content hash, or ``None`` when its bytes cannot be read."""
        try:
            with attachment.file.open("rb") as handle:
                return hash_file(handle)
        except (OSError, ValueError) as exc:
            self.stderr.write(f"Skipped attachment {attachment.attachment_id}: {exc}")
            return None


__all__ = ["Command"]
