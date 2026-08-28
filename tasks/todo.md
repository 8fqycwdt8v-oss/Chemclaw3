# The cross-session "plans waiting on you" inbox

Two repositories, one capability: a chemist who closed a tab must be able to find the plan the
agent is blocked on, without remembering which conversation it was in.

## Where this came from

`Chemclaw3_ui`'s `/review` page carries an empty slot and a paragraph explaining it
(`src/components/ReviewQueue.tsx`): the durable-hold section was deleted with the routes behind it
(`D-2026-08-27-a-hold-nothing-can-open-is-not-a-hold`), and

> The gate that *does* block work is the plan approval, which is answered per session on
> `POST /sessions/{id}/plan/decision` and currently lives only as an inline card in a live turn.

The reload half of that harm is closed — `App.tsx` re-reads `GET /sessions/{id}/plan` and
re-attaches the decision — but only for a conversation somebody opens. **Nothing answers "which of
my conversations is waiting on me", because no route asks that question across sessions.** Every
plan route is `/sessions/{id}/…`.

## The decision that shapes the route

`api/runner._pending_plan_approval` emits the decision card whenever a plan-gated turn ends with a
non-empty plan holding no *live* approval. That predicate is right for a card inside a turn and
wrong for an inbox: an approval is spent at the end of the turn it authorized, so every finished
plan-gated conversation would sit in the inbox forever — the mirror of the permanently-empty inbox
`ISSUES.md` records as the failure worth not repeating.

So the inbox lists a narrower set: **a plan nobody has decided at all** — no row in
`plan_approvals` for this session and this plan hash. A spent approval and a rejection are both
answers; the conversation is where a *re*-approval is asked for, by the card at the turn's end.
What that misses is stated in the route's docstring rather than left to be discovered: a plan
re-proposed byte-identically after its approval was spent has a row, so it does not list.

## Chemclaw3 (backend)

- [x] `SessionOwnerStore.list_for_owner` also returns each session's `profile` (already a column),
      so a session that cannot hold a plan is skipped without a checkpoint read.
- [x] One read path for a session's plan, shared by `GET /sessions/{id}/plan` and the inbox, so the
      two surfaces cannot disagree about what a plan is or whether it was decided (`_read_plan`).
- [x] `GET /plans/pending` → `{plans, considered, gated, unread}`. The three counts are what make an
      empty list unambiguous: `gated == 0` means this deployment does not gate plans at all,
      `unread > 0` means the scan hit its bound and the answer is partial.
- [x] Bound the scan (`service_max_plan_scans`): `AsyncPostgresSaver` serializes every statement
      behind one `asyncio.Lock`, so an unbounded per-session checkpoint read would hold the
      checkpointer against every concurrent turn on the pod.
- [x] Tests: the filter (undecided lists, approved/rejected/spent do not), ownership scoping, the
      counts, the bound, and the empty envelope under `session_store="memory"` —
      `tests/test_plan_inbox.py`, eight cases.
- [x] ADR + `docs/decisions/README.md` row.

## Chemclaw3_ui (frontend)

- [x] Whitelist `GET /api/plans/pending` in `server/routes.ts`, with a test pinning it.
- [x] `api.listPendingPlans` + types.
- [x] `/review` grows the section its own docstring says is deliberately empty: title, when, the
      plan's steps, and a link into the conversation. It does **not** decide in place — the same
      reason the deleted holds section gave, and here the plan's own reasoning is one click away.
- [x] The three empty states rendered distinctly (not gated / nothing waiting / partial), plus the
      failure, which is a fourth and was the whole defect in the section this replaces.
- [x] Correct `USER-STORIES.md` F3 and `ISSUES.md`, both of which still describe the deleted
      `GET /approvals` inbox as the answer.

## Review

**What the work turned on.** One decision, taken twice before it was right. The obvious route is
"list the sessions whose plan holds no live approval" — the predicate
`runner._pending_plan_approval` already uses. It is wrong for an inbox, and the reason is D-167: an
approval is consumed at the end of the turn it authorized, so *no live approval* is the resting
state of every finished plan-gated conversation. That route ships a permanently full inbox, which is
the mirror of the permanently empty one `Chemclaw3_ui`'s `ISSUES.md` records. The filter is "no
decision recorded" instead, and what that misses is written into the route's docstring rather than
discovered later.

**What measurement changed.** Two things were read rather than assumed:

- `harness_enabled` defaults to **False**, so in a default deployment no session has a todo list at
  all and the inbox is structurally empty. That is what made the `gated` count non-negotiable: an
  empty list had to be able to say it is a property of the configuration.
- `AsyncPostgresSaver` serializes every statement behind one `asyncio.Lock`
  (`agent/checkpointer.py`'s own docstring, reason 2). So concurrency buys nothing here and the
  scan needed a bound, and the profile prune stopped being an optimisation — it is what keeps the
  route free in the deployment that cannot ever have a row.

**What I did not build, and why it is written down.** A durable "pending" table would answer the
query in one statement and be exact. It is a second piece of state saying what the checkpoint and
`plan_approvals` already determine — the DARK-1 shape. Deriving "blocked" from plan-gate refusals in
`audit_events` is the most faithful signal and is the named restart condition in the ADR; it needs a
reader on a table that has none and an INSERT-only grant, which is a bigger decision than a route.

**What I got wrong on the way.** I read a mid-run `F` in the suite as a timing flake in
`test_connector_transport.py` from the test index, and ran that file alone to "confirm" it. The
actual failure was `test_config.py::test_env_example_documents_every_field` — my new setting was not
in `.env.example`. A guess about which test failed is not a diagnosis, and the run's own summary was
twelve minutes away. Logged in `tasks/lessons.md`.

**Verification.** `make lint` clean; `mypy --strict` over `src examples tests` clean;
`pytest` 5,293 passed / 9 skipped with Postgres up (the skips are `helm` not installed and the
truncated-history migration checks — no Postgres-gated test skipped). UI: `tsc -b`, `eslint`,
`prettier --check` and 730 vitest tests green, and the `/review` axe pass runs against the new
section in both themes with the e2e fixture serving a pending plan.
