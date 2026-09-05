# D-2026-09-04-tools-help-a-third-of-the-time-and-hurt-a-quarter — the control arm this corpus never had

**Status:** accepted · **Date:** 2026-09-04 · Closes the second half of the `BACKLOG.md` row "Half
the probe corpus tests one tool", and supplies the instrument
`D-2026-08-15-a-claim-is-a-mutex-not-a-line-edit`'s deleted routing measurement could not be.

## Context

`evals/ab.py::compare_tool_utility` has been able to compare a metric with tools against the same
metric without them since phase 2b, and nothing has ever produced its inputs from a live run. Its
one registered caller, `autonomy.plan_execute_utility`, reads four hand-written floats out of a case
file — `suzuki-yield: baseline 62.0, augmented 78.5`, a number nobody measured. So the comparison
this corpus most needs was implemented, registered, gated by `make eval`, and never run.

What made it worth running is that the corpus cannot answer its own question without it.
`gather_evidence` is in `expects_tools` for 125 of 288 probes; bucket C scores restraint but never
asks the same question tool-free, so ChemToolAgent's finding — **tool augmentation does not
consistently beat the base LLM, and hurts on general chemistry questions** — was a citation here
rather than a measurement. Every previous attempt was blocked on a working model credential, and
the mock cannot stand in: `cli.mock_llm` emits scripted tool calls without *choosing* them, so both
arms would measure the script.

The credential answered today (probed with one Haiku call before anything was built), so the
measurement was owed today.

## Decision

**The control arm is a profile, not a flag.** `data/evals/profiles/no-tools.yaml` declares
`tool_names: []`, and `build_langgraph_agent` removes every capability tool — and every connector's
allow-list — before `create_agent` is called, so the compiled graph's `ToolNode` never holds them.
The skills narrow with them for free, by `ToolScopedSkills`' existing rule. A flag would have been a
second way to express a narrowing this repository already expresses one way.

**It is not in `data/profiles/`.** Everything there is advertised by every deployment that starts
the front door, and a toolless agent is a measurement instrument rather than a capability anybody
should be able to pick off a list. `infra/live/processes.sh` puts `data/evals/profiles` on
`CHEMCLAW_PROFILES_DIR`; the shipped agent surface does not change, and
`tests/test_tool_utility.py` asserts both halves — that the profile builds an agent with no
capability tools, and that it is absent from the shipped set.

**A run that could not tell its arms apart is refused before it spends anything.** `--suite ab`
opens one session against the control profile first: `get_profile` raises on an unknown name, so a
front door started without the eval profile directory fails at the probe rather than after the
augmented arm has been paid for — or, worse for a reader, comparing the default profile with itself.
Each outcome records the profile that produced it, so the two halves stay tellable apart once they
are files.

**A verdict becomes a number, and `fabricated` scores below `unserved`.** The five judge verdicts
collapse onto one axis (`served` 1.0, `partial` 0.5, `unserved` 0.0, `fabricated` −1.0), because the
whole point of the comparison is that tools introduce an error class of their own: an answer that
invents a citation is worse than one that declines, and a scale scoring them level would report the
failure mode this measurement exists to find as a tie. `ungraded` is dropped and named — it means
the judge failed, and scoring it as anything would put a grader outage into the system's own utility
number.

**Buckets are summarised apart.** A is "the capability exists, tools should win"; C is "there is no
capability, and the honest answer is a refusal". A single aggregate lets a gain on one cancel a loss
on the other, which is the averaging that left the deleted routing measurement unable to answer its
own question.

## The measurement

221 probes (every bucket A and C), 442 turns, `claude-haiku-4-5-20251001` on both arms, judged by
the shipped `claude-sonnet-5`, ~29 minutes. Full record:
[`docs/archive/tool-utility-2026-09-04.md`](../archive/tool-utility-2026-09-04.md).

| set | n | helped | hurt | no effect | net delta |
| --- | --- | --- | --- | --- | --- |
| A | 169 | 52 | 39 | 78 | +14 |
| C | 48 | 19 | 7 | 22 | +12.5 |

**ChemToolAgent's finding reproduces, on this system's own corpus.** On bucket A tools helped 31%
of the questions and hurt 23%. The mean is positive and small (+0.083 per probe) and is the least
interesting thing in the table: what it averages is two opposite effects of similar size. Tools turn
declines into answers (`unserved → served` 16, `unserved → partial` 12) and rescue fabrications
(16 more), and they turn **19** questions the toolless model correctly declined into fabricated
ones. That last transition is the tool-induced error class, and it had never been counted here.

**Bucket C came out the other way round, and the hypothesis it was built on is wrong.** Tools helped
19 against 7 hurt; fabrication *fell* from 16 to 12 and clear refusals rose from 18 to 30. An agent
that reaches for a tool, finds nothing, and says so refuses better than one guessing at what it
cannot do. Whatever else is true of the tool surface, it is not the thing making this system
overclaim on questions it cannot answer.

**A tool-armed turn costs 30.6x a toolless one** — 48.3M billed tokens against 1.58M, read off the
front door's own per-profile counters — and 98% of the augmented arm's input was a cache read, so
that ratio is the *cached* one.

## Consequences

- `make live-ab` exists and is repeatable; a site can re-run it on its own model with one command,
  and should, because **this is Haiku 4.5's tool utility rather than a deployment's.**
- **The tool-schema deferral and the `default` allow-list now have their gate.** Both rows are
  blocked on "the live lane can show every probe still reaching its tool", and this run establishes
  the before-figure: 133 of 171 expected tools reached (78%), per-probe verdicts in `evidence.json`.
- **`plan_execute_utility` is untouched and still scores literals.** Its four invented tasks are a
  plan-versus-single-shot comparison, which is not what was measured here; folding tool-utility data
  into a metric named for planning would trade one honest gap for a mislabelled number. The row
  about eval gates scoring literals stays open, with one fewer excuse.
- **The judge's own token usage is counted nowhere**, so the only cost figure in this work that is
  an estimate rather than a measurement is the grading half.
- Two defects were found by trying to run this and fixed in the same commit, both of which had made
  the measurement impossible rather than merely awkward: `infra/live/processes.sh` hardcoded
  `chem safety` as the fleet bundles to start, so `rxnpredict` — wired as a bundle earlier the same
  day by `D-2026-09-04-wiring-an-endpoint-bundle-is-invisible-to-the-ratchet` — was discovered,
  enabled, required, and served by nothing, which failed the front door on every `make live-up`; and
  `evals/live_judge.py` honoured `llm_base_url` while ignoring `llm_tls_ca_bundle`, so a deployment
  pointing the judge at exactly the internal gateway that setting exists for could not verify its
  certificate and every grading call died at TLS. The first is now derived from the two manifests
  that already declare the set; the second reuses the agent's own `_tls_http_client`.

## Alternatives considered

- **A `--no-tools` flag on the corpus suite.** Rejected: the narrowing already has one expression
  (`AgentProfile.tool_names`) that the graph builder, the skills backend and the audit trail all
  read. A second one would be the "two live definitions" failure `connectors/README.md` records.
- **Grading with the agent's own model** to make the run cheaper. Rejected: `live_judge.py`'s own
  argument is that a judge sharing the agent's blind spots ratifies them, and the judge is one call
  per probe against the agent's several — it is ~40% of the bill for the run and the wrong 40% to
  save.
- **Sampling the corpus.** Rejected once the cost was measured: the full A+C set is ~$13 and needs
  no sampling caveat. `--sample N` exists for a site that wants one, and draws a systematic sample
  across sections rather than the first N, which would ask one user story's questions and report
  them as a reading of the corpus.
