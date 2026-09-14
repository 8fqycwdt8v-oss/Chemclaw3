# D-2026-09-13-a-deadline-that-stops-at-the-first-byte-is-not-a-deadline — the wedge reappeared one stage past where it was closed

`D-2026-09-12-a-parse-that-cannot-be-killed-wedges-its-replica` moved document parsing into a
`forkserver` child so a runaway parse could be killed and its parse slot returned. The child is the
right answer. The **waiting** around it had one deadline, and `Connection.poll` returning True
consumes it.

## What was measured

`parse_document_isolated` bounded `reader.poll(timeout)` and nothing else: `recv` then blocks until
the whole pickled message arrives or the pipe reaches EOF, `child.join()` had no timeout at all, and
neither path killed anything. Driven against the shipped function with a 1 s deadline and 15 s of
patience, on a worker thread arranged exactly as `agent/attachments.py` arranges it:

| child behaviour | worker thread |
| --- | --- |
| exits 0 without answering | ends, `ParseWorkerLost` |
| `os._exit(7)` | ends, `ParseWorkerLost` |
| non-terminating before any byte | ends, `ParseWorkerLost` |
| **writes a truncated message, then stops** | **still alive at +15 s** |
| **answers correctly, then does not exit** | **still alive at +15 s** |

A thread that never ends never releases its slot, so at the shipped cap of two such uploads the
replica's upload path is down for the life of the process — the exact failure that ADR was written
for. `agent/attachments.py`'s `wait_for` backstop cannot see it because that wait is `shield`ed: it
frees the caller while the thread it stands for runs on.

Two smaller things measured in the same pass. A child that starts a process of its own survives the
kill — `Process.kill()` signals one pid — so the slot came back while the CPU stayed spent, driven
at one orphan per killed parse. And a `Pipe` created before the `try` leaked two descriptors when
`child.start()` raised.

## The decision

**One monotonic deadline across the whole exchange, and every stage measures against it.**

- `poll` gets what is left.
- `recv` gets a deadline through a `threading.Timer` that kills the child, because `recv` has no
  timeout argument and a dead child closes the write end — which turns a stalled `recv` into the
  `EOFError` this function already handled. `OSError` is caught beside it: a partially-read message
  raises *"got end of file during message"*, not `EOFError`.
- the reap gets what is left and then kills, rather than waiting.
- one `threading.Event` makes the three stages book **one** kill rather than three.

**The kill is a group kill.** `_parse_into` calls `os.setsid()` so the child leads its own process
group, and `_kill` signals that group only when it is demonstrably not this process's own — a child
that could not detach is killed singly, exactly as before, because `killpg` on our own group would
take the front door down with it. `Chemclaw3-mcp` reached the same answer for `xtb`
(`run_isolated`: `start_new_session=True` plus a group kill); this is that shape for a `forkserver`
child, which has no `start_new_session` to pass.

**The share crawler now parses in that same child.** `ingest/documents/sync.py` wrapped
`asyncio.to_thread(_read_and_parse, …)` in `wait_for` and its own docstring stated the consequence in
the present tense: the pass moved on while the thread ran the hostile document to completion. That
thread belongs to the Temporal worker's **shared** default executor, so N pathological documents on
an SMB share consume N executor threads for the life of the process, on a pool nothing caps, with
the subprocess sitting one module over as a drop-in. `_read_and_parse` reads on the worker thread —
the mount is what a child cannot bound — and parses in the child. The crawl's `wait_for` stays and
is now narrow and stated: it covers the *read*, and it is the parse budget plus
`attachment_parse_reap_grace_seconds` for the reason `agent/attachments.py` gives. `ParseWorkerLost`
gets its own `except` arm, because it is a `DocumentParseError` subclass and would otherwise be
filed under `skipped_unreadable` beside a corrupt PDF, zeroing the one counter that says a bound
fired.

Two things recorded rather than changed. `isolate.py`'s docstring claimed the post-`poll` transfer is
"bounded by `attachment_max_bytes` upstream" (2 MB); it is bounded by `document_max_expanded_bytes`,
64 MB, which the same docstring names two paragraphs later. And a parse child re-executes the
serving process's `__main__` on every parse (`multiprocessing.spawn._fixup_main_from_path`), so the
14 ms is the small half and the hazard is that any side effect at module scope in an entry point now
runs once per upload, in a child — demonstrated accidentally by a probe script written for this ADR
that had no `if __name__ == "__main__"` guard and turned every parse into `ParseWorkerLost`.

## What keeps it true

`tests/test_parse_isolation.py::test_a_child_that_stalls_after_its_first_byte_still_frees_the_worker_thread`
drives `tests/parse_stalls.py` as a subprocess. It is a subprocess because the only channel into a
`forkserver` child is the server's preload list and that server is a process-wide singleton another
test has already warmed; what it substitutes is the child's *behaviour*, and the parent side is the
shipped function. It leads its own process group and leaves by `os._exit`, because
`multiprocessing`'s exit handler joins live children without a timeout — with the regression present
this probe would otherwise hang after printing, and the caller would see a timeout instead of the
`alive` line the assertion is about.

`tests/test_document_share.py::test_a_share_document_is_parsed_in_a_process_the_crawl_can_kill`
drives the real crawl over a document whose parse is an order of magnitude past the deadline, and
asserts `skipped_timeout`, `skipped_unreadable == 0`, the kill counter, and that the pass duration —
which `asyncio.run` makes include the worker thread — is under the parse.

`tests/test_parse_isolation.py::test_a_parse_child_is_still_inside_the_no_egress_posture` was
non-vacuous in one direction and vacuous in the other: its probe lived in the test module, so the
child imported that module, and with it `chemclaw.core.config`, which armed the guard *in the child,
on that line*. Driven with `_PRELOAD` emptied, it passed. The probe is now `tests/egress_probe.py`,
which imports `socket` and `sys`, and reads `chemclaw.core.netguard` out of `sys.modules` rather
than importing it.

Four mutations, each restored from a `.bak`:

| mutation | result |
| --- | --- |
| the whole merged waiting logic restored | red, on the named assertion: *"the worker thread for a child that truncate.txt was still alive after 15 s"* |
| `os.setsid()` removed from the child | red, on the orphan assertion |
| `_PRELOAD` emptied | red — `assert None is True`, the guard not armed in the child |
| the crawler back to the in-thread `parse_document` | red — `skipped_timeout` 0, the document indexed |
