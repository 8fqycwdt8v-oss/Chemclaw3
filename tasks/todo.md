# Task: the ACS/CHEM21 green-chemistry guides as an offline `green` connector

Requested 2026-08-02. Branch: `claude/acs-chemistry-guides-integration-8qreny`.

Source: <https://www.acs.org/green-chemistry-sustainability/research-innovation/research-tools.html>
— specifically the solvent selection guide and the reagent guides, wanted **offline** (a committed
corpus, not a web-lookup tool).

**Status: blocked at step 0, no code written.** The investigation below is complete and is the
deliverable of this session; the sourcing it depends on cannot happen from this environment. See
"Step 0" for the one thing that unblocks it.

(The previous occupant of this file, the full-codebase-review implementation, is merged; its
record is PRs #98 and #103.)

## Why this is worth building

The repo already teaches this judgment with nothing behind it. `skills/solvent-selection/SKILL.md:64`
names CHEM21 by name as prose — "Prefer the recommended set … treat dipolar aprotics as flagged" —
and no tool can return an actual row. The live test recorded the predictable result: case `kn-07`
in `tasks/live-test/findings-knowledge.md:341` has the model **fabricating** "2-MeTHF is a CHEM21
recommended solvent", with invented CHEM21 and ICH claims attached.

That is precisely the failure `science/safety/ich.py` exists to end — "recited a PDE from training
as though it were the record" — one guide over. The fix is the same fix: a transcribed, cited
table with an honest miss.

## What the ACS page actually offers

- **Solvent Selection Tool** — 272 solvents × 70 properties (30 experimental, 40 calculated), a PCA
  property map. Built by AstraZeneca, donated to the ACS GCI Pharmaceutical Roundtable, served as a
  Tableau web app. **ACS copyright, not redistributable.**
- **CHEM21 Solvent Selection Guide** — the guide the Roundtable itself *recommends*. Prat, Wells,
  Hayler, Sneddon, McElroy, Abou-Shehada, Dunn, *Green Chem.* 2016, **18**, 288–296,
  DOI 10.1039/C5GC01008J. ~50 classical and less-classical solvents scored 1–10 on Safety / Health /
  Environment from GHS H-statements and physical properties (bp, flash point, autoignition, H4xx),
  ranked *Recommended / Problematic / Hazardous / Highly Hazardous*; 1–3 green, 4–6 yellow, 7–10 red.
  Open access; the ESI carries an Excel scoring calculator for solvents not in the list.
  **Licence appears to be CC BY-NonCommercial — unverified, see S1.**
- **Reagent Guides** — ~28 transformations (amidation, achiral/chiral hydrogenation, biocatalysis,
  BOC deprotection, borylation, halogenation, Buchwald–Hartwig, cyclopropanation, epoxidation, ester
  deprotection, peptide synthesis, ketone/nitro/amide reduction, metals removal, N-alkylation,
  O-dealkylation, alcohol oxidations, pyridine ring synthesis, reductive amination, SNAr,
  Suzuki–Miyaura, sulfide oxidation, thioether formation), each a Venn diagram over
  *greenness × utility × scalability*, at `reagents.acsgcipr.org`. **ACS copyright, portal-gated,
  not redistributable.**

So: the solvent guide can be transcribed (licence permitting); the ACS reagent verdicts cannot, and
must be re-expressed as a curated summary citing the primary literature the guides themselves point
at.

## Step 0 — the blocker

Every source domain is refused at this environment's gateway. Confirmed twice against
`$HTTPS_PROXY/__agentproxy/status`, `connect_rejected … gateway answered 403`, for all of:
`pubs.rsc.org`, `doi.org`, `api.crossref.org`, `api.unpaywall.org`, `www.acs.org`, `acsgcipr.org`,
`learning.acsgcipr.org`, `reagents.acsgcipr.org`. Only `api.github.com`, `pypi.org` and WebSearch
are reachable.

`science/safety/ich.py`'s module docstring forbids the obvious shortcut: a cited table filled from
model memory is the exact defect that file was written to end. **No corpus row gets written until
these hosts are allowed.**

- [ ] **S0** Allow the hosts below on the environment's network policy
      (<https://code.claude.com/docs/en/claude-code-on-the-web>), then re-probe before anything else.

| host | needed for |
| --- | --- |
| `pubs.rsc.org` | the CHEM21 paper and its ESI scoring spreadsheet |
| `doi.org`, `api.crossref.org`, `api.unpaywall.org` | resolving and confirming each cited paper's OA licence |
| `www.acs.org` | the research-tools page and the GCI-PR guide PDFs |
| `acsgcipr.org`, `learning.acsgcipr.org`, `reagents.acsgcipr.org` | the reagent-guide transformation set and its reference lists |

- [ ] **S1** Read the licence statement off the CHEM21 article itself, not off a search result. If it
      is CC BY-**NC**, redistributing the table inside a commercially-used ChemClaw is a decision to
      take explicitly and record in the ADR — not a detail to assume away.

## Sourcing procedure (both corpora)

The shape `ich_q3c.yaml` already sets:

1. Fetch the primary source; record URL, retrieval date and `sha256` in the YAML header.
2. Quote the licence statement in the header.
3. Transcribe only what the source states. A value that cannot be verified is **omitted with a named
   reason**, never inferred — `ich_q3c.yaml:21-24` omits *tert*-butyl alcohol's PDE on exactly this
   ground and says so in the file.
4. Add the machine-checkable identity (T7/T8) so a transcription slip fails the suite rather than
   shipping under a real citation.
5. Delete the fetched artifact. The repo carries the transcription and the provenance, not the PDF.

## Design

A new `green` bundle, sibling of `safety` — not a fourth table inside it. `safety/connector.yaml`
gives the reason such a thing is separately governed: "every one of its tables is a curated,
literature- or guideline-cited artifact … an auditor asking *what did the system say and on whose
authority* should find one deployable unit to point at." The green guides carry their own
attribution obligations and their own sources.

### Engine — `src/chemclaw/science/green/`

Pure computation, no MCP/FastAPI/Temporal (`tests/test_layering.py`).

- [ ] **E1** `solvent_guides.yaml` — the corpus, shipping beside the module and resolved against
      `__file__`, **not** a setting: same reasoning as `ich.py:19-25`, nobody has their own CHEM21.
      Header comment carries provenance, licence, the scoring definitions, an "adding a row" recipe
      and an explicit "deliberately not transcribed" section, as `ich_q3c.yaml:21-36` does.

      ```yaml
      guides:
        chem21:
          citation: "Prat, D. et al. Green Chem. 2016, 18, 288–296. DOI 10.1039/C5GC01008J"
          licence: "<verified licence string>"
          scoring: "Safety, Health, Environment each 1–10; 1–3 green, 4–6 yellow, 7–10 red."
          rankings: {recommended: "...", problematic: "...", hazardous: "...", highly_hazardous: "..."}
      solvents:
        - name: 2-methyltetrahydrofuran
          synonyms: [2-MeTHF]
          chem21: {safety: 5, health: 5, environment: 5, ranking: problematic}
          boiling_point_c: 80.0
      ```

- [ ] **E2** `guide.py` — loader and lookup, structurally a copy of `science/safety/ich.py`: private
      `_Row`/`_Table` models mirroring the YAML; `@lru_cache(maxsize=1) _index()`; `_fold()` name
      normalisation; `_register()` **raising on a synonym collision** at load; unmatched queries
      falling back through `chemclaw.core.reagents.resolve_compound_name`, so `2-MeTHF`,
      `2-methyltetrahydrofuran` and `C1CCOC1` land on one row without copying synonyms.

      A miss is a **model, not `None`** — `SolventGuideLookup(query=…, rating=None)` with a
      `@computed_field verdict`: "this system does not carry a guide entry for that, **not** that the
      solvent is unassessed; read the guide, do not state a ranking from memory." The
      `@computed_field` is non-negotiable — `ich.py:81` records that a bare `@property` is not
      serialised, so the caveat never reached the model. **The caveat goes in the payload.**

- [ ] **E3** `alternatives.py` — `greener_alternatives(solvent)`: rows with a strictly better ranking,
      ordered by closeness on the properties the corpus actually carries (boiling point, solvent
      family), each with the reason it is proposed *and* the reason a swap is still a process
      decision. Computed from committed fields only. This is the offline stand-in for the ACS tool's
      PCA map and says so rather than pretending to be it.

- [ ] **E4** `reagent_guides.yaml` + `reagents.py` — second corpus, same loader shape, separate file
      because it is separately sourced and separately licensed. A **curated cited summary, never a
      copy of the Venn verdicts**: per transformation a preferred set and an avoid set, each entry
      carrying *why* and a citation to the primary literature, plus the ACS guide URL so a reader can
      reach the authority. A claim with no citation does not go in the file.

      ```yaml
      transformations:
        - name: amidation
          synonyms: [amide coupling, amide bond formation]
          acs_guide_url: "https://reagents.acsgcipr.org/reagent-guides/amidation/"
          preferred: [{reagent: "...", why: "...", citation: "..."}]
          avoid:     [{reagent: "...", why: "...", citation: "..."}]
      ```

      Seed with amidation first — `knowledge/optimization-campaign/opt-amide-solvent.md` and
      `knowledge/failure-mode/failure-dcm-amide-coupling.md` are already about it — then alcohol
      oxidation, ketone/nitro reduction, reductive amination, N-alkylation, Suzuki–Miyaura. The rest
      of the ACS set is listed in the header as not-yet-transcribed, so a miss can name them.
      **These rows are judgment and enter through the PR-gate**, unlike the CHEM21 scores, which are
      transcribed fact. The ADR records that difference.

### Bundle — `src/chemclaw/connectors/green/`

- [ ] **B1** `connector.yaml` (`name: green`, HTTP on **port 8817**, the next free loopback),
      `__init__.py`, `server/__init__.py`, `server/app.py` (3 lines, `connector_app(server,
      name="green")`), `server/tools.py` (thin `@server.tool()` wrappers whose substance is the
      docstring).

      Every tool under **`read_only`** — the validator refuses a tool that is not in exactly one of
      `read_only`/`state_changing`, and `safety/connector.yaml` gives the reason it matters: read-only
      tools stay callable under an unapproved plan (D-167), which is exactly when a chemist wants a
      green screen. No name may start with `index_`/`write_`/`delete_`/`remove_`/`update_`/
      `propose_`/`submit_` (`cli/validate_connectors.py::_MUTATING_PREFIXES`).

| tool | returns |
| --- | --- |
| `solvent_guide_rating(solvent)` | the row — S/H/E scores, ranking, the band's own wording, citation — or an explicit miss |
| `rank_solvents(solvents)` | a candidate list worst-ranking-first, plus the ones the corpus does not carry, named |
| `greener_solvent_alternatives(solvent)` | better-ranked solvents of comparable properties, with what the corpus cannot see |
| `reagent_guide(transformation)` | curated preferred/avoid reagents with per-claim citations and the ACS URL; on a miss, the transformations carried *and* those known-but-untranscribed |

Tool docstrings follow `safety/server/tools.py:101-128`: when to call it ("**whenever a green ranking
is about to appear in an answer**"), what inputs resolve, **what a miss means**, and what the tool is
not ("the guides give the ranking a judgement needs; they are not the judgement").

### The skill that consumes it

- [ ] **B2** `skills/solvent-selection/SKILL.md` — add the new tools to the `tools:` frontmatter
      (`make skill-validate` checks each name against the live registry) and rewrite the "Green
      chemistry" bullet at line 64 from recalled prose into a tool call, keeping the judgment (why
      the guides bind before a computed ΔG) and deleting the recalled facts. Step 2 of "How to answer
      a solvent question" — filter by green-chemistry class *before* computing anything — becomes
      executable for the first time.

### Gates that fire mechanically on a new bundle

- [ ] **G1** `deploy/helm/chemclaw/values.yaml` → `connectors.green: {enabled: true, server: true,
      replicas: 1}` (`tests/test_deploy_chart.py:298`). No entrypoint or Containerfile edit — the
      `connector-*` case is generic.
- [ ] **G2** `docs/guides/runbook.md:224` → add `` `green` `` to the "**What ships today.**" paragraph
      (`tests/test_repo_map.py::test_the_runbook_names_the_bundles_that_actually_ship`).
- [ ] **G3** `ARCHITECTURE.md` — the bundle list in the `connectors/` row's prose is hand-maintained.
      `science/green/` needs no README and no row (the map test is scoped to top-level dirs and
      direct children of `src/chemclaw/`).
- [ ] **G4** ADR `docs/decisions/D-2026-08-02-<slug>.md` + its ledger row: the licence position, why
      the ACS tool/reagent-guide *content* is linked rather than copied, why the two corpora are
      governed differently (transcribed fact vs PR-gated curation), and why this is a sibling of
      `safety`.
- [ ] **G5** `docs/planning/DEFERRED.md:73` (D-080, hazard screening beyond structural alerts, blocked
      on a *licensed* source) — do **not** delete the row. This is openly-licensed guide data and does
      not close it; note the boundary in the ADR instead.
- [ ] **G6** `docs/planning/BACKLOG.md` — a row for the ACS transformations left untranscribed, so the
      gap is registered rather than implied by a miss.

### Tests — `tests/test_green_guide.py`

Modelled on `tests/test_safety.py`: import the engine from `science.green` **and** the tool from
`connectors.green.server.tools`, pinning behaviour with named solvents in module-level dicts.

- [ ] **T1** One solvent per ranking band, by name, pinned to its published scores.
- [ ] **T2** `2-MeTHF`, `2-methyltetrahydrofuran` and `C1CCOC1` return the same row — the direct
      regression for live-test case `kn-07`.
- [ ] **T3** A solvent the corpus does not carry returns `rating=None` **and** a `verdict` containing
      the "does not carry" sentence, asserted on the serialised payload, not the object.
- [ ] **T4** A synonym collision in the YAML raises at load.
- [ ] **T5** `rank_solvents` names its misses rather than dropping them.
- [ ] **T6** No rendering anywhere says a solvent is "safe" or "green" unqualified.
- [ ] **T7** Corpus integrity: every `ranking` band is consistent with its S/H/E scores under the
      guide's own stated rule — the analogue of `ich_q3c.yaml`'s ppm = PDE × 100 identity, which its
      header calls "the cheapest check a reviewer can run".
- [ ] **T8** Reagent corpus: every `preferred`/`avoid` entry has a non-empty `why` **and** a citation
      — the structural guard that keeps a curated summary from drifting into recalled opinion.
- [ ] **T9** `reagent_guide` resolves a synonym (`amide coupling` → `amidation`), and its miss names
      both the carried transformations and the known-but-untranscribed ones.

## Verification

- [ ] **V1** `make lint type test` green.
- [ ] **V2** `make connector-validate` — manifest, tool-name partition, no mutating prefix.
- [ ] **V3** `make skill-validate` and `make prose-validate` — the new tool names resolve.
- [ ] **V4** `pytest tests/test_green_guide.py tests/test_repo_map.py tests/test_deploy_chart.py -q`.
- [ ] **V5** Show the structural tests failing first (`tests/README.md`: "a structural test must be
      shown failing — these break by finding nothing rather than by raising"): revert the runbook and
      `values.yaml` edits, watch those two go red, restore.
- [ ] **V6** `make connectors`, then call `solvent_guide_rating` for `2-MeTHF`, `DMF` and a solvent
      deliberately absent from the corpus; and `reagent_guide` for `amide coupling` and for a
      transformation not carried. Check the miss verdict is in the serialised payload both times.
- [ ] **V7** The behavioural diff that is the point of the change: re-run live-test case `kn-07`
      with the bundle enabled and disabled, record both answers here. Done when the disabled run
      reproduces the fabricated CHEM21 claim and the enabled run returns the cited row — or an
      honest miss.

## Review

To be written when the work lands. Nothing has been implemented: the session stopped at S0 with
every source host refused, and filling the corpus from model memory is the one thing this task
exists to prevent.
