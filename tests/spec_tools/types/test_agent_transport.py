from __future__ import annotations

import django_service_specs

import django_spec_agents.spec_tools as spec_tools
from django_spec_agents.spec_tools import types as spec_tools_types
from django_spec_agents.spec_tools.types import agent_transport


def test_the_value_is_the_plain_string_agent():
    """A multi-transport callable compares the plain string, not an imported name."""
    assert agent_transport.AGENT_TRANSPORT == "agent"
    assert type(agent_transport.AGENT_TRANSPORT) is str


def test_the_types_package_exports_the_name():
    """``types/__init__`` lists every public name its leaf modules define."""
    assert "AGENT_TRANSPORT" in spec_tools_types.__all__
    assert spec_tools_types.AGENT_TRANSPORT is agent_transport.AGENT_TRANSPORT


def test_every_name_the_types_package_lists_resolves():
    assert all(hasattr(spec_tools_types, name) for name in spec_tools_types.__all__)


def test_the_package_re_exports_the_very_value_the_leaf_defines():
    """The toolset stamps the leaf's value; the package a consumer imports from holds it."""
    assert spec_tools.AGENT_TRANSPORT is agent_transport.AGENT_TRANSPORT


def test_the_kernel_lists_no_transports():
    """The value belongs to the package that stamps it, not to the kernel."""
    assert not hasattr(django_service_specs, "AGENT_TRANSPORT")
