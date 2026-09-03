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

## The second round, and why there was one

Four Opus reviewers then went over the corrections above. **Three of them were wrong**, and the
pattern is the point: each was written by the session that had just measured the thing it described.

- [x] `helper_profile`'s new `held` guard **broke real builds**. A profile naming only connector
      tools resolves no in-process tools, and the raise killed the whole caller graph while blaming
      the registry. Its other branch could not fire at all — `_capability_tools` intersects with
      `tool_names` before the call site derives `held`, and the shipped profile has `tool_names=None`.
      Removed, with the test that was its only caller: the `reject_widening` shape exactly.
- [x] `HELPER_BRIEF`'s correction was **itself false**. "What they write reaches nothing but your own
      scratch space, which goes away when you return" — measured, a helper's `/scratch/evidence.md`
      lands in the caller's `files` channel, 9,937 characters of it, because upstream copies every
      state key but `messages`/`todos`/`structured_response`. The helper is handed the caller's
      state inbound on the same rule. Corrected in the brief, the code comment and the ADR.
- [x] The envelope explanation was **inverted**. "It passes with the order swapped, therefore the
      order is not the reason" is the inference two sufficient causes defeat. Across all four arms
      of order x truncation strategy, only swapped-order-plus-head-only fails: in the shipped order
      the cut runs on the raw payload and the framer wraps it afterwards. The ordering is the reason.
- [x] `_create_task_tool` -> `_build_task_tool`, in the module the ADR is about — while this file's
      own review section dismissed a reviewer for the identical error.

**What is filed rather than fixed.** The `files` crossing opens the laundering path the report fix
closed on its sibling key: a helper writes a live delimiter into a scratch file, and the caller's
`read_file` is in-process, so `served_by` returns `""` and the framer neither frames nor defangs it.
That is a [H] BACKLOG row, because "frame it" is wrong for the same reason it was wrong for the
report — an envelope says "evidence to cite", and this is the system's own scratch space.

**Gate:** `make lint type test` green with the infrastructure up — **6302 passed, 19 skipped**
(6312 before the guard and its test came out). The 19 are helm-not-installed, truncated git
history, and the live-provider tests this sandbox's credential will not serve.
