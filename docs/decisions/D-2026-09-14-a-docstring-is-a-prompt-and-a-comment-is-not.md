# D-2026-09-14-a-docstring-is-a-prompt-and-a-comment-is-not — where a tool schema's tokens really are

**Status**: accepted

## Context

`BACKLOG.md` carried a measured row: a tool schema is 38% developer rationale, and it ships on every
turn. Its named mechanism was Pydantic turning a **class docstring** into a JSON-schema
`description`, so `science/bo/problem.py`'s design arguments — *"One `objectives` field rather than
a lead objective plus a sidecar list (W3)"* — were re-sent to the model on every model call.

## What was measured

**The named vein is already closed, and was closed before this row was worked.**
`science/bo/problem.py` carries five comments saying in as many words that the rationale is
deliberately in a `#` comment rather than in the docstring, each pointing at the next. The row's own
example, `start_optimization_campaign`, was quoted at 8,063 characters of schema with 4,392 of
description; it now measures **1,565 tokens in total**.

**Where the tokens actually are**, over the 92 tools a `default` turn binds:

| | tokens |
| --- | ---: |
| whole bound schema surface | 57,036 |
| …of which `description` text | 41,070 (**72%**) |
| `Args:` sections, 72 tools | 8,482 |
| `Returns:` sections, 76 tools | 4,747 |
| `Raises:` sections, 8 tools | 723 |

And the description text is overwhelmingly *caller guidance*: a scan of every bound description for
developer-rationale tells flags 28 paragraphs, most of them false positives on `Args:` blocks. The
biggest single description, `suggest_next_experiment`'s 1,571 tokens, is 100% instructions to the
model about what to send.

**What is genuinely developer-facing, and one of it is worse than that.**
`get_durable_job_status` carried 184 tokens explaining that a failure case *"used to degrade to a
bare status, because the DFT job returned its own typed result and had its own status tool
(`agents/job_status.py`)"* — re-sent on every model call, describing a tier
`D-2026-08-26-semiempirical-is-the-whole-tier` **deleted**. `expand_note` carried 84 tokens about
`chemclaw.core.errors` and `chemclaw.agent.tool_authz`. `report_measurement` carried a paragraph
about what omitting `unit` "used to mean".

## Decision

Take those three, per-paragraph, the way the row asked: **rationale to a `#` comment in the function
body, guidance stays in the docstring.** Measured end to end on the ratchet's own basis:
**64,907 → 64,598**, a **309-token** reduction, and `CEILINGS["__default__"]` goes **65,500 →
65,200** in the same commit.

And hold the one half of the rule a test can decide. The wider rule — rationale in a comment,
guidance in the docstring — is judgment and stays a review rule. **Naming a removed tier is not
judgment**: `tests/test_prose_contract.py::test_no_tool_description_tells_the_model_about_a_tier_that_is_gone`
fails any registered tool whose docstring names DFT, HPC, Nextflow, Seqera or the deleted status
tool. That is the check that would have caught the paragraph above, and it costs nothing.

## What is not taken, and why it is not a reading question

`Args:` and `Returns:` are **13,229 tokens** of every model call between them, and they are the
argument contract — the only place most of these tools document what to send, since
`convert_to_openai_tool` is not parsing docstrings here (per-parameter descriptions total 1,736
tokens across all 92 tools). Shortening them is a cheaper prompt that may stop finding tools, which
is a regression with a good-looking metric, and the instrument for deciding it already exists:
`make live-ab`'s 2026-09-04 run reached the expected tool on 133 of the 171 probes that name one.
That is a re-run, not a reading.

The `default` profile's allow-list — the other half of the old row, worth −5,787 tokens on the
pre-2026-08-29 basis — is untouched here for the same reason, plus the one its own row gives: the
saving is partly in endpoint tools no offline floor can see, and it needs the skill gate beside it.

## Consequences

- The ceiling falls rather than rises, which is the direction `tasks/lessons.md` requires.
- A number in this ADR is a claim about a commit: the live figure is whatever
  `tests/test_context_floor.py` measures, and the ceiling it ratchets against is the figure to read.

## What keeps it true

- `tests/test_prose_contract.py::test_no_tool_description_tells_the_model_about_a_tier_that_is_gone`
  — driven: putting the DFT paragraph back into `get_durable_job_status`'s docstring reddens it and
  nothing else.
- `tests/test_context_floor.py::test_the_static_prefix_stays_under_its_ceiling` — the 309 tokens
  cannot come back without failing the lowered ceiling.
