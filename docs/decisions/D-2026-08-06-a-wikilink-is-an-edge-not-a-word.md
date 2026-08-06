# D-2026-08-06-a-wikilink-is-an-edge-not-a-word — A wikilink is an edge, not a word

**Status:** accepted · **Date:** 2026-08-06

## Context

Two rows from the security sweep's data-plane lane, and one that turned out to be already closed:

- **[M] ELN free text becomes real knowledge-graph edges** (`ingest/eln/note.py`). A chemist can
  forge `contradicts`/`supersedes` relations into a PR-gated reaction note by writing them into an
  ELN field — the gate reviews the note, not the edges it asserts.
- **[M] A report note wikilinks non-note evidence ids** (`retrieval/harness.py`), producing an
  unmergeable report and a fabricated relation type.
- **[Low] DARK-10 — the PR-gate's checkout window exposes unreviewed notes to readers.**

Both open rows are the same mistake in two places: `[[…]]` is treated as *text being rendered* when
it is *an edge being written*.

## Decision

### The gate reviews a claim; an edge is a claim a reviewer does not read as one

The PR-gate exists so a human checks what an agent proposes. That check is only worth anything over
text a human reads *as* a claim. `[[supersedes:reaction-eln-0001]]` inside a procedure paragraph
retires another team's result on merge, and to a reviewer skimming a recipe it reads like a
reference. Reproduced before the fix — a `procedure_text` of
`"[[supersedes:reaction-eln-0001]] and [[contradicts:playbook-degassing]]"` yielded exactly those
two ids from `cited_ids` on the mapped note.

### The ELN mapper can be escaped whole, because it emits no links of its own

`ingest/eln/note.py` has claimed since it was written that it carries no `[[wikilink]]` — a dangling
one would fail `kg-validate` on the very PR it opens. Verified: a clean record maps to a body
containing no `[[` at all. So every `[[` in the composed body arrived from the record, and a single
escape at the composition point is sound where a per-field escape would be six calls that a seventh
field will forget.

That invariant is now *asserted* rather than trusted. If a future change gives the mapper a real
link to emit, `test_the_mapper_emits_no_wikilinks_of_its_own` fails and says the escaping has to
move to the individual fields.

Escaped and not stripped: the reviewer is the control, so they must see what was attempted. A silent
strip would hide the one thing worth escalating — that an ELN entry contained an attempt to write a
graph edge — behind a note that looks ordinary.

### The report renderer asks the reader what it is allowed to write

`f"[[{chunk.source_note_id}]]"` assumed every retriever returns a note id. Two shipped ones do not:
`ingest/eln/warehouse/retriever` returns `<source>:<row key>` and `ingest/sources/vendored_dataset`
returns `vendored:<dataset>:<index>`. Both are correct provenance and neither is a note, and the
colon makes the *reader* split the prefix as a relation:

```
'reaction-eln-0001'           -> cited_ids=['reaction-eln-0001']
'eln-snowflake:reactions:12'  -> cited_ids=['reactions:12']      # relation 'eln-snowflake'
'molfp:CCO'                   -> cited_ids=['CCO']               # relation 'molfp'
```

So one wikilink produced two failures: a citation of a note that does not exist, and a relation type
the vocabulary does not contain — which `kg-validate` refuses, making the draft unmergeable.

The predicate is the **reader's**: a target is safe to link exactly when `cited_ids` gives it back
unchanged. Inventing a slug pattern here would be a second definition of "note id" to drift against
`chemclaw.kg.note`; deriving the writer's rule from the reader's parser means a change to the link
syntax cannot leave the two disagreeing.

That precision earns its keep immediately. A hand-written "reject anything with a colon" would also
refuse `[[:x]]` and `[[rel:]]` — and it would be wrong to, because the reader returns *those* whole:
they are ordinary citations of invalid ids, which `kg-validate` already refuses by name. They are a
dangling link, not a forged relation, and the two failures want different answers. The first version
of the test asserted the cruder rule and had to be corrected against the code.

A non-note source stays visible as plain text rather than being dropped: it is what the section
rests on, and a reader who cannot see it cannot check it.

### DARK-10 was already closed, and the row was stale

`kg/git_submitter.py` performs each submission in a private `git worktree` under `.git/`, never
switching the tree readers resolve. That landed on 2026-08-05 in
`D-2026-08-05-three-searches-that-disagreed-about-one-note`, whose own record states the property
the row asked for. The row is ticked with that reference rather than re-implemented.

## Consequences

- An ELN record's free text can no longer write an edge. Its text is unchanged and legible; only
  the `[[` opener is escaped, visibly, so a reviewer sees the attempt.
- A report drawing on the warehouse or a vendored dataset renders and merges. Before this, any
  report citing either was refused by `kg-validate` for a relation nobody wrote.
- Both fixes are mutation-proven **at the call site**. That distinction is the session's recurring
  lesson and it fired again here: the first version of the report test called `_citation` directly
  and passed with the call site reverted. The helper was pinned; the line that used it was not.
- The forgery test is parametrized over the five ELN fields whose text actually reaches the body,
  established by measurement — the first version covered `procedure_text` and `failure_reason`,
  neither of which reaches the body alone (a free-text procedure renders only beside `steps`, a
  failure reason only with a failing `outcome_class`). It passed, and proved nothing. Each case now
  asserts the text *arrived* before asserting it minted no edge.

## Alternatives rejected

- **Escaping each ELN-sourced field at its interpolation site.** Six calls today and a seventh field
  tomorrow that nobody escapes. Correct only if the mapper had links of its own to preserve, which
  is exactly what the asserted invariant covers — and if that changes, the test says so.
- **Stripping forged links instead of escaping them.** Hides the attempt from the only control that
  could act on it.
- **A slug regex for "is this a note id".** A second definition of note identity, free to drift, and
  measurably cruder than the reader — it would refuse two shapes that are not forgeries.
- **Dropping non-note provenance from reports.** The section rests on it; a citation a reader cannot
  see is worse than one they cannot click.
