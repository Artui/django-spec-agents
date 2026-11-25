from __future__ import annotations

from django_spec_agents import PromptMetadataRunner


class _Flagged:
    accepts_prompt_metadata = False


def test_the_flag_makes_the_shape_and_its_value_is_the_opt_in() -> None:
    assert isinstance(_Flagged(), PromptMetadataRunner)
    assert not isinstance(object(), PromptMetadataRunner)
