# core-infra — design and simplification (round 1)

Slice: `src/chemclaw/core/{db,migrate,grants,http,egress,logging,metrics,metrics_bridge,embeddings,temporal_client,identity_context,turn_signals,bounded}.py`
Lens: design and simplification only. All files read in full. Reproductions run with `uv run` in this
environment.

---

## Two predicates for "is this counter labelled", and they disagree on the empty tuple

- **Severity**: medium
- **Location**: `src/chemclaw/core/metrics.py:460` and `:466` (`Metrics.increment`) vs
  `src/chemclaw/core/metrics.py:530` (`Metrics.render`)
- **Trigger**: a row in `_COUNTER_LABELS` whose value is `()`. That is the natural edit when a label
  is removed from an existing counter — you empty the tuple rather than delete the row.
- **Consequence**: `increment` reads `declared = _COUNTER_LABELS.get(name, ())` and branches on
  `if not declared:`, so it takes the *unlabelled* path and accumulates into `self._counts[name]`.
  `render` branches on `if name not in _COUNTER_LABELS:`, so it takes the *labelled* path, iterates
  `series.get(name, {})` — which is empty — and emits `# HELP` / `# TYPE` with **no sample line at
  all**. The counter vanishes from every scrape while `Metrics.value()` keeps returning the right
  number, so every in-process test that asserts on `value()` stays green. This is precisely the
  failure the module's own comment says the strict declaration exists to prevent ("the failure mode
  of a label typo is not a crash but a second, silent time series that no dashboard queries").
  Nothing catches it: `tests/test_metric_declarations.py` and `tests/test_metrics.py` both read
  `_COUNTER_LABELS` but neither asserts the tuples are non-empty.
- **Evidence**:

  ```
  $ uv run python -c '...'   # /tmp probe, full script below
  value() reports: 1.0
  render() emits: ['# HELP chemclaw_turns_started_total Turns admitted and started.',
                   '# TYPE chemclaw_turns_started_total counter']
  ```

  script:
  ```python
  from chemclaw.core import metrics as M
  M._COUNTER_LABELS["chemclaw_turns_started_total"] = ()
  r = M.Metrics()
  r.increment("chemclaw_turns_started_total")
  print("value() reports:", r.value("chemclaw_turns_started_total"))
  print("render() emits:", [l for l in r.render().splitlines() if "turns_started" in l])
  ```
- **Fix**: one predicate, not two. Add a module-level `def _labels_for(name: str) -> tuple[str, ...]:
  return _COUNTER_LABELS.get(name, ())` and have both `increment` and `render` branch on
  `if not _labels_for(name):`. Behaviour-preserving for every counter as currently declared (no row
  is empty today), and it makes the empty tuple mean the same thing in both places.

---

## `_dsn_password` is a weaker re-implementation of `db._redact`, and the difference is a leaked password

- **Severity**: medium
- **Location**: `src/chemclaw/core/logging.py:643` (`_dsn_password`) vs
  `src/chemclaw/core/db.py:66` (`_redact`)
- **Trigger**: a deployment whose `CHEMCLAW_POSTGRES_DSN` is in libpq keyword form
  (`host=… password=…`) or carries the password as a URL query parameter. Both are forms
  `psycopg.AsyncConnection.connect` accepts and `core/db.connect` passes through untouched.
- **Consequence**: `_dsn_password` splits on `"://"` and `"@"`, so it returns `""` for both of those
  forms. `_secret_values` therefore registers only the *whole DSN string* and never the password on
  its own, and a log line that quotes just the password — which is what a `password authentication
  failed for "…"` server error does — passes through the filter verbatim. `db._redact`, two modules
  away, already handles all three forms correctly by round-tripping through libpq's own parser, and
  its docstring names exactly this gap: *"every form psycopg accepts is covered — URL userinfo, URL
  query parameter, and the keyword `host=... password=...` form — **not just the userinfo case a URL
  split can see**."* `logging._dsn_password` is the userinfo case a URL split can see. The
  `_STRUCTURAL_SECRETS` backstop does not cover it either: the line has no `password=` anchor, and
  the value has no digit so `_HAS_DIGIT` fails.
- **Evidence**:

  ```
  DSN        : postgresql://u:hunterhunter@db:5432/chem
    db._redact       : user=u dbname=chem host=db port=5432
    _dsn_password    : 'hunterhunter'
  DSN        : host=db port=5432 dbname=chem user=u password=hunterhunter
    db._redact       : user=u dbname=chem host=db port=5432
    _dsn_password    : ''                                    <-- disagrees
  DSN        : postgresql://u@db/chem?password=hunterhunter
    db._redact       : user=u dbname=chem host=db
    _dsn_password    : ''                                    <-- disagrees
  ```

  end to end, with `settings.postgres_dsn` set to the keyword form:
  ```
  IN : psycopg.OperationalError: connection failed: password authentication failed for "Correct-Horse"
  OUT: psycopg.OperationalError: connection failed: password authentication failed for "Correct-Horse"
  _secret_values holds: ['host=pg.internal port=5432 dbname=chem user=app password=Correct-Horse']
  ```
- **Fix**: one spelling of "the password in a DSN". Replace the body of `_dsn_password` with
  `psycopg.conninfo.conninfo_to_dict(value).get("password") or ""`, guarded by
  `except psycopg.ProgrammingError: return ""`. `psycopg` is already a hard runtime dependency and
  the import is module-scope, so the "nothing on the logging path imports anything" rule that
  `tests/test_filtering_a_record_never_imports_anything` enforces is unaffected. Behaviour-preserving
  for the URL form (both spellings return `hunterhunter` above) and strictly wider for the other two.
  `_published_values`, the other caller, only gets more accurate.

---

## The specialist identity carrier has no writer: `AuditEvent.agent` is structurally always `""`

- **Severity**: medium
- **Location**: `src/chemclaw/core/identity_context.py:64,99,118` (`_current_specialist`,
  `set_current_specialist`, `reset_current_specialist`); consumed at
  `src/chemclaw/agent/audit.py:350`
- **Trigger**: none — no input reaches it. `set_current_specialist` has zero callers in `src/`.
- **Consequence**: `get_current_specialist()` returns the ContextVar's default `""` on every code
  path, so `AuditEvent.agent` is the empty string for every audit row this system will ever write.
  Three public functions, a ContextVar and ~35 lines of docstring arguing why the value must be
  recorded *beside* the actor are kept alive entirely by `tests/test_audit.py`, which calls the
  setter directly. This is the same cluster as `core/turn_signals.record_handoff` (`turn_signals.py:226`),
  which likewise has zero `src/` callers — together they are the residue of the deleted specialist
  team: the two producers went, the two carriers stayed.
- **Evidence**:

  ```
  $ grep -rn "set_current_specialist\|_current_specialist" --include=*.py src
  src/chemclaw/agent/audit.py:59:    get_current_specialist,
  src/chemclaw/agent/audit.py:350:    event_agent = get_current_specialist()
  src/chemclaw/core/identity_context.py:64:  _current_specialist: ContextVar[str] = ContextVar(...)
  src/chemclaw/core/identity_context.py:99:  def set_current_specialist(name: str) -> object:
  src/chemclaw/core/identity_context.py:115:     return _current_specialist.set(name)
  src/chemclaw/core/identity_context.py:118: def reset_current_specialist(token: object) -> None:
  src/chemclaw/core/identity_context.py:124:     _current_specialist.reset(token)
  src/chemclaw/core/identity_context.py:127: def get_current_specialist() -> str:
  ```
  (`grep -rn "record_handoff" src/` returns only its own `def`.)
- **Fix**: pick one and do it in the same commit as `record_handoff`. Either (a) delete
  `_current_specialist` and its three functions, drop the `agent` read in `audit.py:350` and the
  column's population, and delete `tests/test_audit.py`'s three specialist tests — a test that is
  the only caller of the thing it tests proves nothing; or (b) set it where a subgraph is entered,
  next to the `record_handoff` call that has to be added anyway, so the two ends of the same
  observation land together. Option (a) is behaviour-preserving (the emitted value is already `""`);
  option (b) is not, and is a decision, not a cleanup.

---

## `core/logging.py` is one third OpenTelemetry bootstrap, while `core/tracing.py` sits next to it

- **Severity**: medium
- **Location**: `src/chemclaw/core/logging.py:129-436` — `_NOOP_METERS_INSTALLED`,
  `_install_noop_meter_provider`, `_DEFAULT_SERVICE_NAME`, `_TRACING_INSTALLED`,
  `_build_tracer_provider`, `configure_telemetry`, `_warn_about_sensitive_data`,
  `_instrument_llm_calls`, `_trace_config`
- **Trigger**: reading or changing anything about span export. 308 of the module's 966 lines are OTel
  provider construction, meter-provider suppression, OpenInference instrumentation and its content
  hide-flag policy. None of them touch a `logging.Handler`, `Filter` or `Formatter`; none are called
  by anything else in the module.
- **Consequence**: the module named `logging` is three unrelated modules — logger configuration
  (~74 lines), the telemetry pipeline (~308), and the secret-redaction engine plus JSON formatter
  (~528). Every entrypoint writes `from chemclaw.core.logging import configure_logging,
  configure_telemetry`, which is a name that actively misleads about what the second import does.
  `core/tracing.py`, the first-party span module, opens its own docstring with *"`configure_telemetry`
  installs the tracer provider…"* — it is describing a function it does not contain and cannot see.
  The practical cost is that a change to span content policy (`_trace_config`'s eleven hide flags) is
  a diff in the file every process imports first and whose other 600 lines are on the per-log-record
  hot path.
- **Evidence**:

  ```
  $ grep -rn "configure_telemetry" --include=*.py src | grep import
  src/chemclaw/api/app.py:86:from chemclaw.core.logging import configure_logging, configure_telemetry
  src/chemclaw/durable/background_worker.py:24:from chemclaw.core.logging import configure_logging, configure_telemetry
  src/chemclaw/connectors/worker.py:24:from chemclaw.core.logging import configure_logging, configure_telemetry
  src/chemclaw/connectors/server_entry.py:36:from chemclaw.core.logging import configure_logging, configure_telemetry
  ```
  `core/tracing.py:3` — the sibling that should hold it — cites `configure_telemetry` in its first
  sentence and does not define it.
- **Fix**: move those nine symbols into `core/tracing.py` (or a new `core/telemetry.py` if
  `tracing.py` should stay span-only) and update the four entrypoint imports plus the four references
  in `tests/test_logging.py`. Behaviour-preserving: both module flags (`_NOOP_METERS_INSTALLED`,
  `_TRACING_INSTALLED`) are written and read only inside the functions moving with them, nothing in
  the telemetry block references a logging symbol, and `tracing.py` imports only `settings` today so
  no cycle is created.

---

## `record_metric` is a callback seam whose 21 call sites all write one of two lambdas

- **Severity**: low
- **Location**: `src/chemclaw/core/metrics_bridge.py:42` (`record_metric`), and its 21 call sites
- **Trigger**: any call site that wants to count something.
- **Consequence**: `record_metric(update: Callable[[Metrics], None])` is general enough to apply any
  update, and every one of its 21 call sites is exactly `lambda m: m.increment(...)` (20 sites) or
  `lambda metrics: metrics.observe(...)` (1 site — `agent/audit.py:76`). Nothing composes two updates
  under one swallow. The generality buys nothing and costs a lambda at every site, six of which need
  a multi-line `record_metric(\n  lambda m: ...\n)` wrap purely to fit the line length
  (`agent/compaction.py:302`, `agent/repeat_guard.py:107`, `agent/audit.py:76`, `kg/proposal.py:308`,
  `retrieval/fanout.py:118`, `durable/job_record.py:190`, `connectors/registry.py:529`). It also
  varies the bound name (`m` vs `metrics`) across the tree for no reason, and it puts the closure
  between the reader and the metric name — the thing the site is actually about.
- **Evidence**: all 21 sites enumerated by
  `grep -rn "record_metric(" --include=*.py src | grep -v "def record_metric"`, each inspected;
  none contains more than a single `increment`/`observe`.
- **Fix**: give `metrics_bridge` the two functions the call sites are actually reaching for —
  `def count(name: str, amount: float = 1.0, labels: Mapping[str, str] | None = None) -> None` and
  `def observe(name: str, seconds: float) -> None`, each a one-line body wrapping the same
  `try/except Exception: pass`. Rewrite the 21 sites (`count("chemclaw_notes_proposed_total")`,
  `count("chemclaw_repeated_tool_calls_total", labels={"tool": name})`). Delete `record_metric` — its
  Rule-of-Three justification (`"the ~10 call sites across six packages go through here"`) is about
  the *swallow*, which the two new functions keep, not about the callable parameter. Fully
  behaviour-preserving.

---

## `db.bind_pool_metrics` hides an import behind a rule that does not exist

- **Severity**: low
- **Location**: `src/chemclaw/core/db.py:227-231` (the comment and the function-body import)
- **Trigger**: reading the comment. It says: *"Imported inside the function: `core/metrics.py` is a
  sibling of this module and `core` keeps its no-module-scope-sibling-import rule
  (`tests/test_layering.py`), the same lazy exception `core/logging.py` declares."*
- **Consequence**: both halves are false, and a reader takes away a constraint the codebase does not
  have. `tests/test_layering.py:441-442` computes `_CORE_FORBIDDEN_SIBLINGS = set(_PACKAGES) -
  {"chemclaw.core"}` — the rule is about sibling *packages* (`chemclaw.agent`, `chemclaw.connectors`,
  …), never about modules inside `core`. And `core/logging.py`'s lazy exception is for
  `chemclaw.connectors`, a different package; `core/logging.py` itself imports `core.metrics_bridge`
  at module scope, which imports `core.metrics` at module scope, so the module cited as the precedent
  for keeping `core.metrics` lazy in fact pulls it in eagerly. `core/metrics.py` is stdlib-only and
  cannot create a cycle with anything.
- **Evidence**:

  ```
  $ uv run python -c "import chemclaw.core.logging, sys; print('core.metrics imported:', 'chemclaw.core.metrics' in sys.modules)"
  core.metrics imported: True
  $ uv run python -c "import chemclaw.core.db, sys; print('db pulls metrics:', 'chemclaw.core.metrics' in sys.modules)"
  db pulls metrics: False
  ```
  `src/chemclaw/core/metrics_bridge.py:37`: `from chemclaw.core.metrics import METRICS, Metrics`
  (module scope, in `core`).
- **Fix**: `from chemclaw.core import metrics` at module scope in `db.py`, and have `bind_pool_metrics`
  call `metrics.METRICS.bind_gauge(...)`. That removes the function-body import *and* the false
  comment while keeping the late attribute lookup that `tests/test_db_pool.py:222` depends on (it
  monkeypatches `chemclaw.core.metrics.METRICS`), so the change is behaviour-preserving including
  that test. Note the test's own comment repeats the false claim and should be corrected with it.

---

## `turn_signals._emit`'s docstring describes exception handling the function no longer contains

- **Severity**: low
- **Location**: `src/chemclaw/core/turn_signals.py:150-174` (`_emit`)
- **Trigger**: reading `_emit` to find out what it catches.
- **Consequence**: three of the docstring's four paragraphs describe a `try/except` that is not in
  the body. *"**Two exception types for one condition, both measured**, which is why this catches a
  pair that otherwise looks careless"* — `_emit` catches nothing; it delegates to
  `stream_writer_or_none`. And the pair it names (`RuntimeError`, `KeyError`) is not the set actually
  caught: `stream_writer_or_none:197` catches `(RuntimeError, LookupError, AttributeError)`. So the
  file states the narrower, superseded set as current fact in one place and the wider one in the
  place that implements it — which is the exact drift `stream_writer_or_none`'s own docstring says it
  was extracted to end (*"two call sites were asserting two different things about one upstream
  call"*). It ended the drift in the code and left it in the prose.
- **Evidence**: `turn_signals.py:160-167` (the claim) against `turn_signals.py:195-198` (the
  three-type `except`). `_emit`'s body is four lines: call `stream_writer_or_none()`, return on
  `None`, `writer({_KEY: signal})`.
- **Fix**: cut the "Two exception types" and "The guard is the design" paragraphs from `_emit` down to
  one sentence — "Drops the signal where no graph is streaming; see `stream_writer_or_none` for why
  that is the design and not a precaution" — and leave the measured exception argument in the one
  function that implements it. Behaviour-preserving (comment only).

---

## `connection()` parses the DSN twice on every unpooled call

- **Severity**: low
- **Location**: `src/chemclaw/core/db.py:191` vs `:127`
- **Trigger**: any `connection()` call in a process that has not entered `pooling()` — i.e. every
  script, every migration path and the whole test suite.
- **Consequence**: line 191 computes `options = _merged_options(dsn, statement_timeout_seconds)`, then
  line 192 branches to `connect(dsn, statement_timeout_seconds=...)`, which recomputes the identical
  value at line 127. The line-191 result is only ever consumed by `_pool_for` on the other branch.
  Each `_merged_options` call runs `conninfo.conninfo_to_dict(dsn)`. It is a hoisted computation sitting
  above the branch that needs it, and it makes the two branches look like they share a value they do
  not.
- **Evidence**: `grep -n "_merged_options" src/chemclaw/core/db.py` → definition at 82, calls at 127
  (inside `connect`) and 191 (inside `connection`, before the `if not _POOLING` branch at 192).
- **Fix**: move line 191 below the `if not _POOLING:` block, immediately above `pool = _pool_for(dsn,
  options)`. Behaviour-preserving.

---

## What I checked and found clean

- **`bounded.BoundedLru`** — five real callers (`api/state.py`, `api/budget.py`, `api/rate_limit.py`,
  `agent/attachments.py`, `cli/mock_llm.py`), `peek` and the `pinned` hook each used by a distinct
  one. The abstraction is earned; the module docstring's claim that `core/metrics.py`'s series cap is
  deliberately *not* folded in matches the code (`increment` refuses new series, does not evict).
- **Dead metric declarations** — none. Scripted every name in `_COUNTERS`/`_GAUGES`/`_HISTOGRAMS`
  against all of `src/` excluding `core/metrics.py`: every declared metric has at least one
  non-declaring reference.
- **`http.py`** — both symbols have real cross-package callers (`is_loopback_url` →
  `connectors/manifest.py`, `LOOPBACK_HOSTS` → `api/middleware.py`, `error_detail` → three sites in
  `connectors/qm/hpc/nextflow.py`). The module docstring's "the Entra token/OBO exchanges" consumer no
  longer exists, but the remaining caller count still justifies the extraction.
- **`egress.py`, `temporal_client.py`, `grants.py`** — single-purpose, one process-wide decision each,
  no structure to remove. `grants.py`/`migrate.py` share ~6 lines of read-then-execute shape; at two
  callers that is under the Rule of Three and extracting it would couple the checksum ledger to the
  reconciliation loop for no gain.
- **`embeddings.py`** — `clear_embedding_cache` is production-dead by its own admission, but it is a
  four-line cache reset with seven test callers and no alternative (module-global `_CACHE`); reporting
  it would be noise.
