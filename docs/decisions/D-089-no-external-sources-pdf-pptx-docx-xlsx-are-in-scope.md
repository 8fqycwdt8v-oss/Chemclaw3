# D-089 — No external sources; PDF/PPTX/DOCX/XLSX are in scope

Three review decisions on the F11 work, taken by the user and recorded here with what each changed.

### 1. No external sources. The PubChem retriever is removed, not switched off.

D-084 chose PubChem PUG-REST for TOOL-6 and shipped it off-by-default, reasoning that registry
membership was a sufficient enable switch and that opting in constituted accepting the egress. The
scope answer is simpler and stricter: **this system takes no external sources at all.** So
`report/literature.py`, its registry entry, its two config fields and its five tests are deleted
rather than left dormant — a dormant integration is still a maintained one, and "off by default"
invites a deployment to turn it on.

**The interesting part is why a test was added rather than a note.** The constraint was *already
written down*. `DEFERRED.md` carried TOOL-6 as "blocked on a decision: which source, under which
licence" — which reads as an invitation to answer the question, and that is exactly what happened.
Prose stated the constraint and did not enforce it, so `tests/test_no_egress.py` now fails on any
first-party module that names a third-party data host, plus a registry-membership check for a
source whose address would arrive entirely from config. Both `DEFERRED.md` rows are rewritten from
"not yet" to "rejected", because the old wording is the actual root cause here.

The allowlist holds exactly one host — Entra's login endpoint, which genuinely is Microsoft's since
that is the identity provider F4 chose. Everything else the stack talks to (LLM, Temporal, Postgres,
Tower, the git remote) carries *no host default in source at all*: it is required config, so a
deployment cannot inherit somebody's address by accident. That the list is one entry long is the
useful fact it records.

### 2. PDF, PPTX, DOCX and XLSX are in scope, read through their own document models.

D-084 refused these formats with a specific argument: a PDF "parsed" by scraping text-like bytes
produces confident nonsense a chemist cannot distinguish from a real reading. The scope decision
reverses the refusal. It does not refute the argument — so the fix is *real extraction*, never a
relaxed version of the guess. Each format is read through its own library (`pypdf`, `python-pptx`,
`python-docx`, `openpyxl`), page/slide/sheet boundaries are preserved because "the table on page 3"
must still resolve after ingest, and a file the library cannot open is refused rather than salvaged.
All four parse locally, which is what makes them consistent with decision 1.

**What survives from the original refusal is the one case extraction cannot fix.** A scanned PDF
opens fine and yields nothing; returning that as an empty document would tell a chemist their CoA
was blank. It is refused by name instead. The test is **"did any page produce text at all"** and
deliberately not a minimum length — the first cut used a 32-character floor, which would have
refused a legitimate one-line CoA, i.e. reproduced the false-negative the refusal exists to avoid.
Zero characters is the property that actually distinguishes a scan; anything else is a magic number.

Two smaller calls worth stating: speaker notes are extracted from decks, because a project deck's
reasoning usually lives there and dropping them would discard the informative half; and `openpyxl`
reads with `data_only=True`, because a chemist attaching a yield sheet means the yields — `=B2/C2`
is not an answer.

Fixtures are **built by each format's own writer inside the tests**, never committed blobs, so the
assertions are about our parsing rather than about a file someone once produced. The PDF fixture is
assembled by hand (catalog, page tree, a `BT … Tj ET` content stream, a correct xref) because
`pypdf` writes PDFs but cannot typeset, and adding a renderer purely to make fixtures would be a
dependency the shipped code never uses.

### 3. Audit-trail archive-then-reseal stays in the backlog.

No change. `workflows/retention.py` continues to refuse `audit_events`, and the reasoning in
`DEFERRED.md` — deleting from a hash chain is indistinguishable from the tampering it detects, so
safe disposal needs an out-of-band genesis anchor and QA sign-off — stands as written. Recorded
here only so the decision is visibly *made* rather than overlooked.
