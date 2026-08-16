# Round 1 — security & hardening: `servers/chem`, `servers/rxnpredict`, `packages/mcp_server_kit`, `manifests/`, Makefile + pyproject

Repo: `/workspace/chemclaw3-mcp`. Every finding below was reproduced against the code as it stands;
scripts are under `/tmp/repro/` and `/tmp/probe*.py`, and the output quoted is what they printed.

Interpreting "trigger": the MCP surface is bearer-authenticated, so the immediate caller is always
Chemclaw3's agent. That does **not** make the arguments trusted — the agent's job is to take a
chemist's free text, an ELN note, or a retrieved document and turn it into a `smiles=` / `reactants=`
argument. Every string below is one an untrusted document can put in front of the agent, so
"untrusted input reaches this" is the normal case, not the adversarial edge.

---

## A 20,000-character SMILES argument kills the server process (SIGSEGV)

- **Severity**: critical
- **Location**:
  - `servers/chem/src/chemclaw_mcp_chem/engine/chem.py:84-92` (`require_molecule`) and
    `:95-104` (`require_canonical_smiles`) — reached from `resolve_compound`,
    `stoichiometry_table` (`basis`, every entry of `reagents`/`solvents`) and `render_structure`
  - `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/preprocessing.py:11-25`
    (`canonical_smiles` / `canonical_multi_smiles`) — reached from `classify_reaction`,
    `predict_forward_reaction`, `predict_reaction_conditions`
- **Trigger**: one authenticated MCP `tools/call` whose SMILES argument is `"C" * 20000` — a
  20 KB string, i.e. **2 % of the 1,000,000-byte `BodySizeLimit`**. No concurrency, no repetition.
- **Consequence**: `Chem.MolToSmiles` overflows its stack inside RDKit's C++ and the **whole uvicorn
  process dies with SIGSEGV**. Not an exception — a signal, so nothing in `_sanitize_tool_errors`,
  no `try/except ValueError`, and no `asyncio.to_thread` boundary contains it (the segfault in a
  worker thread kills the process just the same). Every in-flight session on that pod is lost, the
  MCP session state goes with it, and the pod restart-loops as long as the input is retried. On
  `chem` this is reachable through the tool whose entire job is to canonicalise "whatever the
  chemist typed"; on `rxnpredict` it is on the **event loop**, so it is not even offloaded.
- **Evidence**:

  The validator that runs first checks emptiness, embedded whitespace and non-ASCII — and nothing
  about size (`chem.py:84-92`):

  ```python
  stripped = smiles.strip()
  if not stripped or any(ch.isspace() for ch in stripped): ...
  if not stripped.isascii(): ...
  mol = Chem.MolFromSmiles(stripped)
  if mol is None or mol.GetNumAtoms() == 0: ...
  return mol
  ```

  Isolated (`/tmp/probe3.py`) — the parse is cheap, the canonicalisation is what dies, and the cliff
  is between 15k and 20k atoms:

  ```
  --- n=15000 canon
  parse ok 0.027s atoms=15000
  canon ok 4.842s
  exit=0
  --- n=20000 canon
  parse ok 0.034s atoms=20000
  Segmentation fault
  exit=139
  ```

  End-to-end against the real `chem` app under uvicorn, over a real MCP handshake with a valid
  bearer token (`/tmp/repro/serve_chem.py` + `/tmp/repro/attack_chem.py`):

  ```
  {"status":"ok","server":"chem","revision":"unknown"}
  sending resolve_compound with a 20000-byte SMILES (2.000% of the 1,000,000-byte body cap)
  wrap.sh: line 2: 719 Segmentation fault  .venv/bin/python /tmp/repro/serve_chem.py
  SERVER EXITED WITH STATUS 139  (139 = 128+SIGSEGV(11))
  ```

  Same input against the real `rxnpredict` app, tool `classify_reaction`
  (`/tmp/repro/serve_rxn.py` + `/tmp/repro/attack_rxn.py`):

  ```
  {"status":"ok","server":"rxnpredict","revision":"unknown"}
  httpx.RemoteProtocolError: Server disconnected without sending a response.
  SERVER EXIT=139
  ```

  The server's own log ends mid-call with no traceback, which is the signature:

  ```
  server rxnpredict request: path=/mcp actor=- session=- dry_run=-
  Processing request of type CallToolRequest
  ```

- **Fix**: cap the input where it is validated, not where it is used. In
  `chem.py::require_molecule` add a length bound before `MolFromSmiles` (a bench SMILES is
  hundreds of characters; 4,000 is already generous) and raise `InvalidSmilesError` naming the
  limit, so it reaches the agent as a usable message. Do the same in
  `rxnpredict/engine/preprocessing.py::canonical_smiles`, which is the only other door to
  `MolToSmiles`, and bound the *number* of dot-separated components in `canonical_multi_smiles`
  and the length of `reagents`/`solvents` in `charge_table`. Make the limit a module constant, not
  a literal, and add a test at the boundary — the current `test_canonicalization_contract.py`
  tables contain no large input at all. `DEFAULT_MAX_REQUEST_BYTES = 1_000_000` is not a substitute:
  the lethal input is 2 % of it.

---

## Eight `render_structure` calls with a 4 KB SMILES take the pod out of service, and the caller hanging up does not stop it

- **Severity**: high
- **Location**: `servers/chem/src/chemclaw_mcp_chem/engine/depiction.py:30-51` (`render_svg`,
  specifically `Draw.rdDepictor.Compute2DCoords`), called from
  `servers/chem/src/chemclaw_mcp_chem/tools.py:140-153` via `asyncio.to_thread`
- **Trigger**: 8 concurrent `render_structure` calls with `smiles = "C" * 4000` — 4 KB each,
  0.4 % of the body cap. 8 is not arbitrary: it is the size of the default `asyncio.to_thread`
  executor on this host (`min(32, cpu_count + 4)` = 8 on 4 CPUs), i.e. the entire RDKit capacity of
  the process.
- **Consequence**: `/healthz` stops answering entirely, and stays that way long after the attacker
  disconnects. `Compute2DCoords` has no timeout and `asyncio.to_thread` cannot be cancelled, so the
  work continues after the client's socket closes and after Chemclaw3's own
  `request_timeout: 30` (`servers/chem/connector.yaml`) has already given up — the timeout is
  client-side only, so the *agent* backs off while the *server* does not. Meanwhile every other
  tool on the process (`resolve_compound`, `stoichiometry_table`) is queued behind the same
  executor. The pod's Containerfile `HEALTHCHECK --timeout=3s` and any k8s liveness probe both fail
  during this window, so the outcome is a restart loop driven by 32 KB of request body.
- **Evidence**: single-call cost first — `Compute2DCoords` on a 5,000-atom chain did not finish
  within a 120 s ceiling:

  ```
  --- n=5000 coords
  parse ok 0.011s atoms=5000
  (killed at 120s, no "coords ok" line)
  ```

  Then the live server (`/tmp/repro/starve.py`, 8 concurrent renders of `"C"*4000`):

  ```
  baseline healthz: {"status":"ok","server":"chem","revision":"unknown"}      # instant
  (during)  /healthz -> httpx.ReadTimeout after 10s
  (after the attacking client exited and cancelled its tasks:)
  t+20s healthz: 000 in 30.002865s
  t+40s healthz: 000 in 30.002894s
  ```

  Two 30 s probes returning nothing, roughly a minute *after* the client went away, is the
  no-cancellation half of the finding measured directly.
- **Fix**: three things, none optional on its own. (1) Bound the input — an atom-count check after
  `require_molecule` and before `Compute2DCoords` in `render_svg` (RDKit exposes
  `mol.GetNumAtoms()`; a depiction above a few hundred atoms is not a picture anyone reads).
  (2) Give the fleet its own bounded executor instead of the shared default `to_thread` pool, so a
  slow tool cannot starve a fast one — `connector_app` is the single place that can own it.
  (3) Put a wall-clock bound on the offloaded call (`asyncio.wait_for` around the `to_thread`) so
  the caller gets a `ValueError` rather than a hang; note this bounds the *coroutine*, not the
  thread, so it only helps in combination with (1).

---

## `rxnpredict`'s two synchronous tools run RDKit on the event loop — one call stalls every other request on the process

- **Severity**: high
- **Location**: `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/tools.py:333`
  (`def list_available_models`) and `:381` (`def classify_reaction`) — declared `def`, not
  `async def`, and neither uses `asyncio.to_thread`. `classify_reaction` calls
  `_classify` (RDKit `MolFromSmiles` + `HasSubstructMatch` over 10 rules) and then
  `_safe_canon_reactants` / `_safe_canon_single` (`MolToSmiles`).
- **Trigger**: one `classify_reaction` call with `reactants = "C" * 15000` (15 KB, 1.5 % of the
  body cap).
- **Consequence**: everything else on the process stops for the duration. The MCP SDK calls a
  synchronous tool body **inline on the event loop** — `mcp/server/fastmcp/utilities/func_metadata.py:93-96`:

  ```python
  if fn_is_async:
      return await fn(**arguments_parsed_dict)
  else:
      return fn(**arguments_parsed_dict)
  ```

  So there is no thread hop anywhere on this path. `rxnpredict` is also the **only** server in the
  fleet with no `tests/test_event_loop_offload.py` — `chem`, `safety` and `calc` all ship one, and
  `chem`'s says in as many words that "while one request depicts a molecule, every other request on
  the process is stopped". The property it guards was never asserted here.
- **Evidence** (`/tmp/repro/attack_rxn.py`, which polls `/healthz` every 100 ms while the tool call
  is in flight, against the real server under uvicorn):

  ```
  --- baseline (small input) ---
  classify_reaction(10-byte SMILES) took 0.09s, isError=False
  WORST /healthz latency during the call: 0.10s
  --- 15000-byte input ---
  classify_reaction(15000-byte SMILES) took 4.97s, isError=False
  WORST /healthz latency during the call: 4.86s
  ```

  0.10 s → 4.86 s on an endpoint that touches nothing: the loop is blocked, not busy.
- **Fix**: make both tools `async def` and wrap the RDKit work in `asyncio.to_thread`, exactly as
  `chem/tools.py` does, and port `tests/test_event_loop_offload.py` into
  `servers/rxnpredict/tests/` so the hop is asserted rather than assumed. The input cap from the
  first finding is still required — offloading turns a total stall into a thread-pool stall (second
  finding), it does not make the work bounded.

---

## `top_k` is unbounded on every served prediction tool; the `le=50` bound lives on schemas nothing calls

- **Severity**: high
- **Location**: `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/tools.py:139-144, 203-209,
  269-274, 300-306` (`top_k: int = 5`, bare `int`) versus
  `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/schemas.py:74` and `:84`
  (`top_k: int = Field(default=5, ge=1, le=50)`). Consumed at
  `engine/predictors/forward/reaction_t5.py:71-72`, `forward/chemformer.py:66-67`,
  `forward/t5chem.py:69-70`, `forward/megan.py:62`, `conditions/reagents_mt.py:75`,
  `conditions/askcos_condition.py:49`, `conditions/parrot.py:55`,
  `conditions/two_stage_dnn.py:60`.
- **Trigger**: `predict_forward_reaction(reactants="CCO", top_k=1000000000)`.
- **Consequence**: the value goes straight into `num_beams=max(top_k, 5)` and
  `num_return_sequences=top_k` on a HuggingFace `generate()` call. Beam-search allocates
  `num_beams`-sized tensors, so a single tool call is an out-of-memory kill of the pod in the
  shipped image (the Containerfile installs `[reaction_t5,rxn_insight]`). `top_k=-1` is also
  accepted and reaches the predictor, where `results[:top_k]` and `n_best=-1` are simply wrong
  rather than refused. `ForwardRequest`/`ConditionsRequest`/`ClassifyRequest` are the REST-era
  envelopes carrying the real bounds and **nothing in the repository references them** — `grep -rn
  "ForwardRequest\|ConditionsRequest\|ClassifyRequest\|HealthResponse" --include=*.py servers/rxnpredict`
  returns only `schemas.py` itself. The validation exists, is reviewable, and is unreachable.
- **Evidence** (`/tmp/probe_topk.py`, which registers a spy predictor and records what it received):

  ```
  ForwardRequest REJECTS top_k=1e9: Input should be less than or equal to 50 [type=less_than_equal, input_value=1000000000, input_type=int]
  MCP tool accepted top_k -> predictor received top_k = 1000000000
  MCP tool accepted top_k=-1 -> predictor received top_k = -1
  advertised schema for top_k: {'default': 5, 'title': 'Top K', 'type': 'integer'}
  ```

  The last line is the tool's own advertised JSON schema as the agent sees it: no `minimum`, no
  `maximum`.
- **Fix**: annotate the tool parameters so the bound is on the served surface —
  `top_k: Annotated[int, Field(ge=1, le=50)] = 5` on all four tools; FastMCP builds its argument
  model from the signature, so the bound then appears in the advertised schema and is enforced
  before the tool body runs. Then delete `ForwardRequest`, `ConditionsRequest`, `ClassifyRequest`
  and `HealthResponse`, which are dead and currently read as the validation that exists. Bound
  `models: list[str] | None` the same way.

---

## The body cap's chunked path returns 500 with a full traceback instead of 413, and its docstring says otherwise

- **Severity**: medium
- **Location**: `packages/mcp_server_kit/src/mcp_server_kit/auth.py:117-183` (`BodySizeLimit`),
  specifically the `except _BodyTooLarge:` at `:173`
- **Trigger**: an authenticated `POST /mcp` with a body sent chunked (no `content-length`) larger
  than `max_bytes`. httpx does this whenever `content=` is an iterator.
- **Consequence**: 500 to the caller and an attacker-triggerable stack trace in the server log
  naming absolute internal paths. `BaseHTTPMiddleware` (both `BearerAuthMiddleware` and
  `CallerLogMiddleware`) runs the downstream app inside an anyio task group, so `_BodyTooLarge`
  arrives at `BodySizeLimit.__call__` wrapped in an `ExceptionGroup` and the bare
  `except _BodyTooLarge` does not match it. It falls through to `ServerErrorMiddleware`. The
  class docstring claims "**Refuse an oversized request body with 413 before any handler reads
  it**" and that "the running total still guards the chunked case where no such declaration
  exists"; `auth.py`'s module docstring separately names "a 500 with a traceback that any remote
  party can produce at will" as the failure mode it exists to avoid. This is that failure mode, one
  layer down. The memory bound itself does hold — the raise fires the moment `seen > max_bytes` —
  so this is a wrong-status/log-noise defect rather than an unbounded read.
- **Evidence** (`/tmp/probe_body.py`, `/tmp/probe_body2.py`, against the real `chem` app):

  ```
  declared content-length=1200064:            413 'request body too large'
  AUTHENTICATED chunked ~1.2MB:               500 'Internal Server Error'
  AUTHENTICATED chunked ~60MB:                500 'Internal Server Error'
  ```

  server log:

  ```
  + Exception Group Traceback (most recent call last):
  |   File ".../starlette/middleware/errors.py", line 186, in __call__
  |     raise exc
  |   File "/workspace/chemclaw3-mcp/packages/mcp_server_kit/src/mcp_server_kit/auth.py", line 172, in __call__
  |     await self._app(scope, counting_receive, send)
  |   File ".../starlette/middleware/base.py", line 198, in __call__
  ...
  | ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)
        | raise _BodyTooLarge
        | mcp_server_kit.auth._BodyTooLarge
  ```

  The only test of this cap, `packages/mcp_server_kit/tests/test_auth.py:110-121`, uses
  `content=b"x" * 4096` — a bytes body, which httpx sends with a `content-length`. It exercises the
  declared-length branch only, which is why the counting branch has been broken and green.
- **Fix**: catch the group as well — `except (_BodyTooLarge, BaseExceptionGroup) as exc:` with a
  re-raise if `_BodyTooLarge` is not among `exc.exceptions` — or, cleaner, do not rely on an
  exception crossing the middleware stack at all: have `counting_receive` return a
  `{"type": "http.disconnect"}` after setting `refused`, and send the 413 from `__call__` once the
  inner app returns. Add the chunked case to `test_auth.py` (pass a generator as `content=`), which
  is the test that would have caught this.

---

## The egress guard does not cover UDP or DNS, and its docstring says it covers everything

- **Severity**: medium
- **Location**: `packages/mcp_server_kit/src/mcp_server_kit/egress.py:117-139` (`arm`, which patches
  only `socket.socket.connect` and `connect_ex`); claim at `:12-14`
- **Trigger**: any code in the process — first-party or, more realistically, a transitive dependency
  — using a connectionless socket or a name lookup, with the guard armed and
  `MCP_EGRESS_ALLOW` empty.
- **Consequence**: data leaves the host with the guard reporting itself armed. The module names the
  exact scenarios it is for — "an ML library fetching model weights on first use, a package phoning
  home with usage telemetry, **a licence check over DNS**" — and then asserts "they all end at one
  place — `socket.socket.connect`". DNS does not; `getaddrinfo` is a libc call, not a `connect`.
  Neither does UDP `sendto`/`sendmsg`, which is the classic low-volume exfiltration channel. The
  static half is consistent with the gap: `no_egress.py:23-40`'s `FORBIDDEN_MODULES` lists no
  DNS client. `_is_loopback`'s own docstring even says "resolving an arbitrary name here would mean
  a DNS lookup, which is itself a call out" — correct, and unguarded everywhere else.
- **Evidence** (`/tmp/probe_egress.py`, guard armed by importing `mcp_server_kit` normally):

  ```
  armed: True allowed: frozenset()
  TCP connect: BLOCKED -> outbound connection to '93.184.216.34' refused: servers in t
  UDP sendto 8.8.8.8:53: SENT 32 bytes -- guard never fired
  getaddrinfo('example.com'): RESOLVED -> ('104.20.23.154', 80)
  UDP sendmsg 1.1.1.1:53: SENT 4 bytes -- guard never fired
  ```

- **Fix**: patch `socket.socket.sendto` and `socket.socket.sendmsg` through the same `_check`
  (both take the destination as their last positional argument when the socket is unconnected), and
  patch `socket.getaddrinfo`/`socket.gethostbyname` to refuse any name that is not loopback or
  allow-listed. Add each to `packages/mcp_server_kit/tests/test_egress.py` — the file currently
  asserts the `connect` path and the default-on behaviour, so the three uncovered calls are exactly
  the ones with no test. Separately, either soften the module docstring's "they all end at one
  place" or make it true; leaving it is the thing this repository's own conventions call a claim
  that outlived its code.

---

## The model checkpoint is pinned to a moving branch, in a file whose docstring says it is pinned by revision — and `SHA256SUMS` records rather than checks

- **Severity**: medium
- **Location**: `servers/rxnpredict/scripts/fetch_models.py:22-25` and `:47-49`;
  `servers/rxnpredict/Containerfile:21-26`
- **Trigger**: any rebuild of the `rxnpredict` image.
- **Consequence**: whatever `sagawa/ReactionT5v2-forward` has on `main` at build time is downloaded
  into the image and becomes the model every prediction comes from — with no reviewable diff, no
  pinned digest, and no comparison against an approved value. The code:

  ```python
  # Pinned by revision, not by tag: a tag can move, and "the model changed under us" is invisible in
  # a rebuild otherwise. Update deliberately, in a pull request, alongside the trust priors that were
  # calibrated against it.
  MODELS: tuple[tuple[str, str], ...] = (("sagawa/ReactionT5v2-forward", "main"),)
  ```

  `"main"` is a branch — strictly more movable than a tag, which is the thing the comment rejects.
  The Containerfile then does `sha256sum > /opt/models/SHA256SUMS`, which hashes whatever arrived;
  nothing ever reads that file, at build or at runtime. Compare
  `packages/mcp_server_kit/src/mcp_server_kit/datasets.py:94-99`, which verifies every vendored
  corpus against a checksum a human approved and refuses to start otherwise — the largest and most
  behaviour-determining artifact in the image is the one without that control. The `trust_priors`
  the aggregator weights every vote by are calibrated against a specific checkpoint, so a silently
  moved checkpoint also silently invalidates them. If the upstream repo ever serves a
  `pytorch_model.bin` rather than safetensors, `from_pretrained` deserialises it, which is the usual
  pickle-execution concern; I did not verify what that repo currently serves, so treat that half as
  conditional — the unreviewed-substitution half needs no condition.
- **Evidence**: the two lines above, plus `Containerfile:26`
  (`find /opt/models -type f -print0 | xargs -0 sha256sum > /opt/models/SHA256SUMS`) and the absence
  of any reader: `grep -rn "SHA256SUMS" servers/rxnpredict` matches only that line.
- **Fix**: pin the immutable commit SHA of the HF repo revision instead of `"main"`, and pass
  `revision=` through to `snapshot_download` (it already is — only the value is wrong). Commit the
  expected `SHA256SUMS` into the repository and have the build stage *verify* against it
  (`sha256sum -c`) rather than generate it, so a moved checkpoint fails the build instead of
  shipping. Optionally pass `local_files_only`-style enforcement at load time; the existing
  `HF_HUB_OFFLINE=1` already covers the runtime half.

---

## `make type` (and therefore CI) never type-checks `servers/rxnpredict`

- **Severity**: low
- **Location**: `Makefile:4`
- **Trigger**: any change to `servers/rxnpredict/src`.
- **Consequence**: the largest server in the repo — the one holding every predictor adapter, the
  aggregator and the unbounded `top_k` above — is outside `mypy --strict`, so a type error there
  ships green. It passes today (I ran `mypy servers/rxnpredict/src` directly: *Success: no issues
  found in 28 source files*), which means the gap is currently latent rather than hiding a defect
  — but it is exactly the drift class `.github/workflows/ci.yml` documents having already fixed
  once ("this step had already drifted — it named three source trees while the Makefile named four,
  so `servers/safety/src` was type-checked locally and not in CI"). The fix moved the list into the
  Makefile and left one server out of it.
- **Evidence**:

  ```make
  SRC := packages/mcp_server_kit/src servers/props/src servers/chem/src servers/safety/src servers/calc/src
  ```

  `servers/rxnpredict/src` is absent, while `pyproject.toml`'s `mypy_path` lists it.
- **Fix**: add `servers/rxnpredict/src` to `SRC`. Better, derive it: `SRC := $(wildcard
  packages/*/src servers/*/src)`, so the next server is covered by existing.

---

## An unauthenticated caller can write unbounded attacker-controlled content into the server's logs

- **Severity**: low
- **Location**: `packages/mcp_server_kit/src/mcp_server_kit/auth.py:98-110`
  (`CallerLogMiddleware.dispatch`), reached for the `OPEN_PATHS` at `:46` because
  `BearerAuthMiddleware` passes those straight through (`:65-66`)
- **Trigger**: `GET /healthz` (or `/metrics`) with a 60 KB `X-Chemclaw-Actor` header and no
  credential.
- **Consequence**: the header value is written verbatim into the log line at `logger.info`, with no
  length cap and no rate limit, by a caller who has no token. In a fleet shipping its logs to a
  shared aggregator this is unauthenticated write access to that stream, and the same request is a
  ~1:1 log-volume amplifier. Newline injection is *not* possible — httptools rejects a bare LF in a
  header value with 400, which I checked rather than assumed — so this is log flooding and
  content spoofing within a line, not line forgery.
- **Evidence** (`/tmp/probe_loginj.py`, raw sockets against the real `chem` server):

  ```
  bare-LF in actor   -> b'HTTP/1.1 400 Bad Request'
  60 KB actor header -> b'HTTP/1.1 200 OK'
  ```

  and the resulting log line, ~60 KB of `A` followed by ` session=- dry_run=-`.
- **Fix**: truncate the three caller headers to a sane bound (say 256 chars) in
  `CallerLogMiddleware.dispatch` before logging or binding them, and skip the log line entirely for
  `OPEN_PATHS` — a kubelet probe every few seconds carries no identity worth recording, which is
  the same argument `auth.py` already makes for leaving those paths open.

---

## Checked and clean (negative results worth recording)

These are the places this lens expected to find something and did not; each was tested, not
assumed.

- **SVG / XSS.** `render_structure` returns raw SVG that a chat surface will render inline, so I
  looked for markup injection through the SMILES. RDKit 2026.03.5 emits **no `<text>` nodes at all**
  — atom labels are rendered as paths — and no `<script>` or `<foreignObject>` in any of nine probe
  inputs including dummy atoms, atom maps, isotopes and reaction SMILES (`/tmp/probe_svg.py`). I
  found no route from SMILES to attacker-controlled markup. Whether the consuming UI sanitises what
  it renders is a question for the UI repo, not a defect here.
- **Bearer check.** Verified it is middleware rather than a route dependency (so the `/` mount does
  not bypass it), that a missing `token_env` variable yields 401 rather than open access, that
  comparison is `hmac.compare_digest` on bytes, and that a non-ASCII header yields 401 rather than
  a 500. All four hold. The one wrinkle — a token containing non-ASCII bytes can never match,
  because the header is latin-1-decoded and the environment value is UTF-8-decoded before both are
  re-encoded as UTF-8 — fails *closed*, so it is a usability bug, not a hole.
- **`/metrics`.** Unauthenticated by design; the docstring claims "counts only". No first-party
  collector is registered anywhere (`grep` for `Counter(`/`Gauge(`/`Histogram(` across `packages`
  and `servers` returns nothing outside tests), so the exposition is `prometheus_client`'s default
  process/GC collectors and the claim holds. No user-controlled label, no cardinality vector.
- **Injection.** No SQL, no shell, no `subprocess`, no `eval`/`exec`, no templating, and no
  filesystem path built from a tool argument anywhere in `chem` or `rxnpredict`. Dataset paths are
  module-relative constants; model paths come from the environment, not from a request.
  `engine/predictors/__init__.py`'s `importlib.import_module` iterates a hardcoded module list, not
  configuration.
- **Manifests.** All five `manifests/<name>/connector.yaml` are symlinks into the servers'
  own files, so the tool surface is declared once. `chem`'s and `rxnpredict`'s `read_only:` lists
  cover every declared tool, which is the classification that fails open if omitted.
- **Deserialisation.** No `pickle`, `yaml.load`, `joblib` or bare `torch.load` in either server;
  `datasets.py` uses `json.loads` on a checksummed file and `yaml.safe_load` is used in
  `testing.py`.
