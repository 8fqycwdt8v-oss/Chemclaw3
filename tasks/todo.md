# The capture half of the knowledge loop

Follow-on to `D-2026-09-04-a-ranker-that-sorts-alphabetically-is-not-a-ranker`, which closed the
retrieval half and said plainly what it had not done: **data is captured automatically, conclusions
are not.** All four review claims re-verified against `HEAD` before building — the tree had moved
twice — and all four held.

## Done

- [x] 1. **Nine `run_*` procedures wrote no durable record.** `record_job` had one caller in the
      tree (`durable/connector_job.py`), so a template run left no `job_records` row: never
      findable by `find_past_jobs`, and `get_durable_job_status` answered for its id only until
      Temporal retained its history away. A *failing* run left nothing anywhere.
      `TemplateWorkflow` now records on both paths. Proven on a real broker, not just by the
      builders: removing the success-path call makes `tests/test_template_job_record.py` report
      "got 1" instead of 2.
- [x] 2. **Two docstrings asserted the opposite in the present tense** (`agent/durable_tools.py`) —
      both true of connector jobs alone. Corrected, and `find_past_jobs` now documents the
      `connector="template"` filter.
- [x] 3. **A correction was recorded as a confirmation.** `memory/interaction.py` rendered
      `A (confirmed):` unconditionally while three docstrings and the system prompt said
      "confirmed **or corrected**". `corrected_from` carries what the system had said; empty means
      confirmed.
- [x] 4. **The recording rule had no trigger.** "A computed value that matters beyond the
      conversation" named no moment; now a comparison whose margin *clears* the stated uncertainty
      does, with the inside-the-error-bar case pointed at the ceiling section.
- [x] 5. **Nothing graded the write-up after a calculation.** `propose_knowledge_note` is named by
      fourteen probes across seven files and by none in `durable.yaml` or
      `multistep-calculation.yaml`. Two new probes, `ms-18` and `ms-19`.

## Rejected, with the reasoning kept

- [x] 6. **An automatic `publish_to_graph` over calc's twelve durable jobs.** Designed, reviewed and
      **not built** — two of its premises were false (`job-result` *is* minted, by
      `propose_knowledge_note`; the record does *not* stop at the cache, `_publish_result` runs for
      every job), `skills/computational-evidence` already forbids it in as many words, roughly half
      the notes would have read "this calculation could not distinguish them" at GFN2-xTB's ±3
      kcal/mol, and neither default is defensible. The ADR keeps the whole argument so it is not
      re-proposed from scratch.

## Two things measurement changed

**The eval fix as proposed would have made the probes weaker.** The recommendation was to add
`propose_knowledge_note` to `expects_tools` on `ms-07`/`ms-08`. `evals/live.py` scores that field
with `any()`, so a second name makes a probe pass on *either* tool — `ms-07` would then have been
satisfied by a turn that recorded a note and never ranked anything. Separate probes instead.

**`turn_costs` already is the per-turn outcome row**, so the "no end-of-turn record" finding was
half wrong: `tool_calls`, `tool_failures`, `jobs_started` and `outcome` are written every turn.
What is missing is the knowledge dimensions (did this turn retrieve, cite, capture) — a much
cheaper change than the new table that was proposed, and queued rather than rushed at the end of
this one.

## Cost, stated

The context floor moves 43,063 -> 43,316 against the unraised 43,500 ceiling: **184 tokens of
headroom**, from one optional argument on `record_confirmed_answer`. That is tight enough to be the
next person's problem, and the reclaim is already a `BACKLOG.md` row.
