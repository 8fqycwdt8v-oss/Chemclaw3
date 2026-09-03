# What a fresh-context review found

Four reviews were run over the previous session's own output — the helper subagent, its narrowing,
and `agent/tool_result_shape.py` — with no memory of writing any of it. The code held. Four of the
sentences about it did not, and every one had been written by the session that had just measured the
thing it was describing.

- [x] Context floor: prose said **42,505** with 995 of headroom; `HEAD` measures **42,549**. Stop
      writing the figure — same rule this file already applies to `make` targets and skipped tests.
- [x] Helper tool counts: "54 in-process → 24" is two bases. In-process it holds **18**; the graph
      binds **24**, six of them `FilesystemMiddleware` verbs `tool_names` cannot reach.
- [x] `HELPER_BRIEF` said "read-only tools only" while binding `write_file`/`edit_file`.
- [x] "57 characters" is the whole thread, not "the `task` call and the report". Pinned as a ratio.
- [x] `rewritten_tool_messages` handles only a dict-shaped `Command.update` — asserted upstream now.
- [x] The suppression check quoted upstream's description; it imports the constant now.
- [x] `helper_profile` raises on a `held` set it cannot have come from.
- [x] Both controls on one report, and on one connector result — neither pairing had a test.
- [x] `make lint type test` green with the infrastructure up.

## Review

**What the reviewers got wrong is part of the result.** Two of their strongest claims did not
survive being checked: "no test locks the regression down" (four do — they grepped filenames the
tests do not live in), and "`effort` + `model_route` breaks helpers on Anthropic" (the caller's own
build raises first, so routing introduces nothing). One named `_create_task_tool`, which is
`_build_task_tool`. Taking any of those at face value would have produced a change fixing nothing.

**And one of mine did not survive either.** I wrote a test asserting that an oversized connector
result stays one well-formed envelope *because* `bound_tool_results` runs inside
`frame_connector_results`, with a docstring saying "swap the two entries and this is the test that
notices". Swapping them, it passed. The envelope survives because the truncation keeps **head and
tail** — the closing delimiter is in the tail either way. The test ships against the property that
is actually load-bearing (a head-only cut, or a notice appended past the delimiter), and the
docstring says what was measured. This is the second time this week that running the obvious
explanation is what showed it was the wrong one.

**The merged ADR keeps its wrong gloss.** `D-2026-08-29-a-helpers-report-is-model-prose-in-its-callers-thread`
says "the `task` call and a 28-character report", and this repository does not rewrite a merged ADR.
The correction is in `CLAUDE.md`, in the new ADR, and in the assertion — which is the half that
cannot go stale.

**Gate:** `make lint type test` green, with `dockerd` and `make up` first so the Postgres-backed
tests actually ran rather than skipping.
