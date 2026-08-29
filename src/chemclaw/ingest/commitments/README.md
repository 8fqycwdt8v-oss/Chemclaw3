# `ingest/commitments/` — the commitment mirror

**What a programme has committed to, mirrored in from the system that owns it.**

Nine of nineteen `manager` bucket-C probes in `data/evals/probes/` needed one object this schema did
not have: a unit of committed work. Seventy-three migrations, and `project` was a nullable text tag
on `reaction_records` — a facet on a row, not an entity.

## A mirror, not a plan

The organisation already runs a portfolio tool and **that tool is the truth**. Nothing here plans,
schedules, levels resources or computes a critical path, and a deployment that let it try would have
two answers to "when does this land" — the second one wrong more often.

What this adds is the join no portfolio tool can compute: between a slipping milestone and the
*chemistry* slipping it. `note_ids`, `job_ids` and `compounds` are how the source states that link,
and they are the reason the mirror is worth keeping at all. A commitment with no link is a row the
portfolio tool already holds and holds better.

## The third half, not a fourth seam

`ingest/sources/base.py` composes optional halves and argues for it: the capabilities are disjoint,
so the seam composes them rather than merging them into one fat interface. A commitments half is the
third such capability — its own Protocol, its own DTO, `commitments:` in the manifest.

The 2026-08-28 audit found the source seam *corpus-shaped* (records become chunks, notes,
fingerprints) and a portfolio export not a corpus but typed entities with lifecycles. Both are true;
the conclusion is not a new seam. Adding a half costs a field, where a fourth seam would cost a
manifest, a registry, a validator and a discovery path an operator has to learn.

## Four rules

1. **Read-only.** `ingest/sources/README.md`'s rule that a source "cannot acquire a write path by
   declaring one" is unchanged: mirroring a milestone in does not confer the ability to move one.
   Writing back belongs to the effector seam.
2. **It converges rather than accumulating.** Upserted on `(source, external_id)` — two systems may
   both call something `PRJ-14` — so re-reading a whole snapshot is free and is the normal case.
3. **Every reading reports its own staleness.** A mirror's characteristic failure is being stale,
   not wrong: the export stops running and the numbers keep answering. `observed_at` is on the
   answer rather than something a reader has to think to ask for.
4. **It infers nothing.** A row missing a required field is rejected and counted rather than
   repaired. A mirror that guessed a due date would be asserting a plan, which is the one thing this
   tier must not do.

## Layout

| Module | What it is |
| --- | --- |
| `models.py` | `Commitment`, and the two vocabularies a surface groups by. |
| `adapter.py` | `CommitmentAdapter` — the third half of the `DataSource` seam. |
| `store.py` | The upsert and the two readings. |
| `json_export.py` | A half over a JSON extract on disk — the shape a portfolio tool writes. |

The durable mirror is `durable/commitment_sync.py`; the agent-facing surface is one read tool,
`review_commitments`, in `chemclaw.agent.commitment_tools`.
