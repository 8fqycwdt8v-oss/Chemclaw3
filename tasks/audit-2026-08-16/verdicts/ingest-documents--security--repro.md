# Repro verdicts — `src/chemclaw/ingest/documents/` security findings

Lens: **does it actually reproduce?** In-scope: findings marked critical or high. The file
contains exactly one — the `O_NOFOLLOW` finding. The other four are medium/medium/low/low and
were not examined.

All reproductions below are my own scripts (`/tmp/verif/repro_nofollow.py`,
`/tmp/verif/repro_scope.py`, `/tmp/verif/cross_share.py`), written from the source. The
reporter's `/tmp/aud/` scripts were not run. No source file was mutated; the slice under audit is
byte-identical to the pristine `HEAD` copy (`diff -r` over
`src/chemclaw/ingest/documents/` reports only `__pycache__`).

---

## `O_NOFOLLOW` guards only the last path component, so a parent-directory swap reads outside the mount

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

### What I did

**1. End-to-end reproduction from scratch** (`/tmp/verif/repro_nofollow.py`): build a mount, crawl
it with the real `crawl_share`, swap the ancestor directory `Projects` for a symlink pointing
outside the mount, then hand the crawl's own `FileRef` to the real `_read_and_parse`, and then to
the real `_parse_changed`:

```
crawl files: [('Projects/report.txt', '/tmp/verif/scene/mount/Projects/report.txt')]
after swap: Projects is symlink -> /tmp/verif/scene/outside_the_mount
ref.absolute still: /tmp/verif/scene/mount/Projects/report.txt
READ SUCCEEDED. text = 'SECRET: k8s serviceaccount token eyJhbGciOi... \n'
realpath of what was opened: /tmp/verif/scene/outside_the_mount/report.txt
ESCAPED THE MOUNT: True
stored path in document_files: Projects/report.txt
_parse_changed -> [('Projects/report.txt', 'SECRET: k8s serviceaccount token eyJhbGciOi...')]
                  refused: [] skipped_unreadable: 0
```

So: no exception, no counter, no `refused` entry — the escape is silent, and `_file_record`
(`sync.py:174`) stores `ref.path` verbatim, exactly as reported. `ShareDocumentRetriever._chunks`
(`retriever.py:227`) then renders `source=hit.path`, so the citation names
`Projects/report.txt` for content that came from outside the mount.

**2. Line numbers and symbols verified against the working tree**: `_read_and_parse` is
`sync.py:136-171`; line 153 is `descriptor = os.open(ref.absolute, os.O_RDONLY | os.O_NOFOLLOW)`;
the read is dispatched at `sync.py:204`, the crawl at `sync.py:282`; `_within_mount` is
`crawl.py:110-121`; the docstring making the broader claim is `sync.py:141-145`. All current.

**3. How far the escape actually reaches** (`/tmp/verif/repro_scope.py`). Two probes:

```
crawl accepted: ['B/tmp/verif/scope/fakesecrets/notes.md']
 READ B/tmp/verif/scope/fakesecrets/notes.md -> 'MARKDOWN SECRET'
crawl of 1000 files: 0.017s ; parse of all: 0.021s ; window for the LAST file >= 0.038s
```

- A share entry named `token` (an extension-less secret, the reporter's headline example) is
  **not** accepted by the crawl — `_accept` filters on `_extension_of(entry.name)` against
  `binding.extension_set`, so it never becomes a `FileRef` and is never read.
- An ancestor symlinked to `/` **does** give absolute-path reach: the crawl accepted
  `B/tmp/verif/scope/fakesecrets/notes.md` and the read returned the file's real contents from
  outside the mount. So the reachable set is *any file anywhere on the worker's filesystem whose
  basename ends in one of the eight allowed extensions* (`formats.py:19-28`: `.md .txt .csv .tsv
  .pdf .pptx .docx .xlsx`) — not "arbitrary files readable by the worker's UID".

**4. An escalation the report does not name** (`/tmp/verif/cross_share.py`). Two shares mounted in
the same pod is a supported deployment (`durable/document_sync.py:60` `share_sources()` returns a
dict; `retriever.py` discusses two mounted shares by name). Swapping a public share's ancestor for
a symlink at a **role-gated** share's subtree:

```
gated share required_roles: ['hr.read']
  PUBLIC source indexes 'Projects/minutes.md' -> 'HR BOARD MINUTES: layoffs Q3'
```

The gated share's document is indexed under the *public* source's name, so
`ShareDocumentRetriever._entitled` (`retriever.py:116-126`) — the package's entire security model
by its own docstring — never sees it. The extension constraint that narrows probe 3 does not bite
here at all: another document share's corpus is by construction all allowed extensions.

**5. Existing test coverage checked.**
`tests/test_document_share.py:1377` (`test_a_file_swapped_for_a_symlink_after_the_crawl_is_not_followed`)
covers only the **final**-component swap. The parent-directory variant is untested, and the
`follow_symlinks` binding is not consulted at read time at all — `_read_and_parse` takes only
`ref` and `max_bytes`.

### Why

The mechanism is exactly as stated and reproduces on a script I wrote from the source. `O_NOFOLLOW`
constrains the final component only; every intermediate directory is still traversed through links,
and nothing between `os.open` and the `document_chunks` row re-establishes containment. The
docstring at `sync.py:141-145` — "the open re-checks rather than trusts … refuses a path that
became a symlink — pointing at, say, the workload-identity token the crawl never saw" — is a claim
the code does not support, and the workload-identity token is the one example that is *doubly*
wrong (no extension, so the crawl would not accept it either).

One clause of the finding **is** overstated and should be corrected before it becomes a fix
commit: the consequence is not "arbitrary files readable by the worker's UID"; a service-account
token and `/proc/self/environ` are unreachable because the crawl's extension allowlist runs on the
share-side name. That correction does not lower the severity, because what replaces it is worse
than what it removes: any `.md`/`.pdf`/`.docx`/`.xlsx`/`.csv` anywhere on the pod filesystem
including the knowledge repo (which the finding does name correctly), plus the whole corpus of any
*other* mounted share regardless of its `required_roles`.

Two caveats on reachability, neither fatal:

- It is a TOCTOU race, not a static hole. The window is the whole batch — `crawl_share` returns all
  candidates for the chunk before `_parse_changed` opens the first one, and
  `document_sync_batch_size` defaults to 500. On my tmpfs that window measured 38 ms for 1000
  files; on a real CIFS mount a 500-file crawl plus parse is seconds to minutes, and the workflow
  restarts from the top of the share on every scheduled run, so an attacker flipping the symlink in
  a loop gets unlimited attempts.
- It requires symlink creation on the mount. That is a property of the mount options (Samba unix
  extensions, or `mfsymlinks`, under which a symlink is an ordinary file any member can write), not
  of this code — and the package's own design treats symlinks on the share as in-scope
  (`follow_symlinks`, `_within_mount`, `crawl.py:262`), so it cannot claim they are impossible here.

The reporter's proposed fix (resolve `/proc/self/fd/<n>` after the open and refuse anything outside
`Path(binding.mount).resolve()`) is sound and I confirmed it closes probe 1, 3 and 4 in principle:
the descriptor is already open, so the readlink cannot be re-pointed. The `dir_fd=` walk is
stronger and not required.
