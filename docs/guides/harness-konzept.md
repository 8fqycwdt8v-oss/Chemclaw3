# Konzept: Der Plan-/Ausführungs-Harness (LangGraph)

> Status: **gebaut und in Betrieb hinter `harness_enabled`** (Default `false`). Dieses Dokument
> beschreibt, was der Harness *heute ist* — nicht mehr, was er einmal werden sollte. Es ist eine
> Ergänzung zu [`architektur.md`](./architektur.md) §1, **keine** Revision der Vier-Schichten-
> Trennung. Abschnittsverweise ohne Doku-Namen beziehen sich auf `architektur.md`.
>
> Der Harness wurde ursprünglich auf dem Microsoft Agent Framework entworfen und gebaut
> (D-038/D-040). Schicht 1 läuft seit
> [`D-2026-08-10-langgraph-rebuild-of-the-conversation-layer`](../decisions/D-2026-08-10-langgraph-rebuild-of-the-conversation-layer.md)
> auf LangGraph; §10 hält fest, was dieser Wechsel am Harness konkret geändert hat, weil zwei der
> damaligen Entwurfsentscheidungen ausschließlich Framework-Eigenheiten kompensiert haben.

---

## 0. Kernidee in einem Satz

Der Agent bekommt eine **eigene, selbst-generierte Aufgabenliste** (`TodoListMiddleware`, Tool
`write_todos`) plus ein **Freigabe-Gate vor jeder zustandsändernden Ausführung**
(`agent/plan_gate.py`): er zerlegt eine komplexe Chemiker-Anfrage zuerst in nachvollziehbare
Teilschritte, lässt den Plan vom Menschen freigeben und arbeitet ihn dann eigenständig ab —
**ohne** dass wir dafür einen zweiten Orchestrator oder ein zweites Durability-System bauen. Die
schwere, lange Ausführung bleibt exakt bei Temporal (D-002); der Harness plant und sequenziert nur
die *kurzen Reasoning-Schritte*, die Schicht 1 ohnehin verantwortet.

## 1. Motivation & Abgrenzung

**Die Lücke, die er schließt.** Für *offene, mehrstufige* Anfragen — „Kläre die Regioselektivität
von X, prüf, ob wir Ähnliches hatten, und rechne nur nach, wo nötig" — braucht es einen
Mechanismus, in dem der Agent selbst einen **überprüfbaren Plan** aufstellt, ihn dem Chemiker
zeigt und ihn dann Schritt für Schritt abarbeitet. Ohne ihn kann der Agent zwar Tools aufrufen,
seine Mehrschritt-Absicht ist aber nur implizit im Chatverlauf, nicht als sichtbare,
zustandsbehaftete Liste — und was nicht sichtbar ist, kann niemand vor der Ausführung korrigieren.

**Ziele:**
1. **Sichtbare Planung** — der Chemiker sieht *vorab*, welche (ggf. teuren) Schritte anstehen, und
   kann korrigieren, bevor Rechenzeit verbraucht wird.
2. **Dynamische Zerlegung** — der Agent bestimmt Schrittzahl und Reihenfolge selbst, statt dass
   jeder Ablauf vorverdrahtet wird.
3. **Autonome Abarbeitung mit Zwischenstand** — mehrstufige Untersuchungen laufen ohne ständiges
   Nachfragen durch, melden aber Fortschritt und halten am PR-Gate an.

**Nicht-Ziele:**
- **Kein zweites Durability-System.** Der Harness ist keine Ausführungs-Engine für lange Jobs.
  Temporal bleibt der *einzige* Ort für durable, langlaufende Arbeit (D-002, D-006). Der
  Checkpointer unter dem Graphen hält Turn-Zustand — und nichts, was ein Job wäre
  (D-2026-08-10 §3).
- **Kein Ersatz der festen Pipelines.** Der Report-Pfad (5b, D-020) bleibt ein deterministischer
  Temporal-Fluss; der Harness ist für das *offene* Terrain (§11).
- **Keine Aufweichung des PR-Gates.** Mehr Autonomie heißt *mehr*, nicht weniger menschliche
  Freigabe (§6).

## 2. Woraus der Harness besteht

Er ist kein Framework-Baustein, den man einschaltet, sondern **vier Teile**, die
`langgraph_agent._harness_middleware` und `langgraph_agent.tool_call_middleware` an den kompilierten Graphen
hängen — beide nur, wenn `harness_enabled_for(profile)` wahr ist, damit der klassische Agent
unverändert der ist, der er ohne Harness war.

| Baustein | Was er tut |
|---|---|
| **`TodoListMiddleware`** (LangChain) | Stellt dem Modell `write_todos` bereit und besitzt das Feld `todos` im Graph-State. Ein `Todo` ist `{content, status}` — **ohne** Beschreibungsfeld, was in §4 wichtig wird. |
| **`ChemclawState`** (`agent/state.py`) | Erweitert `PlanningState` um zwei Felder: `model_calls` (der Zähler der Runaway-Bremse) und `loop_capped` (ob sie gefeuert hat). Felder kommen mit der Phase, die sie liest — ein deklariertes Feld, das niemand konsultiert, ist derselbe Stub wie eine Funktion, die niemand aufruft. Ein drittes, `awaiting_jobs`, stand hier, bis auffiel, dass es nie jemand geschrieben oder gelesen hat (§4). `turn_input` setzt beide Felder beim Turn-Start zurück — der Checkpointer hält den Thread, also ist ein Feld ohne Reset **pro Session** und nicht pro Turn. |
| **`enforce_loop_cap`** (`agent/loop_cap.py`) | Ein `@before_model`-Hook, der die Modellaufrufe dieses Turns zählt und den Lauf bei `harness_max_loop_iterations` mit `{"jump_to": "end", "loop_capped": True}` beendet. Er *erzwingt* die Grenze und *protokolliert* sie in einem Zug; `loop_capped(state)` liest die Tatsache zurück — ein Flag, keine Zahl: der stoppende Zweig zählt nicht hoch, also endet ein gedeckelter Turn bei genau derselben Zahl wie einer, der seinen letzten erlaubten Aufruf verbraucht und dann geantwortet hat. |
| **`enforce_plan_approval`** (`agent/plan_gate.py`) | Ein `@wrap_tool_call`-Gate, das jeden zustandsändernden Aufruf ablehnt, solange für den *aktuellen* Plan keine lebende menschliche Freigabe vorliegt. |

**Warum der Deckel ein eigener Zähler ist und nicht `ModelCallLimitMiddleware`.** Die
Framework-Middleware erzwingt genau diese Grenze, und sie war der erste Versuch. Sie führt zwei
Zählstände — einen, der über den Thread persistiert, und einen, der es nicht tut —, und der, der
zu einem *Turn* passt, ist der zweite. Gegen eine gecheckpointete Session gemessen trägt der
Endzustand den Thread-Zähler und **gar keinen** Run-Zähler: „wurde dieser Turn gedeckelt" war
daraus nicht beantwortbar. Erzwingen dort und nochmal zählen hier wären zwei Zähler für eine Zahl
gewesen; erzwingen hier ist eine Zahl, die zugleich Grenze und Protokoll ist.

## 3. Einordnung in die Vier-Schichten-Architektur

Der Harness ist eine **reine Reasoning-Schicht-Erweiterung** und respektiert D-002:

```
┌─ Reasoning-Schicht (LangGraph) ───────────────────────────────────────┐
│                                                                        │
│   create_agent(state_schema=ChemclawState, middleware=[…])             │
│        │            │                │                                 │
│        │            │                └─ enforce_plan_approval → Freigabe vor Wirkung
│        │            └─ TodoListMiddleware  → write_todos, State-Feld `todos`
│        │            └─ enforce_loop_cap    → `model_calls`, harte Obergrenze
│        │            └─ ContextEditingMiddleware → Kontext-Budget (`agent/compaction.py`,
│        │                 **nicht** harness-bedingt: hängt immer, auch am Einzel-Turn-Agenten)
│        │                                                                │
│        └─ ruft pro Schritt vorhandene Tools auf:                        │
│             • inline (xTB, Löslichkeit, pKa, Graph-Query) — synchron    │
│             • fire-and-forget (durable Launcher) ──────────────────────┼──► Temporal
│                                                                        │        (Durability,
└────────────────────────────────────────────────────────────────────────┘         D-002/§2)
```

**Schichtreinheit (G6):** Der Harness-Zustand (Plan, Wartestand, Zählerstand) ist
Konversationszustand und lebt im Graph-State, den der Postgres-Checkpointer
(`agent/checkpointer.py`) zwischen Turns hält. Er sickert **nicht** in Temporal-Workflows, Skills
oder den Wissensgraphen. Umgekehrt bleibt jeder teure/lange Schritt ein normaler
Fire-and-Forget-Aufruf an Temporal — der Harness ändert daran nichts, er *sequenziert* nur, wann
der Aufruf passiert.

**Blast-Radius im Code:** klein und an einer Stelle. `_harness_middleware` entscheidet, ob die
Todo-Liste und der Deckel überhaupt hängen; `tool_call_middleware` schiebt das Freigabe-Gate ein, wenn
`gate_applies(profile)`. Tools und Skills bleiben unverändert, weil der Harness dieselbe
Registrierung nutzt.

## 4. Execute-Loop vs. Fire-and-Forget

Das ist die Stelle, an der der Harness und die bestehende Async-Job-Mechanik aufeinandertreffen.

**Problem:** Die Abarbeitung will „arbeite Todos ab, bis keine mehr offen sind". Unsere teuren
Schritte sind aber **nicht-blockierend** (D-002): ein durable Launcher gibt sofort eine `job_id`
zurück, das Ergebnis kommt später über den Push-Back in die Session. Ein naives „Loop bis fertig"
würde entweder blockieren (verbietet die Architektur) oder das Todo fälschlich abhaken, obwohl der
Job noch läuft.

**Lösung: die Buchhaltung steht gar nicht erst im Plan.** Ein Schritt, der einen Temporal-Job
auslöst, hinterlässt die `job_id` als `job_records`-Zeile und als `session_events`-Push-Back —
*nicht* im Plan. Der Agent formuliert den Zwischenstand („DFT-Validierung gestartet, ID qm-8f2a"),
der Turn gibt die Kontrolle ab, und der zurückgemeldete Abschluss bringt die Folgeschritte wieder in
Gang.

Ein State-Feld `awaiting_jobs` war dafür vorgesehen und wurde deklariert, bevor die durable Seite
gebaut war; die ging dann in die beiden Stores oben. Geschrieben oder gelesen hat es nie jemand, und
es ist entfernt statt nachgereicht (D-2026-08-11-a-policy-nobody-can-see-is-a-policy-nobody-has).

**Warum das mehr ist als Aufräumen.** Vorher wurde ein wartendes Todo dadurch markiert, dass sein
Beschreibungsfeld mit `awaiting-job:` präfigiert wurde — eine Konvention, die es nur gab, weil das
Todo-Objekt kein Feld dafür hatte. Der Plan-Identitätshash musste diese Einträge dann wieder
herausfiltern, sonst hätte ein freigegebener Plan seine eigene Freigabe in dem Moment widerrufen,
in dem er den ersten Job startet. Heute steht die Buchhaltung schlicht **nicht in dieser Liste**:
die Ausnahme, die das Gate braucht, ist strukturell statt geparst — und `Todo` hat kein
Beschreibungsfeld mehr, in das die Konvention zurückkriechen könnte.

**Durability-Grenze — was einen Absturz überlebt:**
- **Der Job**: immer — er lebt in Temporal (Event-Replay, §2), unabhängig vom Harness.
- **Plan, Wartestand und Zählerstand**: soweit der Checkpointer reicht, also über einen
  Pod-Neustart hinweg. Das ist eine echte Verbesserung gegenüber „im schlimmsten Fall neu planen"
  und trotzdem **keine** neue Durability-Anforderung: es ist derselbe Turn-Zustand, nur an einem
  Ort, den ein Prozessende überlebt.

## 5. Konkrete Workflows, die das ermöglicht

**(a) Mehrstufige Untersuchung (der Leitfaden-Testfall, §5).** Der Agent plant selbst:
```
Plan:  1. Graph nach Verbindung X + ähnlichen Substraten durchsuchen  [find_notes/expand_note]
       2. Schnellen xTB/ML-Screen der Regioselektivität rechnen        [compute_xtb_energy]
       3. NUR bei niedriger Konfidenz DFT eskalieren                    [durable QM-Job → awaiting]
       4. Ergebnis als Note vorschlagen                                 [propose_knowledge_note → PR]
```
Schritt 3 ist *bedingt und agenten-entschieden* — genau die Dynamik, die ein vorverdrahteter Fluss
nicht ausdrückt. Das Tiering-Prinzip (§2: schnell zuerst, DFT nur bei Bedarf) wird damit vom
Skill-Urteil zur **sichtbaren, überprüfbaren Plan-Entscheidung**.

**(b) BO-Kampagnen-Supervision.** Eine mehrrundige Optimierung als Todo-Sequenz („propose →
evaluate → tell → prüfe Konvergenz → wiederhole oder stoppe"), wobei die eigentliche durable
Kampagne weiter der Temporal-Workflow ist — der Harness plant nur die *Betreuung*.

**(c) Deep Research.** `decompose → fan-out → verify → cite → synthesize` ist wörtlich ein
Plan/Execute-Muster. Der Harness ist der natürliche Träger für `decompose`; der lange Lauf bleibt
Temporal. Die Fan-out-Stufe selbst ist inzwischen echte Parallelität im Graphen
(`retrieval/fanout.py`, ein `Send`-Zweig pro Quelle), was den Beitrag jeder einzelnen Quelle
sichtbar macht — vorher war eine Quelle mit null Treffern nicht von einer nicht befragten zu
unterscheiden.

**(d) Plan-Modus als Human-in-the-Loop-Punkt.** Der Plan-Modus ist die natürliche Stelle, an
der „der Agent schlägt vor, ein Mensch entscheidet" *vor* der Ausführung greift — komplementär zum PR-Gate,
das *nach* der Wissensproduktion greift (§6).

## 6. Governance-Verzahnung (mehr Autonomie ⇒ mehr Gates, nicht weniger)

- **PR-Gate bleibt terminal (D-005).** Egal wie autonom abgearbeitet wird: jede
  `created_by: agent`-Note geht über Branch → PR → menschliche Freigabe. Autonomie erzeugt
  *Vorschläge*, keine gemergte Wahrheit.
- **Die Freigabe gilt dem Akt, nicht der Sitzung.** `enforce_plan_approval` hängt am
  Tool-Aufruf-Rand, weil die Einheit, die eine Freigabe autorisiert, eine *Handlung* ist — dieselbe
  Begründung, die `agent/tool_authz.py` für die Per-Tool-RBAC führt. Eine Prüfung beim Turn-Start
  sieht plausibel aus und ist die Stelle, an der der naheliegende Fix falsch wird: der Plan wird
  *danach* umgeschrieben, eine Prüfung davor läse also den alten, freigegebenen Plan und winkte
  alles Folgende durch. Genau das war DARK-1: nach einer Freigabe wurde eine völlig andere Frage
  gestellt, und der Turn führte autonom eine Rechnung und einen Graph-Schreibvorschlag aus.
- **Der Plan wird aus dem Graph-State gelesen**, nicht aus einem umgebenden Sitzungsobjekt. Damit
  fragt das Gate den Plan *so, wie er in diesem Augenblick steht* — die Eigenschaft, die vorher
  eigens hergestellt werden musste.
- **Lesen bleibt offen.** Ein Gate über *alle* Tools machte `plan_only` unbenutzbar — der Agent
  könnte nichts nachschlagen, um den Plan zu bauen, den er freigegeben braucht —, und die
  Deployments mit der strengsten Haltung würden es abschalten. Die Linie liegt bei der
  Zustandsänderung (`agent/authz.py`, plus jeder durable Launcher): eine nicht freigegebene Session
  darf recherchieren und vorschlagen, und sonst nichts.
- **RBAC bleibt davor.** Die fachliche Prüfung „darf *dieser* Nutzer *diesen* Job auslösen" liegt
  in der einen Autorisierungs-Middleware, die *innerhalb* des Audit-Rings und *vor* dem
  Tool-Körper läuft — der Harness umgeht das nicht.
- **Audit-Trail pro Aktion.** Der Entra-`oid` des Nutzers wird nicht nur am Job, sondern an jedem
  auslösenden Tool-Aufruf mitgeführt; läuft ein Spezialist (§7), nennt die Zeile ihn **neben** dem
  Menschen, in einer eigenen Spalte — „der Agent" als Verursacher wäre in einem regulierten System
  ein wertloser Trail.

## 7. Interaktion mit den bestehenden Schichten

- **Skills (§3).** Unverändert nutzbar: bei der Planung werden dieselben Skills
  (`calculation-selection`, `reaction-search`, …) als *Urteil* geladen, welche Schritte in den Plan
  gehören. Progressive Disclosure bleibt — sie läuft jetzt über `deepagents.SkillsMiddleware`, die
  jedem Skill seinen *Pfad* in den System-Prompt schreibt und erwartet, dass das Modell den Körper
  liest. Deshalb ist die Verengung am **Backend** verankert (`agent/skill_backend.py`) und nicht an
  der angezeigten Liste: ein reiner Listen-Filter verbärge einen rollen-gegateten Skill und
  händigte ihn jedem aus, der den Pfad errät, den der Prompt ohnehin schon beigebracht hat.
- **Berechnungs-Store (D-011).** Ein Schritt, dessen Ergebnis bereits im Store liegt, wird zum
  **Cache-Hit** — es wird nicht doppelt gerechnet. Der Plan macht nur sichtbar, *dass* geprüft wird.
- **Eval-/Metrik-Schicht (D-009).** Autonomie muss ihren Nutzen **belegen**. `evals/autonomy.py`
  bewertet u. a. die Runaway-Rate; seit der Deckel ein gelesener Zähler statt einer Schlussfolgerung
  ist, kann diese Metrik „abgebrochener Schritt" von „korrekt an einen durable Job übergeben"
  unterscheiden, was sie aus Residuen allein nie konnte.
- **Spezialisten-Team (`agent/team.py`).** Ein Supervisor mit fünf Spezialisten ist gebaut und per
  Default **aus**, bis Routing-Genauigkeit und Token-Kosten gegen den Einzelagenten gemessen sind:
  ein Supervisor, der falsch routet, ist schlechter als gar kein Team. Für den Harness ändert das
  nichts an den Regeln — ein Spezialist ist ein Profil plus ein kompilierter Subgraph, seine
  Werkzeugmenge ist eine *Abschwächung* der des Aufrufers, und `safety` lässt sich nicht
  wegnarrowen.
- **Gedächtnis.** Ein abgeschlossener, vom Chemiker bestätigter Plan ist selbst eine episodische
  `interaction`-Note — dieselbe Note, dasselbe Gate. Das System lernt aus seinen eigenen
  erfolgreichen Plänen, ohne neuen Mechanismus.

## 8. Config & Leitplanken (keine Magic Numbers, G3)

Alles über **eine** `pydantic-settings`-Quelle (`src/chemclaw/core/config/`), ENV-überschreibbar.
Implementiert sind bewusst nur die *tatsächlich konsumierten* Felder:

| Setting | Zweck | Default |
|---|---|---|
| `harness_enabled` | Master-Schalter (Fallback: klassischer Agent ohne Todo-Liste und ohne Deckel) | `false` |
| `harness_autonomy` | `plan_only` (Freigabe-Gate aktiv) \| `execute` | `plan_only` |
| `harness_max_loop_iterations` | Runaway-Bremse; als Modellaufruf-Zähler in `ChemclawState` geführt | `25` |
| `agent_teams_enabled` | Supervisor + fünf Spezialisten statt eines Agenten (§7) | `false` |

Beide Harness-Dimensionen sind **pro Profil überschreibbar**, und beide werden über *einen*
Resolver gelesen (`harness_mode.harness_enabled_for` / `.autonomy_for`). Das ist kein Stilpunkt:
die Regel war einmal an drei Stellen ausgeschrieben, und ein Profil mit `plan_only` unter einem
globalen `execute` bekam das Gate angehängt, ohne dass seine Freigabe je verbraucht wurde — eine
Entscheidung autorisierte damit jeden weiteren Turn.

**Kill-Switch & Beobachtbarkeit.** `harness_enabled=false` fällt sofort auf das heutige Verhalten
zurück. Der Deckel ist **abgelesen, nicht erschlossen**: `enforce_loop_cap` beendet den Lauf und
hinterlässt die Zahl in `model_calls`, `loop_capped(state)` liest sie, und damit ist auch der Fall
`harness_max_loop_iterations == 1` beantwortbar — die frühere Schlussfolgerung war dort blind, weil
die Schleife bei einem Deckel von 1 nie nach ihrer Fortsetzung gefragt wurde. **Offen** ist die
Verdrahtung dieser Lesung in den Turn-Runner: `chemclaw.api.runner` sendet
`ErrorEvent(code="loop_cap_reached")` und zählt `chemclaw_turn_loop_caps_total` noch aus dem alten
Contextvar-Signal, das der Graph-Pfad nicht setzt (§13).

**Governance-Härtung.** Generische Batterien — File Memory, File Access, Shell, Web Search — sind
**nicht** angeschlossen, aus demselben Grund, aus dem sie beim Vorgänger-Framework abgeschaltet
waren: Chemclaws Fähigkeit ist ihr *expliziter* Tool-/Skill-Satz, kein generischer Datei- oder
Shell-Zugriff (§6, G6). Das kostet genau eine Handvoll Zeilen: statt deepagents'
`FilesystemMiddleware` (die `read`, `write`, `edit`, `glob`, `grep` und `execute` mitbrächte, für
die dann die Prompt-Verträge, die Rollen-Gates und die Sicherheits-Rubrik geradestehen müssten)
hängt genau **ein** handgeschriebenes `read_file` am verengten Skills-Backend. Progressive
Disclosure braucht ein Verb.

## 9. Risiken

- **Determinismus** — die Abarbeitung ist LLM-getrieben und nicht deterministisch. Sie darf
  deshalb **nie** in einen Temporal-Workflow eingebettet werden (Determinismus-Regeln, §2). Der
  Harness bleibt strikt in Schicht 1; Temporal sieht nur fertige Tool-Aufrufe.
- **Runaway-Kosten** — ein Agent, der sich selbst Todos gibt, kann teure Schritte multiplizieren.
  Gegenmittel: der Modellaufruf-Deckel, das Freigabe-Gate vor jeder Zustandsänderung, die
  Wiederholungs-Bremse (identische Aufrufe werden nach einer gemessenen Schwelle abgelehnt) und
  RBAC davor.
- **Junges Framework** — der Wechsel tauscht die Fehlerlast des einen jungen Frameworks gegen die
  eines anderen. LangChain 1.x hat offene Punkte bei dynamischem Tool-Hinzufügen und kennt keinen
  rein beobachtenden `before_tool`/`after_tool`-Hook. Das ist ein realer Preis, und die
  Live-Revalidierung ist das, was ihn von einer Annahme in eine Messung verwandelt.
- **Kontext-Kompaktierung** — sie darf die Provenienz-Trennung (episodisch vs. semantisch, §9 der
  Architektur) nicht verwischen; bei Report-Läufen sind Zitate/Belege auszunehmen. Seit
  `agent/compaction.py` ist das kein hypothetisches Risiko mehr, sondern ein benannter Handel: ein
  geräumtes Werkzeugergebnis hinterlässt einen Platzhalter *ohne* die zitierte Spur, die D-025 noch
  behielt. Der Handel greift erst oberhalb des Budgets, wo die Alternative ein harter Abbruch am
  Kontextlimit ist; `exclude_tools` ist die Notbremse, falls eine Installation misst, dass sie eine
  braucht.

## 10. Was der Wechsel auf LangGraph am Harness geändert hat

Historisch, aber nicht folgenlos: zwei der ursprünglichen Entwurfsentscheidungen existierten nur,
um Eigenheiten des alten Frameworks zu kompensieren, und sind mit ihm verschwunden.

- **Der Harness lief in der Streaming-Praxis überhaupt nicht.** Das Framework aktivierte
  History-Persistenz pro Service-Aufruf *und* installierte eine Middleware, die die Antwort im
  Streaming-Pfad neu zusammensetzte und dabei die Sentinel-`conversation_id` verlor. Der Loop
  schickte den Transkript erneut, während die History unabhängig davon re-injiziert wurde — ein
  `user`-Block zwischen `tool_use` und `tool_result`, HTTP 400 bei **100 %** der Tool-Aufrufe, in
  beiden Autonomiestufen. Jeder Unit-Test war dabei grün. Das ist der Grund, warum die Abnahme
  eines Harness-Pfades eine Live-Prüfung verlangt und keine Testsuite.
- **Der Deckel war unsichtbar.** Er griff an einer Stelle, an der ihn nichts beobachten konnte, und
  ein gedeckelter Turn sah von außen aus wie ein fertiger. Die Rekonstruktion („die Schleife wollte
  zuletzt weitermachen, also hat sie etwas anderes gestoppt") war korrekt und hatte ein Loch bei
  einem Deckel von 1. Heute ist es ein Zählerstand (§2).
- **`mode_set` musste zurückgenommen werden.** Das alte Framework injizierte ein Tool, mit dem sich
  das *Modell* selbst in den Execute-Modus versetzen konnte; die Härtung bestand darin, den
  Provider zu unterklassen und das Tool wieder zu entfernen. Hier wird es schlicht nie exponiert —
  es gibt nichts zurückzunehmen.
- **Ein Client pro gleichzeitigem Turn** war nötig, weil der Anthropic-Client die Identität eines
  im Streaming geparsten Tool-Aufrufs auf der *Client-Instanz* hielt: 8 von 8 gleichzeitigen Turns
  scheiterten auf einem geteilten Client, 0 von 8 auf eigenen. Der Ersatz-Client hält diesen
  Zustand nicht.

Was **nicht** verschwunden ist und auch nicht sollte: die Freigabe-Semantik. Beide Engines haben
denselben Plan-Hash über dieselben Todo-Texte gebildet und dieselbe durable Zeile gelesen — die
eine Divergenz, die *rückwirkend* gewesen wäre, weil sie Entscheidungen entwertet hätte, die ein
Chemiker bereits getroffen hat.

## 11. Ersetzt der Harness die festen Abläufe? — Nein.

**Der Harness ersetzt weder Temporal noch die deterministische Report-Pipeline — er ist ein
dritter, komplementärer Baustein.**

| Ansatz | Zweck | Verhältnis zum Harness |
|---|---|---|
| **Temporal-Workflows** | Durable, lang laufende, deterministisch wiederholbare Ausführung | **Bleibt.** Teure/lange Schritte gehen unverändert fire-and-forget dorthin. Keine Überschneidung. |
| **Report-Pipeline** (D-020) | *Fester*, deterministischer Synthese-Fluss (decompose → retrieve → verify → cite) mit erzwungener Zitat-Treue | **Bleibt.** Die Pipeline garantiert reproduzierbare Struktur und Belegpflicht; der Harness plant *offene*, vorab unbekannte Schrittfolgen. Ein dynamischer Plan erzwingt die Provenienz-/Zitatstruktur nur per Instruktion, nicht *strukturell* — schwächer für den Audit. |
| **Spezialisten-Team** (§7) | Aufteilung *einer* Anfrage auf mehrere schmal geschnittene Agenten | Orthogonal: das Team ändert, *wer* einen Schritt ausführt, nicht *ob* geplant und freigegeben wird. Beide Gates gelten eine Ebene tiefer unverändert. |

**Empfehlung:** Die Pipeline für die feste Berichts-/Provenienz-Struktur behalten und den Harness
für die offene Recherche nutzen — sauber getrennt, nicht das eine durch das andere ersetzen.

## 12. Auswirkung auf DECISIONS

- **D-038 und D-040** (Harness als dritter Reasoning-Baustein; autonomer Plan/Execute-Pfad) sind
  durch `D-2026-08-10-langgraph-rebuild-of-the-conversation-layer` **abgelöst**. Beide bleiben als
  gemergte ADRs stehen, wie es sich für gemergte ADRs gehört; ihre *Absicht* ist unverändert
  gültig, ihre Mechanik nicht mehr.
- **D-137/D-167** (menschliche Freigabe, Bindung an den Plan statt an die Sitzung) gelten weiter
  und sind der Grund, warum §6 so und nicht anders geschnitten ist.
- **D-002** ist unverändert. Was sich verschoben hat, ist keine Regel, sondern eine
  Implementierungsfolge: der Turn-Zustand liegt jetzt im Checkpointer statt in handgebautem SQL
  der Konversationsschicht (D-2026-08-10 §3).

## 13. Offene Punkte

1. **`loop_cap_reached` auf dem Graph-Pfad.** `loop_capped(state)` ist die richtige Lesung, aber
   der Turn-Runner liest noch das alte Contextvar-Signal — auf dem Graph-Pfad wird das Ereignis
   damit nicht gesendet und `chemclaw_turn_loop_caps_total` nicht gezählt (§8).
2. **Mid-Turn-Resume** eines Turns, der auf einen durable Job wartet, ist auf dem Graph-Pfad noch
   nicht scharf geschaltet und braucht eine eigene Entscheidung.
3. **Plan-/Loop-Metriken** für die Eval-Schicht ausbauen: Plan-Qualität (nötige vs. geplante
   Schritte) und ein A/B „hat die Loop geholfen" je Aufgabentyp.
4. **Team-Routing messen** (Genauigkeit, Token-Kosten pro Spezialist), bevor `agent_teams_enabled`
   irgendwo der Default wird.
