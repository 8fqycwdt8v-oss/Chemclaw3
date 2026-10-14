"""Read the helpers a compiled agent was handed, from the call that handed them over.

`SubAgentMiddleware` closes over its compiled roster with no accessor, so a test that wants the
graph a caller will spawn takes it from the `subagents=` argument `build_langgraph_agent` passes to
`create_deep_agent`. That is this repository's own call site, so it needs no upstream internals.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from deepagents import create_deep_agent


@contextmanager
def captured_roster() -> Iterator[dict[str, Any]]:
    """Fill a dict, by helper name, with the specs the agents built inside the block were given.

    Yields:
        The mapping, empty until an agent is built inside the block. A spec's `runnable` is the
        compiled helper graph for `general-purpose` and a lazily compiling runnable for a rostered
        name.
    """
    roster: dict[str, Any] = {}

    def spy(*args: Any, **kwargs: Any) -> Any:
        roster.update({spec["name"]: spec for spec in kwargs.get("subagents") or []})
        return create_deep_agent(*args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("chemclaw.agent.langgraph_agent.create_deep_agent", spy)
        yield roster
