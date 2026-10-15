# deepagents: let a caller hand `create_deep_agent` its own `HarnessProfile`

**File against:** `langchain-ai/deepagents`. **Status:** draft, not filed. **Reproduced on:**
deepagents 0.7.25 (latest on PyPI, 2026-10-14) and 0.7.8 (installed here).

## Title

`HarnessProfile` (`excluded_middleware`, `excluded_tools`, `general_purpose_subagent`) can only be
reached through a process-global registry keyed by the model's provider, so an agent cannot carry its
own and a key miss is silent

## What happens

The levers that decide what a deep agent is made of live on `HarnessProfile`:
`excluded_middleware` (for example `SummarizationMiddleware`), `excluded_tools` (for example
`execute`), and `general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)` (no `task`
tool). `create_deep_agent` accepts none of them. It resolves one from `register_harness_profile`'s
process-global registry by the model's reported provider and identifier.

Two consequences for an integrator who needs those levers as a guarantee rather than a preference:

- The registration is process-wide, so it applies to every agent built on that provider, including
  ones that should keep the defaults.
- A model object whose provider is not the registered key gets the empty default profile, silently
  (the miss is logged at DEBUG). The stack then contains exactly the middleware the profile was
  registered to remove.

## Minimal repro

```python
from typing import Any

import deepagents.graph as g
from deepagents import HarnessProfile, create_deep_agent, register_harness_profile
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_openai import ChatOpenAI


class Model(GenericFakeChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


def summarizers(model: Any) -> list[str]:
    """The names of the summarization middleware `create_agent` is finally handed."""
    seen: list[str] = []
    real = g.create_agent

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.extend(m.name for m in kwargs["middleware"])
        return real(*args, **kwargs)

    g.create_agent = spy
    try:
        create_deep_agent(model=model)
    finally:
        g.create_agent = real
    return [name for name in seen if "Summarization" in name]


register_harness_profile("openai", HarnessProfile(excluded_middleware={"SummarizationMiddleware"}))
print("ChatOpenAI (profile applies)    :", summarizers(ChatOpenAI(model="gpt-5.5", api_key="k")))
print("other model object (silent miss):", summarizers(Model(messages=iter([]))))
```

Output on 0.7.25:

```
ChatOpenAI (profile applies)    : []
other model object (silent miss): ['SummarizationMiddleware']
```

## Proposed change

A keyword-only `harness_profile: HarnessProfile | None = None` on `create_deep_agent`, merged over
the registry's result with the existing `_merge_*` rules (so it is additive for callers who do not
pass it). The same profile would apply to the default general-purpose subagent, which is how a
caller removes `task` or the shell without replacing core middleware.

## First-party shim, and what turns red when this is fixed

- `src/chemclaw/agent/compaction.py::disabled_summarizer` replaces upstream's summarizer by name
  with `SummarizationMiddleware(trigger=None)`, because a summary rewrites framed, untrusted
  evidence into unframed prose.
- `src/chemclaw/agent/langgraph_agent.py::_middleware` replaces `FilesystemMiddleware` to withhold
  `execute` and `delete` (see draft 02).
- `src/chemclaw/agent/subagents.py` claims the name `general-purpose` so upstream inserts no
  ungoverned helper.
- Pins: `tests/test_upstream_surface.py::test_create_deep_agent_still_takes_the_parameters_the_harness_is_assembled_from`
  lists the parameters first-party code passes; add `harness_profile` to a W5.3 absence pin so the
  appearance of the keyword turns it red.
