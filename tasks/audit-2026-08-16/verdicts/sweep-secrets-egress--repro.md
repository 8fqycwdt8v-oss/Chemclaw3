# Verdicts: sweep-secrets-egress (lens: does it actually reproduce?)

In-scope findings: **one**. Only "Every configured credential is printed to stderr when config
validation fails" is marked `high`; the other four are `medium`/`low` and are out of scope.

Working tree checked against `HEAD` (`0da9f3d`) — `git status --short` shows only untracked audit
files, no source mutation, so no diff against the pristine copy was needed.

---

## Every configured credential is printed to stderr when config validation fails

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

### What I did

All scripts written from scratch under `/tmp/vfy`, run with `uv run --project /home/user/Chemclaw3
python`, each one clearing every `CHEMCLAW_*` variable it inherited first and running from a
directory with no `.env`.

**1. The cited construction site is real and unguarded.**

```
$ grep -n "^settings = Settings()" src/chemclaw/core/config/__init__.py
327:settings = Settings()
$ grep -n "ValidationError\|SystemExit\|try:" src/chemclaw/core/config/__init__.py
(no output)
$ grep -rn "excepthook" src/ deploy/
(no output)
```

So there is no `try:` anywhere in the module, and no process installs a `sys.excepthook`. The
traceback goes to stderr through Python's default hook, at import, before any logging is
configured. That half of the mechanism holds exactly as described.

**2. The minimal repro reproduces verbatim.** `/tmp/vfy/repro_a.py` — only
`CHEMCLAW_LLM_PROVIDER=openai_compatible` and `CHEMCLAW_LLM_API_KEY=sk-internal-9f2a4c` set:

```
pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings
  Value error, llm_provider='openai_compatible' requires llm_base_url, llm_model to be set
  [type=value_error, input_value={'llm_provider': 'openai_...': 'sk-internal-9f2a4c'}, input_type=dict]
```

Full key on stderr. Same shape on a second, independent validator
(`store.py:173 _pool_bounds_are_orderable`, `/tmp/vfy/repro_d.py`), so it is not specific to the LLM
check — there are 13 `model_validator(mode="after")` methods across `core/config/`, and each
attaches the same dict.

**3. `exc.errors()[0]["input"]` does carry every set field in full** (`/tmp/vfy/repro_b.py`):

```
{'note_webhook_secret': 'WEBHOOKSECRET-abc123', 'framing_envelope_secret': 'ENVELOPESECRET-abc123',
 'llm_provider': 'openai_compatible', 'llm_api_key': 'PRIMARYKEY-abc123',
 'hpc_api_token': 'HPCTOKEN-abc123', 'postgres_dsn': 'postgresql://u:DBPASSWORD1@h:5432/d',
 'vector_store_provider': 'qdrant', 'vector_store_api_key': 'qdr8ntKeyAbc123XyzPq',
 'temporal_api_key': 'REAL-TEMPORAL-KEY-abc123'}
```

**4. Where it breaks: what actually reaches stderr.** `str(exc)` truncates `input_value` to roughly
the first ~25 and last ~25 characters of the dict repr. I measured the boundary by padding the
environment with ordinary (non-secret) fields, one at a time (`/tmp/vfy/_t.py`, `pad=0..8`), with
`CHEMCLAW_LLM_API_KEY=PRIMARYKEY-abc123` set throughout:

```
pad= 0 setfields= 3 secret_visible=True  :: input_value={'llm_provider': 'openai_...y': 'PRIMARYKEY-abc12
pad= 2 setfields= 5 secret_visible=True  :: input_value={'knowledge_dir': 'someva...y': 'PRIMARYKEY-abc12
pad= 4 setfields= 6 secret_visible=False :: input_value={'knowledge_dir': 'someva..._endpoint': 'somevalu
pad= 8 setfields=10 secret_visible=False :: input_value={'knowledge_dir': 'someva..._endpoint': 'somevalu
```

Six explicitly-set fields is enough to push the key out of the visible window. The dict is ordered
by **field declaration order**, not by env order, so what is visible is decided by which mixin's
fields sit at the two ends — not by how many credentials the deployment holds.

**5. The finding's own named production trigger leaks nothing.** `/tmp/vfy/repro_e.py` builds the
env the shipped chart actually produces — all 33 keys from `deploy/helm/chemclaw/values.yaml`'s
`config:` block, plus five real secrets as the Secret would inject them — and then makes the
"one-line mistake" the finding names (drops `CHEMCLAW_LLM_BASE_URL`):

```
pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings
  Value error, llm_provider='openai_compatible' requires llm_base_url to be set
  [type=value_error, input_value={'knowledge_dir': 'knowle...hutdown_seconds': '120'}, input_type=dict]
```

Zero credentials on stderr. `/tmp/vfy/order.py` prints why: 36 set fields, first
`knowledge_dir`, last `worker_graceful_shutdown_seconds`, and the four credentials sit at positions
1, 17, 24 and 28 — all inside the elided middle.

**6. It does still leak on a slim env.** `/tmp/vfy/order2.py`, secrets plus two LLM fields and
nothing else:

```
keys in order: ['note_webhook_secret','llm_provider','llm_model','llm_api_key','postgres_dsn','temporal_api_key']
  ... input_value={'note_webhook_secret': '...': 'TEMPORALKEY-abc123'}
```

`TEMPORALKEY-abc123` verbatim on stderr, because `temporal_api_key` happens to be the
last-declared set field.

### Why

The mechanism is real and I confirm every structural claim: `settings = Settings()` is uncaught at
module scope, no excepthook exists, pydantic attaches the whole set-fields dict to the error, and
the redaction apparatus is genuinely one line too late to help. The fix the finding proposes is
correct and cheap.

What does not hold is the title and the consequence as stated. Two parts:

1. **"Every configured credential is printed to stderr" is false.** Only `str(exc)` reaches stderr,
   and it prints ~25 characters from each end of the dict repr — at most the head field's value and
   the tail field's value, decided by field *declaration* order. I measured the cutover at six set
   fields. The full dict lives on the exception object, but nothing catches it and nothing calls
   `.errors()`, so the full-dict exposure is a property of an object no output ever renders. The
   finding's own quoted "measured with the full secret set" block is `exc.errors()[0]["input"]` —
   i.e. a value its scaffolding read deliberately, not a value the crash prints.

2. **The trigger the finding names as "the most likely one in production" is the one arrangement
   where nothing leaks.** Under the shipped chart's own env the visible window is
   `knowledge_dir` … `worker_graceful_shutdown_seconds`. The credential-visible reproductions all
   use a 2-to-5-variable environment, which is a bench setup, not a deployment. That is the
   scaffolding-dependence this lens is meant to catch.

So: real, worth fixing, one wrapper — but a conditional single-value leak in an attended
crash-loop state, not a wholesale dump of the credential set. Medium.

Two things the reporter did not claim that I would add to the fix note: the exposure is not limited
to the five validators listed (there are 13 `mode="after"` validators), and the same dict is
attached for a `mode="before"` / extra-field failure too — so the guard belongs at the construction
site as proposed, not on any individual validator.
