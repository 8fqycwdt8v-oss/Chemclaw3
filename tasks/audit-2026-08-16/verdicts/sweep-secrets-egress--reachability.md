# Verdicts: sweep-secrets-egress — lens "is the trigger reachable, is the consequence what is claimed?"

Scope filter: the file carries five findings, of which exactly **one** is `critical`/`high`
("Every configured credential is printed to stderr when config validation fails", high). The other
four are medium/medium/medium/low and are out of scope. One verdict below.

Working tree checked against `HEAD` first (`git status --porcelain`): only untracked audit files
differ, no mutation markers in `src/`. All runs below are `uv run python` from a clean `/tmp/vr1`
with no `.env`.

---

## Every configured credential is printed to stderr when config validation fails

- **Verdict**: OVERSTATED
- **Severity I would assign**: low

### What I did

**1. Reproduced the mechanism.** The exception object really does carry every configured value.
`/tmp/vr1/full2.py` sets six credentials plus the one-line operator mistake
`CHEMCLAW_SERVICE_UVICORN_WORKERS=2`, imports `chemclaw.core.config`, catches the `ValidationError`
and prints `errors()[0]["input"]`:

```
N ERRORS: 1
INPUT: {'note_webhook_secret': 'WEBHOOKSECRET-abc123', 'service_uvicorn_workers': '2',
        'framing_envelope_secret': 'ENVELOPESECRET-abc123', 'llm_api_key': 'PRIMARYKEY-abc123',
        'hpc_api_token': 'HPCTOKEN-abc123',
        'postgres_dsn': 'postgresql://u:DBPASSWORD1@h:5432/d',
        'vector_store_api_key': 'qdr8ntKeyAbc123XyzPq', 'temporal_api_key': 'TEMPORALKEY-abc123'}
```

So that half of the finding is exact, and I found a *more* reachable trigger than the one it names:
`service_uvicorn_workers > 1` is an ordinary capacity knob an operator would reach for, and
`_guards_that_the_comments_already_demand` (`core/config/__init__.py:150`) rejects it. There is no
`try/except` at `__init__.py:327` and no `field_validator` anywhere in `core/config/` (`grep -rn
field_validator src/chemclaw/core/config/*.py` → no output), so the model-level validators are the
only error class that attaches a dict, and nothing catches them.

**2. Attacked the consequence — what actually reaches stderr.** `str(ValidationError)` does not print
the dict; pydantic-core truncates the `input_value` display to a fixed **25-char head + 25-char
tail**. Whether a credential is inside that 50-character window depends entirely on how many fields
the deployment sets. `/tmp/vr1/thresh.py` sweeps the number of chart config keys present, with three
credentials always set:

```
N_config= 0  leaked=['DBPASSWORD1']  repr=input_value={'note_webhook_secret': '...u:DBPASSWORD1@h:5432/d'}
N_config= 3  leaked=['DBPASSWORD1']  repr=input_value={'note_webhook_secret': '...u:DBPASSWORD1@h:5432/d'}
N_config= 5  leaked=['DBPASSWORD1']  repr=input_value={'note_webhook_secret': '...u:DBPASSWORD1@h:5432/d'}
N_config=33  leaked=[]              repr=input_value={'knowledge_dir': 'knowle...hutdown_seconds': '120'}
```

**3. Ran the finding's own named trigger — "the shipped chart's own posture".** `/tmp/vr1/deploy_sim.py`
loads all 33 `config:` keys out of `deploy/helm/chemclaw/values.yaml` verbatim, adds all five
`secret.keys` plus the four `optionalKeys` (real-looking values), sets
`CHEMCLAW_SERVICE_UVICORN_WORKERS=2`, and imports the module. Grepping the **entire** 14-line stderr
for each of the eight secret values:

```
exit=1
PRIMARYKEY-abc123            0
HPCTOKEN-abc123              0
DBPASSWORD1                  0
ghp_KNOWLEDGEREPOTOKEN123    0
WEBHOOKSECRET-abc123         0
ENVELOPESECRET-abc123        0
CHEMTOKEN-abc123             0
CALCTOKEN-abc123             0
```

Zero. The printed window is `{'knowledge_dir': 'knowle...hutdown_seconds': '120'}`.

**4. Checked why that is structural rather than lucky.** The dict is ordered by **field declaration
order**, not env order — I printed it:

```
total fields 327
FIRST 8: ['report_section_timeout_seconds', ... ]
LAST 8:  [..., 'worker_metrics_port', 'worker_graceful_shutdown_seconds']
CRED FIELDS + index: note_webhook_secret 130, session_store_dsn 158, framing_envelope_secret 185,
  llm_api_key 208, llm_fallback_api_key 215, hpc_api_token 267, hpc_artifact_store_token 275,
  postgres_dsn 277, postgres_migration_dsn 278, vector_store_api_key 299, temporal_api_key 307
```

Every credential-shaped field sits between index 130 and 307. The chart's ConfigMap sets
`CHEMCLAW_KNOWLEDGE_DIR` (index < 130) and `CHEMCLAW_WORKER_GRACEFUL_SHUTDOWN_SECONDS` (index 326),
which **bracket the whole credential range**, so the head/tail window provably cannot land on one.
And every container in the chart gets that same full map — `_helpers.tpl:217 chemclaw.envFrom` is a
single `configMapRef` included by `deployment-service.yaml`, `deployment-workers.yaml`,
`deployment-connectors.yaml`, `migrate-job.yaml` and `schedules-job.yaml`, so there is no
reduced-env container that would shrink the dict.

**5. Checked whether anything else renders the exception.** `exc.errors()` is called exactly once in
the tree and not on this error (`grep -rn "\.errors()" --include=*.py src/` → only
`cli/validate_skills.py:96`). There is no `sys.excepthook` override, no `rich.traceback`, and
`deploy/entrypoint.sh` `exec`s the component directly with no wrapper that would re-render locals.
So the only renderer is CPython's default excepthook, i.e. the 50-char-truncated `str(exc)` measured
above.

### Why

The **mechanism** is confirmed and the timing claim is right: `settings = Settings()` is at module
import, before `configure_logging()`, so no `SecretRedactingFilter` exists and the object that would
seed the value inventory is the one that failed to construct. That much I could not break.

What does not hold is the **consequence as titled**. "Every configured credential is printed to
stderr" is false, and in the one deployment posture the finding itself nominates as most likely
("the shipped chart's own posture") **zero** credentials are printed — measured, eight values, eight
zeroes. Only ~50 characters of the dict ever leave the process, and under the chart those 50
characters are `knowledge_dir` and `worker_graceful_shutdown_seconds`, which is not an accident of
value length but a consequence of a fixed field order that brackets all eleven credential fields.
The finding's supporting evidence is real but is a **small-env repro** (two to six variables), and
it generalises the result from that repro to production without re-running it under production's
env — which is exactly the size dimension that decides the outcome. Its own quoted "with more
variables set" example stops at the point where the tail still happened to be a credential.

The residual risk is genuine but narrow: a deployment that does **not** use the chart — a bare
`docker run` with a handful of `-e` flags — can put a credential at the head or tail of the dict, and
would then print exactly **one** credential (the first- or last-declared field that is set), not
every one. The full dict is otherwise confined to the exception object, which nothing in this tree
reads or logs. That combination — no reachable path under the shipped chart, at most one credential
under a hand-rolled env, nothing that escalates it — is a low, not a high.

Two things worth carrying forward even at low severity, because the reporter's proposed fix is cheap
and correct: (a) the secrets do sit in a live exception object, so the day anyone adds
`log.exception(...)` or an `exc.errors()` dump around a settings reload, the full dict becomes
loggable — the `try/except ValidationError → SystemExit(...) from None` wrapper closes that class,
not just the stderr instance; (b) the same wrapper would replace a 14-line pydantic traceback with a
readable message, which is the larger practical benefit. I would file it as a low-severity hardening
item, not as a credential-disclosure defect.
