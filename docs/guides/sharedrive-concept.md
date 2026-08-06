# Making a classical file share answerable

How to attach an on-prem SMB/CIFS share — the shared drive full of reports, decks, spreadsheets and
PDFs — so ChemClaw3 answers questions from it with a citation to the file and page. The decision
record is `docs/decisions/D-2026-08-06-a-share-is-mounted-not-called.md`; the code is
`src/chemclaw/ingest/documents/` (its `README.md` is the developer view).

Nothing here writes to the share, and nothing here enters the knowledge graph.

## The shape of it, in one paragraph

The share is **mounted read-only** into the background worker as a PersistentVolume. A scheduled
Temporal job walks it, reads each document with the parsers the upload path already uses, cuts it
into chunks that keep their page or slide number, embeds them, and stores them in Postgres. At query
time `gather_evidence` fans out to the share alongside the knowledge graph, and the share answers
only for callers who hold the entitlement its AD group grants. There is no SMB client in the
application, no credential in Python, and no new network peer.

## 1. Mount the share

Create a CIFS PersistentVolume and a PVC in the namespace. The mount credential is a Kubernetes
Secret the CSI driver reads — the application never sees it, which is the whole point of mounting
the share rather than speaking SMB from Python.

Then in `values.yaml`:

```yaml
documentShare:
  enabled: true
  claimName: chemclaw-sharedrive
  mountPath: /mnt/sharedrive
```

Only the background worker gets the mount. The front door answers from the index and would gain
nothing but attack surface.

**`mountPath` must equal the `mount:` in the binding below.** A mount with no enabled source is
crawled by nothing; an enabled source with no mount fails loudly on the first crawl (`share mount
… is not a directory — the volume is not mounted`), which is deliberate: the one failure that must
never degrade to "the share is empty".

## 2. Describe the share

Copy `src/chemclaw/ingest/sources/sharedrive/datasource.yaml` into a folder you mount, and put that
folder **first** on `CHEMCLAW_DATA_SOURCES_DIR`. Your site's layout is then not a change to this
repository at all.

```yaml
name: sharedrive
description: >-
  The R&D departmental drive.
retrieve: chemclaw.ingest.documents.retriever:ShareDocumentRetriever
config:
  binding:
    mount: /mnt/sharedrive
    required_roles: [chemclaw.sharedrive.reader]
    roots:
      - path: Projects
        tags: [project-work]
        tag_from_path: {segment: 0}   # Projects/<PROJECT>/… -> tag <PROJECT>
      - path: SOPs
        tags: [sop]
    exclude: ["~$*", "**/Archive/**", "*.tmp"]
    extensions: [.pdf, .docx, .xlsx, .pptx, .csv, .tsv, .md, .txt]
    max_file_bytes: 52428800
    chunk_chars: 1800
    chunk_overlap_chars: 200
```

What each part buys:

- **`roots`** is your staged-rollout control. Start with one folder. Roots may not overlap and may
  not be combined with `.`; the crawl refuses at load rather than indexing a file twice under two
  tag sets.
- **`tag_from_path`** recovers the project code from the folder a file sits in — usually the only
  place it is written down — so "what did we try in ACME-17" becomes answerable. `segment: 0` is
  the first component *below* the root.
- **`exclude`** globs match the mount-relative path and the basename. Office lock files (`~$…`) and
  archive folders are the usual population; excluding is cheaper than parsing, and an archive
  nobody consults only dilutes retrieval.
- **`extensions`** narrows what is opened. Anything not listed is counted per extension and
  reported — never silently dropped. A misspelt extension is refused at load, because `.pdff`
  matches no file and would leave you with a share that indexes cleanly and holds nothing.

## 3. Wire the AD group

The share's AD group becomes an **entitlement**, matched against the same role set that gates every
tool and skill. Two tenant wirings work, and the code is identical for both:

| Wiring | What to do | `required_roles` holds |
| --- | --- | --- |
| **App role** (preferred) | Assign the AD group to an Entra app role on the API's app registration. Needs Entra ID P1 for group-based assignment. | the app-role value, e.g. `chemclaw.sharedrive.reader` |
| **Group claim** | Emit the `groups` optional claim on the app registration; set `CHEMCLAW_ENTRA_GROUP_CLAIMS_AS_ROLES=true`. Any tier. | the group's **object-id** |

A caller without it gets nothing from this source — not a filtered list, nothing.

Two things to know before choosing the claim route:

- The gate then reads as GUIDs, which is harder to audit than a named role.
- A user in more groups than a token can carry (~150+) receives a **claim overage** instead of
  `groups`. Resolving it needs a Microsoft Graph call, which D-089 does not permit, so this system
  logs a warning naming the user rather than silently reading it as "no groups". Those users lose
  group-derived entitlements until the tenant is reconfigured. The app-role route has no such limit.

## 4. Cost it before you buy it

**Do this before enabling the source.** It walks the real mount exactly as the crawl does, reads no
file, and embeds nothing:

```
make share-estimate SHARE=sharedrive
```

```
candidates:        183402
over size limit:   1204
unreadable formats:
  .doc       97315
  .msg       41022
  .jpg       28870
  .xls       9981

embedding calls a first full run would make: roughly 183402 to 1834020 chunks …
```

The `.doc` line is usually what changes which roots you start with.

Rough shape at 500k files: ~30–50% pass the extension filter, content-hash dedup removes another
20–40%, and the survivors average ~8 chunks each — order of a million embedding calls and ~6 GB of
vectors. It is the dominant cost and the only one worth controlling.

## 5. Enable and run it

```
CHEMCLAW_DATA_SOURCES=graph,sharedrive
```

That is the only enable switch. The crawl earns its Temporal Schedule automatically once an enabled
source carries a share (every `CHEMCLAW_DOCUMENT_SYNC_SCHEDULE_MINUTES`, six hours by default);
`make schedules-apply` installs it. To run one now:

```
make share-sync SHARE=sharedrive
```

Validate the binding at any time with `make datasource-validate`, and
`python -m chemclaw.cli.validate_datasources --construct` to actually build it (the binding is a
rich document, and only `--construct` parses it).

## What will be invisible, and why

A decade-old share contains a great deal this system cannot read. It is counted and reported rather
than quietly dropped, because silence would be read as "the share held nothing else".

| What | Reported as | Why |
| --- | --- | --- |
| Scanned PDFs (no text layer) | `skipped_scan` | Reading them needs OCR, which is not built. Returning empty text would present to a chemist as "there was nothing in it". |
| `.doc` / `.xls` / `.ppt`, `.msg`, images, CAD, archives | `skipped_unsupported`, per extension | No offline structural reader is shipped for them. Converting legacy Office would mean a LibreOffice subprocess — a deferred item, not in scope. |
| Files over `max_file_bytes` | `skipped_oversized` | A 2 GB scanned archive or a database export named `.csv` is not a document. |
| Anything under a folder that is not a declared root | not counted | It was never a candidate. |

## Operational notes

**A re-crawl of an unchanged share costs nothing.** Files are compared by an `mtime_ns:size` stat
signature, so an unchanged file is never opened — a scheduled run over a static share is one
`scandir` pass and zero embedding calls.

**Duplicates are free.** A document's identity is the hash of its parsed text, so the same report in
four project folders is one set of chunks and one embedding call, and moving or renaming a file
costs nothing.

**Deleted files leave the index — but only after a complete crawl.** If any root fails to walk
(a dropped mount, a permission change, a renamed folder), *nothing* is pruned that run and an ERROR
is logged. An unreachable share and an empty one look identical from the inside, and of the two
possible mistakes re-indexing is recoverable and deleting is not. Expect this to be the log line you
see if the CIFS mount flaps.

**A gated share contributes nothing to scheduled reports.** `durable/report_workflow.py` runs with
no user identity, so the entitlement cannot be checked and the source correctly declines. Right by
construction, and tracked in `docs/planning/BACKLOG.md` for identity propagation.

**Citations look like** `sharedrive:doc-9f2a1c…#3`, with the readable location — `Projects/acme-17/
2024/report.pdf [page 7]` — carried alongside. When several paths hold identical content the
smallest is cited, deterministically, so a repeated question does not alternate between copies.
