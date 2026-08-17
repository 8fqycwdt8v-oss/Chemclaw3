# kg / retrieval / memory — CORRECTNESS · reachability + consequence verification

Lens: is the trigger reachable by a real caller in a real deployment, and is the consequence what is
claimed?

**In scope**: one finding. The findings file marks exactly one item **high** and none **critical**;
the other four are medium/medium/medium/low and were not examined.

---

## `reindex_notes` stamps the post-edit fingerprint onto pre-edit text, so a note edited during the run is never re-embedded again

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

### What I did

**1. Confirmed the mechanism with a real concurrent writer, no patching of the code under test.**
The reporter's repro monkeypatched `vector_index.load_notes` to write the file itself. I did not —
`/tmp/rr/sweep2.py` builds a 3000-note corpus, starts a plain `threading.Thread` that rewrites one
note after a delay, and runs the unmodified `reindex_notes`. The only patching is observational
(timestamps around `kg.graph.read_note` and `note_file_fingerprints`, both pass-through):

```
delay=0.1: parse_of_target@+0.069  write@+0.101  fingerprint_scan_started@+0.431
   stored_text='compound-00000 compound  OLD BODY 0 about benz'
   stored_fp=1786954957633126354:119  disk_fp=1786954957633126354:119  equal=True
   disk_body='NEW BODY about toluene'
   subsequent run embedded: 0
   subsequent run embedded: 0
   subsequent run embedded: 0
```

The ordering defect is real and I reproduce it: the file's content is read at +0.069, the write lands
at +0.101, and `note_file_fingerprints` stats it at +0.431, so the row is written with the *new*
fingerprint against the *old* text and three further runs embed nothing. A sweep
(`/tmp/rr/sweep.py`) hits it at every delay from 0.10 s to 0.30 s and misses only at 0.05 s, where
the write beat the parse.

**2. Traced the trigger to the outermost entry point.**
- `deploy/helm/chemclaw/templates/deployment-workers.yaml:53` puts `chemclaw.knowledgeSidecar` in the
  **same pod** as the background worker that runs `NoteReindexWorkflow`, and `_helpers.tpl:339` runs
  it as `chemclaw-knowledge-sync loop` over `chemclaw.knowledgeMounts` — the same volume the worker
  reads. So a real concurrent writer of `knowledge_path` genuinely exists in the shipped topology.
  `kg/git_submitter.py:458` writes only inside a private `.git/` worktree, so the sidecar is the
  *only* writer of the read tree. This half of the reporter's reachability argument holds.
- But the two settings that make the index matter are **off by default**:
  `core/config/retrieval.py:172` — `note_reindex_enabled: bool = False`, so
  `durable/schedules.py:118` plants no schedule; and `core/config/sources.py:45` —
  `data_sources: str = "graph,eln-json"`, so neither `vector` nor `lexical` is enabled.
  `grep -n "CHEMCLAW_DATA_SOURCES\|CHEMCLAW_NOTE_REINDEX\|CHEMCLAW_RETRIEVAL_MODE"
  deploy/helm/chemclaw/values.yaml` returns **only a comment**, no key — the shipped chart does not
  turn either on. The trigger therefore requires an opt-in hybrid deployment; it is not the default
  install.

**3. Measured the window, against the claim that it is "occupied continuously".**

```
notes=38 load=0.0062s fp_scan=0.0006s -> worst-case race window per note ~0.0068s
as a fraction of a 300s rsync period: 0.00227%
```

(`/tmp/rr/realtime.py`, against the repo's own `knowledge/`.) At 3000 notes the same measurement is
0.307 s / 0.028 s, i.e. ~0.33 s. Against a 300 s rsync period and a 60-minute reindex schedule
(`note_reindex_schedule_minutes: float = Field(default=60.0)`), the per-run overlap chance is on the
order of 0.1 %, and only for a tick that actually carries a delta for that specific note. The
merge-webhook path (`api/routes/proposals.py:143`) does not raise the correlation — it fires the
reindex *at merge time*, when the sidecar has not yet synced the merge, so that run reads a
self-consistent old tree.

**4. Checked the consequence — what a chemist is actually shown.** This is where the finding breaks.
`retrieval/retrievers.py:317 _chunks_from_hits` maps a hit to a chunk via `_chunk_for(note, …)`,
where `note` comes from `_eligible_notes(self._dir, …)` — a **fresh on-disk load** — and
`_chunk_for` builds `content=_excerpt(note.body)`. The indexed text is never rendered.
`/tmp/rr/consequence.py` runs it end to end with a safety-shaped correction:

```
indexed text (stale?): compound-00000 compound  OLD BODY 0 tolerates palladium catalysis
on-disk body        : CORRECTED: hydrogen cyanide evolves on acidification
vector   q='hydrogen cyanide': target_retrieved=False
lexical  q='cyanide': target_retrieved=False
graph    q='cyanide': target_retrieved=True | content shown: CORRECTED: hydrogen cyanide evolves on acidification
```

**5. Checked "never re-embedded again".** `/tmp/rr/heal.py`:

```
after race, stored: compound-00000 compound  OLD BODY 0 about be
no-op run: 0
after later edit, embedded: 1
stored now: compound-00000 compound  REV3 about xylene
```

### Why

The mechanism is genuine and the two-line reorder the reporter proposes is strictly better than what
is there. I would take the fix. But both things my lens targets are overstated:

**The consequence is wrong as written.** "The dense and lexical retrieval legs answer from the
superseded body permanently" does not happen. The index stores text and an embedding used *only* for
matching and ranking; every chunk that reaches the agent carries `_excerpt(note.body)` read live off
disk. So a chemist is **never shown superseded text** by this defect — including in the
safety-shaped case I built deliberately: the corrected hazard sentence is what the retrieved chunk
prints. The real harm is the opposite shape and much narrower: a **recall/ranking miss** in two
optional legs, where the note is not found by terms only its corrected body contains. And the
always-enabled `graph` leg (`data_sources` default includes it, substring over live notes) *does*
find it and shows the correction, as the run above prints — so hybrid retrieval as a whole is not
blind to the update, two of its three legs are.

**"Never re-embedded" is wrong.** The staleness is scoped to one revision, not to the note: the
note's *next* edit moves the fingerprint again and the row jumps straight to the newest text
(measured above, `after later edit, embedded: 1`). The finding lists only `--full` and an
`embedding_key` change as exits and misses the ordinary one. For a note that is written once and
frozen the effect is indeed indefinite, which is why this is not REFUTED.

**"That window is occupied continuously" is wrong.** Measured, it is ~7 ms on the shipped corpus and
~0.33 s at 3000 notes, against a 300 s rsync period and an hourly job — a narrow race of order
0.1 % per run, not a standing condition. It additionally requires an operator to have enabled
`vector`/`lexical` and `note_reindex_enabled`, neither of which the code defaults or the Helm values
do.

Real, silent, no metric, cheap to fix, but a rare race in an opt-in configuration whose worst
outcome is a degraded ranking rather than a wrong answer. Medium.

**One thing the reporter missed that affects the proposed fix.** Reading `note_file_fingerprints`
*before* `load_notes` opens the mirror window: a note created in between is in `notes` but absent
from `current_fingerprints`, so `_needs_embedding` (vector_index.py:456) takes its `fingerprint is
None` branch and logs `"note %r has no file fingerprint (its filename does not match its id)"` — a
WARNING that names the wrong cause, and whose docstring at :452 asserts "the only way to be here is
that mismatch". The behaviour still self-heals (the row is written with `fingerprint=""`, which
`fingerprints()` omits, so the next run re-embeds it), but the reorder should soften that log line
or the fix trades a silent staleness bug for a misleading operator warning.

**Working-tree hygiene**: no source file in the checkout was modified. All scripts and corpora are
under `/tmp/rr/`.
