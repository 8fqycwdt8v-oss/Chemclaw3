# Verdicts — `connectors-bundles--design.md`, reproduction lens

Scope: the two findings marked **high**. The other seven (three medium, four low) are out of scope
and were not verified.

Working tree confirmed clean vs `HEAD` (`e48441d0`) for every file cited below, so nothing here is
another agent's live edit. All scripts are my own (`/tmp/v_imp*.py`, `/tmp/v_err*.py`); the
reporter's `/tmp/chain2.py`, `/tmp/proof.py` and `/tmp/calcerr2.py` were not run.

---

## One private-constant import drags LangGraph and layer 1 into every `calc` and `bo` process

- **Verdict**: CONFIRMED
- **Severity I would assign**: medium (the finding says high — see below)
- **What I did**

  Symbols and line numbers are current:

  ```
  $ grep -rn _READ_TIMEOUT_GRACE_SECONDS src/
  src/chemclaw/connectors/calc/remote.py:42:from chemclaw.connectors.registry import _READ_TIMEOUT_GRACE_SECONDS
  src/chemclaw/connectors/calc/remote.py:127:            sse_read_timeout=timedelta(seconds=bound + _READ_TIMEOUT_GRACE_SECONDS),
  src/chemclaw/connectors/registry.py:89:_READ_TIMEOUT_GRACE_SECONDS = 5.0
  ```

  My own per-process import measurement (`/tmp/v_imp1.py`, one fresh interpreter per target):

  ```
  chemclaw.connectors.calc.server.app: langgraph=True  langchain_core=True  agent=True mods=2318 t=2.00s
  chemclaw.connectors.calc.worker:     langgraph=True  langchain_core=True  agent=True mods=2298 t=2.01s
  chemclaw.connectors.bo.server.app:   langgraph=True  langchain_core=True  agent=True mods=6608 t=7.11s
  chemclaw.connectors.bo.worker:       langgraph=True  langchain_core=True  agent=True mods=6582 t=7.45s
  chemclaw.connectors.qm.worker:       langgraph=False langchain_core=False agent=True mods=1829 t=1.55s
  chemclaw.connectors.molfp.server.app:langgraph=False langchain_core=False agent=True mods=1304 t=1.13s
  ```

  Every module count matches the reporter's to the unit, from an independently written script.

  Causality, first by a `sys.meta_path` watcher that prints the chemclaw frames at the *first*
  import of `langchain_core` (`/tmp/v_imp2.py`) — no stubbing, no mutation:

  ```
  === first import of langchain_core
     src/chemclaw/connectors/calc/server/app.py:18  from chemclaw.connectors.calc.server.tools import server
     src/chemclaw/connectors/calc/server/tools.py:33  from chemclaw.connectors.calc import compose
     src/chemclaw/connectors/calc/compose.py:38  from chemclaw.connectors.calc.remote import cached_remote, remote_call
     src/chemclaw/connectors/calc/remote.py:42  from chemclaw.connectors.registry import _READ_TIMEOUT_GRACE_SECONDS
     src/chemclaw/connectors/registry.py:40  from langchain_core.tools import BaseTool
  ```

  and then by counterfactual (`/tmp/v_imp3.py`, my own stub of only that one module):

  ```
  calc server app w/ stubbed registry: langgraph=False langchain_core=False agent=True mods=1473 t=1.53s
  constant in use: 5.0
  ```

  RSS at import, same two configurations (`/tmp/v_rss.py`, `ru_maxrss`):

  ```
  with registry: RSS_MB 215.9 mods 2319
  stubbed:       RSS_MB 157.9 mods 1474
  ```

  Deployment reachability checked against the chart rather than assumed: `values.yaml.connectors`
  gives `calc` and `bo` each `enabled: true, server: true, worker: true` and **no** `url`, and
  `deployment-connectors.yaml` renders the app Deployment exactly when `server and not url` — so all
  four processes are real pods. `resources.connector` is `requests 256Mi / limits 512Mi`.

  I also checked the two escape hatches the finding claims are blind, and both claims hold:
  `tests/test_connector_isolation.py` asserts only that `tblite/bofire/botorch/torch` stay out of the
  *agent's* process (`_HEAVY`, `_BUNDLE_ONLY_PACKAGES = ("calc",)`) and never asks the reverse;
  `tests/test_layering.py:321` sanctions `("chemclaw.connectors", "chemclaw.agent")` at package
  granularity.

- **Why**

  The mechanism reproduces exactly, on my own scripts, with the reporter's numbers: one private
  constant is the sole reason `langgraph`/`langchain_core`/`langchain_mcp_adapters` enter the `calc`
  and `bo` pods, worth 845 modules, ~58 MB RSS and ~0.5 s of cold start each. Removing it is
  behaviour-preserving and the finding's proposed fix is sound.

  Two corrections, neither fatal:

  1. **The causal narrative is wrong in detail.** The finding says "`chemclaw.agent` pulls
     `langgraph`, `langchain_core`, `langchain_mcp_adapters`". It does not: `chemclaw/agent/__init__.py`
     is docstring-only and `chemclaw.agent.turn_flags` imports nothing but `contextvars`. The weight
     comes from `registry.py`'s own line 40/41 (`langchain_core.tools`,
     `langchain_mcp_adapters.sessions`) and from `connectors/jobs.py:37` importing
     `chemclaw.agent.authz`. The finding's own counterfactual already shows this — `agent=True`
     survives the stub — so the conclusion is unaffected, only the story.
  2. **"high" is a notch generous.** Nothing fails, nothing is exposed, and no behaviour changes:
     the cost is startup weight, headroom (216 MB of a 512Mi limit consumed before the first
     request, vs 158 MB) and an unpoliced layering edge. That is a real maintenance and capacity
     argument — and the fix is one line each side — but it is a hygiene finding, not an operational
     one. Medium.

---

## A calc-server outage reaches the model as "an internal error occurred"

- **Verdict**: CONFIRMED — and worse than reported
- **Severity I would assign**: high
- **What I did**

  Read the two cited sites at HEAD. `connectors/server.py:373` is verbatim
  `if isinstance(exc.__cause__, ValueError): raise` inside `_sanitize_tool_errors`'s `except ToolError`,
  with everything else replaced by `f"Error executing tool {tool_name}: an internal error occurred"`.
  `core/errors.py:37` declares `class SubsystemUnavailableError(Exception)` — outside `ValueError`.

  End-to-end repro of my own (`/tmp/v_err1.py`, `/tmp/v_err2.py`): point
  `CHEMCLAW_CALC_SERVER_URL` at a dead port, `import chemclaw.connectors.calc.server.app` so the
  sanitizer is applied to the real served `FastMCP`, then drive `server._tool_manager.call_tool`:

  ```
  CalcServerError is ValueError? False
  CalcToolError   is ValueError? True
  CalcServerError is SubsystemUnavailableError? True

  compute_xtb_energy       -> Error executing tool compute_xtb_energy: an internal error occurred
  predict_pka              -> Error executing tool predict_pka: an internal error occurred
  predict_solubility       -> Error executing tool predict_solubility: an internal error occurred
  compute_thermochemistry  -> Error executing tool compute_thermochemistry: an internal error occurred
  predict_logd             -> Error executing tool predict_logd: an internal error occurred
  calculator_trust         -> Error executing tool calculator_trust: an internal error occurred
  calculator_outliers      -> Error executing tool calculator_outliers: an internal error occurred

  RAW (one layer down, unsanitised) -> CalcServerError: the calculation service is not answering,
    so no calculation was run. This is an outage rather than a problem with what was asked; the
    same request will work once it is back.
  ```

  Seven of the eleven measured directly; the remaining four (`compute_electronic_properties`,
  `predict_site_reactivity`, `optimize_geometry`, `predict_developability_profile`) go through the
  same `cached_remote` at `calc/server/tools.py:743/782/907/653` under the same sanitizer.

  I then checked whether anything downstream restores the message, because that would have killed
  the finding. It does not: `agent/audit.returned_failure` picks the connector's `ToolMessage`
  (`status="error"`) up, and `agent/tool_authz.answered_failure` clears only the error *flag* and
  keeps the text (`returned_failure_detail` = `message.text[:300]`). So the string the model reads
  is literally `Error executing tool compute_xtb_energy: an internal error occurred`.
  `tool_authz.surface_domain_errors:327` does catch `(ChemclawError, SubsystemUnavailableError)`
  and forward it verbatim — but only for exceptions raised in the *agent's* process, which since the
  physics split this one never is. The docstring at `remote.py:55-72` naming `agent/tool_authz.py`
  as the reader of that message is therefore stale, as the finding says.

  **What the reporter missed.** The same sanitizer is applied by `connector_app` to *every* bundle,
  and `bo` reaches the same client through `bo/calculators.py:16` → `bo/server/tools.py:541` and
  `:845`. Driven for real (`/tmp/v_err3.py`, a two-ligand problem carrying `structures`, same dead
  port, `bo`'s own served tool manager):

  ```
  bo suggest_next_experiment -> Error executing tool suggest_next_experiment: an internal error occurred
  ```

  So the blast radius is 11 `calc` tools **plus** `bo`'s `suggest_next_experiment` and
  `predict_outcome` whenever the problem declares molecular structures — i.e. the flagship BO tool
  reports a calc outage as an internal fault too.

- **Why**

  Every link reproduces on a freshly written script: the class is outside `ValueError`, the guard
  tests `ValueError`, the replacement fires, and nothing downstream re-widens it. The trigger is an
  ordinary outage of a service that lives in another repository and another pod, so it is not a
  corner case. The consequence is precisely the one `SubsystemUnavailableError`'s own contract
  exists to prevent — the model gets a contentless failure and cannot tell "we are down, retry" from
  "your request was impossible" — and the asymmetry the finding highlights is real and backwards:
  `CalcToolError` *is* a `ValueError`, so the half a chemist could act on survives and the half that
  says "this will work later" is the half destroyed. The proposed one-line fix
  (`isinstance(exc.__cause__, ValueError | SubsystemUnavailableError)`) is correct, and I found no
  raiser inside a connector pod whose `SubsystemUnavailableError` message would leak an address if
  forwarded. High stands.
