from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from django_service_specs import DEFAULT_POOL_SEEDS, PoolSeeds, ServerArguments, UnknownArguments
from pydantic_ai.capabilities import AbstractCapability

from django_spec_agents.spec_tools.spec_toolset import (
    ExceptionHandler,
    ProgressExtractor,
    SpecSource,
    SpecToolset,
    UserExtractor,
)
from django_spec_agents.spec_tools.types.agent_conventions import AgentConventions


class SpecCapability(AbstractCapability[Any]):
    """A Pydantic-AI capability exposing specs as tools."""

    def __init__(
        self,
        specs: SpecSource,
        *,
        id: str = "spec-tools",
        defer_loading: bool = False,
        description: str | None = None,
        instructions: str | None = None,
        conventions: AgentConventions | None = None,
        get_user: UserExtractor | None = None,
        get_progress: ProgressExtractor | None = None,
        unknown_arguments: UnknownArguments = UnknownArguments.REJECT,
        tool_server_arguments: Mapping[str, ServerArguments] | None = None,
        max_retries: int = 1,
        max_result_bytes: int | None = None,
        tool_max_result_bytes: Mapping[str, int | None] | None = None,
        max_page_size: int | None = None,
        include_total: bool = False,
        dispatch_timeout: float | None = None,
        require_permissions: bool = True,
        descriptions: Mapping[str, str] | None = None,
        exception_map: Mapping[type[BaseException], ExceptionHandler] | None = None,
        pool_seeds: PoolSeeds = DEFAULT_POOL_SEEDS,
        thread_sensitive: bool = True,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        toolset = SpecToolset(
            specs,
            id=id,
            instructions=instructions,
            conventions=conventions,
            get_user=get_user,
            get_progress=get_progress,
            unknown_arguments=unknown_arguments,
            tool_server_arguments=tool_server_arguments,
            max_retries=max_retries,
            max_result_bytes=max_result_bytes,
            tool_max_result_bytes=tool_max_result_bytes,
            max_page_size=max_page_size,
            include_total=include_total,
            dispatch_timeout=dispatch_timeout,
            require_permissions=require_permissions,
            descriptions=descriptions,
            exception_map=exception_map,
            pool_seeds=pool_seeds,
            thread_sensitive=thread_sensitive,
            executor=executor,
        )
        self._configure(toolset, defer_loading=defer_loading, description=description)

    @classmethod
    def from_toolset(
        cls,
        toolset: SpecToolset,
        *,
        defer_loading: bool = False,
        description: str | None = None,
    ) -> SpecCapability:
        """Wrap an already-built ``SpecToolset`` (the compose path)."""
        self = cls.__new__(cls)
        self._configure(toolset, defer_loading=defer_loading, description=description)
        return self

    def _configure(
        self, toolset: SpecToolset, *, defer_loading: bool, description: str | None
    ) -> None:
        # ``id`` mirrors the toolset's, so the catalog entry and toolset share one identity.
        self.id = toolset.id
        self.description = description
        self.defer_loading = defer_loading
        self._toolset = toolset

    def get_toolset(self) -> SpecToolset:
        return self._toolset
