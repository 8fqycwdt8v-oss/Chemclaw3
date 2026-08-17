# Verdicts — kg / retrieval / memory, security (lens: reachability + consequence)

Source: `tasks/audit-2026-08-16/findings/round1/kg-retrieval-memory--security.md`.
Two findings are in scope (both **high**); the other three are medium and were ignored per scope.
Scripts under `/tmp/claude-0/-home-user-Chemclaw3/41f2465f-44e8-5661-9ba7-5183da558c73/scratchpad/`.
Nothing in the working tree was mutated.

---

## Git stderr is redacted before it is stored and handed to the model verbatim

- **Verdict**: OVERSTATED
- **Severity I would assign**: low

### What I did

**1. The mechanism reproduces exactly** (`scratchpad/f1.py`, driving the real `propose_note` with a
submitter raising the reporter's stderr string, `CHEMCLAW_KNOWLEDGE_REPO_TOKEN` set in env):

```
MODEL sees        : Error: git push --force-with-lease -u origin note/failure-abc failed: ... 'https://x-access-token:ghp_S3cr3tTokenValue0123456789@git.example.corp/knowledge.git/': ... 403
TRANSCRIPT sees   : GitSubmitError: ... 'https://x-access-token:ghp_S3cr3tTokenValue0123456789@git.example.corp/...
PERSISTED reason  : ... 'https://x-access-token:***@git.example.corp/knowledge.git/': ... 403
token in model text?      True
token in persisted row?   False
```

So the asymmetry is real: `pr_gate.py:140` redacts only the durable column and re-raises the
original exception, `domain_error_result`/`failure_detail` render `str(exc)` unfiltered, and the
raise is reachable from the `record_failure` / `propose_knowledge_note` tools
(`agent/graph_tools.py:271,350` → `propose_note` → `GitSubmitError`, a `ChemclawError`, caught by
`surface_domain_errors` at `tool_authz.py:329`). I grant all of that.

**2. The payload the finding claims does not exist.** The finding's own evidence is a *hand-written*
stderr string, not git's. I measured git instead (git 2.43.0, the version `dnf install -y git` puts
in the UBI9 image per `deploy/Containerfile:53`):

```
$ git ls-remote https://x-access-token:ghp_S3cr3t...@nonexistent.invalid/k.git
fatal: unable to access 'https://nonexistent.invalid/k.git/': CONNECT tunnel failed, response 502

$ git ls-remote http://x-access-token:ghp_S3cr3t...@127.0.0.1:8931/k.git      # server returns 403
fatal: unable to access 'http://127.0.0.1:8931/k.git/': The requested URL returned error: 403

$ git ls-remote http://x-access-token:ghp_S3cr3t...@127.0.0.1:8947/k.git      # server returns 401
fatal: Authentication failed for 'http://127.0.0.1:8947/k.git/'

# and with the credential in the *configured* remote, via the real command the submitter runs:
$ git push --force-with-lease -u origin HEAD:refs/heads/note/x
fatal: unable to access 'http://127.0.0.1:8931/k.git/': The requested URL returned error: 403
```

Git strips both userinfo components from the URL it quotes, on every failure shape the submitter can
produce — including the exact "git quoting a push URL with its token in the userinfo" message the
finding names. Neither the token nor the username survives.

**3. The shipped deployment does not put a credential in the URL either.**
`deploy/knowledge-sync.sh:105-115` delivers `CHEMCLAW_KNOWLEDGE_REPO_TOKEN` through a `GIT_ASKPASS`
helper; `chemclaw.knowledgeSyncEnv` (`_helpers.tpl:267`) passes only `knowledge.sync.repoUrl`, and
`provision_note_repo` clones the submitter's checkout from that same URL. So `.git/config`'s origin
carries no userinfo unless an operator hand-writes one into `repoUrl` — and per (2), even then git
does not echo it.

### Why

The structural claim (redact once at the boundary, not once at the durable column) is correct and
the fix is a one-liner worth taking as hardening. But the finding is written as a credential
disclosure — "the credential is scrubbed out of the compliance table and written, in full, into the
model's context, the SSE stream and the persisted thread" — and that consequence is asserted from a
fabricated exception rather than measured from git. Against the git the image actually ships, the
text reaching the model contains a repo path, a branch name and an HTTP status, and nothing
`redact_secrets` would have touched.

I found exactly one surviving path where a credential can enter `str(exc)`: `_git` builds its own
message as `f"git {' '.join(args)} failed: {stderr}"` (`git_submitter.py:231`), and `args` includes
`self._remote` = `settings.git_remote`. That field is a bare `str` with no validator
(`core/config/kg.py:48`), so an operator who sets `CHEMCLAW_GIT_REMOTE` to a credentialed URL
instead of a remote *name* would have the code — not git — echo it into the model and the transcript
while the durable row stays redacted. `.env.example:335` and the config comment both document a
remote name, so this is a misconfiguration, not the documented shape.

Low, not high: real gap in defence-in-depth, no demonstrated disclosure. The comment at
`pr_gate.py:136-139` that the finding leans on ("a realistic credential-bearing git failure — git
quoting a push URL with its token in the userinfo — measures 118 characters…") is itself a claim
nobody measured; git does not produce that string. It should be corrected in the same change.

---

## A decided proposal's supporting files are silently replaced, and its branch force-pushed, without re-entering the review queue

- **Verdict**: CONFIRMED
- **Severity I would assign**: medium

### What I did

**Store half** (`scratchpad/f2.py`) — built the two submissions with the *real* `failure_note()` /
`close_refuted_note()` the `record_failure` tool uses, for `held_until` 2026-01-01 then 2020-01-01,
and ran both backends, the Postgres one against the live database:

```
--- InMemoryProposalStore
  same content_hash?       True
  same dependency bytes?   False
  decided -> rejected reviewer-bob at 2026-08-17 08:32:04.499741+00:00
  same row?                True
  state after re-proposal  rejected | decided_by reviewer-bob
  dep stored now           '...this held until 2020-01-01 and no longer does...'
  submitted_at             2026-08-17 08:32:04.499784+00:00
  decided_at               2026-08-17 08:32:04.499741+00:00
  submitted AFTER decided? True
  visible in open queue?   False
  mark_merged moved        0
--- PostgresProposalStore (live)   ... identical, submitted_at 08:32:04.534987 > decided_at 08:32:04.523829
```

(Test rows deleted afterwards: `delete from note_proposals where actor = 'chemist-alice'`.)

**Git half** (`scratchpad/f2_git.py`) — the real `GitNoteSubmitter` against a real bare remote, two
submissions of the same subject note with different dependency bytes:

```
push held_until=2026-01-01: branch=note/failure-e80161fc7e  origin -> valid_to: 2026-01-01 ...
push held_until=2020-01-01: branch=note/failure-e80161fc7e  origin -> valid_to: 2020-01-01 ...
```

The second push replaces the first on origin (`--force-with-lease`, `git_submitter.py:479`), and
`_remove_worktree`'s docstring is right that the branch is never deleted, so the rejection leaves
the ref standing.

**Reachability trace.** Outermost entry point is the `record_failure` tool
(`graph_tools.py:279-350`), reachable by any authenticated chemist through an ordinary turn. Nothing
upstream blocks the second call: `failure_note`'s id and body derive from `refutes`,
`what_happened`, `require_actor()` and `date.today()` only, so a same-day re-report is byte-stable
(measured: `same content_hash? True`); the guard at `graph_tools.py:337` refuses `held_until` only
when the *graph* note already carries `valid_to`, which it does not while the proposal is unmerged;
and `propose_note` consults no proposal row before submitting. An even easier trigger than the
finding's: first report with no `held_until` (dependencies `()`), rejected, then a second with a
date — the subject note is identical and the dependency set goes from empty to non-empty.

### Why

Every claim reproduces: identity is `stable_hash(content)` only (`proposal.py:115`), so a materially
different submission collapses onto the decided row; `dependencies = EXCLUDED.dependencies` and
`submitted_at = now()` sit outside the `CASE WHEN … state = 'failed'` guard that protects `state`
and `reason` (`proposal_store.py:55-66`), mirrored at `proposal.py:175-199`; the row ends up
asserting a decision timestamped *before* the submission it now describes, against files no reviewer
saw; and it is neither in `state='open'` listings nor movable by `mark_merged`. `_SELECT_MANY`
orders by `id DESC` and the id is unchanged, so the re-proposal does not even resurface in the
all-states listing — it is genuinely invisible as new work. Add one consequence the reporter did not
name: if a human at the git host later merges that branch, the merge webhook's `close_merged_notes`
still moves nothing (rejected ≠ open), so the durable record says "rejected" for a note that is in
the graph.

Medium rather than high, on the finding's own scoping. Nothing reaches the knowledge graph without a
human merging the PR at the git host, which the reporter concedes in their parenthetical, so this is
not a defeat of "the agent proposes, a human decides". What is lost is narrower than the write-up's
framing implies: the subject note in the collapsed submission is byte-identical to one a human
already rejected, and the design deliberately refuses to reopen that — the only genuinely new
content dropped from the queue is the `close_refuted_note` retirement riding as a dependency. The
solid harms are (a) a compliance row that misdescribes what was decided and when, and (b) a
submission that is force-pushed to origin while ChemClaw's review surface shows nothing pending. The
proposed fix — hash the ordered `(path, content)` of the dependencies into `content_hash`, or move
`dependencies`/`submitted_at` under the same `failed` guard — is correct, and the
delete-the-branch-on-rejection suggestion is worth taking separately.
