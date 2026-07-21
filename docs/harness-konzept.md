# Konzept: MAF Agent Harness als Planungs-/Ausführungs-Rückgrat

> Status: **Entwurf / zur Diskussion.** Dieses Dokument schlägt einen dritten MAF-Baustein
> (den *Agent Harness*) als Rückgrat für autonome, dynamische Workflows vor. Es ist eine
> Ergänzung zu [`architektur.md`](./architektur.md) §1, **keine** Revision der Vier-Schichten-
> Trennung. Abschnittsverweise ohne Doku-Namen beziehen sich auf `architektur.md`.

---

## 0. Kernidee in einem Satz

Der MAF-Agent bekommt eine **eigene, selbst-generierte Aufgabenliste** (`TodoProvider`) plus
einen expliziten **Plan-/Execute-Modus** (`AgentModeProvider`): er zerlegt eine komplexe
Chemiker-Anfrage zuerst in nachvollziehbare Teilschritte, lässt den Plan (bei teuren Läufen)
vom Menschen freigeben und arbeitet ihn dann eigenständig ab — **ohne** dass wir dafür einen
zweiten Orchestrator oder ein zweites Durability-System bauen. Die schwere, lange Ausführung
bleibt exakt wie bisher bei Temporal (D-002); der Harness plant und sequenziert nur die
*kurzen Reasoning-Schritte*, die MAF ohnehin verantwortet.

## 1. Motivation & Abgrenzung

**Heute (§1):** Die Reasoning-Schicht nutzt zwei MAF-Bausteine — `Agent` (LLM-Einheit mit
Tools/Skills) und **Graph-based Workflows** (ein vom Entwickler *vorab* verdrahteter Graph aus
Executors mit typisiertem Routing). Der Graph ist statisch: „dynamisch" heißt dort nur
*bedingte Kantenwahl* zur Laufzeit, nicht *der Agent erfindet seine eigenen Schritte*. Für
feste Abläufe (z. B. der `development-report`-Graph, Plan 5b.5) ist das genau richtig.

**Die Lücke:** Für *offene, mehrstufige* Anfragen — „Kläre die Regioselektivität von X, prüf,
ob wir Ähnliches hatten, und rechne nur nach, wo nötig" — gibt es heute keinen Mechanismus, in
dem der Agent selbst einen **überprüfbaren Plan** aufstellt, ihn dem Chemiker zeigt und ihn
dann Schritt für Schritt (mit Zwischenstand) abarbeitet. Der Agent kann Tools aufrufen, aber
seine Mehrschritt-Absicht ist nur implizit im Chatverlauf, nicht als sichtbare, zustandsbehaftete
Liste. Genau das liefert der Harness.

**Was wir wollen (Ziele):**
1. **Sichtbare Planung** — der Chemiker sieht *vorab*, welche (ggf. teuren) Schritte der Agent
   vorhat, und kann korrigieren, bevor Rechenzeit verbraucht wird.
2. **Dynamische Zerlegung** — der Agent bestimmt Schrittzahl und Reihenfolge selbst
   (agenten-geplant), statt dass wir jeden Ablauf als Graph vorverdrahten.
3. **Autonome Abarbeitung mit Zwischenstand** — mehrstufige Untersuchungen laufen ohne
   ständiges Nachfragen durch, melden aber Fortschritt und halten am PR-Gate an.

**Was wir ausdrücklich NICHT wollen (Nicht-Ziele):**
- **Kein zweites Durability-System.** Der Harness ist *keine* Ausführungs-Engine für lange
  Jobs. Die „4 D's" der MAF Durable Extension bleiben bewusst ungenutzt (§1); Temporal bleibt
  der *einzige* Ort für durable, langlaufende Arbeit (D-002, D-006).
- **Kein Ersatz der Graph-Workflows.** Feste, wiederkehrende Abläufe bleiben Graph-Workflows.
  Der Harness ist für das *offene* Terrain, nicht für alles.
- **Keine Aufweichung des PR-Gates.** Mehr Autonomie heißt *mehr*, nicht weniger menschliche
  Freigabe (siehe §6).

## 2. Was der MAF-Harness liefert (die Bausteine)

Der Harness ist ein von Microsoft angekündigter (Build 2026), noch **`[Experimental]`**
markierter Zusatz im `microsoft/agent-framework`. Er bündelt mehrere `ContextProvider`/
Middleware-Teile — für uns relevant sind zwei:

| Baustein | Was er tut | Analogie |
|---|---|---|
| **`TodoProvider`** | Stellt dem LLM Tools bereit, um Todo-Items **anzulegen, abzuhaken, zu entfernen und abzufragen**. Der Zustand (`TodoState`/`TodoItem`) liegt im Session-State (`TodoSessionStore`), getrennt vom Chatverlauf. | Wie die Todo-/Task-Liste, die ein Coding-Agent für sich führt. |
| **`AgentModeProvider`** | Implementiert ein explizites **Zwei-Phasen-Muster**: **Plan-Modus** (interaktiv: Fragen stellen, Todos anlegen, Freigabe holen) → **Execute-Modus** (autonom: Todos abarbeiten). | ReAct/Plan-and-Solve, aber als First-Class-Middleware. |

Ergänzend liefert der Harness eine **Completion-Loop** (`TodoCompletionLoopEvaluator` in .NET
bzw. ein `todos_remaining()`-Helfer in Python), die den Agenten so lange erneut aufruft, wie
noch offene Todos existieren, sowie Tool-Approval- und Kontext-Kompaktierungs-Middleware. Der
Einstiegspunkt ist ein `HarnessAgent` / `create_harness_agent(...)`.

> **Quellen (bei Implementierung gegenprüfen — API ist experimentell):**
> `agent_framework._harness._todo.TodoProvider` (Python-Quelle), `python/samples/02-agents/harness/`
> (Plan/Execute-README), Build-2026-Devblog. Reifegrad-Vorbehalt siehe §9.

## 3. Einordnung in die Vier-Schichten-Architektur

Der Harness ist eine **reine Reasoning-Schicht-Erweiterung** — er lebt vollständig innerhalb
der MAF-Schicht und respektiert D-002:

```
┌─ Reasoning-Schicht (MAF) ─────────────────────────────────────────────┐
│                                                                        │
│   HarnessAgent = Agent  +  TodoProvider  +  AgentModeProvider          │
│        │            │            │                │                     │
│        │            │            │                └─ Plan → (Freigabe) → Execute
│        │            │            └─ selbst-generierte Todo-Liste (Session-State)
│        │            └─ dieselben Tools + Skills wie heute (agents/*.py, skills/)
│        │                                                                │
│        └─ ruft pro Todo-Schritt vorhandene Tools auf:                   │
│             • inline (xTB, Löslichkeit, pKa, Graph-Query) — synchron    │
│             • fire-and-forget (submit_qm_job / submit_calculation) ─────┼──► Temporal
│                                                                        │        (Durability,
└────────────────────────────────────────────────────────────────────────┘         D-002/§2)
```

**Schichtreinheit (G6):** Der Harness-Zustand (Todo-Liste, Plan/Execute-Modus) ist
Konversationszustand und bleibt **leichtgewichtig** in der MAF-Session (§1: „Session-State in
Redis/Postgres reicht meist"). Er sickert **nicht** in Temporal-Workflows, Skills oder den
Wissensgraphen. Umgekehrt bleibt jeder teure/lange Schritt ein normaler Fire-and-Forget-Aufruf
an Temporal — der Harness ändert daran nichts, er *sequenziert* nur, wann der Aufruf passiert.

**Blast-Radius im Code:** minimal. Betroffen ist im Kern `agents/chemclaw_agent.py`
(`build_agent` konstruiert künftig optional einen `HarnessAgent` statt eines nackten `Agent`);
die Tools (`agents/*.py`) und Skills bleiben unverändert, weil der Harness dieselbe Tool-/Skill-
Registrierung nutzt.

## 4. Das zentrale Spannungsfeld: Execute-Loop vs. Fire-and-Forget

Das ist der Teil, der bewusst entworfen werden muss — die Stelle, an der der Harness und die
bestehende Async-Job-Mechanik aufeinandertreffen.

**Problem:** Die Harness-Completion-Loop will „arbeite Todos ab, bis keine mehr offen sind".
Unsere teuren Schritte sind aber **nicht-blockierend** (D-002): `submit_qm_job` gibt sofort eine
`job_id` zurück, das Ergebnis kommt Stunden später via `notify_agent`-Callback (Plan 1.7). Ein
naives „Loop bis fertig" würde entweder (a) blockieren/busy-warten (verbietet die Architektur)
oder (b) das Todo fälschlich als erledigt abhaken, obwohl der Job noch läuft.

**Lösung — drei Todo-Zustände, entkoppelt über den vorhandenen Callback:**
1. Ein Todo, dessen Schritt einen Temporal-Job auslöst, wird nicht „completed", sondern
   **`awaiting`** (Zwischenzustand): Feld enthält die `job_id`. Der Agent formuliert den
   Zwischenstand („DFT-Validierung gestartet, ID qm-8f2a") und die Execute-Loop **gibt die
   Kontrolle ab**, statt zu warten — die Session pausiert.
2. Der bestehende `notify_agent`-Callback (Plan 1.7) **weckt** die Session bei Job-Abschluss und
   markiert das `awaiting`-Todo als `completed` (Ergebnis angehängt).
3. Die Completion-Loop läuft weiter mit den nun freigeschalteten Folge-Todos.

**Durability-Grenze — was einen Absturz überlebt:**
- **Der Job**: immer — er lebt in Temporal (Event-Replay, §2). Unabhängig vom Harness.
- **Die Todo-Liste/der Plan**: nur so weit wie der MAF-Session-State (Redis/Postgres). Das ist
  bewusst *keine* harte Durability — ein verlorener Plan wird schlimmstenfalls **neu geplant**
  (billig, ein LLM-Aufruf), während der teure Job nie verloren geht. Damit bleibt die Regel
  „schwere Durability nur in Temporal" (D-002) intakt; der Harness fügt **keine** neue
  Durability-Anforderung hinzu.
- **Konsequenz:** Wir brauchen die MAF Durable Extension weiterhin **nicht** (Deferred-Tabelle
  bleibt gültig). Der einzige Grenzfall — sehr lange Konversationspausen — wird durch den
  `awaiting`-Zustand + Temporal-Callback ohnehin abgedeckt, nicht durch Harness-Durability.

## 5. Konkrete Workflows, die das ermöglicht

**(a) Mehrstufige Untersuchung (der Leitfaden-Testfall, §5).** Statt eines vorverdrahteten
Graphen plant der Agent selbst:
```
Plan:  1. Graph nach Verbindung X + ähnlichen Substraten durchsuchen  [find_notes/expand_note]
       2. Schnellen xTB/ML-Screen der Regioselektivität rechnen        [compute_xtb_energy]
       3. NUR bei niedriger Konfidenz DFT eskalieren                    [submit_qm_job → awaiting]
       4. Ergebnis als Note vorschlagen                                 [propose_knowledge_note → PR]
Execute: arbeitet 1→2 ab; entscheidet an 2 datengetrieben, ob 3 nötig ist; pausiert an 3;
         nimmt nach Callback 4 auf.
```
Schritt 3 ist *bedingt und agenten-entschieden* — genau die Dynamik, die ein statischer Graph
nicht ausdrückt. Das Tiering-Prinzip (§2: schnell zuerst, DFT nur bei Bedarf) wird damit vom
Skill-Urteil zur **sichtbaren, überprüfbaren Plan-Entscheidung**.

**(b) BO-Kampagnen-Supervision (Phase 1d).** Eine mehrrundige Optimierung als Todo-Sequenz
(„propose → evaluate → tell → prüfe Konvergenz → wiederhole oder stoppe"), wobei die eigentliche
durable Kampagne weiter der Temporal-Workflow ist — der Harness plant nur die *Betreuung*
(wann stoppen, wann Kandidaten dem Chemiker vorlegen).

**(c) Deep-Research-/Report-Harness (Phase 5b).** Das dortige `decompose → fan-out → verify →
cite → synthesize` ist wörtlich ein Plan/Execute-Muster. Der MAF-Harness ist der natürliche
Träger für den `decompose`-Schritt; der lange Lauf bleibt Temporal-`background-jobs` (5b.6). Der
Harness ersetzt hier nichts, er macht die Zerlegung explizit und überprüfbar.

**(d) Plan-Modus als Human-in-the-Loop-Punkt (GxP).** Der Plan-Modus ist die natürliche Stelle,
an der „AI schlägt vor, Mensch zeichnet ab" *vor* der Ausführung greift — komplementär zum
PR-Gate, das *nach* der Wissensproduktion greift (siehe §6).

## 6. Governance-Verzahnung (mehr Autonomie ⇒ mehr Gates, nicht weniger)

- **PR-Gate bleibt terminal (D-005).** Egal wie autonom die Execute-Loop läuft: jede
  `created_by: agent`-Note (Job-Ergebnis, Kampagne, Report-Entwurf) geht weiterhin über
  Branch → PR → menschliche Freigabe. Autonomie erzeugt *Vorschläge*, keine gemergte Wahrheit.
- **Neuer Gate: Plan-Freigabe vor teurer Ausführung.** Bevor die Execute-Loop Schritte auslöst,
  die Rechenbudget verbrauchen (`submit_qm_job`, `submit_calculation`, später HPC/DFT), muss der
  Plan im Plan-Modus **bestätigt** werden. Welche Tools eine Freigabe erzwingen, steht in der
  Config (`plan_mode_required_for`, §8) — nicht im Code.
- **RBAC (Phase 6) wird wichtiger, nicht optional.** Ein autonom planender Agent, der teure
  Pfade selbst auslösen kann, verschärft die Autorisierungsfrage. Die fachliche Prüfung „darf
  *dieser* Nutzer *diesen* Job auslösen" bleibt im MCP-Server (§8 der Architektur), *vor* dem
  Todo-Ausführungsschritt — der Harness umgeht das nicht.
- **Audit-Trail pro Todo-Aktion.** Der Entra-ID-`oid`/`upn` des Nutzers (Plan 1.9) wird nicht
  nur am Job, sondern an jeder auslösenden Todo-Aktion mitgeführt, damit der Audit-Trail
  „wer hat welchen Schritt veranlasst" auch bei autonomer Abarbeitung vollständig bleibt.

## 7. Interaktion mit den bestehenden Schichten

- **Skills (§3).** Unverändert nutzbar: Der Harness lädt bei der Planung dieselben Skills
  (`calculation-selection`, `reaction-search`, …) als *Urteil*, welche Schritte in den Plan
  gehören. Progressive Disclosure bleibt; Skills werden zur Planungshilfe, nicht nur zur
  Ausführungshilfe.
- **Berechnungs-Store (Phase 1b, D-011).** Ein Todo, dessen Ergebnis bereits im Store liegt,
  wird zum **Cache-Hit** — die Execute-Loop rechnet nicht doppelt. „Nie zweimal rechnen" gilt
  unverändert; der Plan macht nur sichtbar, *dass* geprüft wird.
- **Eval-/Metrik-Schicht (Phase 2b, D-009).** Autonomie muss ihren Nutzen **belegen** (die
  Schicht ist genau dafür da). Neue registrierte Metriken: Plan-Qualität (nötige vs. geplante
  Schritte), „hat die Loop geholfen" (A/B Plan/Execute vs. Einzelaufruf pro Aufgabentyp),
  Abbruch-/Runaway-Rate. Regression = Testfehler (2b.5).
- **Gedächtnis (Phase 5).** Ein abgeschlossener, vom Chemiker bestätigter Plan ist selbst eine
  **episodische `interaction`-Note** (Plan 5.5) — dieselbe Note, dasselbe Gate. Das System lernt
  aus seinen eigenen erfolgreichen Plänen, ohne neuen Mechanismus.

## 8. Config & Leitplanken (keine Magic Numbers, G3)

Alles über **eine** `pydantic-settings`-Quelle (`chemclaw/config.py`), ENV-überschreibbar.
**Implementiert** sind bewusst nur die *tatsächlich konsumierten* Felder (config.py-Disziplin:
keine „für später"-Settings):

| Setting | Zweck | Default |
|---|---|---|
| `harness_enabled` | Master-Schalter (Fallback: klassischer `Agent`) | `false` |
| `harness_autonomy` | `plan_only` (interaktiv) \| `execute` (Loop im Execute-Modus) | `plan_only` |
| `harness_max_loop_iterations` | Runaway-Loop-Bremse; greift nur bei `execute` | `15` |

**Bewusst (noch) nicht als Config verdrahtet** — jeweils mit Grund, statt spekulativem Feld:
- *`max_todos`*: Der `TodoProvider` nimmt keine Obergrenze entgegen; eine künstliche Kappung
  bräuchte einen eigenen Store-Wrapper (Rule of Three nicht erfüllt) → ausgelassen.
- *`token_budget`*: Bindet an die Kompaktierungs-Strategie (braucht Tokenizer/Kontextfenster);
  Kompaktierung ist hier bewusst aus (v1). Kommt, wenn ein realer Kostendruck es misst.
- *`plan_mode_required_for` (harte Tool-Sperre)*: Die *fachliche Autorisierung* „darf dieser
  Nutzer diesen teuren Pfad auslösen" gehört laut Architektur an **eine** Stelle — den
  MCP-Server (§8) — nicht parallel in den Agenten. Bleibt Phase 6, wird hier nicht dupliziert.

**Kill-Switch & Beobachtbarkeit:** `harness_enabled=false` fällt sofort auf das heutige
Verhalten zurück (der Harness-Agent degradiert zur klassischen `Agent`-Konstruktion). Die
`execute`-Loop ist zusätzlich durch `harness_max_loop_iterations` hart begrenzt
(`AgentLoopMiddleware.max_iterations`). Loop-/Plan-Metriken für Schicht 2b sind Folgearbeit
(Backlog).

**Governance-Härtung (implementiert):** Der Harness aktiviert per Default generische
File-Memory-, File-Access-, Shell- und Web-Search-Werkzeuge — diese sind in `build_agent`
**abgeschaltet** (`disable_file_memory/…access/…web_search=True`, kein `shell_executor`, keine
`background_agents`). Chemclaws Fähigkeit ist ihr *expliziter* Tool-/Skill-Satz, kein generischer
Datei-/Shell-Zugriff (§6, G6). Übrig bleiben genau `TodoProvider` + `AgentModeProvider` über den
bestehenden Tools/Skills.

## 9. Reifegrad & Risiken (Caveats, im Stil §15)

- **`[Experimental]`-API** — direkter Konflikt mit dem Projektprinzip „off-the-shelf, mature,
  defer until measured" (D, DEFERRED). Deshalb: **Spike zuerst** (§10, H0), harte Kapselung
  hinter `build_agent`, und ein **funktionierender Fallback** (klassischer `Agent` + explizite,
  tool-getriebene Schrittfolge), falls sich die Harness-API als instabil erweist.
- **Determinismus** — die Execute-Loop ist LLM-getrieben und nicht deterministisch. Sie darf
  deshalb **nie** in einen Temporal-Workflow eingebettet werden (Determinismus-Regeln, §2). Der
  Harness bleibt strikt in der MAF-Schicht; Temporal sieht nur fertige Tool-Aufrufe.
- **Runaway-Kosten** — ein Agent, der sich selbst Todos gibt, kann teure Schritte multiplizieren.
  Gegenmittel: `max_loop_iterations`, `token_budget`, `plan_mode_required_for` + RBAC (§6/§8).
- **Kontext-Kompaktierung** — die Harness-Kompaktierungs-Middleware darf die Provenienz-Trennung
  (episodisch vs. semantisch, §9 der Architektur) nicht verwischen; bei Report-Läufen (5b) sind
  Zitate/Belege von der Kompaktierung auszunehmen.

## 10. Stufenweiser Einbau (Phasen mit Quality-Gate)

Analog zum `implementation-plan.md`: kleine, einzeln abnehmbare Schritte, jeder mit CHECKMATE
(G1–G7) und grünem `make lint type test`.

- **H0 — Spike (Risiko zuerst). ✅ erledigt.** Verifiziert gegen die *installierte*
  `agent-framework-core 1.11`: `create_harness_agent` konstruiert **ohne** LLM-Aufruf mit einem
  Dummy-Client; die Provider sind bei abgeschalteten Batterien exakt `TodoProvider` +
  `AgentModeProvider` (+ History); die Default-Modi heißen `plan`/`execute`; `todos_remaining(
  looping_modes=["execute"])` bindet die Loop nativ an den Execute-Modus. API real und stabil
  genug → weiter (kein „verworfen").
- **H1 — Planung sichtbar (Backbone verdrahtet). ✅ erledigt.** `build_agent` baut hinter
  `harness_enabled` den Harness-Agenten über *dieselben* Tools/Skills; Batterien abgeschaltet;
  klassischer Fallback bleibt Default. Getestet (`tests/test_agent.py`): Backbone-Auswahl,
  Provider-Set, gleiche Domain-Tools, keine File/Shell-Batterien. *Offen:* echte read-only-Sicht
  (nebenwirkungsfreie Teilmenge) im Live-Chat beobachten.
- **H2 — Plan-Modus mit menschlicher Freigabe. (teilweise)** `AgentModeProvider` ist aktiv
  (`plan`→Freigabe→`execute` ist der Provider-Default-Fluss); `harness_autonomy=plan_only`
  hält die Loop interaktiv. *Offen:* Live-Erprobung des Freigabe-Übergangs mit echtem Modell.
- **H3 — Gebundene Execute-Loop mit `awaiting`-Muster. (Loop erledigt, `awaiting` offen)** Die
  Execute-Loop ist verdrahtet und hart begrenzt (`harness_max_loop_iterations`, getestet). Die
  Drei-Zustands-Kopplung an den `notify_agent`-Callback (§4) ist **Folgearbeit** und hängt am
  noch-Stub-Callback (Plan 1.7) — heute bleibt das Fire-and-Forget-Verhalten wie gehabt (der
  Agent meldet die `job_id` und fährt fort), der durable Job liegt ohnehin sicher in Temporal.
- **H4 — Autonomie hinter RBAC (mit/nach Phase 6). (offen)** Feinere Autonomiestufen + harte
  Auslöse-Autorisierung landen im MCP-Server (§6/§8), nicht im Agenten. *Abnahme:* unberechtigter
  Nutzer kann teure Pfade auch autonom nicht auslösen; `oid` im Trail vollständig.

> **CHECKMATE H** (G1–G7 + Autonomie-Spike): Ist der Harness **eine** gekapselte Erweiterung in
> `build_agent` (kein Framework-Bau, G1)? Bleibt der teure/lange Pfad **ausschließlich** bei
> Temporal (D-002, G6)? Fügt der Harness **keine** neue Durability-Anforderung hinzu (§4)?
> Sind Loop-Grenzen/Budgets/Freigabe-Tools **konfigurierbar** (G3)? Belegt die Metrik-Schicht
> (2b) mindestens einen Fall, in dem Plan/Execute real hilft — und einen, in dem es *nicht*
> hilft (selektiver Einsatz, nicht universell)?

## 11. Ersetzt der Harness die graph-basierten Ansätze? — Nein.

Kurzantwort: **Der Harness ersetzt weder Temporal noch die (geplanten) MAF-Graph-Workflows —
er ist ein dritter, komplementärer Baustein.** Wichtig für die Einordnung: Im Repo existiert
**heute gar kein** MAF-Graph-Workflow-Code. Alles unter `workflows/` sind **Temporal**-Workflows
(`qm_job`, `bo_campaign`, `eln_sync`, `memory_jobs`, …); der einzige geplante MAF-Graph-Workflow
ist der `development-report` (Plan 5b.5) und ist noch nicht gebaut. Es wird also aktuell *nichts*
im Code ersetzt.

| Ansatz | Zweck | Verhältnis zum Harness |
|---|---|---|
| **Temporal-Workflows** (gebaut) | Durable, lang laufende, deterministisch wiederholbare Ausführung | **Bleibt.** Der Harness ist MAF-intern und *nicht* durable (D-002). Teure/lange Schritte gehen unverändert fire-and-forget an Temporal. Keine Überschneidung. |
| **MAF-Graph-Workflows** (geplant, 5b.5) | *Feste*, vorverdrahtete, typisierte Kontrollflüsse; strukturell erzwungene Provenienz-Trennung pro Berichtsabschnitt | **Bleibt sinnvoll für feste Abläufe.** Der Graph garantiert reproduzierbare Struktur (GxP-relevant); der Harness plant *offene*, vorab unbekannte Schrittfolgen. Ein dynamischer Plan könnte einen simplen Graphen nachbilden, erzwingt die Sektions-/Provenienz-Struktur aber nur per Instruktion, nicht *strukturell* — schwächer für den Audit. |

**Empfehlung (bestätigt aus §12 Q3):** Beim `development-report` den **Graph für die feste
Berichts-/Provenienz-Struktur** behalten und den **Harness für die offene Recherche je Abschnitt**
nutzen — sauber getrennt, nicht das eine durch das andere ersetzen. Für *offene* Mehrschritt-
Anfragen (§5 (a)) ist der Harness der richtige Träger; dort gab es ohnehin nie einen Graphen.

Fazit der drei Reasoning-/Ausführungs-Formen nebeneinander: **Temporal** = durable Ausführung ·
**Graph-Workflow** = feste, deterministische Reasoning-Flüsse · **Harness** = offene, dynamische
Mehrschritt-Planung. Drei Verantwortlichkeiten, keine Verdrängung.

## 12. Auswirkung auf DECISIONS / DEFERRED

- **ADR D-020 (gesetzt):** „MAF Agent Harness (TodoProvider + AgentModeProvider) als dritter
  Reasoning-Baustein für offene Mehrschritt-Anfragen; strikt MAF-intern, keine neue Durability,
  generische Batterien aus, Fallback auf klassische `Agent`-Konstruktion." — verfeinert D-002
  (Reasoning-Orchestrierung wird *dynamisch*), überstimmt es **nicht** (Durability-Grenze
  unverändert). Siehe `DECISIONS.md`.
- **DEFERRED-Zeile „MAF Durable Extension for jobs"** bleibt gültig — der Harness ändert die
  Begründung nicht; §4 zeigt, dass wir sie weiterhin nicht brauchen.

## 13. Offene Fragen (für den Backlog)

1. `awaiting`-Muster (§4/H3): Kopplung an den `notify_agent`-Callback, sobald dieser nicht mehr
   Stub ist (Plan 1.7). Wo lebt der MAF-Session-State (Redis vs. Postgres) und wie lang halten
   pausierte Sessions? (*keine* harte Durability-Anforderung).
2. Plan-/Loop-Metriken für Schicht 2b (D-009): Plan-Qualität, „hat die Loop geholfen" (A/B),
   Runaway-Rate registrieren.
3. Plan-Modus-Freigabe + feinere Autonomie hinter RBAC (Phase 6), Autorisierung im MCP-Server.
4. Verhältnis Harness-Plan ↔ `development-report`-Graph-Workflow (5b.5) konkret ausbauen, sobald
   der Report-Harness (Phase 5b) gebaut wird (siehe §11).

## 12. Offene Fragen (für den Backlog)

1. Wo lebt der MAF-Session-State konkret (Redis vs. Postgres) und wie lang halten pausierte
   `awaiting`-Sessions? (berührt §4, aber *keine* harte Durability-Anforderung).
2. Reicht der Python-`todos_remaining()`-Helfer, oder brauchen wir die .NET-`Loop`-Semantik
   nachgebaut? (im H0-Spike klären).
3. Verhältnis Harness-Plan ↔ `development-report`-Graph-Workflow (5b.5): plant der Harness den
   Graphen, oder ruft der Graph den Harness pro Knoten? (Empfehlung: Graph für die *feste*
   Berichtsstruktur, Harness für die *offene* Recherche je Abschnitt — sauber getrennt halten).
