# api front door — CORRECTNESS · repro verdicts

Lens: **does it actually reproduce?** Scope: critical/high only. The findings file contains exactly
one **high** finding and no **critical** ones; the other five are medium/low and were not examined.

Working tree: `git status --porcelain` shows no modified tracked files at `e319cdcb` (only untracked
audit artifacts), so no mutation-experiment contamination to diff against the pristine copy.

---

## A failed session-title write bricks the conversation with 409 for 605 seconds

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  1. Re-derived the claim from the source, not the report. `src/chemclaw/api/routes/turns.py`
     at HEAD: `_claim_turn_slot(active_turns, session_id)` is line 75; the title write
     `await front.session_owners.set_title_if_absent(...)` is line 87; the `try:` that owns the
     cleanup opens at line 211 and its `finally` (with the `if not handed_off` pop) is lines
     234–244. So the only pop of `active_turns` on the pre-stream path is unreachable from a raise
     at line 87 — the exception leaves the function before the `try` is entered. The cited symbols
     and line numbers are real and current.

  2. Wrote my own harness (`/tmp/verify_title_leak.py`) — a fake owner store, healthy in every
     method except `set_title_if_absent`, which raises `ConnectionError`; the real `create_app`,
     the real route, a scripted graph factory. `CHEMCLAW_SERVICE_HOST=127.0.0.1 PYTHONPATH=. uv run
     python /tmp/verify_title_leak.py` printed:

     ```
     turn_timeout          : 600.0
     admission_timeout     : 5.0
     active_turns before   : {}
     first POST            : 503 {'detail': 'server at capacity; retry shortly'}
     active_turns after    : {'b92a8d2a52a7446eaa74cac5c672408e': 3725.173692023}
     retry POST            : 409 {"detail":"a turn is already running for this session"}
     lease remaining (s)   : 605.0
     metric                : chemclaw_db_unavailable_total 1
     metric                : chemclaw_turns_in_flight 1
     ```

     The retry ran with `broken = False`, i.e. against a fully healthy owner store — nothing but the
     leaked slot refuses it. My measured lease is **605.0 s**, matching the finding's 605 exactly
     (600 + 5 from `_claim_turn_slot`, `state.py:206-210`).

  3. Removed the fake entirely and re-ran against the **real** `SessionOwnerStore` and the real
     psycopg pool, `session_store=postgres`, DSN pointed at a blackholed `10.255.255.1:5432`, with
     the session pre-seeded into `app.state.live_sessions` so the request's first DB touch is the
     title write (`/tmp/verify_title_leak_real_store.py`):

     ```
     owner store           : SessionOwnerStore
     turn claims           : SessionTurnClaims
     WARNING chemclaw.api.middleware: shedding POST /sessions/warm-session-id/messages:
        Postgres unreachable at ... host=10.255.255.1 port=5432: couldn't get a connection after 10.00 sec
     first POST            : 503 {"detail":"server at capacity; retry shortly"}
     active_turns after    : {'warm-session-id': 3785.496531017}
     retry POST            : 409 {"detail":"a turn is already running for this session"}
     ```

     This is the *pool-checkout* failure the finding names, end to end, with no test double in the
     path at all.

  4. Checked the exception type is the one the 503 handler is registered for:
     `core/db.py:200-205` converts `PoolTimeout`/`PoolClosed` into `ConnectionError`, and
     `app.py:209` registers `_database_unavailable` for `ConnectionError`. Direct call against the
     real store confirmed the type and the message:
     `ConnectionError: Postgres unreachable at user=x dbname=chem host=10.255.255.1 port=5432:
     connection timeout expired after 2.0s`.

  5. Checked for an upstream control that would prevent it. There is none: no broad exception
     handler pops `active_turns`, and `grep set_title_if_absent tests/` shows the only front-door
     test double (`tests/test_service.py:585`) always succeeds — nothing in the suite drives the
     failing branch.

- **Why**

  Every link the finding asserts reproduces on my own scaffolding, and the strongest version (real
  store, real pool, real 503 handler) reproduces too. The trigger is reachable in the shipped
  configuration: under `session_store="postgres"` the owner store is real, and on a **warm** session
  the live-session cache short-circuits `_rehydrate_session` (`deps.py:106-110`), so `set_title_if_absent`
  is the request's first database round trip — the title write is exactly where a transient pool
  hiccup lands. The consequence is as stated: a 503 whose own body tells the client to retry, and a
  retry refused 409 for the full 605 s lease although no turn ever started.

  Two things I would add that make it slightly worse than reported:

  - The leaked entry also **pins the session against LRU eviction** for the whole 605 s
    (`app.py:226-235`, `_turn_in_flight` reads the same unexpired-lease map), so the leak consumes a
    live-session slot as well as bricking the conversation.
  - The `chemclaw_turns_in_flight` gauge claim is not just plausible, it is measured: my run printed
    `chemclaw_turns_in_flight 1`. The gauge's own comment (`app.py:287-289`) says "a leaked entry
    waiting out its deadline is not a turn in flight" — that comment is wrong for an *unexpired*
    leak, which is the entire 605 s window.

  One thing that keeps me at **high** rather than critical, and which the finding already concedes:
  the shipped chart runs `service.replicas: 2` (`deploy/helm/chemclaw/values.yaml:49`), so a retry
  that lands on the other replica succeeds (the durable claim is genuinely not leaked — it is taken
  at line 225, after the raise). The damage is therefore ~50% of retries for ten minutes on a
  two-replica deployment and a total ten-minute brick with one replica or sticky routing. That is
  the severity the finding assigned.
