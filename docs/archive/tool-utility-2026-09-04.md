# The same 221 questions, asked with tools and without — 2026-09-04

The first run of `make live-ab`, and the first time this repository has asked one of its own probes
tool-free. Raised by the `BACKLOG.md` row "Half the probe corpus tests one tool", whose second
consequence had been open since 2026-08-25: *ChemToolAgent's finding — that tool augmentation does
not consistently beat the base LLM, and hurts on general chemistry questions — cannot be reproduced
here. Bucket C scores restraint but never runs the same question tool-free for comparison.*

It reproduces. Design and rationale are in
[`D-2026-09-04-tools-help-a-third-of-the-time-and-hurt-a-quarter`](../decisions/D-2026-09-04-tools-help-a-third-of-the-time-and-hurt-a-quarter.md);
this file is the run.

## What ran

| | |
| --- | --- |
| command | `make live-ab` (`--suite ab --buckets A,C`) |
| probes | 221 — every bucket A (173) and bucket C (48) probe in `data/evals/probes` |
| turns | 442, one per probe per arm, each its own front-door session |
| augmented arm | the front door's default agent |
| baseline arm | `data/evals/profiles/no-tools.yaml` — `tool_names: []` |
| agent model | `claude-haiku-4-5-20251001` (`CHEMCLAW_AGENT_MODEL`), both arms |
| judge | `claude-sonnet-5`, the shipped `live_probe_judge_model` |
| stack | `infra/live/processes.sh up` — Postgres, Temporal, four workers, the four in-process connector bundles, and `chem`, `safety`, `rxnpredict` from `Chemclaw3-mcp` |
| wall clock | ~29 minutes |
| evidence | `tasks/live-test/transcripts/ab/{summary.md,evidence.json}` |

The 442 per-turn transcripts stayed local. `evidence.json` carries every verdict, every paired
score and every aggregate, which is the basis for every number below; the transcripts carry the
prose those verdicts were passed on.

## The result

| set | n | helped | hurt | no effect | net delta |
| --- | --- | --- | --- | --- | --- |
| A | 169 | 52 | 39 | 78 | +14 |
| C | 48 | 19 | 7 | 22 | +12.5 |
| all | 217 | 71 | 46 | 100 | +26.5 |

Four pairs were dropped because one arm came back `ungraded` — a judge failure, which is evidence
about the grader rather than about either arm (`du-08`, `gr-05`, `ms-01`, `rx-26`).

**On bucket A — where the capability exists and tools are supposed to win — tools helped 31% of the
questions and hurt 23%.** Judge verdicts per arm:

| bucket A | served | partial | unserved | fabricated |
| --- | --- | --- | --- | --- |
| with tools | 47 | 31 | 26 | 65 |
| without tools | 28 | 21 | 67 | 57 |

The shape is not "tools are better on average". It is **two opposite effects of similar size**:
tools convert declines into answers (`unserved → served` 16, `unserved → partial` 12) and rescue
fabrications (`fabricated → served` 7, `fabricated → partial` 9) — and they turn **19** questions
the toolless model correctly declined into fabricated ones (`unserved → fabricated`). That last
number is the tool-induced error class, measured on this corpus for the first time. `fabricated →
fabricated` (37) is the largest single cell in the table and belongs to neither effect: those are
questions this model invents an answer to whether or not it can look one up.

**On bucket C — where the system has no capability at all — tools helped.** 19 helped against 7
hurt, and fabrication *fell* from 16 to 12 while `served` (which for bucket C means a clear,
specific refusal) rose from 18 to 30. The hypothesis the bucket was built on — that tools are an
opportunity to fabricate a capability that does not exist — is the opposite of what happened here:
an agent that reached for a tool, found nothing, and said so refused better than one guessing at
what it could not do.

## What the arms actually did

- **Augmented**: 578 tool calls over 221 turns; 66 turns called nothing. The expected tool was
  reached on **133 of the 171** probes that name one (78%). Most-called: `gather_evidence` 96,
  `get_durable_job_status` 56, `find_notes` 31, `similar_reactions` 30, `compute_thermochemistry` 27.
- **Baseline**: 42 turns called something, all of it middleware — `grep` 113, `ls` 36, `glob` 26,
  `read_file` 9, `task` 6, `write_file` 4. **This is the honest limit of the control.**
  `FilesystemMiddleware` and `SubAgentMiddleware` are in `create_deep_agent`'s required set and a
  profile cannot strip them, so the toolless arm still holds six file verbs and `task` over a
  scratch space that does not outlive the turn. It reached nothing of this programme's — the helper
  is compiled from the same profile and so has no capability tools either — but 194 calls went into
  searching an empty filesystem, which if anything costs the control arm rather than flattering it.

## What it cost

Read off `chemclaw_tokens_total` and its four component counters at the front door, before and
after (so this is the two arms exactly, and excludes the judge):

| arm | billed tokens | cache read | cache write | uncached input | output |
| --- | --- | --- | --- | --- | --- |
| augmented | 48,347,530 | 47,524,740 | 657,669 | 2,876 | 162,244 |
| baseline | 1,581,466 | 440,120 | 1,025,047 | 1,300 | 115,000 |

**A tool-armed turn costs 30.6x a toolless one on this corpus**, and prompt caching is why it is not
worse: 98% of the augmented arm's input was a cache read. At list price for the two models this run
used, the whole measurement — both arms plus 442 judge calls — is on the order of $13, of which the
judge is roughly $5 (estimated from prompt sizes; the judge's own usage is not counted anywhere,
which is the one number here that is not measured).

## Caveats, in the order they would change a conclusion

1. **This measures Haiku 4.5's tool utility, not the deployment's.** A cheaper model both reaches
   for tools less often and fabricates more; 35% of all 217 augmented answers were graded
   `fabricated`, and 33% of the toolless ones. Re-running on the model a site actually deploys is
   one command, and the answer could be different in either direction.
2. **The judge is one model's opinion**, and it graded 442 answers with no human sampling behind it.
   Verdicts are in `evidence.json` with the reason for each.
3. **Some bucket-A probes read as follow-ups** ("Those two look right. Put them on record…") and are
   asked here as standalone turns, which is unfair to both arms equally.
4. **The two arms differ in prompt as well as in tools.** The control arm carries its own short
   instructions, because the default prompt describes tools it does not have. That is what "base
   model versus agent" means, and it is a confound worth naming rather than a bug to fix.
