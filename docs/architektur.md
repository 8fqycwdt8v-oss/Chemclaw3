# Architektur: MAF + Temporal + Skills + Markdown-Knowledge-Graph

## 0. Kernidee

Vier klar getrennte Schichten, jede mit einer einzigen Verantwortung:

| Schicht | Verantwortung | Technologie |
|---|---|---|
| **Reasoning/Orchestrierung** | Konversation führen, Skill/Tool auswählen, kurze Schritte ausführen | Microsoft Agent Framework (MAF) |
| **Long-Running Execution** | Rechenintensive, Stunden/Tage dauernde Jobs (QM/DFT, HPC) durchhalten | Temporal |
| **Tool-/Capability-Integration** | Domänenwissen "wie tue ich X" modular verpacken | Agent Skills (SKILL.md) |
| **Persistentes Wissen** | Domänenwissen "was wissen wir über X" strukturiert & vernetzt speichern | Markdown-Knowledge-Graph (Git) |

Diese Trennung ist bewusst: MAF und Temporal beide für Durability zu nutzen wäre redundant (in der MAF-Community selbst wird der Versuch, MAF-Workflows und Dapr/Temporal-Workflows zu verschmelzen, explizit als "a torturous path" beschrieben). Stattdessen: **MAF orchestriert die Konversation, Temporal übernimmt ausschließlich die Lebenszyklen der langlaufenden wissenschaftlichen Jobs**, entkoppelt als asynchroner Tool-Aufruf.

---

## 1. Reasoning-Schicht: Microsoft Agent Framework

MAF (GA seit April 2026) liefert zwei Bausteine, die hier beide genutzt werden:

- **Agents**: LLM-gestützte Einzeleinheiten mit Instructions, Tools, Context-Providern.
- **Graph-based Workflows**: expliziter Graph aus Executors/Agents mit typisiertem Routing, Fan-out/Fan-in, bedingten Kanten – für die *kurzen* Orchestrierungsschritte (Anfrage parsen → Skill wählen → Tool aufrufen → Antwort formulieren), nicht für Stunden/Tage laufende Berechnungen.

**Bewusst NICHT genutzt für diesen Zweck:** MAFs eigene *Durable Task Extension* (basiert auf Azure Durable Functions/Durable Task Scheduler, mit den "4 D's": durable, distributed, deterministic, discoverable). Diese ist wertvoll für Konversationszustände, die über Tage pausieren (z. B. wartet auf menschliche Freigabe), aber sie ist Azure-Functions-nativ und für *lange Nutzerkonversationen* gedacht – nicht der richtige Ort für einen 6-Stunden-DFT-Job. Empfehlung: MAF-Konversationszustand bleibt leichtgewichtig (Session-State in Redis/Postgres reicht meist); die schwere Durability wandert vollständig zu Temporal.

## 2. Long-Running-Execution-Schicht: Temporal

**Warum getrennt von MAF:** Ein Temporal-Workflow ist deterministischer Python-Code, der Activities orchestriert – Activities sind der "unvorhersehbare" Teil (LLM-Aufruf, API-Call, **HPC-Job**) und können unabhängig fehlschlagen/retried werden. Bei Prozessabsturz spielt Temporal die Event-History ab und setzt exakt dort fort, wo abgebrochen wurde – ohne bereits abgeschlossene Activities zu wiederholen. Das ist genau das Verhalten, das ein 6-72-Stunden-DFT-Job auf einem HPC-Cluster braucht.

**Integrationsmuster (MAF ↔ Temporal):**

Es gibt *keinen* offiziellen MAF-Temporal-Adapter (anders als z. B. bei OpenAI Agents SDK, wo Temporal einen nativen `activity_as_tool`-Helper anbietet). Die Integration ist bewusst simpel gehalten:

```
MAF Agent Tool "submit_qm_job"
  └─> startet einen Temporal Workflow (fire-and-forget)
       └─> gibt sofort eine workflow_id / job_id zurück
  └─> MAF antwortet dem Nutzer sofort ("Screening läuft, DFT-Validierung gestartet, ID: qm-8f2a")

Temporal Workflow "QMJobWorkflow"
  ├─ Activity: prepare_input (Geometrie, Methode, Basissatz)
  ├─ Activity: submit_to_hpc (SLURM sbatch via AiiDA/ASE-Wrapper)
  ├─ Activity: poll_hpc_status (mit activity.heartbeat() alle 30s
  │            gegen Preemption/Timeout-Detection)
  ├─ Activity: parse_qm_output (RDKit/cclib-Parsing)
  ├─ Activity: write_knowledge_node (siehe Abschnitt 4)
  └─ Activity: notify_agent (Callback/Webhook zurück in MAF-Session
               oder Teams-Notification via Copilot)

MAF Agent Tool "get_qm_job_status(job_id)"
  └─> fragt Temporal Client nach Workflow-Status/-Result ab
```

- **Caching**: Ein separater Temporal-Workflow-Typ `CachedQMLookup` prüft vor dem eigentlichen Submit einen Cache-Store (Hash aus Molekül+Methode+Basissatz); nur bei Cache-Miss wird `QMJobWorkflow` gestartet.
- **Tiering**: Schnelle ML-Potentiale (MACE/NequIP) laufen synchron als normale MAF-Tools (Sekunden); nur die DFT-Eskalation bei niedriger Konfidenz geht an Temporal.
- **Deployment**: Temporal Cluster (self-hosted oder Temporal Cloud) + Temporal Worker-Prozesse, die auf/nahe der HPC-Umgebung laufen und Zugriff auf den SLURM-Scheduler haben. Worker sind zustandslos, horizontal skalierbar, dürfen abstürzen.

## 3. Tool-Integrationsschicht: Skill-basierter Ansatz (Agent Skills / SKILL.md)

MAF hat seit kurzem native Unterstützung für den offenen **Agent Skills**-Standard (SKILL.md, Dezember 2025 von Anthropic als offener Standard veröffentlicht, seither von Microsoft/Google/OpenAI übernommen). Für Python: `SkillsProvider`/`FileAgentSkillsProvider`, stabil/GA.

**Funktionsprinzip (Progressive Disclosure):**
1. **Advertise** (~100 Token/Skill): Name + Kurzbeschreibung jedes Skills wird ins System-Prompt injiziert.
2. **Load** (<5.000 Token): Bei Bedarf ruft der Agent `load_skill(name)` auf und bekommt die volle SKILL.md-Anleitung.
3. **Read on demand**: `read_skill_resource(name, path)` lädt zusätzliche Referenzdateien/Skripte/Templates nur bei tatsächlichem Bedarf.

Das hält den Kontext schlank, selbst bei Dutzenden domänenspezifischer Fähigkeiten – und löst genau das Problem, das MCP-Server mit zu vielen gleichzeitig geladenen Tools haben.

**Vorgeschlagene Skill-Struktur für dieses Projekt:**

```
skills/
├── eln-reaction-extraction/
│   ├── SKILL.md              # Wann/wie ELN-Freitext → ORD-Schema extrahieren
│   ├── scripts/
│   │   └── validate_ord.py   # RDKit-Validierung, Massenbilanz-Check
│   └── references/
│       └── ord-schema.md
├── qm-regioselectivity/
│   ├── SKILL.md              # Entscheidungslogik: ML-Potential zuerst,
│   │                         # DFT-Eskalation via Temporal nur bei Konfidenz < X
│   └── references/
│       └── confidence-thresholds.md
├── qm-pka-prediction/
│   └── SKILL.md
├── knowledge-graph-query/
│   ├── SKILL.md              # Wie man den MD-Graphen traversiert statt
│   │                         # simpler Vektorsuche
│   └── scripts/
│       └── graph_query.py
├── knowledge-graph-write/
│   ├── SKILL.md              # Note-Template, Frontmatter-Schema, Git-Workflow
│   └── assets/
│       └── note-template.md
└── gxp-scope-guard/
    └── SKILL.md              # Governance-Skill: erzwingt die Trennung
                               # "AI schlägt vor" vs. "GxP-Ausführung"
```

Jeder Skill kann intern sowohl direkten Python-Code als auch MCP-Tool-Aufrufe kapseln (Community-SDKs zeigen bereits Beispiele, die Skills mit LangChain, MAF *und* MCP kombinieren) – die Skill-Ebene ist also die "Bedienungsanleitung", MCP/Temporal/direkte Funktionsaufrufe sind die eigentliche Ausführung dahinter. Das erlaubt Fachexperten (Chemiker), SKILL.md-Dateien direkt zu pflegen, ohne Python-Code anzufassen – genau wie in Abschnitt 3 der MAF-Doku für HR-/Policy-Skills beschrieben, hier übertragen auf Reaktions- und Analytik-Wissen.

## 4. Wissensschicht: Interlinked Markdown Knowledge Graph

**Ziel:** Ein "was wissen wir"-Gedächtnis, komplementär zu den Skills ("wie tut man etwas"), das über simples RAG hinausgeht, indem es echte chemische Beziehungen als Graph-Kanten abbildet statt als Vektor-Ähnlichkeit.

**Notenformat (atomare Einheit = eine Datei):**

```markdown
---
id: rxn-2026-0341
type: reaction
compound_smiles: "CC(=O)Oc1ccccc1C(=O)O"
tags: [acetylation, late-stage-functionalization, project-X]
links: ["[[compound-aspirin]]", "[[campaign-2026-q2]]", "[[qm-job-8f2a]]"]
created_by: agent          # oder: human
source: eln://benchling/entry/48291
confidence: 0.87
---

# Acetylierung von Salicylsäure – Batch 14

Reaktion durchgeführt unter [[condition-standard-acetylation]].
Ausbeute 94%, Reinheit laut HPLC siehe [[analytics-hplc-2026-0341]].

Regioselektivität stimmt mit DFT-Vorhersage aus [[qm-job-8f2a]] überein.
```

**Warum das simples RAG schlägt:**
- **Frontmatter** macht jede Note strukturiert abfragbar (per Typ, SMILES, Tag, Confidence) – kein reines Text-Chunking.
- **Wikilinks** (`[[...]]`) kodieren echte fachliche Beziehungen (Reaktion → Verbindung → Kampagne → QM-Job → Analytik). Ein einfacher Python-Indexer (`python-frontmatter` zum Parsen + Regex/`markdown-it` für Links + **NetworkX** als Graphstruktur) baut daraus einen echten Graphen.
- **Retrieval = Graph-Traversal statt Top-k-Ähnlichkeit**: Ausgehend von einem Treffer (z. B. per Substruktur- oder Volltextsuche) werden 1–2 Hops im Graphen expandiert (Backlinks + Forward-Links), sodass der Agent den *fachlichen Kontext* bekommt (verwandte Reaktionen, deren Bedingungen, zugehörige Analytik, referenzierte QM-Jobs) statt zufällig ähnlicher Textfragmente. Optional zusätzlich Embeddings nur als *Einstiegspunkt* in den Graphen, nicht als alleiniges Retrieval-Prinzip.

**Speicherung & Governance:**
- Der gesamte Korpus lebt in einem **Git-Repository** – volle Versionshistorie, Diffs, Audit-Trail.
- **Von Menschen verfasste Notes** werden direkt committet.
- **Von Agenten erzeugte Notes** (`created_by: agent`) landen zunächst auf einem Feature-Branch/als Pull-Request und benötigen eine menschliche Freigabe, bevor sie in den validierten Hauptzweig gemerged werden – das spiegelt exakt die zuvor diskutierte GxP-Trennung (AI schlägt vor, Mensch validiert, Git protokolliert) und macht den PR-Review-Schritt zum Human-in-the-Loop-Gate.
- Temporal schreibt nach Abschluss eines QM-Jobs automatisch eine neue Note (`write_knowledge_node`-Activity) mit strukturiertem Ergebnis + Link auf Rohdaten – auch dieser Schreibvorgang läuft über den PR-Mechanismus.
- Der `knowledge-graph-query`-Skill exponiert die Graph-Traversal-Funktion als Tool für den MAF-Agenten; der `knowledge-graph-write`-Skill kapselt Note-Template + Git-Workflow.

## 5. End-to-End-Beispielfluss

Chemiker: *"Wie ist die zu erwartende Regioselektivität für die späte C–H-Funktionalisierung von Verbindung X, und hatten wir schon ähnliche Substrate?"*

1. MAF-Agent lädt `knowledge-graph-query`-Skill → traversiert den Graphen ausgehend von Verbindung X (Substruktur-Match) → liefert verwandte Reaktions-Notes, Bedingungen, historische Ausbeuten.
2. MAF-Agent lädt `qm-regioselectivity`-Skill → führt zuerst schnellen ML-Potential-Screen synchron aus.
3. Konfidenz niedrig → Agent ruft `submit_qm_job` → Temporal-Workflow startet DFT-Validierung auf HPC, MAF antwortet sofort mit Zwischenstand und Job-ID.
4. Temporal-Workflow läuft mehrere Stunden, überlebt Worker-Neustarts, Heartbeats verhindern Timeout-Fehlklassifikation.
5. Bei Abschluss: `write_knowledge_node` legt strukturierte Note an (PR-Pflicht) und triggert eine Benachrichtigung zurück in die MAF-Session/Teams.
6. Chemiker erhält finale Antwort inkl. Link auf die neue, nach Freigabe gemergte Knowledge-Graph-Note.

## 6. Deployment-Übersicht

> **Umgesetzt in Phase F5/F6** (siehe `deploy/`, ADR **D-047**/**D-048**). Der Ziel-Stack ist
> **OpenShift + HPC/Nextflow + ein internes OpenAI-kompatibles LLM** – nicht Azure AI Foundry/SLURM/
> Anthropic. §7/§8 (Entra durchgängig) bleiben gültig; einzige Anpassung: Managed Identity →
> **Entra Workload Identity Federation**.

- **Ein einziges, rootless Multi-Target-Image** (`deploy/Containerfile`, UBI9, UID 1001,
  arbitrary-UID-fähig für die OpenShift-SCC). Alle Rollen teilen dieselben Bits; `deploy/
  entrypoint.sh` wählt die Rolle über `CHEMCLAW_COMPONENT`:
  - **Front-Door-Service**: `uvicorn service.app:create_app` hinter einer OIDC-**Route**
    (FastAPI + SSE, POST-Streaming). HPA skaliert nur den zustandslosen Front-Door.
  - **Temporal-Worker**: `hpc-jobs` (wenige, schwer) und `background-jobs` (leicht: Sync/Reindex/
    Reports) – dieselben zwei Queues wie im Kern (D-006).
  - **MCP-Server**: die Fingerprint-Capability-Server (molfp/rxnfp).
- **LLM**: internes OpenAI-kompatibles Endpoint (`agents/llm_provider.py`, `llm_provider=
  openai_compatible`). Der Provider ist die *einzige* Stelle, die eine Client-Klasse importiert; ein
  Provider-Wechsel ist eine Config-Änderung. Das LLM nutzt **eine generische API-Credential** (nicht
  Entra) – die eine dokumentierte Ausnahme von der Entra-Durchgängigkeit.
- **HPC/Nextflow**: der QM-Job läuft real über den Seqera-Platform/Tower-REST-Launcher
  (`workflows/hpc/nextflow.py`, ADR **D-A5a**); nur `workflows/activities.py` dispatcht auf
  `hpc_launch_interface` (`mock` für CI/lokal, `nextflow` produktiv). Der `hpc-jobs`-Worker läuft
  dort, wo er den Launcher erreicht.
- **Temporal: self-hosted im Cluster** (ADR **D-A6a**), nicht Temporal Cloud – hält den durablen
  Kern innerhalb derselben OIDC-Vertrauensgrenze und vermeidet den Egress von Workflow-Payloads (die
  den Entra-`oid` tragen, D-043). Cloud bleibt ein Values-Swap (`temporal_api_key` statt mTLS-Trio).
- **Postgres/pgvector**: Operator- oder Managed-Instanz mit mTLS und `statement_timeout`. Migrationen
  laufen als **Pre-Deploy-Helm-Hook-Job** (`python -m calc.migrate`, D-034), bevor ein App-Container
  startet.
- **Eine Config-Quelle**: die `values.yaml`-`config:`-Sektion → ein `ConfigMap` → `CHEMCLAW_*`-Env,
  Schlüssel identisch zu `chemclaw/config.Settings`. Kein zweites Config-System im Cluster.
- **Nur drei Klartext-Secrets** (F6-T6): die generische LLM-API-Credential, Temporal-mTLS,
  HPC-Bridge-Credential. Alles andere ist Workload Identity Federation – kein Client-Secret at rest.
- **NetworkPolicy**: Default-Deny-Egress mit Allow-List (DNS/Postgres/Temporal/HTTPS). **Probes**:
  `/readyz`+`/healthz` für den Service; die Temporal-Poll-Schleife ist die Worker-Liveness.
- **Skills-Repo**: Git-Repo, read-only in Produktion gemountet, von Fachexperten gepflegt.
- **Knowledge-Graph-Repo**: separates Git-Repo, PR-Workflow, CI-Job baut/validiert den Graphen
  (Broken-Link-Check, Frontmatter-Schema-Validierung) bei jedem Push.
- **MCP-Server**: bleiben die Integrationsschicht (deterministische Capability); Skills rufen sie bei
  Bedarf auf.
- **Observability** (F6-T5): `otel_enabled` + `otel_endpoint` verdrahten OTLP zum In-Cluster-Collector.
- **CI/CD** (`.github/workflows/deploy.yml`): Image-Build + Entrypoint-Smoke, `helm lint`,
  `helm template | kubeconform`; der Rollout ist auf den Default-Branch beschränkt.

## 7. Identity & Authentication: Entra ID durchgängig

Anforderung: **eine** Identität pro Nutzer, die sich konsequent durch den gesamten Stack zieht – wichtig sowohl für Security als auch für den GxP-Audit-Trail ("wer hat wann welchen Vorschlag/Job ausgelöst"). Der Reifegrad der Entra-ID-Integration ist aber pro Komponente unterschiedlich – das muss man bei der Umsetzung einplanen:

| Komponente | Entra-ID-Integration | Reifegrad |
|---|---|---|
| **Chemiker → Copilot Studio/Teams** | Native Entra-ID-SSO (M365-Standard) | ✅ Nativ, keine Zusatzarbeit |
| **Copilot Studio → MCP-Server** | OAuth 2.0 über Power-Platform-Connector, inkl. Dynamic-Client-Registration-Option | ✅ Nativ |
| **Eigener MCP-Server (FastMCP)** | `AzureProvider`/`BearerAuthProvider` in FastMCP validiert Entra-JWTs direkt (JWKS-Endpoint des Tenants); On-Behalf-Of-Flow reicht die Nutzeridentität an nachgelagerte Systeme (Graph, interne APIs) weiter | ✅ Gut unterstützt, aber: Azure unterstützt keine Dynamic Client Registration → OAuth-Proxy-Pattern nötig; **Confused-Deputy-Risiko** beachten (MCP-Server als OAuth-Client *und* Service zugleich) – Consent-Screen/Audience-Checks konsequent implementieren |
| **MAF-Agent-Hosting (Azure AI Foundry/Container Apps)** | Managed Identity / Entra-ID-App-Registration | ✅ Nativ |
| **Skills-/Knowledge-Graph-Git-Repo** | Azure DevOps Repos (nativ Entra-ID) oder GitHub Enterprise Cloud mit Entra-ID-SAML-SSO + OIDC für Git-Operationen | ✅ Gut, Azure DevOps am nahtlosesten |
| **Temporal (Human-Zugriff, Web-UI)** | SAML-SSO direkt mit Entra ID als IdP konfigurierbar ("Continue with Microsoft" oder eigene Entra-ID-Enterprise-App) | ✅ Nativ für Menschen |
| **Temporal (Worker/SDK/Service-zu-Service)** | **Kein natives Entra-ID-Token-Auth.** Temporal Cloud authentifiziert Worker/Clients über **mTLS-Zertifikate oder API-Keys**, nicht über Entra-JWTs. Bei **self-hosted** Temporal lässt sich ein JWT-Authorizer/Claim-Mapper gegen den Entra-ID-JWKS-Endpoint (`login.microsoftonline.com/{tenant}/discovery/v2.0/keys`) konfigurieren – näher an "durchgängig Entra ID", aber Eigenbetrieb | ⚠️ Kompromiss nötig |
| **HPC/SLURM-Zugriff** | Klassische Scheduler sprechen kein OIDC/Entra ID. Erfordert einen **Identity-Bridging-Service**: Entra-ID-Token (mit Nutzer-Claim) → intern gemappt auf HPC-Service-Account/Kerberos-Ticket, mit Logging der Zuordnung für Audit-Zwecke | ⚠️ Eigenentwicklung nötig, größte Lücke |

**Empfehlung für die Umsetzung:**
1. **Wo Entra ID nativ geht (MAF, Copilot Studio, MCP-Server, Git-Repos, Temporal-Web-UI): konsequent nutzen** – keine Extra-Rechtfertigung nötig, ist der Standardweg.
2. **Für Temporal service-seitig**: mTLS-Zertifikate (Azure Key Vault-verwaltet) für Worker/Client-Authentifizierung nutzen; die *Autorisierung*, welcher Chemiker welchen Job ausgelöst hat, wird nicht auf Transport-Ebene, sondern als **Claim im Workflow-Input** mitgeführt (der Entra-ID-`oid`/`upn` des Nutzers wird beim `submit_qm_job`-Aufruf aus dem MAF-Kontext übernommen und als Teil des Workflow-Payloads persistiert) – das erhält die Audit-Fähigkeit, ohne Temporal-intern Entra-Tokens validieren zu müssen.
3. **Für HPC**: einen schlanken Bridging-Service bauen, der ausschließlich Entra-ID→HPC-Identität mapped und jede Zuordnung protokolliert; dieser Service ist der einzige Punkt im System, der beide Identitätswelten kennt.
4. **Für den MCP-Server**: OAuth-Proxy-Pattern (da Azure kein Dynamic Client Registration unterstützt) + On-Behalf-Of-Flow, damit Tools wie `extract_reaction_from_eln` mit der Berechtigung des anfragenden Chemikers (nicht mit einem generischen Service-Principal) auf das ELN zugreifen – wichtig für Data-Governance und Nachvollziehbarkeit.

> **Umsetzungsstand (Phase F4, ADR D-042…D-046).** Auf OpenShift ersetzt **Entra Workload Identity
> Federation** die Managed Identity: der Pod tauscht sein projiziertes ServiceAccount-JWT gegen ein
> Entra-Token (`agents/identity/workload.py`) – kein Client-Secret at rest. Bereits gebaut und
> offline (Fake-Endpoint) getestet: Front-Door-OIDC-Validierung (`service/auth.py`, Audience-/
> Issuer-Check gegen den Tenant-JWKS), **eine** Autorisierungsstelle für teure Trigger
> (`agents/authz.py::authorize_trigger`), die reject-if-absent-Kernregel für user-getriggerte
> Workflows (`require_actor`, der Entra-`oid` als Pflicht-Claim im Payload, D-043), der OBO-Austausch
> (`agents/identity/obo.py`, wired-but-dormant bis zur ersten user-scoped Quelle) sowie beide
> Nicht-Entra-Brücken: Temporal-mTLS/API-Key (`chemclaw/temporal_client.py`) und der HPC-Bridge, der
> jede `oid`→HPC-Identität-Zuordnung protokolliert (`agents/identity/hpc_bridge.py`). Offene
> Live-Kanten benötigen einen echten Tenant/Broker/Cluster.

## 8. Mehrbenutzerfähigkeit & differenzierte Rechte (Multi-Tenancy/RBAC)

**Kurzfassung:** Reine Nebenläufigkeit (viele gleichzeitige Nutzer) ist in allen vier Schichten gut gelöst. **Differenzierte Rechte pro Nutzer/Rolle sind dagegen nicht automatisch vorhanden** – sie müssen an mehreren Stellen bewusst nachgerüstet werden. Schicht für Schicht:

| Schicht | Nebenläufigkeit (viele Nutzer gleichzeitig) | Differenzierte Rechte (unterschiedliche Rollen) |
|---|---|---|
| **MAF-Agents** | ✅ Zustandslos hostbar (Azure AI Foundry/Container Apps), horizontal skalierbar, eine Session pro Nutzer | ⚠️ **Nicht automatisch**: `SkillsProvider` lädt standardmäßig alle Skills aus dem Verzeichnisbaum, unabhängig vom Nutzer. Muss um eine rollenbewusste Filterung erweitert werden (z. B. eigener Context-Provider, der aus den Entra-ID-Claims der Session – App-Rollen/Gruppenmitgliedschaft – ableitet, welche Skills überhaupt advertised werden) |
| **MCP-Server (eigen, FastMCP)** | ✅ Zustandslos, horizontal skalierbar | ✅ **Bester Ansatzpunkt**: Da jeder Tool-Call bereits das Entra-Token des aufrufenden Nutzers trägt (OBO-Flow), lässt sich hier pro Tool-Aufruf autorisieren (Rollen-/Gruppen-Claim prüfen, bevor z. B. `submit_qm_job` oder `extract_reaction_from_eln` ausgeführt wird). **Copilot Studios DLP wirkt nur serverweit, nicht pro Tool** – feingranulare Rechte dürfen sich nicht darauf verlassen, sondern müssen im eigenen MCP-Server implementiert sein |
| **Temporal** | ✅ Für genau diesen Zweck gebaut: Namespaces isolieren Traffic/Konfiguration, Tausende gleichzeitige Workflow-Executions sind Standard | ⚠️ **Temporal-eigenes RBAC (Account-Rollen wie Developer/Read-Only/Custom Roles, per SCIM aus Entra ID synchronisierbar) regelt nur den *operativen* Zugriff** (wer darf Workflows im Temporal-Dashboard einsehen/verwalten) – **nicht**, ob ein bestimmter Chemiker einen bestimmten teuren DFT-Job auslösen darf. Diese fachliche Autorisierung muss *vor* dem `submit_qm_job`-Aufruf in MCP/MAF geprüft werden; die Entra-ID des auslösenden Nutzers wird danach nur noch als Claim im Workflow-Payload mitgeführt (Audit, nicht Zugriffskontrolle). Empfehlung: **Namespace pro Team/Projekt** (Temporals eigene Best Practice für Multi-Tenancy) plus HPC-seitige Quotas/QOS, damit ein Nutzer nicht das gemeinsame Compute-Budget anderer blockiert |
| **Knowledge-Graph (Git)** | ✅ Git ist für viele gleichzeitige menschliche Autoren gebaut (Branches/PRs); bei vielen *agentengenerierten* Schreibzugriffen gleichzeitig ggf. Warteschlange vor dem PR-Merge einplanen | ⚠️ **Größte Lücke**: Git kennt nativ nur Repo-weite Zugriffsrechte, kein Note-Level-ACL. Wenn nicht jeder Chemiker jede Notiz sehen darf (z. B. projektvertrauliche Kampagnen), reicht ein einzelnes Repo nicht. Zwei Optionen: (a) Repos entlang Vertraulichkeitsgrenzen aufsplitten und Entra-ID-Gruppen repo-weise berechtigen (einfach, aber grob), oder (b) eine Serving-/Query-Schicht vor den Graphen setzen, die `visibility`-Metadaten im Frontmatter jeder Notiz gegen die Entra-Gruppen des anfragenden Nutzers prüft, bevor Ergebnisse an den Agenten zurückgehen (flexibler; das ist ohnehin der natürliche Ort, weil der `knowledge-graph-query`-Skill/Tool schon dort sitzt) |
| **Skills-Repo** | ✅ Read-only Mount, unkritisch bei vielen Lesern | ⚠️ Falls manche Skills rollenspezifisch sein sollen (z. B. Freigabe-/Override-Skills nur für QA), braucht es dieselbe rollenbewusste Filterung wie bei MAF oben – sonst sieht jeder Nutzer jeden Skill |

**Zentrale Empfehlung:** Rechteprüfung nicht über die Komponenten verstreuen, sondern **so weit wie möglich an einer Stelle bündeln** – konkret im eigenen MCP-Server, weil dort ohnehin bei jedem Aufruf ein Entra-ID-Token mit Rollen-/Gruppen-Claims vorliegt. Von dort aus:
- Tool-/Skill-Sichtbarkeit für MAF ableiten (welche Skills advertised werden),
- Tool-Ausführung autorisieren (darf dieser Nutzer diesen Job/Extraktionslauf auslösen),
- Wissensgraph-Antworten filtern (welche Notes darf dieser Nutzer sehen).

Das hält die Rollenlogik an einem Ort wartbar, statt sie in MAF, Temporal-Workflows und Git-Berechtigungen parallel pflegen zu müssen.

## 9. Wissensmanagement: Episodisches, semantisches und prozedurales Gedächtnis

### Der große Wurf in einem Satz

Statt *einem* Wissensgraphen baut das System **drei Gedächtnisebenen mit unterschiedlicher Granularität**, verbunden durch einen automatischen Destillationsprozess, der Wissen von "was ist in Projekt X passiert" zu "was haben wir generell über Amidkupplungen gelernt" hochstuft – exakt die Trennung, auf die sich Kognitionswissenschaft (Tulving, 1972) und die aktuelle Agent-Memory-Forschung konvergiert haben (kanonische Referenz: CoALA, Princeton/CMU 2023, seit 2025/26 De-facto-Taxonomie bei Letta, Mem0, Zep u. a.):

| Ebene | Kognitionswiss. Analogie | Inhalt | Granularität |
|---|---|---|---|
| **Prozedural** | "Wie man etwas tut" | **Haben wir schon**: Agent Skills (Abschnitt 3) | projektübergreifend, Handlungswissen |
| **Episodisch** | "Was ist wann passiert" | **Neu zu ergänzen**: projektspezifische, zeitlich/kausal verkettete Experiment-Historie | pro Projekt, faktentreu, mit Kontext |
| **Semantisch** | "Was haben wir daraus gelernt" | **Neu zu ergänzen**: destillierte, transferierbare Heuristiken | projektübergreifend, generalisiert |

Das ist die eigentliche Pointe: **Die prozedurale Ebene existiert in unserer Architektur bereits** (die Skills aus Abschnitt 3) – uns fehlen nur die episodische und die semantische Ebene, und beide lassen sich als *neue Notiz-Typen im bereits bestehenden Markdown-Graphen* nachrüsten, nicht als neue Systeme.

### Ebene 1 – Episodisches Gedächtnis: die Projekt-Chronik

Die atomaren Reaktions-/Analytik-Notes aus Abschnitt 4 sind der Rohstoff. Neu ist ein Aggregations-Notiztyp:

**`campaign`-Notes** fassen eine Sequenz zusammengehöriger Experimente zu einer Erzählung zusammen – genau die vom Nutzer gewünschte automatische "Warum"-Extraktion. Der Trick, um das *ohne* neue Infrastruktur zu bauen:

1. **Kettenerkennung ist bereits gelöst.** Die in Abschnitt 10 übernommenen Fingerprint-Tools (`mcp-molfp`) verknüpfen Verbindungen strukturell. Wenn das Produkt von Experiment A (per SMILES/Fingerprint) dem Edukt von Experiment B entspricht, ist das eine kausale Kante – *automatisch*, ohne dass jemand eine Kampagne manuell taggen muss. Diese Ketten bilden das Rückgrat der episodischen Ebene.
2. **Ein periodischer Job auf einer eigenen Temporal-Task-Queue** (`background-jobs`, getrennt von der HPC-Task-Queue – siehe Abschnitt 12.1) liest neue, noch nicht in eine `campaign`-Note eingebettete Experimentketten eines Projekts und lässt einen Skill (`campaign-narrative-synthesis`) daraus eine Erzählung schreiben: *"Experiment 1 testete Bedingung X, Ausbeute niedrig wegen Nebenprodukt Y → Experiment 2 senkte die Temperatur, um Y zu unterdrücken → Experiment 3 bestätigte...".* Jede Aussage zitiert die zugrunde liegenden Experiment-Notes (keine freie Erfindung).
3. Diese Note ist **initial `created_by: agent`** und durchläuft denselben PR-Review wie jede andere agentengenerierte Notiz (Abschnitt 4) – der Projektchemiker bestätigt oder korrigiert die rekonstruierte Begründung, bevor sie als verlässlich gilt.

Ergänzend eine **`project-summary`-Note** pro Projekt: ein periodisch regenerierter, kurzer Statusdigest (Route-Historie, offene Fragen, letzte Kampagnen) – bewusst als einfache Markdown-Note gehalten, *nicht* als eigene Wiki-App (siehe Abschnitt 10, warum wir den vollen chemclaw2-Wiki-Ansatz nicht übernehmen).

### Ebene 2 – Semantisches Gedächtnis: die projektübergreifenden Playbooks

Das ist der eigentliche Hebel für "Amidkupplungs-Learnings von Projekt A auf Projekt B übertragen":

**`playbook`-Notes** leben in einem eigenen Namensraum, **getaggt nach Transformationstyp/Problemklasse, nicht nach Projekt** (`amide-coupling-epimerization`, `workup-impurity-removal-basic-hydrolysis`, …). Sie entstehen durch einen zweiten periodischen Job (`cross-project-distillation`):

1. Gruppiere episodische Notes **über alle Projekte hinweg** nach Reaktionstyp (Tag) *und* struktureller Ähnlichkeit (dieselben Fingerprint-Tools, jetzt projektübergreifend eingesetzt – eine neue Anwendung derselben bereits vorhandenen Tools).
2. Ein Skill (`playbook-distillation`) liest alle episodischen Instanzen einer Gruppe (z. B. alle Amidkupplungs-Experimente aus fünf verschiedenen Projekten der letzten zwei Jahre) und destilliert wiederkehrende Muster: *"Bei elektronenarmen Anilinen führte Standardaktivierung X in 3 von 4 Projekten zu Epimerisierung; Lösung Y (niedrigere Temperatur / alternatives Kupplungsreagenz) behob das in Projekt B und D."*
3. Jede Aussage im Playbook trägt **Belegverweise zurück auf die konkreten episodischen Instanzen** (nicht anonymisiert wegdestilliert) – das Playbook bleibt nachprüfbar statt zur Blackbox zu werden.
4. **Menschliche Freigabe zwingend** (PR-Gate, idealerweise durch einen erfahrenen Prozesschemiker/eine Fachfunktion, nicht den Projektchemiker allein) – Playbooks sind der Ort, an dem Fehlverallgemeinerung am teuersten wäre.

### Die vierte Quelle: Nutzerinteraktion schließt den Kreis

Der Nutzer nennt explizit "Nutzerinteraktion" als Datenquelle – das ist mehr als nur ELN/LIMS/Berichte zu indizieren: **Jede vom Chemiker bestätigte oder korrigierte Agentenantwort wird selbst zu einer neuen episodischen Notiz.** Fragt ein Chemiker "warum hatten wir diese Verunreinigung", bestätigt der Agent eine Hypothese und der Chemiker sagt "ja, das war's" (oder korrigiert sie), wird genau das festgehalten – mit demselben `created_by`/Validierungs-Statusfeld wie alle anderen agentengenerierten Notizen. Das System lernt so nicht nur aus dokumentierten Experimenten, sondern aus der eigenen Nutzung. Kein neuer Mechanismus nötig – derselbe Note-Typ, derselbe PR-Gate.

### Retrieval: episodisch + semantisch kombiniert, aber sichtbar getrennt

Bei einer projektspezifischen Frage oder einem Berichtsauftrag traversiert der Agent **beide Ebenen** und hält sie in der Antwort auseinander:
- **Episodisch** (dieses Projekt): konkrete Historie, Zahlen, Zitate aus den eigenen Experimenten.
- **Semantisch** (andere Projekte): "Ein generelles Muster aus anderen Projekten legt nahe, dass…", mit Verweis auf das Playbook und optional die Ursprungsprojekte.

Diese Trennung ist für wissenschaftliches Vertrauen entscheidend – ein Chemiker muss wissen, ob eine Aussage projektspezifisch belegt oder aus Analogie übertragen ist. Für Berichte übersetzt sich das eins zu eins in einen MAF-Workflow-Knoten pro Berichtsabschnitt, der jeweils explizit macht, aus welcher Ebene die Information stammt.

### Warum das trotzdem einfach bleibt

Kein einziges neues Subsystem – nur:
- **2 neue Notiz-Typen** (`campaign`, `playbook`) mit klaren Frontmatter-Feldern (Evidenzverweise, Validierungsstatus).
- **2 neue Skills** (`campaign-narrative-synthesis`, `playbook-distillation`), die dieselbe Skill-Infrastruktur aus Abschnitt 3 nutzen.
- **2 periodische Jobs auf einer eigenen Temporal-Task-Queue**, die bereits vorhandene Bausteine (Fingerprint-Tools aus Abschnitt 10, PR-Gate aus Abschnitt 4, LLM-Aufrufe über MAF) neu kombinieren.
- **Kein neues Datenbanksystem, kein neues Auth-Modell, kein neuer Orchestrator.**

Der "große Wurf" liegt nicht in neuer Infrastruktur, sondern darin, dem bereits gebauten Wissensgraphen eine **explizite Drei-Ebenen-Struktur mit einem automatischen Aufwärts-Destillationspfad** zu geben – vom Einzelexperiment über die Projekt-Erzählung bis zum projektübergreifenden Playbook, jede Stufe zitierfähig auf die darunterliegende.

## 10. Adaptionen aus chemclaw2 (externes Repo)

Das Gründungsdokument von chemclaw2 (`chemclaw2_features.md`) verfolgt bewusst ein anderes Grundprinzip als unsere Architektur ("off-the-shelf over self-built", Postgres-first, Claude Agent SDK statt MAF) – trotzdem gibt es mehrere konkrete, kleine Bausteine, die sich **ohne Bruch mit den bisherigen Entscheidungen** übernehmen lassen und echte Lücken in unserem Design schließen.

### Lohnenswert (niedrige Zusatzkomplexität, echter Mehrwert)

1. **Fingerprint-MCP-Server für Ähnlichkeits-/Substruktursuche** (größter Fund). Unsere Architektur hatte bisher keine konkrete Antwort auf "finde strukturell ähnliche Verbindungen/Reaktionen" – der Wissensgraph macht Beziehungssuche gut, aber keine chemische Ähnlichkeitssuche. chemclaw2s Lösung ist minimal-komplex und direkt übertragbar:
   - Zwei winzige MCP-Server (`mcp-molfp`, `mcp-rxnfp`, je ~100 LOC Python): Morgan/ECFP4 (RDKit, radius 2, 2048 Bit) für Moleküle, DRFP für Reaktionen – beides deterministisch, keine GPU, keine Modell-Infrastruktur nötig.
   - Speicherung als `bit(2048)`-Spalten in Postgres, Tanimoto-Ähnlichkeit direkt in SQL, HNSW-Index (pgvector ≥0.7, `bit_hamming_ops`) für schnelle Nearest-Neighbor-Vorfilterung.
   - **Einordnung in unsere Architektur**: als zusätzliche MCP-Tools, die vom `knowledge-graph-query`-Skill bzw. einem neuen `reaction-search`-Skill aufgerufen werden – ergänzt den Graphen, ersetzt ihn nicht. Aufwand: zwei kleine Python-Services + eine Postgres-Tabelle.

2. **Postgres mit Row-Level-Security als Serving-/Such-Layer vor dem Git-Wissensgraphen** – *mit Vorbehalt, siehe Abschnitt 12.2.* Löst die Note-Level-ACL-Lücke, aber ist nur dann seinen Preis (Sync-Pipeline, zweite Quelle der Wahrheit) wert, wenn echte, kombinatorische Vertraulichkeitsanforderungen bestehen. Da Postgres/pgvector durch Punkt 1 ohnehin für die Fingerprint-Suche läuft, ist die Grenzkosten für zusätzliche Tabellen gering – die Sync-Logik bleibt aber ein echtes neues Teilsystem.

3. **~~pg-boss für kleine Jobs~~ → revidiert, siehe Abschnitt 12.1**: Da wir ohnehin einen Temporal-Cluster für die HPC/QM-Jobs betreiben, ist es einfacher, auch die kleinen asynchronen Aufgaben (ELN-Sync, Re-Indexierung, Benachrichtigungen) über eine **separate Temporal-Task-Queue** laufen zu lassen statt ein zweites Queue-System (pg-boss) einzuführen. Details und Begründung in Abschnitt 12.1.

4. **Bi-temporale Felder** (`valid_from`/`valid_to`) als zusätzliche Frontmatter-Attribute auf Knowledge-Graph-Notes. Trivialer Zusatzaufwand, aber wichtig für GxP-adjacente Nachvollziehbarkeit ("was wussten wir zum Zeitpunkt X").

5. **Arbeitsprinzip "off-the-shelf over self-built" + explizite Deferred-Liste mit Trigger-Bedingungen.** Keine Technologie, sondern eine Disziplin: Jede Fähigkeit muss durch eine gepflegte externe Library/einen Standard gedeckt sein; Eigenentwicklung nur, wenn eine klar benannte Bedingung eintritt. Lohnt sich als Leitplanke für das gesamte Projekt, gerade weil unsere Architektur (MAF + Temporal + Skills + Wissensgraph + RLS) sonst leicht zum Over-Engineering neigen kann.

### Nicht übernehmen (würde Komplexität erhöhen oder widerspricht bereits getroffenen Entscheidungen)

- **Claude Agent SDK statt MAF**: chemclaw2 nutzt eine andere Orchestrierungs-Basis. Kein Grund, die bereits getroffene MAF-Entscheidung zu revidieren – beide sind valide, aber ein Wechsel mitten in der Architektur würde nur Reibung erzeugen.
- **Der volle Wiki-Ansatz** (Next.js-App, Tiptap-Editor, Freshness-Tracking, Contradiction-Backlog, Auto-Regeneration): inhaltlich inspirierend, aber ein eigenes Produkt mit eigenem Frontend – deutliche Komplexitätssteigerung. Bezeichnend: **selbst chemclaw2 deferred die anspruchsvollsten Teile davon** (Auto-Regeneration-Daemon, Contradiction-Auto-Detection) im eigenen v1-Scope. Das bestätigt eher, unseren Git+Markdown-Ansatz schlank zu halten, als ihn zur Wiki-App auszubauen.
- **Neo4j/dedizierte Graph-DB**: chemclaw2 verzichtet bewusst darauf ("Postgres + Foreign Keys reichen"), deckt sich mit unserer Entscheidung, ohne separate Graph-DB auszukommen.

## 12. Klärungen & Vereinfachungen (Follow-up)

### 12.1 Braucht es pg-boss neben Temporal? Nein – revidiert.

Guter Einwand, und die Antwort ändert eine frühere Empfehlung: **Nein, sobald ein Temporal-Cluster ohnehin betrieben wird, sollte er auch die kleinen asynchronen Jobs übernehmen.** Meine vorherige pg-boss-Empfehlung war unreflektiert von chemclaw2 übernommen – dort ergibt sie Sinn, weil das Team dort *gar keinen* Temporal-Cluster betreiben will (deren explizite Prämisse: "Postgres-first, keine zusätzlichen Services"). Das trifft auf unsere Situation nicht zu, da wir Temporal für die QM/HPC-Jobs sowieso brauchen (das war die ursprüngliche Anforderung).

- Temporal ist kein "Nur-für-lange-Jobs"-System – es wird produktiv für Sekunden- bis Tage-Workflows gleichermaßen eingesetzt. Ein "Re-Indexiere den Wissensgraphen nach diesem Merge"-Job ist ein völlig normaler, kurzer Temporal-Workflow.
- **Trennung erfolgt über Task Queues, nicht über ein zweites System**: eine `hpc-jobs`-Queue mit Workern, die Zugriff auf SLURM haben (wenige, teure Worker), und eine `background-jobs`-Queue mit leichten, austauschbaren Workern für ELN-Sync, Re-Indexierung, Benachrichtigungen. Beide teilen sich Cluster, Observability, Auth-Setup (Abschnitt 7) und Namespace-Struktur (Abschnitt 8).
- **Ergebnis**: ein Ausführungssystem weniger zu betreiben als in der vorherigen Version dieses Dokuments (kein pg-boss, keine zweite Postgres-Queue-Konfiguration, kein zweites Retry-/Monitoring-Setup). Einziger Nachteil: Temporal-Workflows haben etwas mehr Autoren-Ceremony (Workflow/Activity-Trennung, Determinismus-Regeln) als ein simpler Fire-and-Forget-Job – das ist ein kleiner Autoren-Mehraufwand pro Job, keine operative Mehrkomplexität.
- Bleibt MAFs eigene Durable Extension überhaupt relevant? Nur noch für einen sehr schmalen Fall: sehr lange *Konversationspausen* (Chat wartet tagelang auf menschliche Rückmeldung), falls das MAF-Session-Handling das nicht schon ausreichend abdeckt. Für alles Job-artige gilt: Temporal, ein System, fertig.

### 12.2 Lohnt sich das Postgres-Spiegeln des Wissensgraphen wirklich?

Berechtigter Zweifel – die ehrliche Antwort ist "kommt darauf an", und zwar auf eine konkrete Frage: **Gibt es echte, kombinatorische Vertraulichkeitsanforderungen zwischen Projekten, oder ist breiter interner Lesezugriff akzeptabel?**

- **Wenn breiter Lesezugriff für R&D-Chemiker grundsätzlich akzeptabel ist** (was angesichts des expliziten Ziels aus dem vorigen Abschnitt – projektübergreifendes Lernen – ohnehin nahegelegt wird): **Postgres-Spiegelung weglassen.** Git + ein einfacher lokaler Such-Layer (Volltext über die Dateien, ggf. Embeddings direkt aus den Markdown-Dateien berechnet und in einer einzigen, ungeschützten Tabelle abgelegt) reicht, Autorisierung bleibt grob auf Repo-Ebene. Kein Sync-Pipeline-Risiko, keine zweite Quelle der Wahrheit.
- **Wenn echte Vertraulichkeit zwischen Projekten benötigt wird** (z. B. frühe Discovery-Projekte mit Wettbewerbsrelevanz, oder Partner-/Lizenzprojekte mit vertraglichen Datentrennungspflichten): Repo-Splitting entlang statischer Grenzen stößt schnell an Grenzen, sobald Nutzer in unterschiedlichen Kombinationen mehreren Projekten zugeordnet sind (kombinatorisches ACL-Problem) – **dann lohnt sich RLS tatsächlich**, weil es genau dieses Problem löst, ohne dass man für jede Projekt-Kombination ein eigenes Repo pflegen müsste.
- **Wichtige Kostenkorrektur**: Da wir Postgres/pgvector durch die Fingerprint-Suche (Abschnitt 10) ohnehin betreiben, ist "ein paar weitere Tabellen" kein neues *System* – die tatsächlich neue Komplexität ist ausschließlich die **Sync-Pipeline** (Git-Webhook → Parsen → Upsert, Fehlerbehandlung bei Sync-Ausfällen, Staleness-Fenster). Das ist der Teil, der eine bewusste Entscheidung braucht, nicht "noch eine Datenbank".
- **Praktische Empfehlung**: mit der einfachen Variante (kein RLS-Mirror) starten und erst auf die Postgres-RLS-Variante wechseln, wenn ein konkreter Vertraulichkeitsfall das erfordert – passend zum in Abschnitt 10 übernommenen "defer until measured"-Prinzip.

### 12.3 Chemische Ähnlichkeit/Substruktursuche: MCP-Tool oder Skill?

Beides – aber für unterschiedliche Dinge, nicht als Alternative:

- **MCP-Tool** = die eigentliche Berechnung/Abfrage: `find_similar_molecules(smiles, top_k)`, `find_similar_reactions(...)`, `find_substructure_matches(...)`. Das ist deterministischer, zustandsloser Code (RDKit-Aufruf + SQL-Query) – die klassische Definition eines Tools.
- **Skill** = das Urteilsvermögen darüber, *wann* und *wie* diese Tools sinnvoll eingesetzt werden: Ab welchem Tanimoto-Score gilt ein Treffer als relevantes Präzedens? Wann Ähnlichkeits- statt Substruktursuche verwenden? Wie mit Metadatenfiltern kombinieren ("ähnlich zu X, aber nur Projekt Y, nur logP < 3")? Wie Ergebnisse einem Chemiker präsentieren, wenn mehrere strukturell unterschiedliche, aber funktional ähnliche Treffer existieren?

Diese Trennung ist genau das allgemeine MCP-vs-Skills-Muster (MCP liefert die Fähigkeit, Skills liefern das Domänenwissen, wie man sie benutzt) und war in Abschnitt 10 schon angelegt (`reaction-search`-Skill über den Fingerprint-Tools), sollte hier aber explizit gemacht werden: **ohne den Skill ruft der Agent die Tools zwar korrekt auf, trifft aber keine guten fachlichen Entscheidungen darüber, wann/wie.**

### 12.4 Tailor-made ELN-Integrationen: das Adapter-Muster

Die Individualität jeder ELN-Instanz lässt sich nicht wegabstrahieren – aber man kann die Individualität auf eine dünne, austauschbare Schicht begrenzen, statt sie durchs ganze System durchsickern zu lassen:

- **Stabiler Zielschema-Kern**: Alles oberhalb der Integrationsschicht (Skills, Wissensgraph, Fingerprint-Suche) kennt ausschließlich das kanonische ORD-basierte Schema aus Abschnitt 3/4 – nie die Eigenheiten einer bestimmten ELN-Konfiguration.
- **Ein dünner Adapter pro ELN-Instanz**, mit einem festen, immer gleichen Vertrag (unabhängig davon, wie speziell die jeweilige ELN-Konfiguration ist): `fetch_new_entries(since) -> RawEntry[]` und `map_to_ord(raw_entry) -> OrdReaction`. Die Individualität steckt ausschließlich in der Implementierung dieser zwei Funktionen, nie in der Form des Vertrags. Das macht jeden neuen Adapter überschaubar review- und austauschbar, selbst wenn die zugrunde liegende ELN-Logik hässlich ist.
- **Hybrid pro Feld, nicht pro System** (bereits in der ursprünglichen Recherche angelegt, hier konkretisiert): Für Felder mit sauberer strukturierter API (z. B. Benchlings Reaction-Editor-Felder) deterministisches Mapping; für unstrukturierten Freitext (Prozedur-Kommentare, gescannte Anhänge) die LLM-Extraktion aus Abschnitt 4 als Fallback – innerhalb *desselben* Adapters, Feld für Feld entschieden.
- **Nicht vorab eine universelle ELN-Abstraktion bauen.** Mit einer, maximal zwei tatsächlich genutzten ELN-Quellen anfangen, das Adapter-Muster dort etablieren, erst bei der dritten Quelle prüfen, ob sich gemeinsame Bausteine (z. B. gemeinsame Retry-/Pagination-Logik) verallgemeinern lassen – auch das im Sinne von "defer until measured".

### 12.5 Tabular Foundation Models als zusätzliches Tool

Sehr sinnvoll, und ein Fund, der gut in die bereits aufgebaute Wissensebene passt. LLMs sind bekanntermaßen schwach in präziser numerischer Regression; genau dafür sind **tabuläre Foundation-Modelle** (TabPFN v2/2.5, RealTabPFN, TabICL v2 u. a.) gebaut: Transformer, die per In-Context-Learning eine Bayes'sche Posterior-Vorhersage approximieren, **ohne Training auf dem konkreten Datensatz** – bei bis zu ~100.000 Zeilen/2.000 Features (TabPFN-2.5) state-of-the-art, mit kalibrierter Unsicherheit, in einem einzigen Forward-Pass.

- **Warum das hier besonders gut passt**: Prozessentwicklungsdaten sind fast immer genau der Fall, für den diese Modelle gebaut sind – kleine bis mittlere Tabellen (Dutzende bis wenige Tausend DoE-Zeilen: Temperatur, Katalysatorbeladung, Lösungsmittel, Äquivalente → Ausbeute/Reinheit), kein GPU-Training, kein Feature-Engineering nötig.
- **Schöne Verzahnung mit Abschnitt 9**: Der Kontext, den man dem tabularen Modell füttert, ist genau die episodische Projekt-Historie – die bereits über die Fingerprint-/Metadaten-Suche abfragbaren Reaktionsdaten eines Projekts (oder projektübergreifend über die Playbook-Ebene) werden direkt als In-Context-Trainingsdaten verwendet. Kein separates ML-Pipeline-Engineering.
- **Integration**: als weiteres, sehr kleines MCP-Tool (`predict_from_tabular_context(rows, target_column, query_row) -> prediction + uncertainty`) – ähnlich minimal wie die Fingerprint-Server aus Abschnitt 10, kein zusätzliches Subsystem. Ergänzt die QM/DFT-Modelle (mechanistisch, first-principles) um eine schnelle, rein datengetriebene Vorhersage aus den eigenen historischen Daten – guter erster Screening-Schritt vor einer teuren DFT-Eskalation (passt zum Tiering-Konzept aus Abschnitt 2).
- **Zu prüfen vor Einsatz**: Lizenzierung variiert zwischen den TabPFN-Generationen (die neueren, leistungsstärksten Gewichte – RealTabPFN – stehen z. T. unter einer Non-Commercial-Lizenz); vor Produktivsetzung im kommerziellen Pharma-Kontext die jeweils aktuelle Lizenz der konkret gewählten Modellversion prüfen.

## 14. Ergänzende Skill-Ideen für den Katalog

Über die bisher skizzierten Skills hinaus lohnt sich ein Blick auf weitere Fähigkeitskategorien, die den Skill-Katalog aus Abschnitt 3 sinnvoll abrunden würden – als eigenständig zu implementierende Bausteine, nicht als übernommener Code:

| Kategorie | Mögliche Fähigkeit | Bezug zu unserer Architektur |
|---|---|---|
| Literatur-/Dokumenten-Extraktion | PDF-Literatur in strukturierte Markdown-/Tabellenform überführen (inkl. Grafiken/Formeln), daraus Reaktionsdaten (Edukte, Bedingungen, Ausbeuten) und Charakterisierungsdaten (NMR/HRMS/HPLC/Schmelzpunkt/ee) extrahieren | Direkt einsetzbar für die ELN-/Literatur-Extraktion nach ORD-Schema (Abschnitt 4, 12.4) – deckt den Fall "Freitext/PDF → strukturierte Reaktionsdaten" ab |
| Schnelle semiempirische Vorrechnung | Geometrieoptimierung über eine schnelle semiempirische Methode (z. B. xTB) als Vorfilter, bevor teuer eskaliert wird | Ergänzt die Temporal-orchestrierten QM/DFT-Jobs (Abschnitt 2) – passt exakt zum dort skizzierten Tiering-Konzept (schnell/günstig zuerst, DFT nur bei Bedarf) |
| Eigenschafts-/Spektren-Vorhersage | pKa-, ADME-, NMR-, MS-/IR-/Raman-Spektren-Vorhersage aus SMILES/Struktur | Vorgefertigte, sofort nutzbare Vorhersage-Tools für Standardeigenschaften – reduziert den Aufwand für die in Abschnitt 2 skizzierten Predictive-Modelle (jede Methode vor Einsatz selbst fachlich validieren) |
| Struktur-Erkennung aus Bildern | Molekülstruktur-Bilder (z. B. aus gescannten Dokumenten) in SMILES umwandeln | Nützlich als Ergänzung zur ELN-Extraktion, wenn Strukturen nur als Bild vorliegen |
| Namens-/Strukturkonvertierung | IUPAC ↔ SMILES-Konvertierung | Einfacher, aber nützlicher Baustein, ergänzt die Fingerprint-Tools aus Abschnitt 10 |
| Visualisierung | 2D-/3D-Strukturdarstellung, publikationsreife Molekülgrafiken | Direkt für Chat-Antworten/Berichte nutzbar (Abschnitt 9) |
| Reaktions-Intelligenz (perspektivisch) | Reaktionsausgang/-bedingungen aus historischen Daten vorschlagen | Genau der in Abschnitt 9 postulierte semantische/projektübergreifende Anwendungsfall (Amidkupplungs-Learnings etc.) – anspruchsvoll, eher mittelfristiges Ziel |
| Wissenschaftliches Schreiben (perspektivisch) | Unterstützung beim Verfassen von Berichtsabschnitten/Literaturübersichten | Ergänzt die Berichtsgenerierung aus Abschnitt 9 |

**Wichtiger Grundsatz:** Diese Liste ist als Ideensammlung für eigene, selbst geschriebene und geprüfte Skills gedacht – nicht als Empfehlung, Code aus einem bestimmten externen Repository zu übernehmen. Jede Vorhersage-Fähigkeit (insbesondere pKa/ADME/NMR) muss vor Produktivsetzung fachlich validiert werden (welche Methode, wie kalibriert, welche Fehlerraten) und durchläuft dasselbe Human-Review-Gate wie alles andere agentengenerierte oder importierte Material (Abschnitt 4).

## 15. Caveats

- **Reifegrad**: MAF Agent Skills für Python ist erst seit sehr kurzer Zeit stabil (GA-Ankündigung nur wenige Tage/Wochen alt, Stand dieser Recherche); vor Produktivsetzung eigene Tests der `SkillsProvider`-API empfehlen, da sich Details noch ändern könnten.
- **Kein offizieller MAF-Temporal-Adapter**: Die Integration ist DIY (Activities wrappen MAF-Tool-Aufrufe/HPC-Calls manuell) – mehr Eigenentwicklung als bei nativ unterstützten Frameworks (z. B. OpenAI Agents SDK), dafür maximale Kontrolle und Cloud-Unabhängigkeit gegenüber Azure Durable Functions.
- **Zwei Infrastruktur-Systeme**: Temporal-Cluster zusätzlich zu Azure-Infrastruktur bedeutet mehr Betriebsaufwand als eine reine Azure-native Lösung (MAF Durable Extension). Die Entscheidung für Temporal lohnt sich vor allem, wenn Cloud-Unabhängigkeit oder bereits vorhandene Temporal-Expertise/Infrastruktur im Unternehmen eine Rolle spielen.
- **Graph-Index ist Eigenentwicklung**: Es gibt keine "fertige" Standardlösung für Chemie-spezifische MD-Wissensgraphen; NetworkX + eigener Frontmatter-Parser ist pragmatisch, aber muss selbst gewartet werden (verglichen mit etablierten Tools wie Obsidian/Foam, die primär für menschliche Nutzung, nicht für programmatischen Graph-Zugriff gebaut sind).
- Alle produktspezifischen Angaben (MAF-Versionen, Temporal-Integrationsmuster) spiegeln den Stand der Recherche zum Zeitpunkt dieser Antwort wider – bei einem sich schnell entwickelnden Feld vor Implementierung aktuelle Dokumentation gegenprüfen.
