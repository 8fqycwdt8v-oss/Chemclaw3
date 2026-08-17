# Verdicts — `ingest/eln/*` + `ingest/sources/*` security, reachability lens

Scope: only findings marked **critical** or **high**. The file has exactly one (`high`); the
remaining four are medium/low and were not examined.

---

## The warehouse ELN retrieve half has no authorization gate, and its binding cannot declare one

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  1. Read `src/chemclaw/ingest/eln/warehouse/retriever.py` in full. `retrieve()` (line 81) →
     `_search` → `_chunks`. No identity is read on any of the three.

     ```
     $ grep -rn "get_current_roles\|get_current_actor\|required_role" src/chemclaw/ingest/eln/
     (no identity read anywhere in ingest/eln)
     ```

  2. Built a fake `Warehouse` driver (`/tmp/wh/fakedrv.py`) primed with one row and drove the real
     `WarehouseVectorRetriever` against it with **no** ambient identity
     (`/tmp/wh/probe_authz.py`):

     ```
     actor: None roles: frozenset()
     SQL: SELECT REACTION_ID, REACTION_SMILES, PROTOCOL_TEXT, PROJECT_CODE,
          VECTOR_COSINE_SIMILARITY(REACTION_VECTOR, ?::VECTOR(FLOAT, 1536)) AS CHEMCLAW_SCORE
          FROM V_REACTION_EMBEDDING ORDER BY CHEMCLAW_SCORE DESC LIMIT ?
     chunks: 1
        eln-snowflake:RX-1 | REACTION_SMILES: CC>>CCO / PROTOCOL_TEXT: confidential project ORION
        step 3 / PROJECT_CODE: ORION
     ```

  3. Repeated it with an **authenticated actor holding an unrelated role**, which is the case that
     actually matters in a real Entra deployment (`/tmp/wh/probe_roles.py`):

     ```
     actor: chemist-b-oid roles: {'chemclaw.chemist'}
     chunks: 1
        eln-snowflake:RX-1 | ... PROTOCOL_TEXT: confidential project ORION step 3 / PROJECT_CODE: ORION
     ```

  4. Tested whether an operator can express the entitlement anywhere in the binding
     (`/tmp/wh/probe_forbid.py`) — all three nesting levels are `extra="forbid"`:

     ```
     vector      REJECTED: invalid warehouse binding: 1 validation error ... | vector.required_roles
     top-level   REJECTED: invalid warehouse binding: 1 validation error ... | required_roles
     connection  REJECTED: invalid warehouse binding: 1 validation error ... | connection.required_roles
     documents binding required_role_set: frozenset({'chemclaw.sharedrive.reader'})
     ```

  5. Traced the path from the outermost entry point and looked for anything that stands in the way:
     - `api/auth.py` 401s a request with no valid token when `entra_required` — so the front door
       does establish an actor. It does **not** help: nothing downstream reads its roles for this
       source.
     - `agent/authz.py` lists `gather_evidence` in `READ_ONLY_TOOLS`; `tool_authz_default` is
       `"allow"` and `DEFAULT_WRITE_TOOL_GATES` does not contain it, so the tool gate is open to
       every authenticated caller by default.
     - `agent/research_tools._text_retrievers` → `ingest/sources/registry.active_retrieve_sources`
       → `retrieval/fanout.sweep_sources`: none of the three reads identity; `_build_retrieve_half`
       passes only the manifest name.
     - `durable/report_workflow.default_retrievers()` returns `active_retrieve_sources()` too, so
       the same corpus reaches the report path.

- **Why**

  The mechanism, the trigger and the consequence all hold, and I could not find anything upstream
  that compensates.

  *Reachability.* The prerequisite is an operator adding a warehouse source to
  `CHEMCLAW_DATA_SOURCES` (default is `graph,eln-json`) and pointing its binding at real tables —
  which is exactly the shipped deployment procedure for `eln-snowflake`, not an exotic
  configuration. After that, every ordinary chat turn that calls `gather_evidence` reaches the
  retriever: the tool is read-classified and ungated by default, the sweep reads no identity, and
  the retriever reads no identity. There is no validator, pydantic model, Helm default or startup
  guard between the HTTP request and the rows.

  *Consequence.* Reproduced verbatim: content columns of rows the service credential can see are
  returned as `EvidenceChunk`s, cited as `eln-snowflake:<key>`, to a caller holding no matching
  role. There is no filtering step between the warehouse rows and what a chemist reads, so this is
  a disclosure the chemist is shown as sourced evidence, not an exception the caller might swallow.
  The confused-deputy framing is correct: the shipped manifest connects as one static role
  (`role: CHEMCLAW_READER`), so the caller's own ELN entitlements are never consulted at any point
  in the chain.

  *The comparison the reporter draws is sound and, if anything, understated.* The system clearly
  treats per-source entitlement as a real concept rather than an idea nobody implemented:
  `ShareDocumentRetriever._entitled()` implements it, `ReportRequest.requested_roles` exists solely
  so a background run can carry the caller's roles to where an entitlement is checked
  (`durable/report_workflow.retrieve_section` stamps them), and `agent/durable_tools.py:113`
  records the fix that made that work. The warehouse retriever is inside all of that machinery and
  ignores it. So this is an inconsistency with a control the codebase already ships, not an
  architectural stance that internal corpora are open — `graph` and `vendored` are ungated because
  they are curated/public corpora, which an ELN is not.

  *One correction to the finding, which does not change the verdict or the severity.* The Trigger
  sentence "No identity is required — a turn with `get_current_actor() is None` is served" is the
  weakest part. On the front door under `entra_required=true` an actorless turn is not reachable
  (`require_principal` 401s), and the CLI stamps an identity for the whole session
  (`cli/chat.py:167`). The actorless cases that do exist in production are narrower than stated:
  `durable/report_workflow.retrieve_section` runs with no stamped identity when
  `SectionRequest.requested_by` is empty, and its docstring's claim that the absent case "fails
  closed" is true for the share and false for the warehouse. The defect that matters in a real
  deployment is not the missing actor but that the actor's **roles are never read** — which probe 3
  demonstrates and which carries the full severity on its own.

  *What the reporter missed, and it makes it worse.* The same retriever is in
  `default_retrievers()`, so warehouse rows also flow into report sections, and
  `propose_report` turns the drafted report into a PR-gated knowledge-graph note. Content a caller
  was never entitled to read can therefore be carried from a one-off answer into the shared,
  permanent note tree, where the only thing standing between it and every user of the deployment is
  a human reviewer who has no signal that the quoted rows were project-restricted.

  I considered and rejected two mitigations as sufficient: (a) an operator can narrow the corpus
  statically via `vector.where` or by pointing `relation` at a restricted view, and (b) an operator
  can close `gather_evidence` wholesale via `tool_role_gates`. Both are all-or-nothing for the whole
  deployment; neither expresses "this caller may see this corpus", which is the control the finding
  says is missing and which probe 4 shows the binding cannot accept.
