"""Recorded results from a tool this repository no longer runs.

Two tests need a *realistic* tool result — long enough that its figures fall past the audit
preview cut, and shaped exactly as the wire delivers it — to prove something about the trace and
about the citation scorer. They used to build it by calling `ich_impurity_limit`'s own
implementation, on the argument that "a transcribed table can drift, and a check that passes
against a stale copy of the evidence proves nothing about the live one".

**That argument no longer applies, and saying so is the point of this module.** The ICH tables
moved to `Chemclaw3-mcp` with the rest of the safety capability
(`D-2026-08-15-safety-is-a-tool-not-a-gate`), so there is no live copy in this checkout for a
fixture to drift from. What the two tests assert is a property of `chemclaw.api.runner_trace`
and of the citation scorer — that figures past the preview are read off the whole result, and
that an answer quoting them scores as grounded — and neither depends on the numbers being current
ICH. So the payloads are recorded verbatim from the last in-tree run and frozen beside this
module, where a reader can see they are a recording rather than believe they are a live call.

If the numbers themselves ever matter again, that check belongs against the running server, not
against this file.
"""

import json
from pathlib import Path

# Keyed by the `substance` argument, values exactly as `model_dump_json(indent=2)` produced them.
# A data file rather than string literals in Python: the payload carries lines far past the line
# limit, and reflowing a recording would make it something other than a recording.
RECORDED_ICH_LIMITS: dict[str, str] = json.loads(
    (Path(__file__).parent / "fixtures" / "recorded_tool_results.json").read_text(encoding="utf-8")
)
