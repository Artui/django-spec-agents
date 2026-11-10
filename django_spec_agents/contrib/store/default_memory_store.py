from __future__ import annotations

import re
from uuid import uuid4

from asgiref.sync import sync_to_async
from django.db import transaction
from django.http import HttpRequest
from pydantic_ai import ModelRetry
from pydantic_ai_harness.memory import (
    MemoryConflictError,
    MemoryFile,
    MemoryMutation,
    MemoryOperation,
    MemoryOperationConflictError,
)

from django_spec_agents.contrib.store.models import StoredMemory, StoredMemoryOperation
from django_spec_agents.contrib.store.utils import owner_or_none

# The two literal fence tags the harness wraps injected memory in
# (``memory._capability._MEMORY_DATA_PREFIX`` / ``_SUFFIX``). Matched with
# optional internal whitespace and either slash position, because the reader
# being protected is a language model rather than an XML parser: ``< /MEMORY >``
# ends the block for it just as surely as the exact bytes do.
_FENCE_TAG_RE = re.compile(r"<\s*/?\s*memory\s*>", re.IGNORECASE)

# The column width for ``StoredMemory.path``. Every harness segment is capped at
# 200 characters but the joined path is not capped at all, so the store states
# its own limit rather than letting the database truncate or reject one.
_MAX_PATH_CHARS = 500

_DEFAULT_MAX_FILES = 64
_DEFAULT_MAX_TOTAL_CHARS = 262_144


class DefaultMemoryStore:
    """A durable, owner-scoped ``MemoryStore`` over the reference models."""

    def __init__(
        self,
        request: HttpRequest | None = None,
        *,
        allow_anonymous: bool = False,
        max_files: int = _DEFAULT_MAX_FILES,
        max_total_chars: int = _DEFAULT_MAX_TOTAL_CHARS,
    ) -> None:
        """``request`` is optional, and which of the two modes you get depends on it."""
        self._request = request
        self._allow_anonymous: bool = allow_anonymous
        self._max_files: int = max_files
        self._max_total_chars: int = max_total_chars

    # -- MemoryStore protocol -------------------------------------------------

    async def read(self, path: str, *, max_chars: int) -> MemoryFile | None:
        _validate_path(path)
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")
        return await sync_to_async(self._read)(path, max_chars)

    async def get_operation(self, operation: MemoryOperation) -> MemoryMutation | None:
        return await sync_to_async(self._get_operation)(operation)

    async def write(
        self,
        path: str,
        content: str,
        *,
        expected_version: str | None,
        operation: MemoryOperation | None = None,
    ) -> MemoryMutation:
        _validate_path(path)
        return await sync_to_async(self._write)(path, content, expected_version, operation)

    async def delete(
        self,
        path: str,
        *,
        expected_version: str | None,
        operation: MemoryOperation | None = None,
    ) -> MemoryMutation:
        _validate_path(path)
        return await sync_to_async(self._delete)(path, expected_version, operation)

    async def list_paths(self, prefix: str = "", *, limit: int) -> list[str]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        return await sync_to_async(self._list_paths)(prefix, limit)

    # -- Erasure, which the protocol does not cover ---------------------------

    @staticmethod
    def purge(owner_id: str) -> int:
        """Delete every memory row for one owner, returning how many files went."""
        StoredMemoryOperation.objects.filter(owner_id=owner_id).delete()
        deleted, _ = StoredMemory.objects.filter(owner_id=owner_id).delete()
        return deleted

    def _owner(self, path: str) -> str | None:
        """The owner every query for ``path`` filters on, or ``None`` to degrade."""
        if self._request is None:
            return path.split("/", 1)[0]
        return owner_or_none(self._request, allow_anonymous=self._allow_anonymous)

    def _read(self, path: str, max_chars: int) -> MemoryFile | None:
        owner = self._owner(path)
        if owner is None:
            return None
        row = StoredMemory.objects.filter(owner_id=owner, path=path).first()
        if row is None:
            return None
        return MemoryFile(
            content=row.content[:max_chars],
            version=row.version,
            operation_id=row.operation_id,
            truncated=len(row.content) > max_chars,
        )

    def _get_operation(self, operation: MemoryOperation) -> MemoryMutation | None:
        """A receipt lookup, which is the one call that carries no path."""
        if self._request is None:
            return self._receipt(None, operation)
        owner = self._owner("")
        if owner is None:
            return None
        return self._receipt(owner, operation)

    @staticmethod
    def _receipt(owner: str | None, operation: MemoryOperation) -> MemoryMutation | None:
        """A prior result for ``operation``, or ``None`` if it has not run here."""
        rows = StoredMemoryOperation.objects.filter(operation_id=operation.id)
        if owner is not None:
            rows = rows.filter(owner_id=owner)
        row = rows.first()
        if row is None:
            return None
        if row.fingerprint != operation.fingerprint:
            raise MemoryOperationConflictError(
                f"operation id {operation.id!r} was reused with different arguments"
            )
        return MemoryMutation(version=row.version, replayed=True, existed=row.existed)

    def _write(
        self,
        path: str,
        content: str,
        expected_version: str | None,
        operation: MemoryOperation | None,
    ) -> MemoryMutation:
        owner = self._owner(path)
        if owner is None:
            return _unpersisted_mutation()
        with transaction.atomic():
            if operation is not None and (replay := self._receipt(owner, operation)) is not None:
                return replay
            row = _locked_row(owner, path, expected_version, verb="written")
            safe = _FENCE_TAG_RE.sub(_escape_tag, content)
            self._check_ceilings(owner, path, safe, replacing=row is not None)
            version = uuid4().hex
            operation_id = None if operation is None else operation.id
            if row is None:
                StoredMemory.objects.create(
                    owner_id=owner,
                    path=path,
                    content=safe,
                    version=version,
                    operation_id=operation_id,
                )
            else:
                StoredMemory.objects.filter(pk=row.pk).update(
                    content=safe, version=version, operation_id=operation_id
                )
            mutation = MemoryMutation(version=version, replayed=False, existed=row is not None)
            self._record(owner, operation, mutation)
            return mutation

    def _delete(
        self,
        path: str,
        expected_version: str | None,
        operation: MemoryOperation | None,
    ) -> MemoryMutation:
        owner = self._owner(path)
        if owner is None:
            return MemoryMutation(version=None, replayed=False, existed=False)
        with transaction.atomic():
            if operation is not None and (replay := self._receipt(owner, operation)) is not None:
                return replay
            row = _locked_row(owner, path, expected_version, verb="deleted")
            if row is not None:
                StoredMemory.objects.filter(pk=row.pk).delete()
            mutation = MemoryMutation(version=None, replayed=False, existed=row is not None)
            self._record(owner, operation, mutation)
            return mutation

    def _list_paths(self, prefix: str, limit: int) -> list[str]:
        # The prefix stands in for the path here, so in namespace mode an *empty*
        # prefix resolves to an empty owner and matches nothing. That is the right
        # way round: without a prefix there is no scope to answer for, and the
        # alternative would be listing every namespace in the table. The harness
        # always lists within a scope, so it never asks.
        owner = self._owner(prefix)
        if owner is None:
            return []
        rows = StoredMemory.objects.filter(owner_id=owner, path__startswith=prefix)
        return list(rows.order_by("path").values_list("path", flat=True)[:limit])

    def _check_ceilings(self, owner: str, path: str, content: str, *, replacing: bool) -> None:
        """Refuse a write that would push the owner past its file or byte ceiling."""
        others = StoredMemory.objects.filter(owner_id=owner).exclude(path=path)
        if not replacing and others.count() + 1 > self._max_files:
            raise ModelRetry(
                f"This namespace already holds {self._max_files} memory files, the maximum. "
                "Delete one with `delete_memory` before creating another."
            )
        stored = sum(len(other) for other in others.values_list("content", flat=True))
        if stored + len(content) > self._max_total_chars:
            raise ModelRetry(
                f"Stored memory would exceed the {self._max_total_chars}-character total for "
                "this namespace. Remove or shorten an existing memory file first."
            )

    @staticmethod
    def _record(owner: str, operation: MemoryOperation | None, mutation: MemoryMutation) -> None:
        """Persist the idempotency receipt, when the call carried an operation."""
        if operation is None:
            return
        StoredMemoryOperation.objects.create(
            owner_id=owner,
            operation_id=operation.id,
            fingerprint=operation.fingerprint,
            version=mutation.version,
            existed=mutation.existed,
        )


def _locked_row(
    owner: str, path: str, expected_version: str | None, *, verb: str
) -> StoredMemory | None:
    """The row at ``path``, locked, once its version matches ``expected_version``."""
    row = StoredMemory.objects.select_for_update().filter(owner_id=owner, path=path).first()
    current_version = None if row is None else row.version
    if current_version != expected_version:
        raise MemoryConflictError(f"memory path {path!r} changed before it could be {verb}")
    return row


def _validate_path(path: str) -> None:
    """Reject a path the ``StoredMemory.path`` column cannot hold."""
    if len(path) > _MAX_PATH_CHARS:
        raise ValueError(f"memory path exceeds {_MAX_PATH_CHARS} characters: {path!r}")


def _escape_tag(match: re.Match[str]) -> str:
    """One fence tag with its angle brackets escaped, and nothing else touched."""
    return match.group(0).replace("<", "&lt;").replace(">", "&gt;")


def _unpersisted_mutation() -> MemoryMutation:
    """The result of a write that degraded to a no-op."""
    return MemoryMutation(version=uuid4().hex, replayed=False, existed=False)


__all__ = ["DefaultMemoryStore"]
