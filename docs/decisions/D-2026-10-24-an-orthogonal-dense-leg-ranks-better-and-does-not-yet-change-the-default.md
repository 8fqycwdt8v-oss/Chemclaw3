# D-2026-10-24-an-orthogonal-dense-leg-ranks-better-and-does-not-yet-change-the-default — An orthogonal dense leg ranks better and does not yet change the default

**Status:** accepted · **Date:** 2026-10-24

## Context

`D-2026-09-15-a-weight-small-enough-to-work-is-a-removal-spelled-as-a-number` recorded four
remedies for RRF's correlated legs as no-ops and named the fifth question: an
`openai_compatible` embedder, which makes the dense leg genuinely orthogonal. Every earlier figure
used the offline `hash` embedder. This measures the question.

Setup: `make retrieval-arms` on a scratch database (the shared one untouched), the shipped
`knowledge/` corpus (41 notes), 21 probes, 48 labelled (query, note) pairs. Embedder:
`nomic-embed-text` (768 dimensions) served by Ollama through `/v1/embeddings`, reached by
`embedding_provider=openai_compatible` with no code change (the client sends `model` and `input`
in batches; `dimensions` only when `embedding_request_dimensions` is set).

Controls. At the ADR's own commit (`58fa6ec2`) the instrument reproduces that ADR's table to the
digit, so it is not drifting. Every arm was run three times per embedder and was byte-identical
each time: the instrument is deterministic, so a repeat adds no variance estimate and the
uncertainty is the gold set's size. The `graph+lexical` arm does not touch the embedder and reads
36/48, 3.78 under both embedders, as it must.

## Measured

Mean/median are over found gold notes; `up`/`down`/`lost` are against the shipped round-robin arm
of the same embedder.

`hash` (the shipped default, current corpus):

| arm | found | mean | median | top-3 | top-5 | up | down | lost |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| round-robin, 3 legs (shipped) | 39/48 | 4.87 | 4.0 | 19 | 25 | — | — | — |
| RRF, 3 legs | 39/48 | 4.67 | 4.0 | 18 | 26 | 16 | 12 | 0 |
| RRF, 3 legs, vector at 0.5 | 39/48 | 4.62 | 4.0 | 18 | 27 | 18 | 12 | 0 |
| RRF, 3 legs, vector at 0.1 | 39/48 | 4.62 | 4.0 | 16 | 27 | 18 | 13 | 0 |
| RRF, 2 legs (graph+lexical) | 36/48 | 3.78 | 3.5 | 18 | 27 | 17 | 4 | 3 |
| RRF, 2 legs (graph+vector) | 37/48 | 5.27 | 4.0 | 16 | 21 | 11 | 15 | 2 |

`nomic-embed-text`, no prefixes (the code has no hook for them):

| arm | found | mean | median | top-3 | top-5 | up | down | lost |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| round-robin, 3 legs (shipped) | 42/48 | 4.57 | 3.0 | 24 | 31 | — | — | — |
| RRF, 3 legs | 42/48 | 4.29 | 2.0 | 27 | 30 | 16 | 8 | 0 |
| RRF, 3 legs, vector at 0.5 | 42/48 | 4.36 | 2.0 | 26 | 30 | 17 | 8 | 0 |
| RRF, 3 legs, vector at 0.1 | 42/48 | 4.31 | 2.0 | 27 | 28 | 20 | 7 | 0 |
| RRF, 2 legs (graph+lexical) | 36/48 | 3.78 | 3.5 | 18 | 27 | 10 | 13 | 6 |
| RRF, 2 legs (graph+vector) | 42/48 | 4.12 | 2.0 | 29 | 32 | 19 | 5 | 0 |

Prefixed (`search_document:` at index time, `search_query:` at query time, applied by a scratch
`sitecustomize`, not by repository code), two runs, identical: round-robin 42/48, 4.67, 3.0, 24, 30;
RRF 3 legs 4.29, 2.5, 26, 30 (18 up, 6 down); RRF graph+vector 4.02, 2.5, 28, 32 (23 up, 4 down).
Recall is unchanged and the ordering of arms is the same, so the prefix does not decide the answer
and no hook is proposed.

## Findings

1. **A real embedder finds more.** Round-robin recall goes 39/48 to 42/48, and top-3 19 to 24:
   the dense leg is a better ranker, not only a different one.
2. **With it, RRF beats round-robin on rank at equal recall.** Mean 4.57 to 4.29, median 3.0 to 2.0,
   top-3 24 to 27, 16 pairs up against 8 down, none lost. Top-5 falls by one (31 to 30). The
   earlier claim that `hybrid` is worse than `graph` is true under `hash` only for top-3 and not
   at all for mean rank; under an orthogonal leg it reverses.
3. **The weight is still a no-op.** `vector` at 0.5 and 0.1 moves the mean by at most 0.07 and
   top-3 by at most one, as the `k=60` arithmetic in the earlier ADR predicts.
4. **Dropping the lexical leg is the best arm** (graph+vector: 4.12, top-3 29, top-5 32, no loss)
   and dropping the dense leg is now clearly wrong (6 gold notes lost). Under `hash` it was the
   reverse. Which leg is redundant depends on the embedder.
5. **The evidence is not enough to flip a default.** 21 probes share 41 notes; the 48 pairs are not
   independent (two or more per probe), and a sign test over 16 up / 8 down gives p of about 0.15
   even treating them as independent. The prefixed run's 18 / 6 reaches about 0.02, so the sign of
   the effect holds but its size depends on a choice the code does not make. And the shipped
   `embedding_provider` is `hash`, under which `hybrid` is a mixed result.

## Options

1. **Keep `retrieval_mode=graph` and the fusion knobs; record the measurement.** Costs nothing and
   changes no behaviour; leaves a measured rank gain on the table for deployments with a gateway.
2. **Flip the default to `hybrid`.** Right for a deployment with a real embedder, mixed under the
   shipped `hash`, and resting on 21 probes. A default is a claim about every deployment.
3. **Flip to `hybrid` only when `embedding_provider` is not `hash`.** A conditional default is two
   behaviours behind one name, decided on this evidence.
4. **Remove `retrieval_source_weights` and `retrieval_fusion_k`.** Finding 3 says they do nothing
   in the range anyone would try, but the earlier ADR kept them as the instrument's subject and a
   leg-removal arm is configured elsewhere; deleting settings is a separate change with its own
   enumeration of sites.

## Decision

Option 1. `retrieval_mode` stays `graph`. The gold set, not the embedder, is the limit: the same
table on a larger set decides between 1 and 3, so growing it is the work, and it is a backlog row
(`docs/planning/BACKLOG.md`) rather than a guess recorded as a setting. A deployment that runs a
gateway embedder may set `CHEMCLAW_RETRIEVAL_MODE=hybrid` today; this measurement supports it for
rank and shows no recall cost.

## Consequences

No code changes. `make retrieval-arms` with the settings in the Context reproduces the second table;
it needs a served `/embeddings` route with the stated model and `make db-migrate` at width 768.
The table is a claim about this corpus on this day, as the earlier ADR said of its own.

Revisit when: the labelled gold set reaches 100 (query, note) pairs across at least 40 probes, or a
deployment's embedder is chosen; rerun `make retrieval-arms` and flip the default under option 3 if
`RRF, 3 legs` has equal recall and a better mean rank than the shipped arm there.
