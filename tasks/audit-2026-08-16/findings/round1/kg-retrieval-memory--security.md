# kg / retrieval / memory — security and hardening

Slice: `src/chemclaw/kg/`, `src/chemclaw/retrieval/` (incl. `vectors/`), `src/chemclaw/memory/`.
Every file in the slice was read in full. Reproductions were run under `uv run` against the live
environment (Docker + Postgres started per `CLAUDE.md`; `make up` / `make db-migrate` green).
Scripts live under `/tmp/kgsec/`.

Five findings. Two of them are the code contradicting a property its own comment asserts.

---

## Git stderr is redacted before it is stored and handed to the model verbatim

- **Severity**: high
- **Location**: `src/chemclaw/kg/pr_gate.py:126-143` (`propose_note`); the raise reaches
  `src/chemclaw/agent/tool_authz.py:131-138` (`domain_error_result`, `failure_detail`) because
  `GitSubmitError` is a `ChemclawError` (`src/chemclaw/kg/git_submitter.py:112`)
- **Trigger**: any `propose_knowledge_note` / `record_failure` / memory-synthesis proposal whose
  push fails while git quotes a remote carrying a credential in its userinfo — the exact case the
  comment at `pr_gate.py:136-139` names as "a realistic credential-bearing git failure … git
  quoting a push URL with its token in the userinfo".
- **Consequence**: `pr_gate` calls `redact_secrets` on the failure text *only for the durable
  `note_proposals.reason` column*, then does a bare `raise`. The unredacted exception is what
  `surface_domain_errors` converts into the tool result (`f"Error: {exc}"`) and what
  `failure_detail` puts in the chemist's transcript. So the credential is scrubbed out of the
  compliance table and written, in full, into (a) the model's context, (b) the SSE stream to the
  browser, and (c) the persisted `session_messages` thread — a longer-lived and more widely
  readable place than the row the redaction was added to protect. `GitSubmitError`'s own docstring
  states that showing the reason to the model is the point of the class, so this is not an edge
  case of the design; it is the design.
- **Evidence**: `pr_gate.py:140`

  ```python
  reason = redact_secrets(str(exc))[: settings.proposal_reason_chars]
  failure = proposal.model_copy(update={"reason": reason})
  await record_proposal_failed(failure)
  raise                       # <- the *unredacted* exc
  ```

  `/tmp/kgsec/t2.py` drives `propose_note` with a submitter raising a realistic push failure:

  ```
  PERSISTED reason  : ... unable to access 'https://x-access-token:***@git.example.corp/knowledge.git/': ... 403

  RAISED to model   : Error: ... unable to access 'https://x-access-token:ghp_S3cr3tTokenValue0123456789@git.example.corp/knowledge.git/': ... 403

  TRANSCRIPT detail : GitSubmitError: ... 'https://x-access-token:ghp_S3cr3tTokenValue0123456789@git.example.corp/...

  redact_secrets on the same text -> ... 'https://x-access-token:***@git.example.corp/...
  ```

  (Log lines are safe — `SecretRedactingFilter` covers those. The model-facing and transcript
  channels have no such filter.)
- **Fix**: redact once, at the boundary, and raise the redacted error. In `pr_gate.propose_note`:

  ```python
  except Exception as exc:
      safe = redact_secrets(str(exc))
      await record_proposal_failed(proposal.model_copy(
          update={"reason": safe[: settings.proposal_reason_chars]}))
      raise GitSubmitError(safe) from exc
  ```

  Better still, apply `redact_secrets` inside `domain_error_result`/`failure_detail` so *no*
  `ChemclawError` can carry a credential into a prompt or a transcript, and keep the pr_gate
  redaction as the second layer.

---

## A decided proposal's supporting files are silently replaced, and its branch force-pushed, without re-entering the review queue

- **Severity**: high
- **Location**: `src/chemclaw/kg/proposal_store.py:50-67` (`_UPSERT`, the unconditional
  `dependencies = EXCLUDED.dependencies`), mirrored at `src/chemclaw/kg/proposal.py:175-199`
  (`InMemoryProposalStore.upsert`); identity defined at `src/chemclaw/kg/proposal.py:103-115`
  (`content_hash` covers `content` only)
- **Trigger**: same actor, same day, two `record_failure` calls that differ **only** in
  `held_until`:
  1. `record_failure(refutes="playbook-x", what_happened="did not reproduce", held_until=2026-01-01)`
     — `failure_note()` (`memory/failure.py:33-88`) derives id and body from `refutes`,
     `what_happened`, `reported_by` and *today*, so the subject note is byte-stable;
     `close_refuted_note()` produces the dependency, whose bytes depend on `held_until`.
  2. A reviewer rejects it (`POST /proposals/{id}/decision`). Nothing deletes the branch —
     `git_submitter.py:427` says so explicitly ("Never deletes the branch").
  3. The same call with `held_until=2020-01-01`.
- **Consequence**: `content_hash` is unchanged, so the upsert collapses onto the **decided** row.
  `state` and `decided_by` are correctly preserved — but `dependencies` is refreshed
  unconditionally, and `submitted_at` is bumped to `now()`. The row now asserts a rejection taken
  *before* the submission it describes, attached to files the reviewer never saw. Because the row
  is `rejected`, it is invisible to `list_proposals(state='open')` and immovable by
  `mark_merged` (both scoped to `open`), so nothing re-surfaces it. Meanwhile `GitNoteSubmitter`
  has already `git push --force-with-lease`d the new file set onto the same `note/<id>` branch,
  which is still on origin. The one control the whole system rests on ("the agent proposes, a human
  decides") records a decision about bytes that have since changed. The dependency in this path is
  a `close_refuted_note` copy of an arbitrary already-merged note with `valid_to` set — i.e. the
  half that removes a human-approved note from every current-evidence sweep.

  The module comment at `proposal_store.py:39-41` states the invariant this breaks: "a note
  re-proposed unchanged after a rejection must not silently reopen itself, or the gate is
  defeatable by re-asking." It holds for `state` and not for the files.
- **Evidence**: `/tmp/kgsec/t7.py`, both backends, the Postgres one against the live database:

  ```
  --- InMemoryProposalStore
    same row?                 True
    state after re-proposal   rejected   (decided_by still 'reviewer-bob')
    dependency the reviewer rejected : 'original playbook body'
    dependency stored now            : 'REWRITTEN: playbook retired, valid_to 2020-01-01'
    content_hash unchanged?   True
  --- PostgresProposalStore (live)
    ... identical ...
  ```

  `/tmp/kgsec/t9.py` (live Postgres) on the timestamps:

  ```
  state       : rejected
  decided_at  : 2026-08-17 06:44:29.804275+00:00
  submitted_at: 2026-08-17 06:44:30.918639+00:00  <- bumped AFTER the decision
  ```

  `/tmp/kgsec/t8.py` drives the real `GitNoteSubmitter` against a real bare remote:

  ```
  push 1: note/failure-abc
    origin note/failure-abc dependency: 'original playbook body'
  push 2 (same subject note, rewritten dependency): note/failure-abc
    origin note/failure-abc dependency: 'REWRITTEN: retire playbook-x'
  ```

  (A human at the git host still performs the merge and would see the diff — the bypass is of the
  review *queue* and of the durable record, not of the merge button.)
- **Fix**: make the row's identity the whole submission. `content_hash` should hash
  `content` **plus** the ordered `(path, content)` of every dependency, so a changed supporting
  file is a new version and lands as a fresh `open` row beside the decision it does not share.
  If keeping the subject-note key is required for the stated "compliance history" reason, then
  `dependencies` must move under the same `CASE WHEN … state = 'failed'` guard as `state` and
  `reason`, and `submitted_at` must not be bumped on a decided row. Independently: rejecting a
  proposal should delete `note/<id>` on the remote, so a later force-push cannot resurrect a
  branch that was decided against.

---

## `GraphRetriever.retrieve` is uncapped and does O(corpus) work on the event loop

- **Severity**: medium
- **Location**: `src/chemclaw/retrieval/retrievers.py:160-187` (`GraphRetriever.retrieve`)
- **Trigger**: one `gather_evidence(query="suzuki")` — any single common term — against a corpus
  of realistic size. Reachable by any authenticated chemist, unbounded in rate.
- **Consequence**: `_eligible_notes` is offloaded to a thread, but the loop that follows is not:
  `term_coverage(note, terms)` builds a lowercased copy of **every** eligible note's full
  searchable text, `_chunk_for` constructs a pydantic `EvidenceChunk` per match, and the final
  `sorted()` runs over all of them — all on the event loop. There is no cap: the sibling entry
  point `find_notes` bounds itself at `settings.graph_max_results` (50) and warns on truncation
  (`agent/graph_tools.py:99-118`); this path builds the whole list and the caller throws away all
  but `gather_evidence_max_chunks` (40). The service runs a single uvicorn worker, so the stall is
  the whole pod — the same failure mode the repo elsewhere calls a "whole-pod freeze" and moved
  attachment parsing off the loop to prevent (`api/routes/proposals.py:183-188`). It scales
  linearly with corpus size, and the pathological input is one short word.
- **Evidence**: `/tmp/kgsec/t4.py`, 20,000-note corpus, conflict detection off:

  ```
  chunks returned        : 20000
  settings.graph_max_results (the cap find_notes applies): 50
  gather_evidence_max_chunks (cap applied AFTER the sweep): 40
  one retrieve() took    : 564 ms
  peak python heap in it : 32.2 MB
  ```

  `/tmp/kgsec/t5.py` measures the loop directly with a 5 ms sleep probe:

  ```
  idle baseline   : worst event-loop stall 4 ms
  during retrieve : worst event-loop stall 276 ms
  ```
- **Fix**: two changes. (1) Bound the result: stop at `settings.retrieval_top_k` complete matches
  (or a dedicated `retrieval_graph_max_hits`) and log the truncation, as `find_notes` already
  does — the caller discards everything past 40 anyway. (2) Move the scoring/chunking loop inside
  the existing `asyncio.to_thread` that already loads the notes, so `retrieve` awaits one thread
  hop instead of running O(corpus) string work on the loop.

---

## `conflict_index` holds a process-global lock across a whole-corpus scan inside the shared thread pool

- **Severity**: medium
- **Location**: `src/chemclaw/kg/conflicts.py:296-351` (`_INDEX_LOCK`, `conflict_index`);
  callers at `src/chemclaw/retrieval/retrievers.py:106-116` (`_conflict_index`, via
  `asyncio.to_thread`)
- **Trigger**: any note write or `invalidate_cache()` (the PR-gate calls it), or the `as_of` date
  rolling over at UTC midnight — both make every cached entry miss at once. Then several
  concurrent turns each run `gather_evidence`, which fans out to up to three note-backed
  retrievers, each calling `_conflict_index`.
- **Consequence**: the lock is deliberately held *across the computation* rather than around the
  dict access, so waiters block inside `asyncio.to_thread` — i.e. they occupy workers of the
  process's shared default `ThreadPoolExecutor` while doing nothing. That executor is also what
  `load_notes`, `build_graph` (`find_notes`, `expand_note`), `embed_texts` and attachment parsing
  use. The result is head-of-line blocking of unrelated work across the whole process, triggerable
  by ordinary concurrent queries. The design note at `conflicts.py:288-295` argues correctly that
  a waiter is better than a duplicate computation; what it does not account for is that the waiter
  is holding a slot in a pool that is not the retrieval layer's to spend.
- **Evidence**: `/tmp/kgsec/t6.py`, 4,000-note programme-shaped corpus, default executor
  (8 workers here):

  ```
  cold conflict_index over 4000 notes: 501 ms
  12 concurrent conflict_index calls: slowest 479 ms, fastest 479 ms  (one computation was 501 ms)
  an unrelated asyncio.to_thread() queued behind them waited 466 ms
  default executor max_workers: 8
  ```

  The last line is the finding: a `to_thread(lambda: None)` — standing in for any other offloaded
  work in the pod — waited 466 ms because the pool was full of threads parked on a lock. Cost
  grows linearly with the corpus.
- **Fix**: do not block a pool thread on the lock. Either (a) compute once per fingerprint behind a
  dedicated single-thread executor and have callers await a shared `asyncio.Future` keyed on
  `(dir, fingerprint, as_of)` — so waiters wait on the loop, not in the pool — or (b) at minimum
  give retrieval its own bounded `ThreadPoolExecutor` so a stampede here cannot starve the front
  door's unrelated `to_thread` calls.

---

## The fingerprint leg serves retired and explicitly contradicted notes as unflagged current evidence

- **Severity**: medium
- **Location**: `src/chemclaw/retrieval/retrievers.py:209-256`
  (`FingerprintReactionRetriever.retrieve` — builds `EvidenceChunk` inline instead of via
  `_chunk_for`, and calls `_eligible_notes` only `if wanted`)
- **Trigger**: `gather_evidence(query=..., reaction_smiles="CC>>CCO")` with **no**
  `note_type`/`tag`/`since`/`until` filter — the documented default shape of the call — where a
  structurally similar reaction note has been retired (`valid_to` in the past, as
  `memory.supersede` and `memory.failure.close_refuted_note` set it) and/or carries an incoming
  `contradicts` edge from a `failure-mode` note.
- **Consequence**: two controls are skipped on this leg only.
  * **Currency (KM-7).** `_eligible_notes` — described in its own docstring as "the one eligibility
    gate for every graph-backed retriever" — is reached only when a filter was supplied, so a
    superseded or refuted note is cited as current evidence. The graph/dense/lexical legs correctly
    drop it, so whether a retired note is served depends on which leg found it.
  * **Conflict flagging (KM-8) and provenance (D-160).** The chunk is constructed by hand rather
    than through `_chunk_for`, so `conflicts_with`/`conflicts_total` are empty and
    `created_by`/`source`/`confidence` are blank. `kg/conflicts.py:1-21` states that two
    disagreeing notes returned with no marker "looks like corroboration" and is "a worse failure
    than returning neither"; that is exactly what this path produces for a note something
    explicitly `contradicts`.
- **Evidence**: `/tmp/kgsec/t10.py` — corpus holds `reaction-R1` (`valid_to` = yesterday) and
  `failure-f1` with `contradicts: reaction-R1`; the fingerprint store is stubbed to return `R1`:

  ```
  GraphRetriever hits (currency gate applied): []
  Fingerprint leg chunk:
    source_note_id : reaction-R1
    content        : Similar reaction Suzuki coupling, 82% yield (Tanimoto 0.94)
    conflicts_with : []  conflicts_total: 0
    created_by     : ''  source: ''  confidence: None
  ```
- **Fix**: always resolve the hit's note through `_eligible_notes(self._dir, wanted)` (an empty
  `wanted` is still a currency check) and build the chunk through `_chunk_for` with the
  `_conflict_index` map, overriding only `content` and `score` with the structural values. The
  documented "pending note not yet merged is still cited" behaviour can be kept explicitly, as a
  chunk with no note rather than as an absent gate.

---

## Checked and clean

Stated so the negative result is usable:

- **SQL injection** — every statement in the slice is parameterized. The only f-string
  interpolation into SQL text is `settings.embedding_dim` (`int`, `gt=0`,
  `core/config/llm.py:149`) and the module constants `_COLUMNS` / `TSQUERY_TERMS` /
  `scope`. `core.fulltext.TSQUERY_TERMS`'s claim that "the chemist's query reaches the server only
  as a parameter" is accurate — the widening is done server-side over
  `websearch_to_tsquery(%(q)s)::text`, nothing is spliced.
- **Unsafe deserialization** — `read_note` goes through `python-frontmatter`, whose `YAMLHandler`
  defaults to `yaml.SafeLoader` (verified by inspecting the installed source). No `pickle`, no
  `yaml.load` with an unsafe loader, no `eval` in the slice.
- **Frontmatter injection via an agent-controlled body** — `/tmp/kgsec/t1.py`: a note body
  beginning with `---\nid: playbook-authoritative\n…` round-trips as *body text*; the reparsed note
  keeps `id=benign-note`, `type=reaction`, `created_by=agent`. `frontmatter` only reads the first
  block.
- **Path traversal / ref injection** — `Note._slug_only` (`kg/note.py:352-368`) constrains `id` and
  `type` to `^[A-Za-z0-9][A-Za-z0-9_.-]*$` and additionally rejects `..`, a trailing `.` and
  `.lock`. `note_relative_path` is the single layout definition, `NoteSubmission` is only ever
  built by `_build_submission` (no replay path exists — grepped), `_contained_note_path` resolves
  after the worktree materializes and refuses an escape, and `git add --` really is there
  (`git_submitter.py:462`), so a leading-dash path cannot reach git as an option.
- **Surrogate/unencodable strings** — `note.py:413-440`'s claim that pydantic already rejects a
  lone surrogate in `Relation.rel`/`to` before `_text_is_writable` runs is **true**
  (`/tmp/kgsec/t3.py`: `string_unicode` on both, and on the enclosing `Note`).
- **Prompt-injection framing** — `gather_evidence` wraps every chunk in the nonce'd envelope and
  `safe_id` strips `"`/`<`/`>` from the id attribute, so an ELN-supplied `match.label` or note body
  cannot close the envelope. `_excerpt` strips wikilink markup so a retrieved body cannot inject
  graph edges into a report.
- **SSRF / TLS** — the only outbound client in the slice is `retrieval/vectors/qdrant.py`; its URL
  and API key come from settings, not from a request, `verify` is only ever set to a CA bundle
  path (never `False`), and the vendor import is a constant module name behind a `Literal` provider
  switch — no config-driven arbitrary import.
- **Cross-tenant reads** — the corpus, the conflict index and the observations tier are
  deliberately org-global; the only per-actor resource in the slice is `NoteProposal`, and its two
  gates (`_visible_proposal`, and `scope = "" if _is_reviewer(...) else principal.oid`) cannot
  fail open through an empty `oid`, because `Principal.oid` is `Field(min_length=1)`
  (`api/auth.py:48`).
- **Webhook auth** — `_webhook_signature_ok` uses `hmac.compare_digest`, returns `False` when no
  secret is configured, and `close_merged_notes` is unreachable without a valid signature in both
  branches of `knowledge_merged` (verified by reading both guards).
