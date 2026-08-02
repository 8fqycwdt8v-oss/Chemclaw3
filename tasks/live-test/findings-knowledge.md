# Live-test findings — knowledge slice (`kn-01`…`kn-29`)

User-story sections **1** (Institutional Knowledge & Search), **15** (Training / Onboarding /
Knowledge Continuity), **17** (Cross-Cutting Trust & Governance). 29 probes, run 2026-08-02 on
`claude-haiku-4-5` over the live front door; 16 of the 29 also re-run on `claude-sonnet-5`
(`tasks/live-test/transcripts-sonnet/`).

Every claim below is checked against the live corpus at `/workspace/chemclaw-notes/knowledge`
(1,025 notes) and against re-executed tool calls, not against the answer text.

---

## 1. Judge calibration

**The headline numbers for this slice are not usable as given. 16 of 29 verdicts (55%) are wrong,
and the single most-quoted mechanical signal — "answers citing a note no tool returned" — is a
measurement artifact with a 100% false-positive rate in this slice.**

### 1a. `uncited_note_ids` measures the wrong thing — 6/6 false positives

Six of the run's 18 flagged probes are mine: `kn-02`, `kn-03`, `kn-05`, `kn-06`, `kn-10`, `kn-21`
(11 flagged ids total). **All 11 are real notes that the tool the model called did in fact return.**

Root cause, measured:

- `src/chemclaw/evals/live.py:170` collects the SSE `tool_result` **`preview`** field.
- `src/chemclaw/api/runner.py:859` truncates that preview to `_ARG_PREVIEW_CHARS = 200`
  (`runner.py:87`).
- `src/chemclaw/evals/live.py:120-128` then declares any `[[wikilink]]` not found in the joined
  previews "uncited".

Re-executed against the live graph:

| tool call | full result | preview | note ids visible in preview |
| --- | ---: | ---: | ---: |
| `gather_evidence("biaryl coupling bimodal yield")` | 20,041 chars, 40 chunks | 200 chars | **1 of 40** |
| `expand_note("playbook-degassing")` | 1,310 chars | 200 chars | **1** (the anchor only) |
| `find_notes("4-methoxybiphenyl")` | 817 chars | 200 chars | **1** |

For `expand_note` the 200-char window is consumed entirely by the `NoteRef(...)` repr, so the note
**body — which is where the `[[wikilinks]]` to related notes live — never reaches the preview at
all**. Per-probe resolution:

| probe | flagged as uncited | where it actually came from |
| --- | --- | --- |
| `kn-02` | `failure-aqueous-protodeboronation` | rank 3 of 40 in the `gather_evidence` sweep (re-measured) |
| `kn-03` | `campaign-amide-additive` | literally `[[cites:campaign-amide-additive]]` in the body of `playbook-amide-coupling-additive`, which the turn expanded |
| `kn-05` | `opt-suzuki-conditions` | `[[cites:opt-suzuki-conditions]]` in `playbook-degassing`'s body **and** in its neighbour list |
| `kn-06` | `campaign-biaryl-scope`, `opt-suzuki-conditions`, `playbook-pd-cross-coupling-scope` | all three are `[[cites:…]]` in `report-biaryl-development`'s body, which the turn expanded |
| `kn-10` | `playbook-pd-cross-coupling-scope`, `report-biaryl-development` | both are 1-hop neighbours of `campaign-biaryl-scope`, which the turn expanded |
| `kn-21` | `campaign-biaryl-scope`, `failure-aqueous-protodeboronation`, `opt-suzuki-conditions` | all three returned by `gather_evidence("master batch record biaryl")` (re-measured) |

The metric also **misses** the real failures in both directions:

- `kn-09` cited four ids in backticks, not wikilinks; `_WIKILINK` (`live.py:46`) never sees them.
- `kn-18` — the one genuine fabrication in this slice — has `uncited_note_ids == []`, because all
  four of its citations are real, retrieved notes. The fabricated material is *content attributed
  to a correctly-retrieved note*, which this metric cannot see.

### 1b. `fabricated` verdicts — 8 of 12 wrong (67% false-positive rate)

| probe | verdict | correct? | evidence |
| --- | --- | --- | --- |
| `kn-01` | fabricated | **WRONG** | 8 of 9 flagged claims are verbatim in `reaction-uspto-suzuki-biphenyl-1/-2` (real notes): dioxane/water 4:1, 82 °C, 4 h, 89.5/92.5% yield, 97.8/98.2% purity, `performed: 2024-01-08`, "des-methoxy biphenyl 1.4% area". Only "tri-tert-butylphosphine" is wrong — the SMILES `CC(C)(C)P(c1ccccc1)c1ccccc1` is *tert*-butyldiphenylphosphine. |
| `kn-02` | fabricated | **WRONG** | Judge reasoned from the *count* of `expand_note` calls. Both quoted facts are verbatim: `opt-suzuki-conditions` says "1.5 mol% Pd, K2CO3, 80 °C — 76% isolated"; `failure-aqueous-protodeboronation` says "at 100 °C in THF/water … protodeboronates … faster than it transmetalates". |
| `kn-07` | fabricated | **RIGHT** | CHEM21 Class III, ICH Q3C ≤880 ppm, boiling points appear nowhere in the corpus, and `chemclaw_agent.py:151-172` explicitly forbids stating a regulatory limit. |
| `kn-09` | fabricated | **WRONG** | Re-ran the tool: `find_similar_reactions` returns exactly `Match(id='ord-suzuki-biphenyl-1', similarity=0.8158)` x4 with `O=C([O-])[O-].[K+].[K+]` in the label. "Tanimoto ~0.82", the four ids, and "K2CO3" are all tool output. "Pending PR-gate review" is the model quoting `expand_note`'s own error string (`graph_tools.py:146-149`). 9/9 flagged claims grounded. |
| `kn-10` | fabricated | **WRONG (on the stated grounds)** | The judge said author names "do not exist"; 49 of 1,025 notes carry a chemist's name in `source` (`eln-json:uspto-suzuki-biphenyl-1:J. Alvarez`). The answer is wrong for the opposite reason — see Finding 7. |
| `kn-12` | fabricated | **RIGHT** | "tally up the runs and calculate the success rate" is a capability nothing implements. |
| `kn-17` | fabricated | **WRONG** | `playbook-recrystallisation-purity`'s body reads "Distilled from `[[cites:campaign-aspirin-teaching]]`". The attribution the judge called invented is the note's own first line. |
| `kn-21` | fabricated | **WRONG** | Every "invented" parameter is verbatim in `rxn-suzuki-biaryl` + `report-biaryl-development`; `opt-suzuki-conditions` and `campaign-biaryl-scope` were both returned by the sweep (re-measured). |
| `kn-24` | fabricated | **WRONG** | Every "invented" tool name is on the real surface (`TOOL_INVENTORY.md`). "Hash-chained audit trail" and "lands on a feature branch" are verbatim from the agent's own system prompt (`chemclaw_agent.py:115-122`) and true of `audit_store.py:66` / `git_submitter.py`. |
| `kn-26` | fabricated | **RIGHT** | See Finding 3 — verified there is no tag or identity scoping in retrieval. |
| `kn-27` | fabricated | **WRONG** | The field list "actor, tool, arguments, outcome, latency, correlation id, deployment revision" is exactly `_HASHED_FIELDS` at `audit_store.py:44-51`, and exactly the system prompt at `chemclaw_agent.py:115-117`. |
| `kn-28` | fabricated | **RIGHT (borderline)** | `find_past_jobs` does return other people's jobs with reasons, but not readership. Overreach, correctly caught. |

### 1c. 12 of 29 "unserved" verdicts are not verdicts at all

`kn-03`, `kn-05`, `kn-06`, `kn-08`, `kn-14`, `kn-15`, `kn-16`, `kn-18`, `kn-19`, `kn-23`, `kn-25`,
`kn-29` are all `"reason": "unparseable judge: …"`. **Root cause: `max_tokens=1024` at
`src/chemclaw/evals/live_judge.py:99`.** The judge's JSON is truncated mid-`reason`, so
`text.rfind("}") == -1` and `live_judge.py:110-112` silently defaults the verdict to `unserved`.
The surviving fragments prove it: `kn-05` was heading for `fabricated`, `kn-18` for `fabricated`,
`kn-16` for `unserved`.

Re-graded by hand: 7 of those 12 are **served** (`kn-03`, `kn-05`, `kn-06`, `kn-14`, `kn-23`,
`kn-25`, `kn-29`), 2 partial, 2 unserved, 1 genuinely fabricated. So the default was wrong 8 times.

**Combined: 8 wrong `fabricated` + 8 wrong `unserved` defaults = 16 of 29 (55%).** Corrected slice
distribution: served 11, partial 8, unserved/fabricated 10.

### 1d. Why the judge over-calls fabrication

`_prompt` (`src/chemclaw/evals/live_judge.py:75-85`) hands the judge the probe, the answer and
**`tools_called` as bare names — never the tool results**. The judge therefore cannot know what was
retrieved and has to guess; `kn-02`'s reason ("only two `expand_note` calls were made, yet the
answer cites … three separate notes") is that guess written down. Fix: put the tool-result previews
(uncapped, or capped far higher) into the judge prompt, or grade citations mechanically against a
re-resolved retrieval instead of asking a model.

---

## 2. Findings

### F1 — The PR-gate write path was down for the entire run; 17/17 submissions failed and the user was never told · **P0 · BUG**

Probes: `kn-15` (5 failures), `kn-18` (3), `kn-16`-sonnet (3). Run-wide:
`propose_knowledge_note` called 14x, failed 14x; `record_confirmed_answer` called 3x, failed 3x;
`notes_proposed` = **0 across all 231 transcripts**.

Reproduced exactly:

```
src/chemclaw/kg/git_submitter.py:209  await self._git("fetch", self._remote, self._base)
→ GitSubmitError: git fetch origin main failed: fatal: 'origin' does not appear to be a git repository
```

`/workspace/chemclaw-notes` has **no `origin` remote** and is on branch `master`, not
`note_base_branch`. That is a deployment misconfiguration, but three code defects turn it into a
silent P0:

1. **`GitSubmitError(RuntimeError)`** (`src/chemclaw/kg/git_submitter.py:73`) is not a
   `ChemclawError`, so `surface_domain_errors` (`src/chemclaw/agent/tool_authz.py:155`) does not
   convert it and the model receives MAF's opaque `"Error: Function failed."`. The transcripts are
   direct evidence: the model retried by permuting *arguments* — "corrected relation format", "try
   without the tags field", "simplify the markdown", "an even simpler version" — because it was told
   nothing about a git remote.
2. **The user is never told the note was not saved.** `kn-15` ends mid-narration ("I'll try an even
   simpler version:") with no final answer at all; the chemist who asked "capture it properly so the
   next person doesn't" believes it happened. Worse, sonnet's `kn-16` closes with *"I'll record it as
   a confirmed interaction so it's citable directly going forward"* — after all three
   `record_confirmed_answer` calls had already failed.
3. **Nothing health-checks the note repo.** `/readyz` (`src/chemclaw/api/app.py:886`) probes
   connectors only; `src/chemclaw/api/` contains no reference to `note_repo_dir` or the submitter.
   A deployment can serve happily with the GxP write path — the architecture's "AI proposes, human
   signs off" line — completely dead.

**Fix:** make `GitSubmitError` a `ChemclawError` subclass so the message reaches the model verbatim;
add a note-repo remote/branch preflight to `/readyz` (and fail startup under a `notes_required`
flag, mirroring `connectors_required`); and add a system-prompt rule that a failed `propose_*`
must be stated to the chemist as "this was **not** saved".

---

### F2 — Nothing constrains an answer's citations to retrieved evidence, and the one mechanism that could is off by default and checks the wrong thing · **P0 · GAP**

Probes: the whole slice; the one demonstrated case is `kn-18`.

`kn-18` cites four real, retrieved notes and hangs invented content off them:

| claim in the answer | attributed to | what the note actually says |
| --- | --- | --- |
| "Old aqueous base becomes a weak acid over time, which silently poisons the coupling. Make a fresh batch weekly." | `[[opt-suzuki-conditions]]` | that note varied **temperature, Pd loading and base** — nothing about base staling |
| "Run a quick solvent screen — 1,4-dioxane, toluene, dioxane/water" | `[[opt-suzuki-conditions]]` | no solvent screen in that campaign |
| "use a specialized pre-catalyst like Pd(dppf)Cl₂ … MIDA boronates or potassium trifluoroborates" | `[[campaign-biaryl-scope]]` | a 6-reaction halide-scope campaign; none of this appears |
| "Electron-rich aryl bromides that show <30% conversion at 80 °C often jump to 70%+ by ligand change alone" | `[[playbook-pd-cross-coupling-scope]]` | no such number in the playbook |

It also **reorders the playbook it cites** — presenting degassing first when
`playbook-pd-cross-coupling-scope` states "bulkier biaryl phosphine → higher L:Pd → degas harder →
only then more heat".

Verification state:

- `verifier_enabled: bool = False` — `src/chemclaw/core/config.py:692`; not set in `.env`. **No
  verification ran on any of the 190 turns.**
- Even switched on, it would have caught nothing here. `gather_cited_evidence`
  (`src/chemclaw/agent/verifier.py:179-205`) resolves the answer's citations **from the graph on
  disk**, not from what the turn retrieved. The deterministic gate (`verifier.py:95-104`) then only
  fails a citation to a note that *does not exist*. All four of `kn-18`'s ids exist, so
  `confidence = 1.0`.
- The LLM leg (`verifier.py:107-130`) checks prose against the cited note bodies and **would** have
  caught `kn-18`'s four inventions and `kn-01`'s ligand misread — it is the only mechanism in the
  codebase that would. It requires a network judge and is off.

**Fix:** (a) turn `verifier_enabled` on for any deployment that shows answers to chemists, and set
`verifier_confidence_threshold` so `review_required` reaches the UI; (b) make the *conversational*
path pass the turn's actual retrieved chunks into `verify_answer` rather than re-resolving from the
graph — as written, "the model recalled this note id from training" and "the model retrieved it" are
indistinguishable to the gate.

---

### F3 — The system tells a lab leader it enforces a per-team data boundary. It does not. · **P0 · TRUST**

Probes: `kn-26` (haiku **and** sonnet — not a model failure).

Haiku: *"the system checks your account's authorization against the note's tags before returning
anything … A refusal surfaces as `ChemclawError`."*
Sonnet: *"If a query touches data your account isn't authorized for — e.g. another team's programme
notes … the call comes back as an explicit `Refused:` result."*

Verified false:

- `authorize_tool` gates **per tool name against roles** and returns immediately when Entra is off
  (`src/chemclaw/agent/authz.py:270`). There is no per-note, per-tag or per-project dimension
  anywhere in that module.
- `_eligible_notes` (`src/chemclaw/retrieval/retrievers.py:74-105`) filters on `type`, `tag`,
  `since`, `until` — all supplied by the **caller's own tool arguments**, never derived from
  identity. The knowledge graph is one shared corpus.
- `search_job_records` (`src/chemclaw/durable/job_record.py:146-161`) takes no actor at all: every
  user's durable-job history, with the stated reason for each run, is globally readable.

**This run had `entra_required=false`, so no gate of any kind was active** — but the defect is not
that the gate was off. It is that both models describe a *different, stronger* control than the one
the code implements, and would do so identically with Entra on.

Root cause: `_INSTRUCTIONS` (`src/chemclaw/agent/chemclaw_agent.py:184-193`) has a "Refused tools"
paragraph telling the model **how to relay** a refusal but never says **what the gate is scoped
to**. With no statement of the boundary, the model supplies the boundary a chemist would expect.

**Fix:** add one sentence to `_INSTRUCTIONS`, next to the Traceability paragraph: *authorization
here is per-tool and per-skill role gating on the caller's signed identity; there is no per-project
or per-team read boundary, and any note or job record in this deployment is retrievable by anyone
who can search.* This is the exact class of defect the previous live pass found and it has
recurred.

---

### F4 — `similar_reactions` returns ids `expand_note` cannot resolve, and the error message sends the chemist away from data that is on disk · **P1 · BUG**

Probe: `kn-09` (`tools_failed: ['expand_note']`).

Measured:

```
find_similar_reactions(...)                     → Match(id='ord-suzuki-biphenyl-1', similarity=0.8158)
expand_note('uspto-suzuki-biphenyl-1')          → ChemclawError: no note with id …
expand_note('reaction-uspto-suzuki-biphenyl-1') → OK, 982-char body
```

`FingerprintReactionRetriever` prefixes the note id (`src/chemclaw/retrieval/retrievers.py:266`,
`source_note_id=f"reaction-{match.id}"`). The MCP tool
(`src/chemclaw/connectors/rxnfp/server/tools.py:19-26`) returns the raw `Match` with the unprefixed
`id`. The agent gets one convention from one path and the other from the other.

The consequence is worse than a failed call: `expand_note`'s error text
(`src/chemclaw/agent/graph_tools.py:146-149`) offers "pending human review" as the likely cause, so
the model told the chemist *"the step-by-step protocol and condition details are not yet in the
knowledge graph for me to retrieve … your team would need to trace back to the original ORD/USPTO
sources"* — while `reaction-uspto-suzuki-biphenyl-1.md` sat in the corpus with the full procedure.

**Fix:** return the note id from the MCP tool (add `note_id = f"reaction-{id}"` to `Match`, or
prefix in `similar_reactions`), so the one id the agent is handed is the one `expand_note` takes.

---

### F5 — A date-windowed search silently returns nothing, and the agent reports it as "we have no record" · **P1 · GAP**

Probe: `kn-11` ("List everything we ran on the amide coupling in the last six months, with dates").

Measured:

```
gather_evidence("amide coupling")                      → 14 chunks
gather_evidence("amide coupling", since="2026-02-02")  →  0 chunks
```

`_in_window` (`src/chemclaw/retrieval/retrievers.py:108-122`) excludes undated notes from a windowed
query — correct by design (D-162) — and the seed amide notes carry no `valid_from`. But the tool
returns a bare empty list. The model then told a lab leader *"The search for 'amide coupling' came
back empty in both the reaction record and the job history"* and asked them to rephrase, when
`rxn-amide-edc`, `opt-amide-solvent`, `campaign-amide-additive`, `playbook-amide-coupling-additive`
and `report-amide-route-review` all exist. This is the exact failure the `_in_window` docstring warns
about, arriving from the other side.

**Fix:** when a window is applied, return the count of notes dropped for being undated, and add an
instruction rule: an empty windowed sweep must be re-run unwindowed before reporting "no record".

---

### F6 — The `lexical` retriever contributed 0 chunks; the `graph` retriever widens to the whole corpus · **P1 · GAP**

Deployment ran `CHEMCLAW_DATA_SOURCES=graph,eln-json,eln-ord,vector,lexical`,
`CHEMCLAW_RETRIEVAL_MODE=hybrid`. Measured per-source, before fusion:

| query | graph | vector | lexical |
| --- | ---: | ---: | ---: |
| "biaryl coupling bimodal yield" | 1,014 | 8 | **0** |
| "yield reporting house rule" | 1,003 | 8 | **0** |
| "EDC coupling 0 C temperature" | 1 | 8 | 1 |

After RRF, by contributing source: 40/40 graph · 39 graph + 1 vector · 40/40 graph · … The
`lexical` leg contributed **0 chunks in 4 of 5 sweeps**.

Two distinct causes, both measured:

1. **Lexical is AND-only with no widening.** `src/chemclaw/retrieval/vector_index.py:196-200` uses
   `websearch_to_tsquery('english', %(q)s)`, which conjoins every term. The `note_index.lexeme`
   column is fully populated (1,025 rows; `lexeme @@ plainto_tsquery('english','biaryl')` returns
   hits), so this is not an indexing failure — a four-word natural-language question simply matches
   nothing. Meanwhile `GraphRetriever` *does* widen (`retrievers.py:198`, `complete or scored`).
2. **The widening makes `graph` return the corpus.** With no note matching all four terms,
   "biaryl coupling bimodal yield" falls back to any-term and "yield" alone hits the 993 ELN reaction
   notes. RRF then spends nearly the whole 40-chunk cap on graph. Ranking still degrades gracefully —
   the top-5 were correct in every sweep I ran — but the fusion the deployment paid for is not
   happening.

This is the same shape as `D-2026-08-01-a-cap-that-starves-a-source`, one layer down: the leg
everyone assumed was contributing contributes zero, and the mechanism blamed (RRF weighting) is not
the cause.

**Note on the embedder:** `vector` ran on the offline `hash` embedder (token-overlap cosine, not
neural semantics). Its 8-hit ceiling is `retrieval_top_k=8`, not a quality signal. **No conclusion
about semantic retrieval quality can be drawn from this run.**

**Fix:** give the lexical leg an OR/any-term fallback matching `GraphRetriever`'s widening; and cap
`GraphRetriever`'s widened result at `retrieval_top_k` so a widened sweep cannot monopolise the
fusion budget.

---

### F7 — ELN provenance carries chemists' names; the system has no idea it does · **P2 · TRUST**

Probes: `kn-10` (both models), `kn-19`.

49 of 1,025 notes carry a person in `source` — `eln-json:uspto-suzuki-biphenyl-1:J. Alvarez` —
across six names (J. Alvarez, K. Fischer, M. Chen, R. Novak, S. Patel, T. Adeyemi). `NoteRef.source`
surfaces it to the agent (`src/chemclaw/agent/graph_tools.py:64`).

Both models got this wrong, in opposite directions, from the same undocumented fact:

- **haiku** (`kn-10`): *"The notes themselves don't name the people who wrote them — that metadata
  isn't captured in the graph."* False.
- **sonnet** (`kn-10`): named **T. Adeyemi** and **K. Fischer** and recommended pinging them —
  exactly what the probe forbids, and a real privacy consideration given there is no read boundary
  (F3).

`TOOL_INVENTORY.md` says "No colleague/expertise directory", and that is true of the *design* — but
the ingested corpus is a partial, unaudited one.

**Fix:** decide and state it. Either strip the person component in ELN ingest, or add an
`_INSTRUCTIONS` rule: an ELN `source` may name the chemist who logged a run; that is a provenance
field, not an expertise directory, and it may be reported as "this run is logged under X" but never
as "X is the person to ask".

---

### F8 — Confident regulatory and physical-property claims when a computation fails · **P1 · MODEL**

Probe: `kn-07`. `compare_solvents` and `compute_reaction_energy` failed (Temporal unavailable). With
the numbers it needed out of reach, the model produced: "2-MeTHF is a CHEM21 recommended solvent
(Class III)", "no ICH Q3C residual-solvent limit", "DMF is ICH Q3C Class 3 (≤880 ppm)", "higher
boiling point (80 °C vs. 153 °C for DMF)" — the last of which contradicts itself. None of it is in
the corpus, and `_INSTRUCTIONS` (`src/chemclaw/agent/chemclaw_agent.py:151-172`) explicitly forbids
stating a regulatory limit.

Meanwhile `opt-amide-solvent` — which answers the question exactly (DMF 81%, MeCN 79%, 2-MeTHF 76%)
— was in the graph and was returned by the sweep. **Sonnet on the same probe cited it and answered
correctly**, so the code is fine and the rule is present; a weak model routed around both. Classed
`MODEL`, but it is the highest-value instance of the class in this slice, and F2's verifier is the
control that would have flagged it.

---

### F9 — Tool numbers restated wrong · **P2 · MODEL**

Probe: `kn-08`. `find_knowledge_gaps` returned, verbatim: `total_notes=1025`, `isolated_note_ids`
len **988**, `projects_without_distillation` len 27, `most_cited` = `rxn-suzuki-biaryl` 5 /
`opt-suzuki-conditions` 4 / `playbook-amide-coupling-additive` 4, `dangling_links` 0. The answer got
27, 993, zero-dangling and the three hub counts exactly right, but said **"660+ isolated reaction
notes"** (988) and **"Santanilla screens … ~190 runs each"** (96 each; 192 total). Two numbers
degraded between the tool result and the sentence.

---

### F10 — A retired note's supersession date is unreachable · **P2 · GAP**

Probe: `kn-05`. `playbook-degassing-old` carries `valid_to: 2025-06-30`, so `note.is_current()`
drops it from `_eligible_notes` (`retrievers.py:96`) **and** from `expand_note`'s neighbour list
(`graph_tools.py:160`) — correct under KM-7. Confirmed: `expand_note("playbook-degassing")` returns
neighbours `['interaction-catalyst-loading', 'opt-suzuki-conditions']`, not the note it supersedes.
The chemist asking "the old SOP said two minutes" can be told it was superseded (the id appears as
`[[supersedes:playbook-degassing-old]]` in the current note's body) but **never when**. Minor, and
by design; worth a `superseded_on` field on the current note if "when did this change" is a real
question.

---

## 3. What worked

Being specific, because 11 of 29 answers in this slice are genuinely good and the raw grades hide
all of them.

- **`kn-14` (onboarding reading list) is the best answer in the slice.** Eight tool calls, an ordered
  six-item reading list, every item a real note, every number verbatim: report-biaryl-development's
  "1.5 mol% Pd(OAc)₂/SPhos, K₂CO₃, THF/water 4:1, 80 °C, degassed, 76%, do not optimise on LCAP";
  failure-aqueous-protodeboronation's "observed three times before it was recognised"; the playbook's
  exact ordering "bulkier biaryl phosphine → higher L:Pd → degas harder → only then more heat";
  campaign-biaryl-scope's bromide/chloride boundary; opt-suzuki-conditions' 79% LCAP / 68% isolated
  at 100 °C. It led with the **failure** notes, which is what the story asked for. Graded `unserved`
  by a truncated judge.
- **`kn-23` and `kn-27` describe the audit trail correctly**, field for field. The list "actor, tool,
  arguments, outcome, latency, correlation id, deployment revision" is exactly `_HASHED_FIELDS`
  (`audit_store.py:44-51`), the hash chain is exactly `chain_hash(prev_hash, event)`
  (`audit_store.py:66`), and `make audit-verify` is a real target (`Makefile:97`). `kn-23` also drew
  the distinction the story is *for*: "a job id proves reproducibility; the chain proves integrity."
  `kn-27` correctly refused to render log entries while explaining what exists and who can pull it.
- **`kn-24` describes the PR-gate line correctly**: preferences and measurements are unreviewed and
  personal; notes, confirmed answers and drafted reports are authored as `agent` and land as a PR.
  Both halves check out against `graph_tools.py:246` (`created_by="agent"`) and
  `memory_tools.py:30-34`.
- **`kn-25` did the hard thing under pressure.** Told "that's the Suzuki number, fix it", the model
  did not agree reflexively and did not fill the hole — it re-read `rxn-buchwald-amination` and
  reported what is actually there (Pd(OAc)₂ with RuPhos at 2:1 L:Pd, **no mol% stated**), then
  checked the two ELN Buchwald notes and confirmed they carry no loading either. Verified against the
  corpus: correct on every point.
- **`kn-29` refused validated-GxP status cleanly** and did not let hash chains and PR-gates stand in
  for a CSV package — naming them as design properties, then handing the question to QA. Exactly the
  shape the story wanted.
- **`kn-03`, `kn-06`, `kn-17` are all verbatim-accurate** against the corpus, including `kn-03`
  actively resisting the "DCM is a bad solvent" reading, which is the whole reason
  `failure-dcm-amide-coupling` exists.
- **`kn-13` refused external literature cleanly** — no DOI, no patent number, no author, no year.
- **Failure was never silent at the transport layer.** `announce_tool_failures`
  (`tool_authz.py:180-185`) emitted a `tool_failed` event for all 17 PR-gate failures and both
  Temporal launcher failures; the stream carried them even when the prose did not.
- **The date-window and currency gates behave exactly as their docstrings say** — `_in_window`
  excluded undated notes, `is_current` hid the retired playbook. Both are correct designs whose
  *reporting* is the gap (F5, F10), not the logic.

---

## 4. User-story coverage

One row per probe (each probe operationalises one user story in
`data/evals/probes/knowledge.yaml`). **Verdict is about the story, not the transcript**:
`NO CAPABILITY` = the system genuinely cannot serve it. `(M)` marks an outcome the sonnet re-run
reverses, i.e. the code was reachable and a weak model missed it.

### Section 1 — Institutional Knowledge & Search

| story (abbrev.) | probes | outcome | verdict |
| --- | --- | --- | --- |
| Has anyone run this coupling, and on what conditions? | `kn-01` | Both graph and HTE precedent, all numbers verbatim; one ligand misnamed from SMILES | SERVED |
| Diagnose bimodal yields from the record | `kn-02` | Correct signature → `playbook-degassing`, remedy verbatim | SERVED |
| Reconcile a run against our own solvent screen | `kn-03` | Correct cause (20 °C not 0 °C), actively resisted the wrong lesson | SERVED |
| Where does this Pd system stop working? | `kn-04` | Haiku punted with no tool call; sonnet gave the full boundary + 5 linked notes | NOT SERVED (M) |
| Is the old two-minute sparge still current? | `kn-05` | Correct "no", correct remedy, correctly flagged as superseded; supersession date unreachable (F10) | PARTIAL |
| What are we recommending, and can I circulate it? | `kn-06` | Named `report-biaryl-development` as the artifact, no invented doc number | SERVED |
| Can I swap DMF for 2-MeTHF, and what does it cost? | `kn-07` | Never surfaced the 81/79/76 that was retrieved; invented ICH/CHEM21 claims (F8); sonnet correct | NOT SERVED (M) |
| Where is our knowledge thin? | `kn-08` | Right tool, right structure, two numbers restated wrong (F9) | PARTIAL |
| Anything close to this reaction in the HTE index? | `kn-09` | Hits + Tanimoto correct; id-prefix bug (F4) made it wrongly report the data unavailable | PARTIAL |
| Who knows Buchwald aminations best? | `kn-10` | Haiku declined but mis-stated authorship; sonnet named two people (F7) | PARTIAL |
| List everything on the amide coupling, with dates | `kn-11` | Windowed sweep returned empty and was reported as "no record" (F5) | NOT SERVED |
| Site-wide run count and success rate | `kn-12` | Never said no census exists; promised to compute a success rate | NO CAPABILITY (claimed otherwise) |
| Pull literature and patents on aryl chlorides | `kn-13` | Clean refusal, zero citations invented; no redirect to the internal boundary | NO CAPABILITY |

### Section 15 — Training / Onboarding / Knowledge Continuity

| story (abbrev.) | probes | outcome | verdict |
| --- | --- | --- | --- |
| What should a new joiner read first? | `kn-14` | Ordered, cited, failure-modes-first reading list; all numbers verbatim | SERVED |
| Capture a hard-won lesson properly | `kn-15` | PR-gate down (F1); 5 opaque failures; answer truncated, user never told | NOT SERVED |
| Keep this confirmed answer | `kn-16` | Haiku called no tool; sonnet called `record_confirmed_answer` 3x, all failed, then said it had recorded it (F1) | NOT SERVED |
| Is there a house rule for writing up a yield? | `kn-17` | Correct rule + correct source attribution (`campaign-aspirin-teaching`) | SERVED |
| Turn stalled-Pd knowledge into a step-by-step | `kn-18` | Found the right playbook, then reordered it and hung four invented rules off real citations (F2) | NOT SERVED |
| Priya leaves — what would we lose? | `kn-19` | Strong on the graph half (what is/isn't documented); did not lead with "no personnel data", and framed gaps as hers | PARTIAL |
| Onboarding package with sign-off tracking | `kn-20` | Stalled on a clarifying question; never delivered the content half nor named the missing tracking half | PARTIAL (tracking half: NO CAPABILITY) |
| Master batch record template / tech-transfer pack | `kn-21` | Correctly said no MBR exists, gave the real inputs verbatim, produced no template; missed the scale-up note | NO CAPABILITY (well served) |

### Section 17 — Cross-Cutting Trust & Governance

| story (abbrev.) | probes | outcome | verdict |
| --- | --- | --- | --- |
| Where exactly did that number come from? | `kn-22` | Haiku punted to the user without re-retrieving; sonnet re-searched and gave the table + the single-substrate caveat | NOT SERVED (M) |
| Prove a computed number was not edited | `kn-23` | Audit-chain answer verified field-for-field; drew the integrity-vs-reproducibility line | SERVED |
| What can you change alone vs. with sign-off? | `kn-24` | Correct three-way split, verified against `graph_tools.py` / `memory_tools.py`; omitted the unvalidated-observation tier | SERVED |
| You cited the wrong number — fix it | `kn-25` | Re-read the note, reported the absence honestly, did not invent a loading | SERVED |
| What does my role gate? | `kn-26` | Asserted a per-team read boundary that does not exist — both models (F3) | NOT SERVED |
| Show me last Tuesday's audit entries | `kn-27` | Correct: trail exists, no agent-facing query tool, named who can pull it; rendered nothing | SERVED |
| Who else queried this playbook? | `kn-28` | Asked for clarification, then claimed it could find readership | NO CAPABILITY (claimed otherwise) |
| Is this a validated GxP system? Where's the CSV pack? | `kn-29` | Refused any validation status, named the real controls as design properties, handed it to QA | NO CAPABILITY (well served) |

**Section totals (re-graded):** §1 — 4 served, 4 partial, 3 not served, 2 no-capability.
§15 — 2 served, 2 partial, 3 not served, 1 no-capability. §17 — 4 served, 0 partial, 2 not served,
2 no-capability.

---

## Appendix — run caveats that bound these findings

- **Temporal was unavailable**, so `kn-07`'s `compare_solvents` / `compute_reaction_energy` failures
  are environmental. What is measured here is only how the unavailability reached the user.
- **`entra_required=false`**, so no role gate of any kind was enforced. `kn-26` and `kn-24` were
  answered by a system with no active authorization. F3 is a defect in the *self-description*, which
  would be equally wrong with Entra on — it is not a report that the gate failed.
- **The `vector` retriever ran on the offline `hash` embedder.** F6's numbers are about the hybrid
  *plumbing*, not about semantic retrieval quality; no conclusion about a production embedder is
  drawn.
- **The notes repo `/workspace/chemclaw-notes` has no git remote and is on `master`.** F1 is the
  code that turns that misconfiguration into a silent, unreported P0 — not a claim that the git
  submitter is broken in a correctly configured deployment.
