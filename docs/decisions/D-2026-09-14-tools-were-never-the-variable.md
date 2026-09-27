# D-2026-09-14-tools-were-never-the-variable — what the ChemBench pair actually compared

**Status**: accepted. Supersedes the tools attribution in
`D-2026-09-14-a-number-somebody-else-can-produce`, and amends §3 of
`D-2026-09-14-what-a-deployment-team-is-getting`, which leads with that pair.

## Context

`D-2026-09-14-a-number-somebody-else-can-produce` vendored 100 keyed ChemBench items and reported
two arms: the full system at 62/100 against `no-tools` at 74/100, labelled *"the same model, no
tools, same questions"*. `cli/live_benchmark.py` said the same thing — `--profile no-tools` runs a
toolless agent *"so the difference is measured rather than assumed"* — and the production-readiness
record put the pair first, as the one external number that does not flatter the system.

A fresh-context review of the merged wave asked what the two arms differ by, and the answer is not
the tools.

## The finding

**The arms differ by the whole system prompt.** A profile's `instructions:` *replace* the default
domain prose wholesale (`agent/chemclaw_agent.py::instructions_for`), and
`data/evals/profiles/no-tools.yaml` supplies one. Measured on this tree, at the maximal surface:

| arm | system prompt |
| --- | ---: |
| `default` | 16,271 chars |
| `no-tools` | 2,376 chars |
| **difference** | **13,895 chars** |

Removing the tools does not remove that text, because it is not keyed to any tool. The decisive
sentence is a `PromptBlock` with **no `requires` gate** — *"you must never state a specific
parameter as though it came from the record: no column or part number, gradient table, flow rate,
wavelength, retention time, regulatory limit, form designation, utilisation figure, headcount, date
or percentage"* — so it reaches a toolless agent exactly as it reaches the full one. Only swapping
the instructions removes it, and only the `no-tools` arm swaps them.

So the published pair varies *prompt and tools together* and attributes the whole difference to one
of them. That is the same family of defect the benchmark's own ADR opens with: a number that looks
like a measurement of the system and is a measurement of the instrument.

## What the review measured, and what it is evidence about

Driven on the shipped `_prompt()`/`_chosen()` against the same model and the same 100 items, with a
profile built to vary one thing at a time:

| arm | tools | prompt | correct | named no option |
| --- | --- | --- | ---: | ---: |
| `default` (published) | yes | default | 62 | 20 |
| measured | **no** | default | **58** | 25 |
| measured | no | `no-tools` | **76** | 8 |
| `no-tools` (published) | no | `no-tools` | 74 | 10 |

Holding the prompt fixed and removing every tool moves **62 → 58**: four points on n=100, unpaired,
and in the direction that says the tools *helped*. Holding the tools at zero and swapping the prompt
moves **58 → 76**, paired McNemar over the same items, 2 against 20 discordant, **p = 0.00012**.
Under the markup normaliser `D-2026-09-14-a-scorer-that-reads-latex-scores-latex` adds, the two
prompt arms read 68 and 81.

**The prompt is the variable with an effect here, and the tools are not.** The benchmark's own
reading survives this and is unchanged: a closed-book chemistry question has no evidence in any
corpus this deployment holds, so a system instructed not to answer without evidence declines, and
the benchmark scores the decline as wrong. What does not survive is the attribution — the gap is the
price of *the grounding instruction*, not of binding tools.

Two claims fall with it. The ADR's *"it reproduces
`D-2026-09-04-tools-help-a-third-of-the-time-and-hurt-a-quarter` on independent data"* is withdrawn:
nothing about tools was varied, so it cannot reproduce a result about tools. And the same control
profile is `make live-ab`'s baseline arm, so that ADR's own A/B carries the same confound — recorded
as a queue row rather than re-litigated here, because re-deciding it needs a run this session cannot
pay for.

## Decision

- **`data/evals/profiles/tools-removed.yaml` is the arm that varies only the tools**: `tool_names:
  []` and **no `instructions:`**, so the prose is the deployment's own, narrowed exactly as the
  shipped agent narrows it when a tool is absent (`PromptBlock.requires` / `.absent_unless`). That
  narrowing is a function of the tool surface, so it belongs to the treatment rather than to the
  confound — and the ungated sentence above survives in it, which is the whole point.
- **`no-tools.yaml` stays, and is labelled as what it is**: a prompt contrast. It is the arm the
  merged A/B was run on and deleting it would strand that measurement; what changes is that no
  document may read it as a tools contrast.
- **Every site that quoted the pair as a tools result is restated** to say which variable moved.
- The benchmark stays out of `ci` and stays not a gate, for the reasons the earlier ADR gives.

## Amended rather than superseded: the readiness record

`D-2026-09-14-what-a-deployment-team-is-getting` §3 is edited in place, which is not the convention
for a merged ADR. The reason is that document's own contract: it is a *state* record — "what is
enforced, what is bounded, what is measured, what is accepted" — whose preamble makes a clause's
survival conditional on its citation still holding, and whose stated purpose is to be the document
most likely to be believed without checking. A superseding ADR beside it would leave the false
attribution in the file a deployment team actually opens. The benchmark ADR, which records a
decision and the afternoon behind it, keeps its text and is superseded here in the ordinary way.

## Not measured by this session, and why that is stated rather than implied

The three-arm table above is the review's run, not this one's. Re-running it needs a model gateway,
and the credential in this environment answers **HTTP 400, "credit balance is too low"** — the same
refusal the readiness record already records against the probe lane. What this session verified is
the part that needs no credential and cannot drift: the prompt delta, the ungated block, and that
the new arm builds an agent with no capability tools while carrying the default prose. The arm now
exists, so the number is one run away for anybody with a balance.

## What keeps it true

- `tests/test_live_benchmark.py::test_the_arm_that_varies_the_tools_varies_only_the_tools` — the
  new arm overrides no instructions, binds no capability tool, and still carries the sentence no
  tool removal can remove.
- `tests/test_live_benchmark.py::test_the_prompt_swapping_arm_cannot_be_read_as_a_tools_contrast` —
  the other direction, and the one that would catch a relabelling: `no-tools` *does* replace the
  prose, and the replacement is thousands of characters, so a document calling it a tools contrast
  is wrong about its own fixture.
- `tests/test_readiness_record.py::test_the_external_benchmark_number_is_still_in_it` — the record
  still carries the external figure, now with the variable it measured named beside it.
