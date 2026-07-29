# D-085 — F11 completion: the five items blocked on a decision or a prerequisite

**Phase F11 completion: the five items that were blocked on a decision or a prerequisite.**

**Context.** D-084 closed W3–W4 except five items I had recorded as blocked. Asked to implement them
anyway, the right move was the one D-057 already established for this repo: **make the blocking
decision explicitly and record it, rather than defer a second time.** Each decision below is stated
in the module that embodies it, so a future reader sees the reasoning at the point of use.

**Decisions.**

1. **External literature source → PubChem PUG-REST** (TOOL-6). Chosen because it is the only option
   clearing every constraint this repo actually has: public and licence-clean (Reaxys/SciFinder need
   a site licence this repo cannot assume), credential-free (every other external integration here
   needs a secret), and **structure-keyed** — it answers by the same key the fingerprint index uses,
   so a hit joins the corpus without a second naming scheme. The alternatives are not excluded; they
   are one sibling class each, which is exactly what the F7 retriever seam was built to buy.
   Retrieval is by structure only: free-text search over a chemistry question returns noise, so a
   query with no parseable structure returns empty rather than guessing — the same conservatism
   `resolve_compound` applies. Every failure mode degrades to *empty*: external evidence must never
   be able to sink an answer the internal corpus could already give.

2. **Upload formats → a closed allowlist that refuses what it cannot parse** (AGT-3). Markdown,
   plain text, CSV and TSV parse completely and deterministically offline. PDFs, spectra and images
   are **refused with a message naming what is supported**. This is the load-bearing half: a PDF
   "read" by scraping whatever bytes look like text produces confident nonsense a chemist cannot
   distinguish from a real reading, which is strictly worse than the gap. Attachments are
   session-scoped working material, never knowledge — routing an upload into the graph would bypass
   the PR-gate.

3. **Backfill proposes documents verbatim, one note each** (IDEA-6). No summarizing, no extraction,
   no chunking. A backfill's job is to make existing documents *reachable*; deciding what they mean
   belongs to the retrieval and synthesis layers. An LLM-summarized backfill would put thousands of
   unreviewed paraphrases into the corpus, which is the fastest way to make a knowledge graph
   untrustworthy. Ids follow content, not filename, so a rename cannot mint a duplicate.

4. **Calibration reports three figures, not one** (IDEA-2). Bias says whether a calculator is
   *correctable*; MAE says how far off it typically is; **uncertainty coverage** says how often the
   truth fell inside the stated error bars — the figure a mean error cannot show, and the one that
   distinguishes "imprecise but honest" from "precise-looking and misleading". `n` accompanies every
   figure because a bias from three points is not a bias. Recording is best-effort throughout: a
   ledger about predictions must never cost a prediction.

5. **Digest watermarks advance after delivery** (IDEA-1). A crash between "found matches" and
   "delivered" must re-report rather than silently skip: a duplicate digest line is a nuisance, a
   missed one defeats the feature entirely.

**A pre-existing test earned its keep.** `test_every_session_scoped_route_is_ownership_gated`
enumerates session-scoped routes rather than hardcoding them, and failed the moment the attachments
route appeared — forcing a conscious update plus a behavioural non-owner sweep over the new route.
That is exactly the design intent of an inventory assertion, and worth copying.

**Consequences.** Phase F11 is complete: every finding in `docs/audit/12-capability-gap-analysis.md`
is either implemented or explicitly closed as a not-gap, with three findings (AGT-1, TOOL-7, AGT-6)
withdrawn after assessment and recorded in `DEFERRED.md` so they are not re-opened blindly. Two
things remain genuinely out of reach here and are unchanged: the live edges needing a real
tenant/broker/cluster, and the audit-trail archive-then-reseal design, which needs an ADR with QA
sign-off rather than a cleanup job.
