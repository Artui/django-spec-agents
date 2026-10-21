"""Expose djangorestframework-services specs as a Pydantic-AI toolset."""

from django_spec_agents.spec_tools.spec_capability import SpecCapability
from django_spec_agents.spec_tools.spec_toolset import SpecToolset
from django_spec_agents.spec_tools.types.agent_conventions import AgentConventions
from django_spec_agents.spec_tools.types.agent_deps import AgentDeps
from django_spec_agents.spec_tools.types.query_param import QueryParam
from django_spec_agents.spec_tools.types.url_kwarg import UrlKwarg

__all__ = [
    "AgentConventions",
    "AgentDeps",
    "QueryParam",
    "SpecCapability",
    "SpecToolset",
    "UrlKwarg",
]
