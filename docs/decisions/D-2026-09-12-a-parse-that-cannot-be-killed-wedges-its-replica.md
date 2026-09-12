# D-2026-09-12-a-parse-that-cannot-be-killed-wedges-its-replica — A parse that cannot be killed wedges its replica

**Status:** accepted · **Date:** 2026-09-12 ·
**Extends:** `D-2026-08-08-a-slot-lives-as-long-as-its-response` (the parse cap and its shed) ·
**Corrects:** `core/netguard.py::_host_of`, which refused local IPC while its docstring said it
exempted it

## Context

`agent/attachments.py` bounds how many uploads a replica parses at once
(`attachment_max_concurrent_parses`, shipped at 2), queues briefly past the cap and then sheds with
a retryable 503. That control is real and it was driven again here: with two slots held, a third
upload was refused in **2.00 s** with `AttachmentUnavailable`.

What no test asked is whether a slot ever comes *back*. A slot stands for a running worker thread
and is released by that thread's completion callback — `_ParseSlots._give_back`, hung off
`Future.add_done_callback`. CPython cannot stop a thread, so the slot is released when the parse
returns, and nothing bounded when that is.

### Measured, against a deliberately non-terminating parser, at the shipped cap

```
cap: 2   parse timeout: 1.0s (squeezed)   queue wait: 2.0s
first two:  [('a', 'DocumentParseError', 1.003), ('b', 'DocumentParseError', 1.002)]
in_flight after both callers freed: 2
third:      ('c', 'AttachmentUnavailable', 2.003)
in_flight after +5s: 2
fourth:     ('d', 'AttachmentUnavailable', 2.002)
```

Both callers were freed on their deadline and told the truth. The **replica** then refused every
later upload, permanently, with a message naming two parses in flight that no one was waiting for.
This is a liveness failure of a shared process, not a resource leak: the cap was working exactly as
designed and the pod was dead.

`core/config/memory.py` described this as the design — *"The timeout bounds the wait, not the
thread: Python cannot kill one, so a parse past this limit is refused to its client while the thread
runs to completion against the cap"* — which is an accurate description of the bug, written in the
register of a decision. `_ParseSlots`' own docstring argued the release rule from the same premise.
The premise is true; the conclusion is a pod that never serves an upload again.

## Decision

**The parse runs in a child process the worker thread kills on the deadline.**
`ingest/documents/isolate.py::parse_document_isolated` forks it from a `multiprocessing` forkserver,
waits `attachment_parse_timeout_seconds` for an answer on a pipe, and `SIGKILL`s and reaps the child
if none comes. `agent/attachments.parse_attachment_isolated` is what the upload path's worker thread
now runs; `parse_attachment` stays in-process for `cli/backfill_corpus.py` and the format tests,
where a slow parse costs only the caller.

The release rule is unchanged — the slot still comes back when the thread ends. What changed is that
the thread now ends.

### Why a subprocess at all, and why this one — measured rather than argued

| Arrangement | Cost per parse | Runaway |
| --- | --- | --- |
| Worker thread (before) | 0 | holds its slot for the life of the process |
| Fresh interpreter per parse | **0.97 s** | killable |
| `forkserver` child, parsers preloaded | **0.010 s** (first: 0.857 s) | killable — `exitcode=-9`, reaped |

Four consecutive forkserver parses: 0.857 s, 0.011 s, 0.010 s, 0.010 s. Under pytest each child
additionally costs ~0.03 s re-importing the console-script `__main__` that `multiprocessing` fixes
up, measured at 0.040 s per parse; under a module entry point that fixup is a no-op. A fresh
interpreter per upload was never a serious option and the forkserver is what makes the option
serious.

**`forkserver`, not `fork`.** The front door is threaded — uvicorn's default executor runs these
parses and `api/auth.py` validates every bearer token on it — and `os.fork()` from a multithreaded
parent hands the child a memory image in which any lock another thread held is held forever by a
thread that does not exist. `forkserver` starts its server by fork **and exec**
(`multiprocessing.util.spawnv_passfds`), so the server is a clean single-threaded interpreter and
every parse forks from *it*.

### What it does not buy

An abandoned-thread reclaim was the alternative considered, and it is what this repository would
have had to ship if the measurement had come out the other way: release the slot after some multiple
of the deadline, cap how many threads may be abandoned, and report the pod unready once that budget
is spent. It converts a permanent wedge into a bounded one and leaves the CPU burning. It is not
needed and is not built — but the reasoning is recorded because the rejected option is the one that
gets reinvented.

## Consequence: the egress guard refused the forkserver's own socket

The first end-to-end drive of the fix failed with:

```
egress refused: outbound connection to '/tmp/pymp-x5luogyq/listener-nlzs5bwk' is not on the allowlist
```

`netguard._host_of` fell through to `host = address` for a non-tuple address, so an `AF_UNIX`
address — a bare filesystem path — arrived at `_check` as a hostname, failed `is_loopback_host`,
was counted on `chemclaw_egress_refused_total` and raised `EgressForbidden`. Its own docstring said
the opposite: *"Returns None for a family the check cannot read (AF_UNIX is a path, not a host) so
`_check` treats it as 'nothing to leave for' rather than refusing local IPC."* Nothing asserted it
in either direction.

The C half of the same control had it right all along — `netguard_preload.c` reads `sa_family` and
checks only `AF_INET`/`AF_INET6`, with a comment saying why. The two layers of one guard disagreed
and the Python one contradicted itself.

`_host_of` now reads a host only out of a tuple address, which is what an internet address is. This
narrows the guard and loses nothing: a path under `/tmp` leaves the host by no route the allowlist
is about, so refusing it protected nothing and broke local process IPC. Both spellings of an
internet host — `str` and `bytes` inside the tuple — are still read.

## Consequences

- Every upload costs one forked child: ~10 ms warm, ~0.9 s on the first upload a process serves.
  `isolate.parse_context()` is public so a deployment may warm it at startup; nothing does today.
- `attachment_parse_reap_grace_seconds` (5.0) is new. The thread enforces the deadline itself, so
  the caller's `wait_for` is a backstop over the one thing that enforcement cannot see — the
  forkserver's own 0.86 s start, which happens before the child's clock begins.
- `chemclaw_document_parse_kills_total` counts a killed reader, so losing parse capacity is visible
  before the shed rate says so.
- A child reads `Settings` as the forkserver imported them, not as the caller holds them. Correct in
  a deployment; a test that monkeypatches a parser setting must drive `parse_document` directly.
- `tests/test_request_limits.py::test_a_parse_past_its_timeout_is_refused_and_keeps_its_slot_until_the_thread_ends`
  asserted the wedge as the contract and is renamed and rewritten. It could never have caught this:
  the fake parse it patches in blocks on an `Event`, so it is unkillable by construction and the
  assertion was about the fixture.

## What keeps it true

- `tests/test_parse_isolation.py::test_a_parse_past_its_deadline_frees_its_slot_for_the_next_upload`
  — the regression test, stated as what a chemist experiences. Mutated by parsing in-process again:
  `assert 1 == 0 where 1 = _PARSE_SLOTS.in_flight`.
- `tests/test_parse_isolation.py::test_the_cap_still_sheds_when_the_slots_are_genuinely_busy` — the
  other direction, so a cap that released every slot immediately could not pass the one above.
- `tests/test_parse_isolation.py::test_the_parse_does_not_run_in_this_process`
- `tests/test_parse_isolation.py::test_a_parsers_refusal_crosses_the_process_boundary_as_itself`
- `tests/test_parse_isolation.py::test_a_parse_past_its_deadline_is_killed_and_counted`
- `tests/test_parse_isolation.py::test_local_ipc_is_not_refused_as_egress` — both the address reader
  and a real `AF_UNIX` connect through the armed guard.
