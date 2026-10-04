# Konzept: Der Plan-/Ausführungs-Harness (LangGraph)

> Status: **gebaut und in Betrieb hinter `harness_enabled`** (Default `true` seit
> `D-2026-09-13-the-default-is-the-posture-every-deployment-already-runs`; davor `false`, während
> der Helm-Chart ihn einschaltete). Dieses Dokument
> beschreibt, was der Harness *heute ist* — nicht mehr, was er einmal werden sollte. Es ist eine
> Ergänzung zu [`architektur.md`](../reference/architektur.md) §1, **keine** Revision der Vier-Schichten-
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
   Nachfragen durch, melden aber Fortschritt und halten am Freigabe-Gate an.

**Nicht-Ziele:**
- **Kein zweites Durability-System.** Der Harness ist keine Ausführungs-Engine für lange Jobs.
  Temporal bleibt der *einzige* Ort für durable, langlaufende Arbeit (D-002, D-006). Der
  Checkpointer unter dem Graphen hält Turn-Zustand — und nichts, was ein Job wäre
  (D-2026-08-10 §3).
- **Kein Ersatz der festen Pipelines.** Der Report-Pfad (5b, D-020) bleibt ein deterministischer
  Temporal-Fluss; der Harness ist für das *offene* Terrain (§11).
- **Keine Aufweichung der Freigabe.** Mehr Autonomie heißt *mehr*, nicht weniger menschliche
  Freigabe vor einer Wirkung (§6).

## 2. Woraus der Harness besteht

Er ist kein Framework-Baustein, den man einschaltet, sondern **vier Teile**, die
`langgraph_agent._harness_middleware` und `langgraph_agent.tool_call_middleware` an den kompilierten Graphen
hängen — beide nur, wenn `harness_enabled_for(profile)` wahr ist, damit der klassische Agent
unverändert der ist, der er ohne Harness war.

| Baustein | Was er tut |
|---|---|
| **`TodoListMiddleware`** (LangChain) | Stellt dem Modell `write_todos` bereit und besitzt das Feld `todos` im Graph-State. Ein `Todo` ist `{content, status}` — **ohne** Beschreibungsfeld, was in §4 wichtig wird. |
| **`ChemclawState`** (`agent/state.py`) | Erweitert `PlanningState` um die Zähler-/Flag-Paare der beiden Bremsen: `model_calls`/`loop_capped` (Runaway-Bremse) und `billed_tokens`/`spend_capped` (Ausgaben-Deckel, `agent/spend_cap.py`), dazu das private `loop_wrap_up` (siehe `enforce_loop_cap`). Felder kommen mit der Phase, die sie liest — ein deklariertes Feld, das niemand konsultiert, ist derselbe Stub wie eine Funktion, die niemand aufruft. Die Zähler sind **untracked** Kanäle (`TurnTotal`/`TurnFlag`): der Checkpointer persistiert sie nie, also beginnt jeder Lauf des Graphen bei 0 — pro Turn, ohne dass irgendwer zurücksetzen muss, und ein Fan-out auf Helfer teilt sich ein Budget. |
| **`enforce_loop_cap`** (`agent/loop_cap.py`) | Ein `@before_model`-Hook, der die Modellaufrufe dieses Turns zählt. Bei `harness_max_loop_iterations` setzt er `loop_capped` und gewährt **genau einen** weiteren Aufruf ohne Tools, der die Antwort aus dem Vorhandenen schreibt (`answer_at_the_cap`); erst die zweite Ankunft beendet den Graphen mit `jump_to: end`. Er *erzwingt* die Grenze und *protokolliert* sie in einem Zug; `loop_capped(state)` liest die Tatsache aus dem Endzustand, `loop_hit_cap()` aus einer Contextvar für den Streaming-Runner, der den Endzustand nie zurückbekommt. |
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
*nicht* im Plan. Der Agent formuliert den Zwischenstand („Konformerensuche gestartet, ID calc-8f2a"),
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
       2. Schnellen xTB-Screen der Regioselektivität rechnen           [compute_xtb_energy]
       3. NUR bei enger Energiedifferenz das Konformerensemble suchen   [durable calc-Job → awaiting]
       4. Ergebnis als Note festhalten                                  [record_knowledge_note]
```
Schritt 3 ist *bedingt und agenten-entschieden* — genau die Dynamik, die ein vorverdrahteter Fluss
nicht ausdrückt. Das Tiering-Prinzip (§2: erst der Einzelpunkt, die teure Suche nur bei Bedarf)
wird damit vom Skill-Urteil zur **sichtbaren, überprüfbaren Plan-Entscheidung**.

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
der „der Agent schlägt vor, ein Mensch entscheidet" *vor* der Ausführung greift. Für Wissen gibt es
kein nachgelagertes Gate mehr (§6).

## 6. Governance-Verzahnung (mehr Autonomie ⇒ mehr Gates, nicht weniger)

- **Wissen wird direkt geschrieben und korrigiert, nicht vorab freigegeben**
  (`D-2026-09-05-the-gate-follows-behaviour-not-knowledge`). Das PR-Gate (D-005) ist gelöscht:
  eine `created_by: agent`-Note landet über `kg/record.py` direkt in `knowledge/`, und was sie
  sicher macht, sind Provenienz, die Zitate, die ein Chemiker am Ort der Nutzung prüft, und
  Widerspruch/Supersession. Das Freigabe-Gate dieses Harness gilt **Wirkungen** (zustandsändernde
  Tool-Aufrufe), nicht Wissen; ein `SKILL.md` schreibt kein Agentenpfad.
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
  auslösenden Tool-Aufruf mitgeführt; läuft ein Helfer (`task`, §7), nennt die Zeile dessen
  Profilnamen **neben** dem Menschen, in der eigenen Spalte `agent` (`agent/audit.py`) — „der Agent" als Verursacher wäre in einem regulierten System
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
- **Spezialisten-Team — entfernt (D-2026-08-15); Helfer — gebaut.** Ein Supervisor mit fünf
  Spezialisten war gebaut und blieb per Default aus. Heute liefert deepagents' `task` auf jedem
  Turn einen benannten Helfer-Roster (`agent_helper_roster`, Profile in `data/profiles/`), und die
  Regel bindet jeden davon: seine Werkzeugmenge ist eine *Abschwächung* der des Aufrufers — minus
  `authz.side_effecting_tools()` — (`D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor`).
  Ein Helfer kann also nichts auslösen, was das Freigabe-Gate bräuchte. Handoffs zwischen Peers
  (`agent_peer_roster`, `agent/turn_graph.py`) sind gebaut und per Default aus.
- **Gedächtnis.** Ein abgeschlossener, vom Chemiker bestätigter Plan ist selbst eine episodische
  `interaction`-Note — derselbe Schreibpfad (`kg/record.py`) wie jede andere Note. Das System lernt aus seinen eigenen
  erfolgreichen Plänen, ohne neuen Mechanismus.

## 8. Config & Leitplanken (keine Magic Numbers, G3)

Alles über **eine** `pydantic-settings`-Quelle (`src/chemclaw/core/config/`), ENV-überschreibbar.
Implementiert sind bewusst nur die *tatsächlich konsumierten* Felder:

| Setting | Zweck | Default |
|---|---|---|
| `harness_enabled` | Master-Schalter (Fallback: klassischer Agent ohne Todo-Liste und ohne Deckel) | `true` |
| `harness_autonomy` | `plan_only` (Freigabe-Gate aktiv) \| `execute` | `plan_only` |
| `harness_max_loop_iterations` | Runaway-Bremse; als Modellaufruf-Zähler in `ChemclawState` geführt | `25` |

Beide Harness-Dimensionen sind **pro Profil überschreibbar**, und beide werden über *einen*
Resolver gelesen (`agent/plan_gate.py`: `harness_enabled_for` / `autonomy_for`; ob das Gate hängt, entscheidet `gate_applies`). Das ist kein Stilpunkt:
die Regel war einmal an drei Stellen ausgeschrieben, und ein Profil mit `plan_only` unter einem
globalen `execute` bekam das Gate angehängt, ohne dass seine Freigabe je verbraucht wurde — eine
Entscheidung autorisierte damit jeden weiteren Turn.

**Kill-Switch & Beobachtbarkeit.** `harness_enabled=false` fällt sofort auf das heutige Verhalten
zurück. Der Deckel ist **abgelesen, nicht erschlossen**: `enforce_loop_cap` beendet den Lauf und
hinterlässt die Zahl in `model_calls`, `loop_capped(state)` liest sie, und damit ist auch der Fall
`harness_max_loop_iterations == 1` beantwortbar — die frühere Schlussfolgerung war dort blind, weil
die Schleife bei einem Deckel von 1 nie nach ihrer Fortsetzung gefragt wurde. Der Turn-Runner
liest den Deckel über `loop_hit_cap()` (die Contextvar, die `enforce_loop_cap` beim Feuern setzt),
sendet `ErrorEvent(code="loop_cap_reached")` vor der Antwort und zählt
`chemclaw_turn_loop_caps_total` (`api/runner.py::_loop_cap_event`). Der Turn schlägt dadurch nicht
fehl: die Antwort aus dem werkzeuglosen Abschlussaufruf geht trotzdem raus und wird als *partiell*
markierbar.

**Governance-Härtung.** Shell und Web Search sind **nicht** angeschlossen: Chemclaws Fähigkeit ist
ihr *expliziter* Tool-/Skill-Satz (§6, G6). Das Dateisystem ist deepagents'
`FilesystemMiddleware` über einem `CompositeBackend` (`agent/scratchpad.py`) mit drei Routen —
`/scratch/` (Graph-State, pro Thread), `/skills/` (das verengte, schreibgeschützte Skills-Backend)
und `/memories/` (Postgres-Store, pro Akteur, nur wenn das Deployment es einschaltet). `execute` und
`delete` werden zurückgehalten. Jede Datei-Operation ist ein Tool-Aufruf und läuft damit durch
dieselbe `wrap_tool_call`-Kette wie jeder andere: auditiert, autorisiert, im Dry-Run abgelehnt, und
ein Schreiben unter `/memories/` zählt für das Freigabe-Gate als Wirkung
(`authz.side_effecting_call` liest den `file_path`).

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
| **Helfer (`task`) und Peer-Handoff** (§7) | Aufteilung *einer* Anfrage auf schmal geschnittene Agenten | Orthogonal: sie ändern, *wer* einen Schritt ausführt, nicht *ob* geplant und freigegeben wird. Ein Helfer erreicht keine zustandsändernden Tools; ein Peer erbt höchstens die Fläche des Wurzel-Agenten. |

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

1. **Mid-Turn-Resume** — einen Turn, der durable Jobs gestartet hat, mit deren Ergebnissen
   fortsetzen — ist gebaut (`api/runner.py::_resume_on_job_results`), aber per Default aus
   (`mid_turn_resume_enabled=false`, begrenzt durch `mid_turn_resume_timeout_seconds`). Ein
   Deployment, das es einschaltet, entscheidet das bewusst.
2. **Plan-/Loop-Metriken** für die Eval-Schicht ausbauen: Plan-Qualität (nötige vs. geplante
   Schritte) und ein A/B „hat die Loop geholfen" je Aufgabentyp.
3. **Delegation lohnt sich gemessen nicht** (`D-2026-09-27-delegation-does-not-pay-on-the-measured-gateway-model`,
   `make live-delegation`); der Trigger dieses ADR sagt, wann die Frage wieder aufgeht.
