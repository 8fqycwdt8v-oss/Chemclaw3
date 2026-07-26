# Implementierungs-Plan: Chemclaw3 (stufenweise, mit Quality-Gates)

## Kontext

Das Repository `Chemclaw3` startet leer. Es existiert ein Architektur-Dokument
([`architektur.md`](./architektur.md), MAF + Temporal + Skills + Markdown-Knowledge-Graph),
aber noch keine Umsetzung. Dieses Dokument ist der **stufenweise Umsetzungsplan**.

Der Plan zerlegt das gesamte Projekt in **viele kleine, einzeln abnehmbare Schritte**.
Erster baubarer Meilenstein ist der **MAF-+-Temporal-Spine**. Nach jedem Schritt-Cluster
steht ein **Quality-Gate ("Checkmate")**, das Code-Qualität, Einfachheit und Robustheit
kritisch hinterfragt, bevor weitergebaut wird.

> **Priorisierung (aktualisiert, Nutzerentscheidung).** Der **HPC-/DFT-Anschluss wird
> aufgeschoben**. Zuerst kommen **schnelle, lokal lauffähige** Rechnungen: semiempirisches
> **xTB (neuestes GFN, GFN2)** und ML-Prädiktoren (**GNN-Löslichkeit**, **pKa/Property**).
> Jede Rechnung wird **persistiert** ("nie zweimal rechnen"), daher wird der Ergebnis-Store
> zu einer eigenen, frühen First-Class-Schicht. **Bayesian Optimization mit der
> `BoFire`-Bibliothek** als BO-Engine wird ebenfalls **vorgezogen**. Der Phase-1-Spine bleibt
> gültig: sein gemockter Langlauf-Job ist das *Durability-Muster*, das alle diese Kalkulatoren
> und BO-Kampagnen wiederverwenden — nur der schwere HPC/DFT-Fall wird später angeschlossen.

**Oberstes Ziel:** Am Ende steht **kein Boilerplate**, sondern *einfacher, robuster,
konfigurierbarer, gut dokumentierter* Code. Jeder Schritt ist so klein, dass sein Diff in
einem Review vollständig durchdacht werden kann.

> Abschnittsverweise (z. B. "Abschnitt 5") beziehen sich auf [`architektur.md`](./architektur.md).

---

## Grundprinzipien (nicht verhandelbar, gelten für jeden Schritt)

1. **Einfach vor clever** — die simpelste Lösung, die das Abnahmekriterium erfüllt. Keine
   Abstraktion ohne zweiten realen Anwendungsfall (Rule of Three).
2. **Konfigurierbar, nicht hartcodiert** — jeder externe Endpunkt, Schwellwert, Pfad,
   Timeout, Modellname kommt aus **einer** typisierten Config-Quelle (`pydantic-settings`,
   ENV-überschreibbar). Keine Magic Numbers im Code.
3. **Robust by default** — explizite Fehlerbehandlung, Timeouts, Retries mit Backoff nur wo
   sinnvoll (Temporal übernimmt das für Activities), keine stillen `except: pass`.
4. **Dokumentiert am Entstehungsort** — jedes Modul beginnt mit einem Docstring "Warum
   gibt es das / welche Architektur-Schicht"; jede öffentliche Funktion hat Signatur-Typen
   + Docstring; jede Design-Entscheidung als ADR in `docs/adr/` (1 Datei pro Entscheidung).
5. **Kein Boilerplate** — keine generierten leeren Klassen, keine ungenutzten Getter/Setter,
   keine "für später"-Platzhalter ohne Verwendung. Was existiert, wird benutzt oder gelöscht.
6. **Test = Verhalten, nicht Struktur** — jeder Schritt liefert genau die Tests, die sein
   Abnahmekriterium beweisen. Keine Tests, die nur Mocks spiegeln.
7. **Vier-Schichten-Trennung strikt** — MAF (Konversation), Temporal (lange Jobs),
   Skills ("wie tue ich X"), Markdown-Graph ("was wissen wir"). Nie vermischen.

---

## Das Quality-Gate ("Checkmate") — Definition

Nach jedem Schritt-Cluster wird dieselbe kritische Checkliste durchlaufen. Ein Cluster gilt
erst als fertig, wenn **jede** Frage mit "ja" (bzw. bewusst dokumentiertem "nein") beantwortet
ist. Kein Weiterbauen bei offenem Gate.

- **G1 — Einfachheit:** Kann irgendeine Datei/Funktion/Abstraktion gelöscht oder zusammengelegt
  werden, ohne Funktion zu verlieren? Abstraktion mit nur einem Aufrufer? → inlinen.
- **G2 — Boilerplate-Check:** Ungenutzter Code, tote Parameter, leere Interfaces,
  "TODO später"-Stubs? → löschen.
- **G3 — Konfigurierbarkeit:** Wert (URL, Pfad, Schwelle, Timeout, Modell) hartcodiert statt
  in der Config? → herausziehen.
- **G4 — Robustheit:** Welche 3 Fehlerfälle sind am wahrscheinlichsten (Netz, leere/kaputte
  Eingabe, Prozessabsturz)? Sind sie behandelt und getestet?
- **G5 — Dokumentation:** Versteht ein neuer Entwickler das Modul allein aus Docstring + ADR?
- **G6 — Schichtreinheit:** Greift eine Schicht direkt auf eine fremde zu (z. B. Skill schreibt
  direkt in Postgres statt über MCP-Tool)? → korrigieren.
- **G7 — Testbeweis:** Beweisen die Tests das Abnahmekriterium, oder nur Mock-Verhalten?

Zusätzlich alle 2 Phasen ein **tiefer Review-Durchgang** (`/code-review` bzw. Reviewer-Agent)
über den gesamten bisherigen Code — nicht nur den letzten Diff.

---

## Phase 0 — Fundament & Entscheidungsdisziplin

- **0.1** Runtime festlegen: **Python** (MAF, Temporal SDK, RDKit sind Python-nativ) → ADR-0001.
- **0.2** Tooling: `uv`/`poetry`, `ruff` (Lint+Format), `mypy --strict`, `pytest`,
  `pre-commit`. Ein einziges `Makefile`/`justfile` mit `lint`, `type`, `test`, `up`.
- **0.3** Zentrale Config-Schicht: `config.py` mit `pydantic-settings`, ENV-Override,
  `.env.example` mit **jedem** Wert dokumentiert. Keine zweite Config-Quelle.
- **0.4** Monorepo-Verzeichnisse als leere Pakete mit README je Ordner: `agents/`,
  `workflows/`, `workers/`, `mcp/`, `skills/`, `knowledge/`, `infra/`, `docs/adr/`.
- **0.5** `infra/docker-compose.yml`: Temporal (self-hosted dev) + Postgres/pgvector.
- **0.6** CI-Skelett (GitHub Actions): lint + type + test bei jedem Push; failing = blockiert.

> **CHECKMATE 0** (G1–G7): Läuft `make lint type test` grün auf einem leeren Skelett? Ist die
> Config die *einzige* Quelle für Umgebungswerte? **Null** ungenutzter Code?
> `docker-compose up` bringt Temporal-UI + Postgres hoch.

---

## Phase 1 — MAF-+-Temporal-Spine  ⭐ erster Meilenstein

Kleinste Schritte, jeweils einzeln testbar. HPC wird **gemockt**, damit der Durability-Pfad
ohne SLURM beweisbar ist. Der Mock-Job ist bewusst *generisch* — er steht für „eine lange,
teure Rechnung" und ist damit zugleich (a) der Platzhalter für den **später** angeschlossenen
HPC/DFT-Pfad und (b) das Muster, das die schnellen Kalkulatoren (Phase 1c) und BO-Kampagnen
(Phase 1d) 1:1 wiederverwenden. **Status: implementiert** (Schritte 1.1–1.6, 1.9); die
QM/DFT-Benennung ist die aufgeschobene Zielanwendung, die Mechanik ist rechenart-neutral.

- **1.1** Temporal-Worker-Prozess (leer) startet, verbindet sich, meldet sich an
  `hpc-jobs`-Queue. Abnahme: sichtbar in der Web-UI.
- **1.2** `QMJobWorkflow`-Gerüst mit **einer** trivialen Activity (`prepare_input`, reine
  Funktion). Abnahme: Workflow läuft durch, deterministisch.
- **1.3** Mock-Activity `submit_to_hpc` (Sleep + Fake-Job-Handle) + `poll_hpc_status` mit
  `activity.heartbeat()`. Abnahme: Heartbeat sichtbar; Timeout-Konfig aus Config-Schicht.
- **1.4** `parse_qm_output` (Fake→strukturiertes Result-Objekt, `pydantic`-Modell).
- **1.5** MAF-Agent mit **einem** Skill geladen + Tool `submit_qm_job` → startet Workflow
  fire-and-forget, gibt sofort `job_id` zurück. Agent antwortet nicht-blockierend.
- **1.6** Tool `get_qm_job_status(job_id)` fragt Temporal-Client ab.
- **1.7** `notify_agent`-Callback (v1: In-Process-/Webhook-Stub) zurück in die Session.
- **1.8** Zweite Task-Queue `background-jobs` + leichter Worker (nur registrieren, noch leer).
- **1.9** Entra-ID-Claim (`oid`/`upn`) als Feld im Workflow-Input mitführen (v1 Platzhalter,
  aber im Datenmodell vorhanden für Audit).
- **1.10** ~~`CachedQMLookup`-Struktur~~ → **verallgemeinert und vorgezogen als Phase 1b**
  (Ergebnis-Store für *alle* Kalkulatoren, nicht nur QM). Der `qm_job_key`
  (Molekül+Methode+Basissatz) ist bereits vorhanden und wird dort zum generischen Cache-Key.

> **CHECKMATE 1** (G1–G7 + Durability-Spike): Worker **mitten im Job neu starten** → Workflow
> setzt fort, keine abgeschlossene Activity wird wiederholt (Event-Replay in UI sichtbar).
> Ist die MAF-`SkillsProvider`-API stabil genug (Spike, Abschnitt-15-Risiko)? Ist das
> DIY-MAF↔Temporal-Muster auf **eine** dünne Adapterfunktion begrenzt (kein Framework-Bau)?
> Sind alle Timeouts/Queues/Hashes konfigurierbar, nichts hartcodiert?

---

## Phase 1b — Ergebnis-Store / Berechnungs-Cache (querschnittlich, first-class)

**Motivation (Nutzervorgabe):** Jede Rechnung wird **persistiert**, damit **nichts zweimal
berechnet** wird. Verallgemeinert den QM-spezifischen Schritt 1.10 zu **einem** wiederverwendbaren
Store, durch den *jeder* Kalkulator (xTB, Löslichkeit, pKa, später DFT) und jede
BO-Zielfunktions-Auswertung läuft. Ein Store, ein Schreibpfad — kein Cache pro Kalkulator (DRY).

- **1b.1** Store-Interface (Protokoll): `get(key) -> StoredResult | None`,
  `put(key, result, provenance)`. Ein Vertrag, austauschbare Backends.
- **1b.2** Kanonischer, **versionierter** Cache-Key:
  `CalculationKey(calc_type, calc_version, input_hash, params_hash)`. Die **Kalkulator-Version
  gehört in den Key**, damit ein Modell-/Methoden-Update den Cache nicht *still* vergiftet.
- **1b.3** Zwei Backends hinter demselben Interface: **In-Memory** (Tests, beweist die
  „compute-once"-Logik ohne DB) + **Postgres** (eine Tabelle `calculation_results`: Key,
  JSONB-Ergebnis, Provenienz, `calc_version`, `created_at`). Schema-Migration als einfaches
  SQL-Skript in `infra/`.
- **1b.4** **Eine** Lookup-vor-Compute-Stelle: ein `cached(calculator)`-Wrapper, den jeder
  Kalkulator erbt (DRY, keine Kopie). Cache-Hit/Miss wird als Zähler exponiert (Metrik-Schicht 2b).
- **1b.5** Temporal-Einbettung: Store-Lookup als Activity **vor** dem teuren Compute,
  Persistenz als Activity **danach** — dasselbe Muster, das der Spine (Phase 1) andeutet.

> **CHECKMATE 1b** (G1–G7): Beweist ein Test, dass ein identischer Zweit-Call den Store trifft
> und die Compute-Funktion **nicht erneut** aufruft? Invalidiert ein `calc_version`-Bump den
> Key korrekt (alter Cache wird nicht fälschlich getroffen)? Ist der Store **ein** Interface mit
> wirklich austauschbarem Backend (In-Memory ↔ Postgres, G1/G6)? Keine Cache-Logik dupliziert?

---

## Phase 1c — Schnelle Prädiktoren & Semiempirik (die ersten *echten* Rechnungen)

HPC/DFT bleibt **aufgeschoben**. Fokus: **schnelle, lokal lauffähige** Rechnungen über denselben
Durability-Pfad (Phase 1) und denselben Store (Phase 1b). Kalkulatoren sind **pluggable** —
neuer Kalkulator = neuer Registry-Eintrag, kein Kern-Umbau (Generalisierung erst ab dem 2./3.
Kalkulator, Rule of Three; xTB + Löslichkeit + pKa erfüllen das).

- **1c.1** Kalkulator-Vertrag (Protokoll): `run(input) -> Result`, plus `name`, `version`.
  **Registry** statt hartcodierter `if calc == …`-Ketten.
- **1c.2** **xTB / GFN2** als MCP-Kalkulator: SMILES/3D → xTB (neuestes semiempirisches
  **GFN2-xTB**, GFN-FF optional) → Energie / Geometrie / Deskriptoren. Deterministisch, CPU,
  kein HPC. `xtb`-Binary hinter dünnem MCP-Server; Methode + Ladung/Multiplizität aus Config.
- **1c.3** **GNN-Löslichkeitsmodell** als MCP-Kalkulator: SMILES → Löslichkeit **+ Unsicherheit**.
  Nur Inferenz im Kern (kein Training); Modellgewichte/-version aus Config, im Cache-Key (1b.2).
- **1c.4** **pKa-/Property-Modell(e)** analog (das vom Nutzer genannte „pKs"-Modell; hier als
  **pKa** interpretiert — offene Frage im Backlog). Über dieselbe Registry, gleiches Muster.
- **1c.5** Generischer `CalculationWorkflow(calc_name, input)` (verallgemeinert `QMJobWorkflow`):
  Store-Lookup (1b) → Compute-Activity → Persist. Tools `submit_calculation` /
  `get_calculation_status` (verallgemeinern 1.5/1.6). Fire-and-forget, nicht-blockierend.
- **1c.6** Skill `calculation-selection`: das **Urteil**, welcher Kalkulator zu welcher Frage
  (Löslichkeit? pKa? Konformer-Energie?) und welche Genauigkeitsstufe — Wahl/Schwellen aus Config.
- **1c.7** Ergebnis optional als Graph-Note über das **PR-Gate** (nach Phase 2); ansonsten reicht
  der Store als Persistenz. Kein zweiter Schreibpfad.

> **CHECKMATE 1c** (G1–G7 + Fach-Plausibilität): Liefert xTB(GFN2) für ein Testmolekül eine
> plausible Energie, und wird sie gecached (kein Zweitlauf)? Ist die Registry frei von
> hartcodierten Kalkulator-Verzweigungen (G1)? Trennt der Code **MCP-Fähigkeit** (Rechnen)
> sauber vom **Skill** (Auswahl-Urteil, G6)? Sind Modellversionen/Methoden konfigurierbar **und
> Teil des Cache-Keys** (G3/1b.2)? Weist jede Vorhersage ihre **Unsicherheit** aus (fachlich
> validierbar)?

---

## Phase 1d — Bayesian Optimization (BoFire, vorgezogen)

**Vorgezogen (Nutzerentscheidung):** Optimierungs-Kampagnen (Reaktion/Prozess/Formulierung)
nutzen die schnellen Prädiktoren (1c) und den Store (1b) als Zielfunktions-Auswertungen.
**`BoFire`** ist die BO-Engine (Domain-Modellierung + BoTorch-Strategien) — wir bauen **keine**
eigene BO, sondern kapseln BoFire hinter einem dünnen Adapter.

- **1d.1** Domain-Adapter: Chemclaw-Config → BoFire-`Domain` (Inputs/Features, Constraints,
  Objectives). BoFire-Typen bleiben **gekapselt**, lecken nicht in Agent/Skill.
- **1d.2** Ask/Tell: `propose_candidates(domain, past_results, n) -> Candidates` über eine
  BoFire-Strategie (SOBO/MOBO). Strategie + Acquisition-Function aus Config.
- **1d.3** Zielfunktions-Auswertung = Kalkulatoren aus 1c über den Store (1b), **gemessene oder
  vorhergesagte** Werte; die Provenienz (gemessen vs. prädiziert) wird mitgeführt.
- **1d.4** BO-Kampagne als **Temporal-Workflow** (langlaufend, resumierbar): eine Runde =
  `propose → evaluate` (fan-out Rechnungen) `→ tell → persist`. Überlebt Worker-Neustarts.
- **1d.5** Kandidaten/Experimentvorschläge, die ins Wissen eingehen, über das **PR-Gate**
  (nach Phase 2); reine Zwischenschritte bleiben nur im Store.
- **1d.6** Metrik (Phase 2b): Optimierungs-Fortschritt (bestes Objective über Runden / Regret)
  als registrierte wissenschaftliche Metrik.

> **CHECKMATE 1d** (G1–G7 + Konvergenz-Spike): Schlägt eine BO-Runde für eine **bekannte
> Test-Zielfunktion** sinnvolle Kandidaten vor und nähert sich über Runden dem bekannten Optimum?
> Bleibt BoFire **hinter dem Adapter** (kein BoFire-Typ im Agenten, G6)? Nutzt die Auswertung den
> Store (keine Doppelrechnung)? Läuft eine mehrrundige Kampagne **durabel** (Neustart mittendrin →
> Fortsetzung)?

---

## Phase 2 — Knowledge-Graph-Kern + PR-Gate (Wiederverwendungs-Baustein)

- **2.1** Notenschema als `pydantic`-Frontmatter-Modell: `id, type, compound_smiles, tags,
  links[[…]], created_by, source, confidence, valid_from/valid_to`. Ein Modell, eine Quelle.
- **2.2** Note-Parser: `python-frontmatter` → validiertes Modell. Abnahme: kaputte Frontmatter
  → klare Fehlermeldung, kein Crash (G4).
- **2.3** Link-Extraktor (Wikilinks `[[…]]`) + NetworkX-Graphaufbau aus einem Verzeichnis.
- **2.4** Broken-Link-Check + Schema-Validierung als CLI (nutzbar in CI).
- **2.5** Skill `knowledge-graph-query`: Graph-Traversal (1–2 Hops, Back+Forward-Links),
  **nicht** Top-k-Vektor. Optional Embedding nur als Einstiegspunkt (hinter Config-Flag).
- **2.6** Skill `knowledge-graph-write`: Note-Template + Git-Branch→PR-Workflow.
- **2.7** **PR-Gate** einmal sauber bauen: `created_by: agent` → Feature-Branch + PR; Merge =
  menschliche Freigabe. CI-Job aus 2.4 läuft auf jedem PR.
- **2.8** Temporal-Activity `write_knowledge_node` schreibt Job-Ergebnis (Phase 1) als Note
  über **dasselbe** PR-Gate. Kein zweiter Schreibpfad.

> **CHECKMATE 2** (G1–G7): Erzeugt ein QM-Job automatisch einen PR mit valider Note? Findet
> `knowledge-graph-query` sie nach Merge und expandiert korrekt 1–2 Hops? Ist der PR-Gate-Code
> **eine** wiederverwendbare Funktion (nicht dupliziert zwischen Skill und Activity, G6)?
> **+ tiefer Review-Durchgang über Phase 1+2 gesamt.**

---

## Phase 2b — Evaluations- & Metrik-Schicht (querschnittlich)

Ziel: Nicht nur **Code**-Qualität (Checkmates) gaten, sondern auch **wissenschaftliche
Output**-Qualität messbar machen. Begründung aus `docs/research-review.md`: (a) Tool-/Skill-
Augmentierung verbessert die Leistung *nicht durchgängig* und ist aufgabenabhängig (F8/F9) —
das lässt sich nur mit einer Metrik-Schicht selektiv statt universell steuern; (b) reproduzierbare
Bewertung braucht konkrete Benchmarks + Green-Chemistry-Metriken (F7). Startet **minimal** und
wächst mit jeder späteren Fähigkeit (jede Capability-Phase registriert ihre eigene Metrik).

- **2b.1** Metrik-Interface (reine Funktion): `score(output, reference) -> MetricResult`
  (Wert + Provenienz + optional Unsicherheit). Schwellen aus der Config, nicht im Code.
- **2b.2** Eval-Harness: läuft ein Metrik-Set über ein versioniertes **Fall-Set** (Eval-Cases
  als Notes im Graphen, Phase 2) und erzeugt einen bewerteten, zitierfähigen Report.
- **2b.3** Seed-Metriken (bewusst wenige): Green-Chemistry **E-Factor** & **Process Mass
  Intensity (PMI)**; Vorhersagegenauigkeit gegen zurückgehaltene ORD-Daten; (später,
  kampagnenweit) Schrittzahl / Time-to-in-vitro.
- **2b.4** **Per-Task-Tool-Nutzen** (adressiert F8/F9 direkt): A/B-Vergleich
  Skill-/Tool-augmentiert vs. Basis-LLM auf einem Aufgaben-Set; festhalten, **wo** Tools real
  helfen und wo sie eine eigene Fehlerklasse einführen. Ergebnis steuert selektiven Tool-Einsatz.
- **2b.5** Verdrahtung mit den Gates: jede spätere Fähigkeits-Phase (3–5b) **muss ≥1
  wissenschaftliche Metrik registrieren**; Regressionen werden wie Test-Fehler behandelt. Für
  die Memory-Ebene (Phase 5) gehören Langzeit-/Temporal-Benchmarks (DMR, LongMemEval) hierher.

Bewusst **noch nicht** first-class (bleibt im Backlog, Nutzerentscheidung): die
chemische/biologische **Safety-/Compliance-Schicht** (Gefahren, GxP, Datenintegrität) — sie ist
eigenständig und nicht Teil dieser Output-Qualitätsmetriken.

> **CHECKMATE 2b** (G1–G7): Läuft der Eval-Harness reproduzierbar über das Fall-Set und
> produziert zitierfähige Scores? Ist eine Metrik eine **reine, konfigurierbare** Funktion
> (Schwellen in der Config, G3)? Zeigt der Per-Task-Tool-Vergleich mindestens einen Fall, in
> dem Tooling **nicht** hilft (Beweis, dass die Schicht selektiv steuern kann, nicht nur
> bestätigt)? Registriert jede folgende Phase ihre Metrik?

---

## Phase 3 — Fingerprint-Suche (chemische Ähnlichkeit)

- **3.1** MCP-Server `mcp-molfp` (~100 LOC): SMILES → ECFP4 (RDKit, radius 2, 2048 bit).
  Deterministisch, keine GPU. Radius/Bits aus Config.
- **3.2** Postgres-Tabelle `bit(2048)` + HNSW-Index (`bit_hamming_ops`, pgvector ≥0.7).
- **3.3** Tools `find_similar_molecules(smiles, top_k)` (Tanimoto in SQL) +
  `find_substructure_matches`.
- **3.4** MCP-Server `mcp-rxnfp` (DRFP) analog + `find_similar_reactions`.
- **3.5** Skill `reaction-search`: das **Urteil** (Tanimoto-Schwelle als Präzedens-Kriterium,
  Ähnlichkeit vs. Substruktur, Metadatenfilter) — Schwellen aus Config, nicht im Code.

> **CHECKMATE 3** (G1–G7): Liefert eine SMILES-Query korrekt Tanimoto-sortierte Nachbarn?
> Trennt der Code sauber MCP (Fähigkeit) von Skill (Urteil)? Sind beide MCP-Server je unter
> ~100 LOC, ohne Boilerplate? Ist der Ähnlichkeits-Schwellwert konfigurierbar (G3)?

---

## Phase 4 — ELN-Ingestion (Adapter-Muster)

- **4.1** Stabiler ORD-basierter Zielschema-Kern (`pydantic`); oberhalb kennt niemand
  ELN-Eigenheiten.
- **4.2** Adapter-Vertrag (Protokoll/ABC): `fetch_new_entries(since) -> RawEntry[]`,
  `map_to_ord(raw) -> OrdReaction`. Nur der Vertrag ist fix, nie die Form.
- **4.3** **Ein** konkreter Adapter (eine reale ELN-Quelle). Keine universelle Abstraktion.
- **4.4** Skill `eln-reaction-extraction`: deterministisches Feld-Mapping + LLM-Fallback
  **pro Feld** für Freitext. `scripts/validate_ord.py` (RDKit + Massenbilanz).
- **4.5** Periodischer ELN-Sync auf `background-jobs`-Queue → neue Notes via PR-Gate (Phase 2).

> **CHECKMATE 4** (G1–G7): Landet ein ELN-Eintrag (strukturiert **und** Freitext) als
> validierte ORD-Note im Graphen und ist per Fingerprint + Graph-Query auffindbar? Ist die
> ELN-Spezifik **ausschließlich** im Adapter gekapselt (G6)? **+ tiefer Review über Phase 3+4.**

---

## Phase 5 — Gedächtnis-Ebenen (episodisch + semantisch, keine neue Infra)

- **5.1** `campaign`-Note-Typ (episodisch) + Frontmatter-Evidenzfelder.
- **5.2** Automatische Kettenerkennung über Fingerprints (Produkt A = Edukt B) — nutzt Phase 3.
- **5.3** Skill `campaign-narrative-synthesis` + periodischer `background-jobs`-Job →
  zitierfähige Erzählung, jede Aussage referenziert Quell-Notes → PR-Gate.
- **5.4** `playbook`-Note-Typ (semantisch, projektübergreifend) + Skill `playbook-distillation`
  + Job `cross-project-distillation`; Belegverweise verpflichtend; Freigabe durch Prozesschemiker.
- **5.5** Nutzerinteraktion als vierte Quelle: bestätigte/korrigierte Antworten → episodische
  Note (gleicher Typ, gleiches Gate).
- **5.6** Retrieval kombiniert beide Ebenen, hält sie **sichtbar getrennt** (belegt vs. Analogie).

> **CHECKMATE 5** (G1–G7): Entsteht aus verketteten Experimenten eine `campaign`-Note und über
> ≥2 Projekte eine `playbook`-Note mit Rückverweisen? Wurde **keine** neue Infrastruktur
> eingeführt (nur neue Note-Typen + Skills + Jobs, G1)?

---

## Phase 5b — Deep-Research- & Report-Harness (Wissens-/Daten-Synthese)

Ziel: Eine **on-demand, nutzerinvozierbare** Synthese-Engine über die **eigenen**
akkumulierten Daten (Jahre an ELN-Läufen, Analytik, QM-Jobs, `campaign`- und
`playbook`-Notes) — z. B. um einen **Entwicklungsbericht** zu entwerfen, in dem jede
Aussage auf ihre Quell-Note zurückführbar ist.

**Kernidee:** Dasselbe Deep-Research-Muster (*zerlegen → über Quellen ausfächern →
jede Aussage gegen die Quell-Note adversarial verifizieren → zitieren → synthetisieren*),
aber auf **interne Datenquellen** gerichtet statt auf das Web. Es ist die generelle,
nutzerinvozierbare Verallgemeinerung der bereits geplanten Synthese-Jobs
`campaign-narrative-synthesis` und `playbook-distillation` (Phase 5).

**Ein Harness-Kern, austauschbare Quellen** (dasselbe Prinzip wie das ELN-Adapter-Muster,
Abschnitt 12.4): ein *stabiler* Harness-Kern mit *pluggable* Source-Retrievern.

- **5b.1** Harness-Kern als reine Orchestrierungs-Funktion: `decompose → fan-out →
  verify → cite → synthesize`. Kennt nur das **Retriever-Interface**, keine konkrete Quelle.
- **5b.2** Source-Retriever-Interface (Protokoll/ABC): `retrieve(query, filters) ->
  EvidenceChunk[]`, jeder `EvidenceChunk` trägt **Pflicht-Rückverweis** auf seine Quell-Note.
  Nur der Vertrag ist fix, nie die Form.
- **5b.3** Konkrete Retriever als dünne Adapter über **bereits vorhandene** Bausteine:
  Knowledge-Graph-Traversal (Phase 2), Fingerprint-/Substruktursuche (Phase 3),
  ORD-/Analytik-Daten (Phase 4), TabPFN-Tabellen-Prognose (Abschnitt 12.5, optional).
  Kein neuer Datenspeicher.
- **5b.4** Adversarial-Verify-Schritt: jede synthetisierte Aussage muss durch ≥1
  `EvidenceChunk` belegt sein; **unbelegte numerische Trends werden verworfen** (keine
  erfundene Statistik in einem Entwicklungsbericht).
- **5b.5** MAF-**Graph-Workflow** `development-report`: ein Knoten pro Berichtsabschnitt,
  jeder deklariert explizit seine Gedächtnisebene (episodisch/semantisch, Abschnitt 9) —
  die Provenienz-Trennung wird damit *strukturell* erzwungen, nicht nur per Konvention.
- **5b.6** Lange Läufe (Bericht über Jahre an Daten = hunderte Retrievals/LLM-Calls) laufen
  als **Temporal-`background-jobs`-Workflow** → resumierbar, überlebt Worker-Neustarts
  (gleiches Fire-and-Forget-/Durability-Muster wie der QM-Spine, Phase 1).
- **5b.7** Der Berichtsentwurf ist ein Artefakt und durchläuft das **PR-Gate** (Phase 2):
  ein Prozesschemiker validiert, bevor er als verlässlich gilt (GxP: "AI schlägt vor,
  Mensch zeichnet ab").

Bewusst **noch nicht**: externe Literatur/Patente. Diese werden später zu *genau einem
weiteren Retriever* hinter demselben Interface (5b.2) — kein Umbau des Harness-Kerns.

> **CHECKMATE 5b** (G1–G7 + Zitat-Treue): Erzeugt eine Berichtsanfrage einen sektionierten
> Entwurf, in dem **jede** Aussage auf eine Quell-Note verlinkt? Werden unbelegte Trends
> nachweislich verworfen (Test mit einer Anfrage ohne Datengrundlage → leerer/als unbelegt
> markierter Abschnitt statt Halluzination)? Ist der Harness-Kern **quellen-agnostisch**
> (Retriever austauschbar ohne Kernänderung, G1/G6)? Läuft ein langer Report durabel
> (Worker-Neustart mittendrin → Fortsetzung)? Ist der Entwurf PR-gegated, nicht direkt
> im Hauptzweig?

---

## Phase 6 — Identity, RBAC & Härtung

- **6.1** MCP-Auth: FastMCP `AzureProvider`/`BearerAuthProvider` validiert Entra-JWTs;
  OAuth-Proxy-Pattern (Azure ≠ DCR); OBO-Flow zum ELN. Confused-Deputy aktiv adressieren.
- **6.2** Rollenbewusste Skill-Sichtbarkeit: Context-Provider filtert advertised Skills nach
  Entra-App-Rollen/Gruppen.
- **6.3** Temporal: mTLS für Service-Auth; `oid`/`upn` als Audit-Claim; Namespace pro Team;
  HPC-Quotas/QOS.
- **6.4** Knowledge-Graph-ACL: Start breiter interner Lesezugriff (Repo-Ebene). RLS-Mirror nur
  bei echter Vertraulichkeit (siehe Deferred-Tabelle).
- **6.5** HPC-Bridging-Service: einziger Punkt Entra-ID ↔ HPC-Service-Account, protokolliert.

> **CHECKMATE 6** (G1–G7): Sieht ein Nutzer ohne Rolle X den Skill/Tool X nicht und kann
> `submit_qm_job` nicht auslösen? Zeigt der Audit-Trail den `oid` des Auslösers?
> **+ vollständiger Security-Review über das Gesamtsystem.**

---

## Optionale spätere Bausteine (nach Bedarf, nicht v1)

Tabular Foundation Model (`predict_from_tabular_context`, Lizenz prüfen) ·
Skill-Katalog (PDF-Extraktion, Bild→SMILES, IUPAC↔SMILES, Visualisierung) · **HPC/DFT-Anschluss**
(aufgeschoben, s. Deferred-Tabelle). Jede Vorhersage-Fähigkeit vor Produktivsetzung fachlich
validieren, gleiches Human-Review-Gate. (xTB/GFN2 und BO sind **nicht** mehr hier — vorgezogen
in Phase 1c/1d.)

---

## Bewusst aufgeschobene Entscheidungen (explizit, mit Trigger)

| Entscheidung | Default v1 | Trigger für Wechsel |
|---|---|---|
| **HPC/DFT real** (`submit_to_hpc`, SLURM) | **aufgeschoben**; Mock-Spine beweist das Muster, Phase 1c/1d liefern die frühen Rechnungen | wenn schwere QM/DFT-Genauigkeit gebraucht wird **und** HPC-Zugang bereitsteht |
| Postgres-RLS-Mirror des Graphen | **weglassen** | echte kombinatorische Projekt-Vertraulichkeit |
| `knowledge/` eigenes Git-Repo | **Unterordner** | Governance-/Vertraulichkeitstrennung nötig |
| Zweites Queue-System (pg-boss) | **nein**, nur Temporal-Task-Queues | — (revidiert) |
| MAF Durable Extension | **nicht** für Jobs | nur sehr lange Konversationspausen |
| Universelle ELN-Abstraktion | **nein**, Adapter pro Quelle | ab dritter ELN-Quelle |

> **Vorgezogen** (nicht mehr „später"): **xTB/GFN2** und **ML-Prädiktoren** → Phase 1c;
> **DoE/Bayesian Optimization (BoFire)** → Phase 1d; **Ergebnis-Store** → Phase 1b.

---

## Gesamt-Verifikation (End-to-End-Story, wächst mit jeder Phase)

Leitfaden-Testfall: *"Erwartete Regioselektivität für späte C–H-Funktionalisierung von
Verbindung X — und hatten wir ähnliche Substrate?"*

- **P1:** Agent löst (gemockten) Langlauf-Job asynchron aus und schließt ihn durabel ab.
- **P1b:** Ein bereits berechnetes Ergebnis wird aus dem Store **wiederverwendet** statt neu
  gerechnet (identischer Input → Cache-Hit, kein Zweitlauf).
- **P1c:** xTB(GFN2) bzw. das Löslichkeits-/pKa-Modell liefert für Verbindung X ein **echtes,
  gecachetes** Ergebnis (mit ausgewiesener Unsicherheit bei ML).
- **P1d:** Eine BO-Runde schlägt den nächsten Kandidaten vor, ausgewertet über 1c/1b — durabel.
- **P2:** Ergebnis wird zur zitierfähigen Graph-Note (PR-Gate).
- **P3:** "ähnliche Substrate?" liefert echte Fingerprint-Treffer.
- **P4:** Treffer stammen aus echten ELN-importierten Reaktionen.
- **P2b:** Der Eval-Harness bewertet einen Output reproduzierbar (z. B. E-Factor/PMI) und zeigt
  mindestens einen Task, in dem Tooling nicht hilft — selektiver Tool-Einsatz ist steuerbar.
- **P5:** Antwort trennt projektspezifische Historie von übertragenem Playbook-Wissen.
- **P5b:** Ein Entwicklungsbericht wird durabel aus Jahren interner Daten entworfen — jede
  Aussage zitiert ihre Quell-Note, unbelegte Trends verworfen, Entwurf PR-gegated.
- **P6:** Nur berechtigte Nutzer lösen die teuren Rechen-Pfade (Kalkulatoren/BO, später HPC/DFT)
  aus; Audit-Trail (`oid`) vollständig.

Jede Phase gilt erst als abgeschlossen, wenn (a) ihr Abnahmekriterium demonstriert, (b) ihr
CHECKMATE grün und (c) `make lint type test` grün ist. **Definition of Done pro Schritt:**
Diff klein genug für vollständigen Review · Tests beweisen Verhalten · null Boilerplate ·
alle Werte konfigurierbar · Modul-Docstring + ggf. ADR vorhanden.
