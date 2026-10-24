"""Expose django-service-specs specs as a Pydantic-AI toolset."""

from django_spec_agents.spec_tools.spec_capability import SpecCapability
from django_spec_agents.spec_tools.spec_toolset import SpecToolset
from django_spec_agents.spec_tools.types.agent_conventions import AgentConventions

__all__ = [
    "AgentConventions",
    "SpecCapability",
    "SpecToolset",
]
