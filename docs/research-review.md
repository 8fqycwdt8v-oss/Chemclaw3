# External Architecture Review (2025/2026 literature)

Independent, source-backed pressure-test of the Chemclaw3 design against current
(2025/2026) work on scientific/chemical multi-agent systems. Produced by a deep-research
harness (fan-out search → fetch → 3-vote adversarial verification → synthesis): 15 sources,
29 claims extracted, 25 verified, **3 refuted and dropped**, 12 findings retained.

**Read this as input, not verdict.** Most sources are review articles, a market-blog, and a
few non-replicated preprints with author-reported metrics. Treat concrete numbers as
directional. See *Caveats* at the end.

---

## Bottom line

The **core shape is validated**: an orchestrator/supervisor over specialized, tool-equipped
agents is the established standard pattern for chemical multi-agent systems (F2, F3). But the
review surfaces **real capability gaps** in our four-layer plan and **two design cautions**
that should change how we build the Skills, knowledge-graph, and memory layers.

One nuance worth internalizing: orchestration need **not** be conversational — a validated
chemical multi-agent system drives four specialized agents via Monte-Carlo Tree Search, not a
chat loop (F2). Our MAF-conversational choice is fine, but it's a choice, not a given.

---

## Capability gaps (not covered by the current plan)

The canonical capability checklist is a **five-module framework** — Comprehension, Design,
Execution, Analysis, Optimization (JACS Au review, F3). Measured against it, the plan is
missing:

1. **Retrosynthesis + reaction prediction + closed-loop lab automation** as a unified
   capability layer. State-of-the-art platforms integrate template-free (LLM-based)
   retrosynthesis and Bayesian optimization as *core* features (F5). — *biggest domain gap.*
2. **DoE / Bayesian optimization** for reaction/process optimization (F5).
3. **Lab automation / SiLA2 closed-loop** execution (F5) — open question below.
4. **Process flowsheet synthesis/simulation** (NL spec → validated flowsheet, e.g. Aspen
   HYSYS), already addressed by dedicated multi-agent workflows (F6).
5. **Heterogeneous multimodal analytical data** (spectra, images) ingestion (F4).
6. **Domain foundation models** with chemistry-specific modalities (F4).
7. **Chemical/biological safety layer** — Entra-ID/RBAC is *IT* security, **not** chemical/bio
   safety or GxP data-integrity controls (F4). Distinct, currently absent.
8. **Evaluation / metrics layer** — reproducible agent evaluation needs concrete benchmarks
   (step count, time-to-in-vitro) and **green-chemistry metrics** (E-Factor, Process Mass
   Intensity) (F7). We have quality gates for *code*, none for *scientific output*.

Not every gap must be built — several are natural "defer until measured" items. But each
should be a conscious decision, not an omission. Triaged into `BACKLOG.md`/`DEFERRED.md`.

---

## Design cautions (change how we build existing layers)

- **Tools are not uniformly beneficial** (F8, F9). ChemToolAgent does *not* consistently beat
  base LLMs; tool augmentation is strongly task-dependent and adds its **own error class**
  (tool-errors dominate on specialized tasks, reasoning-errors on general ones). → The
  Skills/MCP layer must be applied **selectively and measured per task**, not universally. This
  *reinforces* our "off-the-shelf, defer until measured" leitmotif and argues for the missing
  evaluation layer.
- **No memory type is universally superior** (F10). Naive vector-RAG degrades on
  long-horizon/temporal tasks; vector vs. graph vs. event-log is a latency/hit-rate/failure-mode
  trade-off. → The CoALA memory layer (§9) should be designed against benchmarks (DMR,
  LongMemEval), not assumed. Our graph-traversal-over-vectors choice is consistent with this,
  but the episodic/temporal layer needs its own validation.
- **Reliable NL querying of a property graph needs a modular pipeline** — query generation +
  schema/entity grounding + execution + iterative correction — not a single LLM call; a
  multi-agent text-to-Cypher workflow measurably improves quality across models (F11). → For
  *non-trivial* natural-language queries, a naive Markdown/NetworkX index is a risk. Mitigation:
  our design uses **deterministic graph traversal**, not NL-to-query, which sidesteps most of
  this — but if we ever expose free-form NL graph queries, adopt the schema-grounded pipeline.

---

## Open questions the review could not resolve

1. **Temporal vs. Restate / DBOS / Prefect / Dapr** for durable execution of long HPC/SLURM
   jobs — no solid head-to-head comparative sources on cost, determinism constraints around
   LLM calls, and operability. Our Temporal choice stands on maturity/fit, not a benchmark.
2. **When does Markdown+NetworkX tip over to a real property-graph DB** (Neo4j/Memgraph +
   GraphRAG correction loop), and is the operational cost worth it for an R&D team this size?
3. **Concrete integration** for lab automation/SiLA2, DoE/Bayesian optimization, and
   retrosynthesis as their own capability layer, wired to Skills + episodic memory.
4. **Domain safety/compliance layer** (chemical/bio hazards, GxP, data integrity) beyond
   Entra-ID/RBAC — do established 2025/2026 reference implementations exist?

---

## What was refuted (dropped, do not act on)

The verification pass killed three plausible-but-unsupported claims, including "multi-agent
coordination per se reduces hallucination" and "the text-to-Cypher accuracy gains are
consistent across all LLM backends." They are **not** part of the findings above.

## Caveats

- **Maturity risk confirmed.** The field is "recent but rapidly evolving" with preliminary
  results and no stabilized best practices (F1). MAF reaches GA only April 2026 and the
  Agent-Skills/SKILL.md standard is young; no source evaluated MAF or SKILL.md *directly* — our
  maturity statements extrapolate from general field-immaturity.
- **Source strength.** Several key supports are secondary/review sources (ScienceDirect, Wiley,
  JACS Au reviews, one market blog) or single non-replicated preprints with author-reported
  metrics (arXiv 2511.08274, 2601.06776). Concrete numbers are author-side, not independently
  reproduced.
- **Fetch note.** Some direct fetches (arxiv.org, ACS DOI) returned 403 via the egress proxy;
  verification relied on search extracts that reproduced citations verbatim. One fetch subagent
  triggered a security-policy warning for probing the documented proxy-status endpoint while
  handling those 403s — reviewed as a benign false positive (proxy troubleshooting, not
  credential access), and it did not feed the findings.

## Sources

- ScienceDirect S2211339825001212 (= arXiv 2508.07880) — review, chemical-engineering MAS
- JACS Au 10.1021/jacsau.6c00213 — review, agent-enabled self-driving labs (5-module framework)
- Wiley Med. Res. Rev. 10.1002/med.70074 — AI synthesis platforms
- arXiv 2601.06776 (AAAI 2026) — 4-agent chemical MAS via MCTS
- arXiv 2411.07228 — ChemToolAgent (tool-augmentation error analysis)
- arXiv 2511.08274 — multi-agent text-to-Cypher / GraphRAG
- marktechpost (2025-11-10) — vector vs. graph vs. event-log memory
- Durable-execution / framework comparisons: DBOS-vs-Temporal (tiarebalbi), Spheron,
  ZenML Temporal-alternatives, Dapr-vs-Temporal (oneuptime), LangChain & qubittool &
  openagents framework comparisons
