# Repro verdicts — kg / retrieval / memory — CORRECTNESS

Scope: findings marked **critical** or **high** only. The file has exactly one — the
`reindex_notes` fingerprint-ordering finding. The other four are medium (×3) and low (×1) and were
not examined.

---

## `reindex_notes` stamps the post-edit fingerprint onto pre-edit text, so a note edited during the run is never re-embedded again

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

### What I did

I did not run `/tmp/repro_reindex.py` and did not monkeypatch `load_notes` (the reporter's stand-in
for an rsync landing mid-run is scaffolding, and a finding that needs it is a finding about it).
Instead I wrote `/tmp/verify_reindex.py`: a real temp corpus of 400 real note files, a real
`threading.Thread` rewriting them on disk, and the real `reindex_notes` from
`src/chemclaw/retrieval/vector_index.py` against `InMemoryNoteIndex` — no chemclaw symbol replaced.
The writer is then stopped and `reindex_notes` is run four more times to quiescence; anything still
mismatched is permanently stale. The same script runs a second arm that is byte-for-byte the shipped
function with only the two lines swapped (fingerprint scan before the parse), so the arms differ in
nothing but the ordering under test.

```
$ uv run python /tmp/verify_reindex.py 400
[SHIPPED] raced pass embedded 400; quiescent passes [8, 0, 0, 0]
[SHIPPED] permanently stale notes after quiescence: 63
[SHIPPED]   e.g. compound-0000
[SHIPPED]   indexed text : 'compound-0000 compound  compound BODY-v2 yy'
[SHIPPED]   on-disk text : 'compound-0000 compound  compound BODY-v3 yyy'
[SHIPPED]   stored fp    : 1786954868489378150:171
[SHIPPED]   on-disk fp   : 1786954868489378150:171
[REORDERED] raced pass embedded 399; quiescent passes [259, 0, 0, 0]
[REORDERED] permanently stale notes after quiescence: 0
```

63 of 400 notes hold the superseded text under a fingerprint that is *bit-identical* to the current
on-disk one, and four further passes embed nothing — `_needs_embedding` (line 465,
`fingerprint != stored.get(note_id)`) compares equal forever. The reordered arm converges to zero
stale notes, which isolates the ordering as the cause rather than the race.

Window width, measured with `/tmp/window.py` (`load_notes` then `note_file_fingerprints`, warm FS):

```
repo knowledge/  n=    39  parse=     5.9 ms  fp-scan=   0.5 ms  => window ~6.4 ms
synthetic 10k    n= 10000  parse=  1433.3 ms  fp-scan=  98.8 ms  => window ~1532.1 ms
```

Both walks are `sorted(rglob)`, so the exposure for essentially every file is the whole parse
duration, not a hairline between two adjacent statements.

Trigger, checked in the shipped topology rather than taken from the finding:
`deploy/knowledge-sync.sh:189` is `rsync -a --delete "${target}/${notes_subdir}/" "${publish_dir}/"`
on a `sleep "${interval}"` loop (`:225`, default 300 s, `:51`), and
`deploy/helm/chemclaw/templates/_helpers.tpl:271` sets
`CHEMCLAW_KNOWLEDGE_PUBLISH_DIR = {{ .Values.knowledge.noteRepoPath }}/{{ CHEMCLAW_KNOWLEDGE_DIR }}`
— which is exactly `Settings.knowledge_path` (`core/config/kg.py:104`, `note_repo_dir /
knowledge_dir`). So the sidecar writes into the directory `reindex_notes` reads, and `rsync -a`
preserves the source mtime, so the post-rsync `(mtime_ns, size)` is stable and the poisoned
fingerprint never moves again. The reader side is scheduled hourly
(`note_reindex_schedule_minutes: float = 60.0`, `durable/schedules.py:118-120`) *and* fired on the
PR-gate merge path (`api/routes/proposals.py:206` → `request_note_reindex` → `NoteReindexWorkflow`).

### Why

The order of operations is the defect exactly as filed. `vector_index.py:504-508` reads

```python
await asyncio.to_thread(invalidate_cache, directory)
notes = await asyncio.to_thread(load_notes, directory) if directory.exists() else []
...
current_fingerprints = await asyncio.to_thread(note_file_fingerprints, directory)
```

so the text is observed at T1 and the fingerprint describing it at T2 > T1, and the record built at
line 521-529 pairs the T1 text with the T2 fingerprint. Any write in `(T1, T2]` makes the stored
fingerprint *describe content the index does not hold*, and since `_needs_embedding` is a pure
equality on that fingerprint, the note is excluded from `changed` on every subsequent run. There is
no other exit: `fingerprints(embedding_key)` (lines 156-167) only drops rows whose embedding
configuration changed, which a note edit does not, so the documented self-healing path at 480-487
does not apply. `--full` is the only recovery, and nothing signals that it is needed — no metric, no
log, no counter. The docstring's claim at 496-501 that the cache bust makes the two halves "come
from the same moment" is false: `invalidate_cache` only forces the *parse* to be fresh; the second
`stat` walk is an independent observation of a tree that has moved on. (Worth noting the parse
itself already reads its own fingerprint *before* parsing — `kg/graph.py:192-198` — so the correct
ordering is the one the code one call down already uses.)

Two corrections to the finding, neither of which changes the verdict or the severity:

1. **"The dense and lexical retrieval legs answer from the superseded body" overstates what is
   served.** `retrievers._chunks_from_hits` (line 317-343) maps a hit id to a note re-loaded from
   disk and `_chunk_for` builds `content=_excerpt(note.body)`, so the *text handed to the model is
   current*. What is permanently stale is the matching and ranking surface — the vector and the
   `tsvector` are derived from the dead body. The real consequence is that the note becomes
   permanently unfindable by its current content, and findable by content that was deleted from it.
   That is still silent, still permanent, and still undetectable from inside the system.
2. The per-event probability is lower than "the window is occupied continuously" implies. It is
   ~(1.5 s window)/(300 s sync period) per rsync that actually deltas the note during a reindex —
   fractions of a percent per merge, not a certainty. What earns `high` is not the rate but that
   each hit is permanent, silent, and only clearable by a `--full` nobody has a reason to run.

The reporter's fix (scan fingerprints first) is the right one and I measured it working: the losing
interleaving then stores an *older* fingerprint against newer-or-older text, which reads as
"changed" on the next pass and re-embeds — 259 notes re-embedded on the next quiescent pass in the
REORDERED arm, then zero, then stale = 0.

### Working-tree hygiene

No source file was modified. The alternative ordering was exercised as a copy inside
`/tmp/verify_reindex.py`, never as an edit to `src/`. `git status --porcelain src/` is clean.
Temp corpora under `/tmp/kg-race` and `/tmp/kg-big` were removed.
