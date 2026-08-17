# Verdicts — `ingest/documents` security, reachability lens

Scope: findings marked **critical** or **high** only. The file has exactly one — the `O_NOFOLLOW`
finding. The other four are medium/low and were not examined.

---

## `O_NOFOLLOW` guards only the last path component, so a parent-directory swap reads outside the mount

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

### What I did

Wrote my own reproductions under `/tmp/verify/` (I read the reporter's `/tmp/aud/t1_nofollow.py`
but did not rely on it). No source file was modified.

**1. Mechanism + the constraint the finding does not mention** (`/tmp/verify/v1.py`) — crawl a
fixture mount, then (A) swap the ancestor directory for a symlink out of the mount, (B) test the
docstring's *own* stated case, (C) test whether an extensionless target can ever be baited:

```
crawl accepted: [('Projects/report.txt', '/tmp/verify/w/mount/Projects/report.txt')]
A) ancestor swap -> 'SECRET=hunter2' | escaped: True
A) stored ref_path (what the citation says): Projects/report.txt
B) final-component symlink refused: OSError 40          # ELOOP
C) crawl accepted for extensionless bait: [] | unsupported tally: {'(none)': 1}
```

**2. End-to-end through `sync_share` into the index** (`/tmp/verify/v3.py`) — `crawl_share` wrapped
so the swap lands inside the real crawl→read window, `InMemoryDocumentIndex`, embeddings stubbed:

```
report: {'source': 'sharedrive', 'scanned': 1, 'indexed': 1, 'embedded_chunks': 1,
         'cursor': 'Projects/note.md'}
  _chunks: ChunkRecord(doc_id='doc-307106be...', content='CONFIDENTIAL: batch 7 failed release', ...)
  _files:  FileRecord(path='Projects/note.md', source='sharedrive',
                      fingerprint='1786954356676561706:21', ...)
```

**3. What is actually mountable/readable in the shipped deployment** — read
`deploy/helm/chemclaw/values.yaml`, `templates/deployment-workers.yaml`, `_helpers.tpl`
(`chemclaw.tlsMount` / `knowledgeMounts` / `documentShareMount`), `ingest/documents/formats.py`,
`binding.py::_is_coherent`, `core/config/sources.py`.

### Why

**The mechanism is real and I confirm it.** `O_NOFOLLOW` is a final-component flag; every ancestor
is still traversed through links. Run (1)A and run (2) show the read escaping the mount and the
escaped bytes landing in `document_chunks` under `path='Projects/note.md'` — the forged,
mount-relative coordinate `ShareDocumentRetriever._chunks` renders verbatim into
`EvidenceChunk.source` (`retriever.py:227`). The docstring at `sync.py:141-145` is narrowly true
((1)B: the final-component swap really does raise `ELOOP`) and its broader sentence — "The open
re-checks rather than trusts" — is not honoured for ancestors. `crawl.py:262`'s `_within_mount`
does exist for exactly this escape one level up, as the finding says.

**One aggravator the reporter missed**, in the finding's favour: the `FileRecord` written is
`_file_record(source, by_path[d.ref_path], ...)`, so the stored fingerprint is the *innocent* file's
`mtime_ns:size` (run 2: `1786954356676561706:21`, the 21-byte decoy, not the 36-byte leaked text).
The next crawl therefore sees the file as unchanged and never re-opens it. The leaked chunk survives
the symlink being removed, indefinitely, with no counter and nothing in `SyncReport` to show it.

**What does not hold is the consequence.** "Arbitrary files readable by the worker's UID" is refuted
by a check the finding never traces: `_accept` (`crawl.py:139-142`) filters on `_extension_of(entry.name)`
against `binding.extension_set` *before* a `FileRef` exists, and the read reuses that same basename.
So the target's basename must end in one of the eight formats in `formats.EXTENSIONS`
(`.md .txt .csv .tsv .pdf .docx .xlsx .pptx`), and that set cannot be widened by a manifest —
`DocumentShareBinding._is_coherent` (`binding.py:187-194`) raises on any extension outside
`SUPPORTED_EXTENSIONS`. Run (1)C shows an extensionless bait file producing zero `FileRef`s.

Every target the finding names is therefore unreachable:

- **service-account tokens** — `token`, `namespace`, `ca.crt`: no allowlisted extension.
- **mounted secrets** — the only secret the chart projects as *files* is `secrets.temporalTls`
  (`_helpers.tpl:449`, `tls.crt` / `tls.key` / `ca.crt`). None allowlisted. Everything else reaches
  the worker through `envFrom`/`env`, which a file read cannot touch at all.
- **`/proc/self/environ`-adjacent config** — no extension.

The remaining volumes on the background worker (`chemclaw.knowledgeMounts` → `/app/knowledge-repo`,
and the note-repo checkout) *are* markdown and *are* reachable — but the knowledge graph carries no
`required_roles` of its own, so those notes are already retrievable by any authenticated caller.
In the shipped chart the escape's payload is, concretely, nothing that a share-entitlement holder
could not already read. It would become high the moment a deployment projects a secret as a file
with a document extension, or mounts a *second*, differently-gated share on the same worker
(`values.yaml` currently declares a single `documentShare`, so that needs a custom chart).

**And the trigger is three conditions deep, not one.**

1. Opt-in twice: `documentShare.enabled: false` in `values.yaml`, and `data_sources` defaults to
   `"graph,eln-json"` (`core/config/sources.py:45`) — `sharedrive` is enabled by neither.
2. The share must let a member create a symlink the Linux CIFS client resolves. The chart hands the
   PV to the operator, so the repo cannot assume this away — but it is not the default case either:
   `mount.cifs` without `mfsymlinks` and without SMB Unix extensions has no symlinks, and NTFS
   symlink creation over SMB needs `SeCreateSymbolicLinkPrivilege`. Against a Samba server with UNIX
   extensions and a POSIX client it is plausible.
3. "No race against the *file* is needed" is true; "no race" is not. The real directory must be in
   place during `crawl_share` and the symlink in place during `_read_and_parse` — my run (2) had to
   force that ordering deliberately, and my first attempt at winning it naturally
   (`/tmp/verify/v2.py`, an alternating flipper thread) did not land. A *persistent* ancestor symlink
   is caught in both positions the finding worries about: at the root by `_within_mount`
   (`crawl.py:262`) and one level down by the `follow_symlinks: false` skip (`crawl.py:202`) or
   `_within_mount` (`crawl.py:204`). The window is minutes wide and an attacker who can flip on a
   timer will eventually hit it, which is why this still deserves fixing — but it is a timed swap,
   not a standing misconfiguration.

**Net.** Mechanism CONFIRMED, citation forgery CONFIRMED and persistent (worse than reported), fix
as proposed is correct and cheap. The "high" label rests on reading secrets and tokens, and the
crawl's closed extension allowlist makes that impossible. Medium.
