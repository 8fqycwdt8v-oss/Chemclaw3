# D-2026-10-02-an-artefact-is-part-of-the-answer-not-an-effect — a versioned working document beside the chat

**Status:** accepted · **Date:** 2026-10-02 · Phase 0 of the artefacts plan; phases 1-2 build it.

## Context

A chemist asks for a design-space study plan, a solvent ranking or a series of analogues, and the
answer arrives as prose in the transcript. Nothing gives it an identity, a revision, a place beside
the chat, an edit by the chemist the agent can see, or an export. The nearest things are each one
domain or one direction:

- `experiment_protocols` (`infra/sql/073`) is the right *shape* — append-only revisions,
  `author_kind agent|human`, a 409 on a stale parent, a diff — but it is the prescriptive record of
  one laboratory procedure, with checks that only mean something for one;
- the tool-result store (`tool_result_blobs`/`tool_result_links`) holds what a *tool* returned,
  which the UI already renders as a result block, but nothing the agent *wrote*;
- a `report` note is layer 4 — knowledge, written through `kg/record.py` — not a draft;
- the calc artifact store (D-124) is calculation by-products. It owns the word "artifact" in this
  tree (`ArtifactRef`, `artifact_blobs`, the `calc` bundle's `list_artifacts`/`fetch_artifact`).

The UI repository has asked for the missing object three times (`Chemclaw3_ui` `USER-STORIES.md`
US-12, C4, G1).

## What was measured

The answer measurement is `chemclaw.evals.answer_shape`; the prefix drafts and the raw output of both
are in `tasks/artefacts-phase-0/results.md`.

**What answers are made of** (`python -m chemclaw.evals.answer_shape tasks/live-*`, 985 recorded
live-probe answers across every set this repository keeps):

| signal | value |
|---|---|
| answers with a Markdown table / with one of >= 4 rows | 71 (7.2%) / 58 (5.9%) |
| tables' share of all answer tokens | **2.7%** (186 tokens per table-bearing answer) |
| table figures that are verbatim tool values, where the run recorded `verified_numbers` | 612 checked → **51.3%** (64-71% in the tool-using arms, 4.1% in the A/B baseline arm) |
| document-shaped answers (>= 600 tokens and >= 2 headings) | **151 (15.3%)** — study plans, impurity investigations, campaign read-outs |
| answers listing >= 3 structures as SMILES | 14 (1.4%) |
| `render_structure` calls | 25 of 2,623 tool calls (1.0%) |

A figure absent from `verified_numbers` is **unchecked, not wrong** — `evals/live.py::_verified_numbers`
measured the inverse signal at precision zero — so the complement is not reported as an error rate.
The corpus is a Q&A probe set, which under-represents drafting sessions; it is the evidence there
is, not a sample of use.

**What the tools cost** (the drafts in `results.md`, on the basis `tests/test_context_floor.py` uses:
`as_structured_tool` → `convert_to_openai_tool` → `count_tokens_approximately`):

| shape of `spec` | create | + revise + read | total |
|---|---:|---:|---:|
| typed discriminated union | 1,492 | 497 | 1,989 |
| **untyped object, validated server-side** | **464** | 497 | **961** |
| one create tool per kind | 6 × 255 | 497 | ~2,027 |

The default profile's static prefix is 72,446 against a ceiling of 72,850. A one-line result handle
on every tool result would cost 4 tokens each.

## Options

1. **Client-only.** The UI lifts tables and headed sections out of the answer into a pane. No
   backend change and no prefix cost — and no identity across turns, no revision the agent can
   revise, and no chemist edit the agent ever learns of, which is the signal
   `infra/sql/073` calls the most informative thing this system can observe about its own output.
2. **Generalise `experiment_protocols`.** Its revision model is right and its subject is not: the
   `mode`, `status` and check columns describe a procedure, and a solvent ranking given a `status`
   of `approved` would be a protocol in name only.
3. **Knowledge notes.** Layer 4 is what we know. A draft that is wrong on Tuesday and fixed on
   Wednesday is not a claim, and notes change through `kg/record.py`, not through a chemist's REST
   edit.
4. **A session-owned, versioned exhibit — chosen.** Its own two tables beside the session tables,
   the protocol revision model copied rather than shared (two callers of different shape do not make
   an abstraction), an SSE event that carries the header, and routes for the body, revisions, diff,
   human revision and export.

## Decision

1. **An artefact is part of the answer, not an effect.** It changes nothing in a laboratory, the
   knowledge graph or another system, as the answer text does not, so its tools are not in
   `STATE_CHANGING_TOOLS` and a table does not need an approved plan. They join
   `subagents.SPEAKS_TO_THE_CHEMIST`, for `ask_clarifying_question`'s reason: a helper would be
   writing onto the chemist's surface from a context the chemist cannot see. A handoff peer keeps
   them, because it answers the chemist itself. Promoting an artefact to a note or a protocol is
   the gated step, and it goes through the tools that already do it.
2. **Named `exhibit` in code, "Artefacts" on screen**, because "artifact" is taken here (D-124).
3. **The spec is an untyped object validated server-side** — 961 tokens for the three tools against
   1,989 for a typed union, whose create tool alone (1,492) would breach `MAX_SINGLE_TOOL_TOKENS`.
   What that gives up is constrained generation; a malformed spec is a worded `ValueError` and a
   retry.
4. **On by default**, with `agent_exhibits_enabled` as the switch that unbinds the tools and pays
   their prefix back. Phase 1 raises `CEILINGS["__default__"]` by the delta it measures, re-derives
   the context budgets that `PREFIX_BOUND` feeds, and says so in its pull request.
5. **Owners and session members may revise**, authorship recorded per revision.
6. **Kinds:** `document`, `table`, `structures`, `chart`, `result` and `link`, all in phase 1, with
   literal values. A `chart` the agent writes carries a "values transcribed by the agent" caption.
   Phase 2 runs the answer's grounding check over an artefact's figures and flags what no tool
   returned.
7. **Bindings into the tool-result store are deferred, and the measurement is why.** The concept
   argued them partly on output tokens, and tables are 2.7% of what answers spend. What is left is
   provenance per cell, and the cost is a handle stamped into *every* tool result's shape. So an
   agent-created artefact holds literal values; a `result` artefact in phase 1 is pinned by the
   **chemist** from a result block, whose `result_ref` the UI already holds.

**Declined: artefacts that run code** (model-authored HTML, JavaScript or React). A tool result is
untrusted text that can carry injected instructions, and an artefact that executes is the channel
that would carry them into a browser on a network with no egress; the UI's `Markdown` refuses
`script`, `iframe`, `object` and `form` for the same reason.

**Revisit when:** `Chemclaw3_ui` serves a sandbox origin for rendered content — a separate origin
under `server/` with `connect-src 'none'` — which is the file to look for there.

**Revisit when:** bindings, if `chemclaw.evals.answer_shape` re-run over a corpus
recorded with artefacts on shows a table or chart in >= 20% of answers, or the phase 2 grounding
check flags a figure in an artefact that contradicts the tool value it was taken from.

## Consequences

- The structures kind is drawn by the UI's RDKit worker from SMILES; neither the model nor the
  server produces an SVG for an artefact. `render_structure` stays for consumers other than the
  chat, at 1% of calls.
- Retention gains `retention_session_exhibits_days` (default 0), and the chart's retention posture
  must state it; erasure follows `session_messages`.
- The prefix grows by the measured amount on every turn of every deployment that leaves the switch
  on, which is the price of decision 4 and is stated here so the ceiling raise is not a surprise.
