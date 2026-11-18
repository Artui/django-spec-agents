from __future__ import annotations

from typing import Any

import pytest

from django_spec_agents.child_sessions.child_lineage import ChildLineage
from django_spec_agents.child_sessions.child_lineage_for import child_lineage_for
from django_spec_agents.child_sessions.child_report import ChildReport
from django_spec_agents.persistence.types.child_lineage_record import ChildLineageRecord
from django_spec_agents.persistence.types.conversation import Conversation
from django_spec_agents.persistence.types.thread_ref import ThreadRef

_ROOT, _PARENT = ThreadRef("root1", "main"), ThreadRef("p1", "main")
_RECORD = ChildLineageRecord(_ROOT, 2, ["delete", "search"], ["delete"])


@pytest.mark.parametrize(("parent", "lineage"), [(None, None), (_PARENT, None), (None, _RECORD)])
def test_a_thread_without_a_stored_lineage_and_parent_gets_nothing(
    parent: ThreadRef | None, lineage: ChildLineageRecord | None
) -> None:
    conversation = Conversation("c1", parent=parent, lineage=lineage)

    assert child_lineage_for(conversation, agent_id="research", owner="7", queue=object()) == []


def test_a_child_gets_its_bounds_and_its_report_back_without_spend() -> None:
    queue: Any = object()
    conversation = Conversation("c1", parent=_PARENT, lineage=_RECORD)

    rebuilt = child_lineage_for(conversation, agent_id="research", owner="7", queue=queue)

    assert rebuilt == [
        ChildLineage(_ROOT, _PARENT, 2, frozenset({"delete", "search"}), frozenset({"delete"})),
        ChildReport(_PARENT, "research", queue),
    ]


def test_a_child_the_user_joined_reports_that_it_did() -> None:
    conversation = Conversation("c1", parent=_PARENT, lineage=_RECORD)

    rebuilt = child_lineage_for(
        conversation, agent_id="research", owner="7", queue=object(), user_joined=True
    )

    [report] = [c for c in rebuilt if isinstance(c, ChildReport)]
    assert report.user_joined is True
