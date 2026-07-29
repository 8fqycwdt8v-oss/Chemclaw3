# D-087 — Second reconciliation with `main` (PR #21): the MCP transport union

`main` landed its own transport discrimination while this branch's networked-MCP work (gap TOOL-1)
was in flight. **`main`'s wins outright.** It is a proper discriminated union
(`StdioMcpServerSpec | HttpMcpServerSpec`) with a *callable* discriminator that defaults an absent
tag to `stdio`, so every existing config keeps loading; this branch had one class with an
either/or `command`-xor-`url` validator, which is exactly the ambiguity a union removes. Per-variant
`request_timeout` also supersedes this branch's global `mcp_request_timeout_seconds` — the timeout
belongs to the remote spec that needs it, not to every server including local subprocesses.

The dispatch in `_mcp_tool` came from `main` unchanged; this branch's contribution here reduces to
the chart-side half (gap DEP-3: the standalone MCP Deployments were default-on while stdio-only,
i.e. a crash loop), which is unaffected and still needed.

Three guards caught the fallout rather than letting it drift: `mypy --strict` on the leftover field
from the resolution, `test_env_example_documents_only_real_fields` on the now-nonexistent env key,
and the chart test on the superseded constructor. That is three independent gates on one merge
mistake, which is the point of having them.
