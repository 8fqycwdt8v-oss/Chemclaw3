# Repro verdicts — kg / retrieval / memory, security and hardening

Lens: **does it actually reproduce?** Only the two **high** findings are in scope; the other three
in the source file are medium and were not examined.

Everything below was re-derived from source and re-run with my own scripts (`/tmp/repro/r1.py` …
`/tmp/repro/r6.py`, plus a real smart-HTTP git remote under `/tmp/gtest/`). I did not run the
reporter's `/tmp/kgsec/` scripts and did not read their transcripts as evidence. No source file was
mutated; `git status --porcelain -- src/` is empty and every file I read is byte-identical to `HEAD`
(`git diff HEAD -- <file> | wc -l` = 0 for `pr_gate.py`, `proposal.py`, `proposal_store.py`,
`git_submitter.py`, `memory/failure.py`, `tool_authz.py`, `graph_tools.py`). All cited line numbers
and symbols are real and current.

---

## Git stderr is redacted before it is stored and handed to the model verbatim

- **Verdict**: OVERSTATED
- **Severity I would assign**: low

### What I did

**1. Does git actually put a credential in its stderr?** This is the finding's whole premise —
"git quoting a push URL with its token in the userinfo". I built a real bare repo served over real
smart HTTP (`git http-backend` behind a Python `HTTPServer`), pointed a clone's `origin` at
`http://x-access-token:ghp_S3cr3tTokenValue0123456789@127.0.0.1:8932/knowledge.git`, and forced
every failure shape the submitter can hit. `git --version` here is 2.43.0; the deployed image
installs `git` from the UBI/RHEL stream (`deploy/Containerfile:53`), i.e. a modern git.

```
$ git -c credential.helper= push origin HEAD:refs/heads/note/x      # server 403 on receive-pack
fatal: unable to access 'http://127.0.0.1:8932/knowledge.git/': The requested URL returned error: 403

$ git -c credential.helper= push origin HEAD:refs/heads/note/x      # server 401
fatal: Authentication failed for 'http://127.0.0.1:8933/knowledge.git/'

$ git -c credential.helper= push origin HEAD:refs/heads/note/x      # non-fast-forward
To http://127.0.0.1:8932/knowledge.git
 ! [rejected]        HEAD -> note/x (fetch first)
error: failed to push some refs to 'http://127.0.0.1:8932/knowledge.git'

$ git -c credential.helper= push --force-with-lease -u origin HEAD:refs/heads/note/x   # stale lease
 ! [rejected]        HEAD -> note/x (stale info)
error: failed to push some refs to 'http://127.0.0.1:8932/knowledge.git'

$ git fetch origin main / git ls-remote origin                       # connection refused, 403, 401
fatal: unable to access 'http://127.0.0.1:8933/knowledge.git/': ...
```

**Git anonymizes the URL in every one of these.** The userinfo is stripped even though it is in
`.git/config`. `grep ghp_` over all of it returns nothing.

**2. The full real path.** `/tmp/repro/r1.py` drives the *real* `propose_note` with the *real*
`GitNoteSubmitter` against that tokenized remote and a 403 receive-pack, and prints what each
channel gets:

```
RAISED to model   : Error: git push --force-with-lease -u origin note/failure-abc failed:
                    fatal: unable to access 'http://127.0.0.1:8932/knowledge.git/': The requested URL returned error: 403
TRANSCRIPT detail : GitSubmitError: git push --force-with-lease -u origin note/failure-abc failed: ...
redact_secrets    : (identical — nothing to redact)
token in raised?   False
PERSISTED reason  : (identical)
token in stored?   False
```

The `redact_secrets` call at `pr_gate.py:140` is a **no-op** on the text this path actually
produces. There is no credential in the model's context, the SSE stream, the transcript, *or* the
`note_proposals.reason` column the redaction was added to protect.

**3. Is there any route at all?** One, and I found it by construction rather than from the finding:
the exception text is built by *this repo*, not by git — `git_submitter.py:231` is
`f"git {' '.join(args)} failed: {stderr}"`, and `args` contains `self._remote`
(`= settings.git_remote`). Setting `CHEMCLAW_GIT_REMOTE` to a tokenized URL instead of a remote
*name* does leak (`/tmp/repro/r6.py`):

```
RAISED  : Error: git worktree add -B note/zz .../note-zz http://x-access-token:ghp_S3cr3tTokenValue0123456789@127.0.0.1:8932/knowledge.git/main failed: fatal: invalid reference: ...
REDACTED: ... http://x-access-token:***@127.0.0.1:8932/knowledge.git/main ...
token in raised? True
```

Note what it fails on: `git worktree add -B <branch> <remote>/<base>` needs a remote *name*, so this
configuration cannot ever submit a note — it dies on the first git command of the first submission.
The default is `git_remote = "origin"` (`core/config/kg.py:48`); nothing in the chart sets it, and
the chart delivers the token through `GIT_ASKPASS` rather than the URL (`deploy/knowledge-sync.sh`),
which I did not have to take on faith — git anonymizes the URL anyway.

### Why

The *mechanism* is exactly as described and needs no experiment: `pr_gate.py:140-143` redacts for the
durable column and then does a bare `raise`, and `domain_error_result` / `failure_detail`
(`tool_authz.py:131-138`) hand `str(exc)` to the model and the transcript untouched. That asymmetry
is real and I confirm it.

What does not hold is the consequence, which is the entire basis for **high**. The finding's stated
trigger — "a realistic credential-bearing git failure … git quoting a push URL with its token in the
userinfo" — is something git has not done for over a decade, and I could not make it do so in five
failure shapes against a real remote whose URL genuinely carried a token. The reporter's evidence
block is a *hand-written* exception string containing
`https://x-access-token:ghp_…@git.example.corp/…`; that string is the reporter's, not git's. The
finding reproduces only with its own scaffolding, which makes it a finding about the scaffolding.

Worth adding: the same measurement falsifies the code comment at `pr_gate.py:136-139`, which asserts
the same non-existent git behaviour ("git quoting a push URL with its token in the userinfo …
measures 118 characters"). The reporter inherited the premise from the comment rather than checking
it — which is exactly the failure mode the comment itself was written to fix.

I would still take the fix, at low priority and for the reason the reporter gives second rather than
first: applying `redact_secrets` inside `domain_error_result`/`failure_detail` is two lines and makes
the `git_remote`-as-URL route (and any future `ChemclawError` that does carry a secret) structurally
safe. It is defense in depth, not a live leak.

---

## A decided proposal's supporting files are silently replaced, and its branch force-pushed, without re-entering the review queue

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

### What I did

**1. The store, both backends, live Postgres** (`/tmp/repro/r2.py` — my own script; upsert
dependency-set A, decide, upsert dependency-set B with identical `content`):

```
--- InMemoryProposalStore
  content_hash           95065df7decdb5aa == 95065df7decdb5aa -> True
  same row?              True
  state                  rejected decided_by: reviewer-bob
  reviewer saw dep       'original playbook body'
  dependency stored now  'REWRITTEN: playbook retired, valid_to 2020-01-01'
  decided_at             2026-08-17 08:29:50.796228+00:00
  submitted_at           2026-08-17 08:29:50.846675+00:00
  submitted_at > decided_at? True
  visible as open?       False
  mark_merged moves      0
--- PostgresProposalStore (live)
  ... byte-for-byte the same, against localhost:5432
```

**2. The trigger is real, not hypothetical** (`/tmp/repro/r4.py`): two `record_failure` calls
differing only in `held_until` produce a **byte-identical** subject note
(`failure_note` derives id and body from `refutes` / `what_happened` / `reported_by` / today —
`content_hash` `caa150a6ca181c25` both times) and **different** dependency bytes
(`close_refuted_note` writes `held_until` into the body and `valid_to`).

**3. End to end through the real tool and a real remote** (`/tmp/repro/r5.py` — the real
`graph_tools.record_failure`, the real `GitNoteSubmitter`, a real git http remote, a seeded
human-authored `playbook-x`):

```
proposal id 1 state open dep: ... this held until 2026-01-01 and no longer does.
decided: rejected reviewer-bob 2026-08-17 08:31:43.815208+00:00
second ref: note/failure-6505885e13 (same branch)
rows now: 1
state      : rejected decided_by: reviewer-bob
dep stored : ... this held until 2020-01-01 and no longer does.
decided_at : 2026-08-17 08:31:43.815208+00:00
submitted  : 2026-08-17 08:31:44.114961+00:00 -> AFTER decision: True
open queue : []
origin branch dep: ... this held until 2020-01-01 and no longer does.
```

One row, still `rejected` by `reviewer-bob`, now naming supporting bytes nobody reviewed;
`submitted_at` stamped **after** `decided_at`; the review queue empty; and `note/failure-…` on
`origin` force-pushed with the rewritten retirement of a human-approved playbook. `/tmp/repro/r3.py`
isolates the git half on its own and shows the same branch serving `'original playbook body'` then
`'REWRITTEN: retire playbook-x'`.

**4. Nothing upstream stops it.** `record_failure`'s only guard
(`graph_tools.py:338`) refuses a second `held_until` when `refuted.valid_to` is set — but it reads
the *merged* graph, and a rejected proposal was never merged, so `valid_to` is still `None`.
`decide_note_proposal` (`api/routes/proposals.py:113-140`) takes no git action on a rejection, and
`_release_worktree` (`git_submitter.py:426-427`) never deletes the branch — so the branch is still on
origin when the force-push lands.

### Why

Every link the finding asserts reproduces, on both backends, with the real tool and real git, and the
cited code does exactly what is claimed: `content_hash` is `stable_hash(self.content)`
(`proposal.py:115`, subject note only), and `_UPSERT` (`proposal_store.py:60-61`) refreshes
`dependencies = EXCLUDED.dependencies` and `submitted_at = now()` *outside* the
`CASE WHEN … state = 'failed'` guard that protects `state` and `reason`. The module comment at
`proposal_store.py:39-44` states the invariant ("must not silently reopen itself, or the gate is
defeatable by re-asking") and it holds for `state` and not for the files — the reporter's
characterization is precise.

The finding is also honest about its own limit ("a human at the git host still performs the merge"),
which is why I would not call it critical. High is right: this is the durable record of the one
control the architecture rests on, and it can be made to name bytes that were never reviewed, with
timestamps in an impossible order, while the re-submission is invisible to every `state='open'` query
and immovable by `mark_merged`.

**Two things the reporter missed that make it worse:**

1. **The `merged` case behaves identically**, and is arguably the sharper one. Re-running the same
   experiment with `MERGED` instead of `REJECTED` (`/tmp/repro/r2.py`, both backends, live Postgres):

   ```
   state                  merged decided_by: reviewer-bob
   reviewer saw dep       'original playbook body'
   dependency stored now  'REWRITTEN: playbook retired, valid_to 2020-01-01'
   submitted_at > decided_at? True
   mark_merged moves      0
   ```

   The row now says "merged by reviewer-bob" while naming files that were never merged, and the
   already-merged `note/<id>` branch has been force-pushed with new content. `mark_merged` is scoped
   to `open`, so if anyone merges the resurrected branch, nothing anywhere records the second merge —
   the record keeps describing the first.

2. The reporter's simplest trigger is not the simplest one. It needs no rejection *and* no changed
   date: a first `record_failure` with `held_until=None` (zero dependencies) followed by the same call
   *with* a `held_until` collapses onto the same row, because the subject note is unchanged. That
   attaches a retirement of a human-approved note to a decided row and pushes it to the branch, from
   two ordinary tool calls with no adversarial input at all.

---

## Not in scope

The remaining three findings in the source file (`GraphRetriever.retrieve` uncapped;
`conflict_index` holding a global lock in the shared pool; the fingerprint leg skipping
`_eligible_notes`) are marked **medium** and were not verified.
