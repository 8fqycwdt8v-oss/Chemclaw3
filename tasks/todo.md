# Task: fix everything the 2026-08-03 grounded live run found

Branch: `claude/chemclaw-live-test-bugs-c3zsgr`. Evidence and measurements:
`docs/archive/live-grounded-2026-08-03.md`.

Six findings, five open — the sixth (`available_tool_names()`'s missing skill name space) shipped in
`baedab3`. Ordered by what unblocks what: **F1 first**, because until the harness can see what a
tool returned, no number this run produces about grounding is trustworthy — including the numbers
that would judge the other four fixes.

_(The previous occupant of this file was the completed R0–R5 refactor checklist; it is in
`git log` and `docs/planning/refactor-hardening-plan.md`.)_

---

## F1 — score against what the tool returned, not a 200-char preview (P0)

**Defect.** `ToolResultEvent.preview` is capped at 200 characters (`api/runner_trace.py:23`).
`gather_evidence` returns up to 40 chunks. `evals/live._score_citations` joins those previews and
calls every cited id it cannot find "uncited"; the judge gets that list under *"NOTE IDS CITED THAT
NO TOOL RETURNED"*. Result: 19 of 36 answers graded fabrication, 9 of 9 checked verdicts false.

The preview exists for a good reason — a browser must not be sent a whole evidence sweep. So do not
widen it; add the machine-readable half beside it.

- [x] `ToolResultEvent` gains `note_ids: list[str]` — every note id the result mentioned,
      untruncated, extracted where the full result is still in hand.
- [x] `runner_trace` fills it from the untruncated tool output.
- [x] `evals/live` collects `note_ids` per turn; `_score_citations` scores against that set.
- [x] Whole-token matching, not `in`. The plan said to reuse `verifier._mentions`; scoring against
      a *set of ids* rather than a blob of text makes it exact equality, so no matcher is needed at
      all — and that closes the same hyphen-suffix hole (`playbook-degassing-old` no longer grounds
      `playbook-degassing`) with less code rather than shared code.
- [x] `evals/live_judge`'s prompt states what the signal is *and is not*. One verdict escalated
      "not in the preview" to "**mechanically verified as absent from the corpus**" — a claim the
      harness never made and cannot make.

**Verify.** A tool result naming 40 ids where only the first fits in 200 chars: `uncited_note_ids`
empty with the fix, 39 long without. Then `--regrade` the stored 36 transcripts (no new agent
turns) and report the corrected count.

## F2 — search before asking (P1)

**Defect.** 10 of 36 turns called no tool and answered with a clarifying question in prose; six
times one search would have found the answer. One answer *promised* `calculator_trust` and
`calculator_outliers` and ended the turn having called neither.

- [x] Agent instructions. **The plan was wrong about what was missing**: "Look before you ask" is
      already there, well-worded, and was ignored on all ten turns — so adding it again would have
      been prose about a rule the model already had. What is actually absent is (a) any instruction
      to ask *through `ask_clarifying_question`* rather than in prose — the tool is never named in
      the instructions, which is why 10 of 13 clarifications took the uninstrumented path — and
      (b) any rule against naming a tool you do not call. Both added; the existing rule untouched.
- [x] Instrument the second clarification path — `ask_clarifying_question` fired on 3 turns while 10
      asked in prose, so `asked_clarifying` undercounts threefold.

**Verify.** Deterministic tests on the instruction text and the detector. The behavioural half is
only provable live; re-run the six probes and report the tool-call count without claiming the prompt
change generalizes further.

## F3 — validate BO inputs at the tool boundary (P1)

**Defect.** `suggest_next_experiment` died inside BoFire's `_optimize_acqf_discrete` with
`KeyError: 'base'`, which `connectors/server.py:137` correctly generalized to "an internal error
occurred" because it is not a `ValueError`. The model could not repair from it and answered anyway.

- [x] Validate before BoFire sees anything: every declared parameter present in every observation,
      no observation naming an undeclared parameter. Raise `ValueError` with the parameter and the
      observation index — that already passes the connector's contract untouched.

**Honest limit, to be written in the code and not glossed:** the exact live trigger is
unreproduced — four hand-built calls in that shape all succeeded or raised the *good*
`ValueError: no col for input feature 'base'`. This closes the class; whether it closes the specific
one is unproven.

## F4 — an unreachable Temporal must say so, once, for every durable tool (P1)

**Defect.** `core.temporal_client.connect()` raises the raw `RuntimeError('Failed client connect: …
tonic::transport::Error …')`, reaching the model as `Error: Function failed.` In gr-35 the model
then wrote the entire development report by hand and presented it as generated.

- [x] `SubsystemUnavailableError` from `connect()`, naming Temporal and what it means for the
      caller. At `connect()`, not the six call sites — one client, one message. It lives in
      `core/errors.py` (whose module docstring now names both contracts), and the transport error
      rides along as `__cause__`.
- [x] **Not** a `ChemclawError` subclass: that hierarchy is the non-retryable bad-data contract
      (`durable/publish._BAD_DATA_TYPES`, enforced by `tests/test_publish.py`) and an unreachable
      broker is retryable. Same reasoning `AuthorizationError` used to stay outside it. The
      completeness walk's mirror image — `test_no_subsystem_outage_error_is_listed_non_retryable`
      — fails if a sweep ever adds it, and says why.
- [x] `surface_domain_errors` catches it alongside `ChemclawError`, with the docstring saying why
      the second type qualifies.
- [x] Not in the plan, found while doing it: `connectors/jobs.py` kept its own copy of this message
      around `connect()`. Removed — it was the same sentence maintained twice, and it re-framed the
      outage as `ConnectorJobError`, which is registered *non-retryable*; that tool runs inside an
      activity on the template path, so a broker restart was being turned into a hard failure.

## F5 — an empty fingerprint index must not read as "nothing similar" (P2)

**Defect.** `similar_reactions` returned `{"result": []}` with 10,000 reaction notes present. A
chemist asking "have we made anything like this" gets "no" from a system that has not been indexed.

- [x] `similar_molecules` / `similar_reactions` distinguish an empty index from an empty result in
      the payload the model sees — a `computed_field`, never a bare `property` (a plain property
      does not survive `model_dump()`, which is how the safety disclaimer reached zero callers).
- [x] The bundle reports fingerprint row counts, so an operator sees it before a chemist.

---

## Sequencing

F1 alone, first — it is the measurement instrument. F3, F4, F5 are independent of it and of each
other (different files, different subsystems), so they run in parallel. F2 lands last: its
`evals/live` half touches F1's file.

## Gate

`make lint type test`, the seven validators, and `make eval-strict` with no new regressions — after
each item, not once at the end.

---

## Review

All six landed; `make lint type test` green, seven validators green. Three things the work changed
about the plan, kept here because a plan that is only ever right is a plan nobody measured against:

**F2's diagnosis was wrong, and its fix only half worked.** "Look before you ask" was already in the
instructions and had been ignored on all ten turns, so the missing pieces were narrower: the
`ask_clarifying_question` tool is never *named* in the instructions, and nothing forbade naming a
tool you do not call. Both added — and the very next live run produced the same
*"I'll call `calculator_trust` … and then `calculator_outliers`"* sentence, with neither called. So
the rule became a scan (`promised_uncalled_tools`), beside the parameter-shape gate whose docstring
had already reached the same conclusion from different evidence: an instruction cannot be relied on
to bind the model being asked not to invent. Three of five zero-tool probes now search first;
two still do not, and that is stated rather than rounded up.

**F3's two directions are not one defect, and the worse one was the mirror case.** A *missing*
declared parameter already produced a good BoFire `ValueError`; the boundary check mainly adds the
observation index. An observation naming an *undeclared* parameter made BoFire **silently succeed**,
dropping the column and answering from a decision space that had discarded it — a fabrication
vector, now a backlog row of its own.

**F1's number half was built, measured and deliberately not shipped in the form planned.** The
whitelist (which of the answer's figures the tools support) is in. Its complement — figures no tool
returned — scored 11 flags and zero fabrications on the three worst answers, because a citation has
a syntax and a number does not. Shipping it under a heading the judge is told to trust would have
rebuilt this task's own defect one field over, so the harness asserts membership and never absence.

Two things found along the way that were nobody's assignment: `connectors/jobs.py` was classifying a
broker outage as *non-retryable* bad data, so a broker restart mid-template-run was a permanent
failure; and gr-18's answer verifies numerically while comparing the wrong isomer — every figure
genuine, the chemistry still wrong, which is the honest ceiling on what any numeric check can do.
