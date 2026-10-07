"""Recorded results from a tool this repository no longer runs.

Two tests need a realistic tool result, long enough that its figures fall past the audit preview
cut, to test `chemclaw.api.runner_trace` and the citation scorer. The ICH tables moved to
`Chemclaw3-mcp`, and neither test depends on the numbers being current, so the payloads are a
frozen verbatim recording.
"""

import json
from pathlib import Path

# Keyed by the `substance` argument, values exactly as `model_dump_json(indent=2)` produced them.
# A data file rather than string literals in Python: the payload carries lines far past the line
# limit, and reflowing a recording would make it something other than a recording.
RECORDED_ICH_LIMITS: dict[str, str] = json.loads(
    (Path(__file__).parent / "fixtures" / "recorded_tool_results.json").read_text(encoding="utf-8")
)
