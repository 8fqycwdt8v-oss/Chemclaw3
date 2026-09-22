# D-2026-09-22-an-unbounded-parse-may-not-blame-the-document — the in-process parse path's refusal

**Status:** accepted · **Date:** 2026-09-22 · Supersedes nothing. Closes the `BACKLOG.md` row *"A parse
reached outside the isolate child is bounded by nothing, so its broad arm has nothing to name"*, which
named two options and asked for one to be chosen.

## Context

`D-2026-09-19-a-refusal-that-blames-the-document-is-worse-than-one-that-says-nothing` put `_at_ceiling`
in the isolate **child**, where the ceiling is known, rather than in `parse_document`'s broad arm, where
it is not. A markup-heavy but entirely legal `.docx` that lxml refused with "Unable to allocate output
buffer" now reaches `too_large_to_read` and says so.

The residual is the path that never forks. `agent/attachments.parse_attachment` calls `parse_document`
directly — `cli/backfill_corpus.py` and the format tests reach it — and no `RLIMIT_DATA` is set on that
process. Verified at `HEAD`: `attachments.py` calls `parse_document(...)` with no ceiling anywhere on
the path, and `isolate._at_ceiling` returns `False` whenever `ceiling is None`, so on this path an
allocation failure inside a C parser and a genuinely malformed file are the *same observation*. The
parser's own words for the first — `unknown error (<string>, line 0)` — read as an accusation against a
document that may be perfectly good.

The row named the two honest options: give that path the forkserver too, at a process per attachment on
a path that parses one file at a time; or state that it is unbounded and make "could not be read" mean
it.

## Decision

**The path stays in-process, and its refusal states what it cannot establish.**

The forkserver is declined because `parse_attachment`'s own docstring already argues the in-process
choice is *right* for its callers, and that argument holds: `backfill_corpus` is a one-document-at-a-time
operator command where a slow parse costs the operator their own wait, and the format tests call it to
assert what each parser extracts. Neither is a shared replica, which is the whole reason
`parse_attachment_isolated` exists for the upload route. Adding a fork per attachment buys a bound
nobody on this path needs and costs a process on every call.

What was wrong was never the absence of the bound — it was a message claiming a verdict the path cannot
reach. So:

- **`UnclassifiedParseError`**, a new `DocumentParseError` subclass, marks **the one broad `except` arm**
  in `parse_document` — the `except Exception` whose own comment reads "Broad on purpose, and only
  here": a third-party parser failed and this system does not know why. That is a different fact from
  "we read it and it is over the limit", and only the first can be a memory failure wearing a parse
  error's clothes.

  This said "the two broad arms" through a first draft, an implementation and a test, and a review
  measured it false. The second site was `_refuse_a_bomb`'s `except zipfile.BadZipFile`, which is
  **narrow and classified** — "this is not a zip archive" — and cannot be an allocation failure, since
  `zipfile` raises `MemoryError` when it runs out. It was raising the unclassified type, so "File is not
  a zip file" arrived followed by a paragraph about memory not being established: the caveat attached to
  the one population that has a definite verdict, which is precisely the failure this decision exists to
  prevent, committed inside the change that prevents it.
- **`read_without_a_ceiling(cause)`** is the wording that population earns on an unbounded path. It
  keeps the parser's own message verbatim, because an operator debugging a share needs it, and adds what
  the message cannot establish. It returns an `UnclassifiedParseError` rather than the base class, so
  the caveat does not *erase* the distinction it is keyed on — rewrapping as `DocumentParseError` left
  the unbounded path unable to tell its own two populations apart. It takes no `name`, because `cause`
  already carries the sanitized one.
- A **classified** refusal passes through untouched — an unsupported format, a container that is not a
  zip, an over-expanding archive, a scanned PDF. Those hold whether or not a ceiling was set, and
  burying them under a caveat about memory would be the same what-do-I-actually-know failure pointed
  the other way. All four are driven, because naming three and asserting one is how the fourth got
  buried.

**The distinction is in the type, not in the string.** A caller cannot sniff an allocation failure out
of a parser's message — that is precisely why `_at_ceiling` measures `VmData` instead — and every
reading of the string it might attempt is one the next lxml release breaks.

## Consequences

**`DocumentParseError` by inheritance**, so every existing handler is unchanged: the share sync's
reject-and-continue net, the upload route, the isolate child. **One caller does read the class *name*,
and the first draft of this sentence said none did.** `ingest/eln/../documents/sync.py` counts refusals
by `type(exc).__name__` so its one WARNING line can name the distinct reasons, and
`tests/test_document_share.py` asserted the literal `DocumentParseError` there — so a corrupt PDF
reporting the more precise `UnclassifiedParseError` turned an improvement into a red test. The
assertion is on the suffix now; the operator-facing line gets strictly more specific, which is the
point of having the type.

**The unbounded path is now honest rather than bounded, and the difference is worth naming.** A chemist
uploading through the API gets a bounded parse and a refusal that distinguishes the two causes. An
operator running a backfill gets an unbounded parse and a refusal that says so. That asymmetry is the
decision; it is not a gap left for later.

**What this does not do:** it does not make the in-process path safe against a non-terminating parse.
`D-2026-09-12-a-parse-that-cannot-be-killed-wedges-its-replica` is about a *shared* replica, and on this
path the process being held is the operator's own terminal. The refusal wording says nothing about
timeouts because nothing here changed about them.

**Revisit when:** a caller of `parse_attachment` appears that is neither an operator command nor a test
— anything serving a request, or anything parsing more than one document concurrently. At that point the
argument above stops holding on its own terms and the forkserver is the answer, not a better message.

**What holds it:** `tests/test_parse_isolation.py` drives the unclassified population through the
in-process path and requires the caveat, drives all four classified refusals through it and requires the
caveat's *absence*, and derives the broad arms from `parse.py`'s own AST — so a third broad arm added
later is either covered or red, rather than silently outside the type.

**That derivation is over `except` handlers, and its first version was over `raise` statements**, which
is not the same guard and is why the `BadZipFile` defect above shipped past it. Counting
`raise UnclassifiedParseError(...)` sites and asserting a number passes unchanged when a third broad arm
is added that raises the base class — measured — so it held neither direction of the property it
advertised. It now walks every `ast.ExceptHandler` in the module, requires each *broad* one (bare,
`Exception`, `BaseException`) to raise this type, and requires that no *narrow* one does. The second
assertion is the one that would have caught the defect.
