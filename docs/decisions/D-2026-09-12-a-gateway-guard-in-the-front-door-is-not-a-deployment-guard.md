# D-2026-09-12-a-gateway-guard-in-the-front-door-is-not-a-deployment-guard — the model-gateway boot check leaves `api/` and the posture it exempts is stated rather than inferred from a bind

**Context.** `D-2026-09-04-a-gateway-is-the-only-provider` left `llm_base_url` shipping a value —
`chemclaw.cli.mock_llm`'s loopback address — so that a fresh checkout needs no credential, and added
`api/middleware._refuse_unconfigured_llm_gateway` so that a deployment which never overrode it would
be told at boot instead of meeting a refused connection on a chemist's first question. The argument
was right. Its reach was not: that function, and `_refuse_unauthenticated_exposure` beside it, have
exactly one caller in the tree — `api/app.py::create_app`. Every other occurrence of either name in
`src/` is prose.

`deploy/entrypoint.sh` dispatches five components, and the front door is one of them. Three of the
five make model calls:

| component | makes model calls | through |
| --- | --- | --- |
| `service` | yes | `api/runner.py` builds the turn's graph |
| `background-worker` | yes | `durable/template_activities.run_agent_step` builds a graph *inside an activity* |
| `mcp-face` | yes | `condense_protocols` is advertised and builds a chat model of its own |
| `connector-*` | no | a bundle's MCP surface: the science tools, none of which reaches the gateway |
| `connector-worker-*` | no | a bundle's own queue only; core's `template_activities` is never imported there |

So the guard reached one of the three. Driven against the live broker with this commit's call
deleted again, in an environment carrying no `CHEMCLAW_*` variable but the namespace (which has no
default):

```
background worker connected: address=localhost:7233 namespace=default queue=background-jobs
  workflows=[… 22 …] activities=[…, run_agent_step, …]
settings.llm_base_url = 'http://127.0.0.1:8820/v1'   llm_allow_loopback_gateway = False
```

It connects and polls for as long as it is left alone, carrying the shipped mock address into an
activity that builds an agent. With the call restored the same command raises before `connect()` is
reached, and the broker is never dialled. The row that opened this
(`docs/planning/BACKLOG.md`) had already driven the second half: in that guard-free process, with a
loopback recorder standing in for the gateway, the model replied and the recorder logged
`/v1/chat/completions`.

The Helm chart is **not** what this bites, and the row's own first attempt at saying so was wrong
about which ConfigMap. Re-rendered here with helm 3.16.3: `CHEMCLAW_LLM_BASE_URL` is in **two**
ConfigMaps, and between them every Deployment and Job `envFrom`s one — the workloads take
`chemclaw-config`, and `chemclaw-migrate` alone takes `chemclaw-config-hook`, which carries the same
key. No single ConfigMap reaches all of them, and the conclusion is unchanged. What this bites is a
non-Helm or
partially-overridden deployment — the one that forgets things — and there the front door refuses
while the worker dials loopback in silence, inside a retry loop with nobody watching.

**Decision.** Two changes, and the second is the one with a cost.

## 1. The guard leaves `api/`

`chemclaw/core/llm_gateway.py::refuse_unconfigured_llm_gateway` is the same check in the kernel every
entrypoint already imports, called from `api/app.py::create_app`, `api/mcp_face.py::main`,
`durable/background_worker.py::main` and `cli/chat.py::main`. `core` was available to it all along:
it needs `settings` and `core.http.is_loopback_url` and nothing else, so no layering edge is created
(`tests/test_layering.py`, `tests/test_third_party_layering.py` both green unchanged).

**Where the boundary is drawn, since "every entrypoint that can reach a model call" is not quite the
set.** `build_chat_model` has five callers: the graph builder, `agent/condense.py`,
`agent/verifier.py`, `cli/verifier_margin.py` and `evals/live_judge.py`. The first three are inside a
turn, so they are covered wherever a turn is taken. The last two are reached only from developer
measurement commands — `cli/verifier_margin.py`, and `cli/live_probes.py` through the judge — and
those are deliberately **not** wired. The guard's whole argument is "loudly at boot rather than
loudly on the first turn", and for a one-shot command typed by a human the model call *is* the first
thing that happens, in front of the person who typed it; `cli/live_probes.py` goes further and
already reports an ungradeable gateway by name. So the guarded set is the process kinds a deployment
*runs* — the components `deploy/entrypoint.sh` dispatches — plus the one interactive front door that
is a long-lived session rather than a command. That is what the partition test enumerates, and it is
why it enumerates components rather than modules.

**What the old home made safe, re-read rather than assumed.** Nothing. The function read two settings
and raised; it held no import-time ordering and no resolved-at-boot state that `api/` provided. What
the move *orphaned* is prose: seven sentences cited the old path in the present tense — two in
`src/` (`core/netguard.py`'s `Raises:` paragraph, which contrasted itself favourably against exactly
this gap, and `core/http.py`'s loopback argument), one in `deploy/helm/chemclaw/values.yaml` (which
claimed "a pod that inherited it would refuse to boot" — true of the front door and of no other
pod the chart renders), and four in tests. All are corrected; the merged ADRs that cite the old name are left alone.

`cli/chat.py` takes the call *inside* `main`'s existing `try`, because that function already turns a
`RuntimeError` into one sentence and an exit code rather than nine frames of asyncio, which is the
whole reason it catches anything.

## 2. The exemption stops being a bind

The old predicate returned early when `service_host` named a loopback interface. That is a fact about
the front door's *socket*, and carrying it into a worker would be reading a field about a bind the
process does not perform — which is precisely why the other half of that backlog row stays open. So
the dev posture is now **stated**: `CHEMCLAW_LLM_ALLOW_LOOPBACK_GATEWAY` (default `false`), set once
by `infra/live/processes.sh` for the whole live lane, by the `chat` make target, and by the suite's
own autouse fixture. **Not by `.env.example`**: that file's promise is that it ships every field at
its code default and `tests/test_config.py::test_env_example_ships_the_code_defaults` holds it, with
an override register that is still empty — so the posture is stated by the lanes where running
against the mock is actually true, which is the same argument this whole change makes one level up.
Every process kind then asks one question with no reference to how, or whether, it binds a socket.

This is **stricter** in one direction and **narrower** in another, and both are real:

- Stricter: a loopback bind no longer skips the check, so the front door is now checked in every
  posture. `make chat` and the live lane carry the flag; a raw `uv run chemclaw --admin` against
  the mock now needs it in the environment, and gets one sentence naming the two edits that
  proceed.
- Narrower: a gateway sidecar on loopback inside the same pod is an ordinary deployment, and the old
  predicate refused it as though the dev mock were the only thing that could answer there. Such a
  deployment sets the flag and says so, with a WARNING on every boot recording that it did.

An **empty** `llm_base_url` is deliberately not checked here: `Settings._gateway_is_addressed`
already refuses it unconditionally, in every process, before this function could run. A second check
would be a control that claims to exist and never fires — the `reject_widening` shape.

**What stays open, said plainly so the two halves are not mistaken for one.**
`_refuse_unauthenticated_exposure` does **not** move and is **not** extended. Its signal *is*
`service_host` being non-loopback — a property of a bind — and what "exposed" means for a process
that only makes outbound calls is a design question, not a relocation. `docs/planning/BACKLOG.md`
keeps the row, reworded to that half alone. **This ADR closes the gateway guard's reach and nothing
about unauthenticated exposure.**

**What keeps it true.** `tests/test_llm_gateway_guard.py`, and the choice of assertion is the point:
a test that the guard *function is called* is the test that would have passed throughout the defect.
Three pairs of arms start real processes — `chemclaw.durable.background_worker`,
`chemclaw.api.mcp_face`, `chemclaw.cli.chat` — and each pair differs by one environment variable, so
a refusal is measured against a **positive control** that must get further: the worker must reach
its broker dial (pointed, in both arms, at a loopback port nothing serves), the face must reach
uvicorn's bind (held by the test process, so "address already in use" is what it fails on), the CLI
must reach its own `--help`. Without the control,
a harness that could not import the module at all would report every refusal identically. Beside them
`test_every_image_component_has_a_verdict_about_model_calls` reads the component names out of
`deploy/entrypoint.sh` and requires a verdict for each, so a sixth component cannot join the
unguarded half by being forgotten; and `test_the_unguarded_components_cannot_reach_the_gateway`
turns the two "no" rows above into a check — no connector bundle imports `agent.llm_provider` or
`core.embeddings` — so the day a bundle grows a model call, that row goes red instead of quiet.

The mutations, each run before this was written: removing the call from the worker, the face, the CLI
and `create_app` fails its own arm and nothing else; restoring the `service_host` exemption fails
`test_a_loopback_gateway_is_refused_in_every_posture`; narrowing loopback to a literal `127.0.0.1`
fails `test_the_whole_of_127_is_loopback_here`; dropping the opt-in WARNING fails
`test_the_stated_posture_boots_and_says_so`; adding a component to `entrypoint.sh` fails the
partition; and importing `core.embeddings` into a connector bundle fails the last one.
