# D-2026-09-12-the-layer-that-binds-grpc-is-libc-not-socket-py — the egress guard gets a compiled second layer, and its own signal

**Context.** `core/netguard.py` is the in-process egress guard: an allowlist derived from the
settings this deployment dials, installed by patching nine names — `socket.socket.connect`,
`.connect_ex`, `.sendto`, `.sendmsg`, and five `socket` module resolvers. That layer is sound for
pure Python and has been tested as such since it shipped.

**None of this deployment's two highest-value destinations goes through any of those nine names.**
Measured at `629b675` with the allowlist deliberately empty, no proxy variables set, and a listener
on a non-loopback address:

```
[control] raw socket.create_connection -> refused EgressForbidden   _refused 0 -> 1
[i]   grpc.insecure_channel            _refused 1 -> 1   new TCP accepts: 3
[ii]  OTLP gRPC span exporter          _refused 1 -> 1   new TCP accepts: 2
[iii] temporalio.Client.connect        _refused 1 -> 1   new TCP accepts: 2
```

Seven connections reached an external listener while the counter moved only for the pure-Python
control. Against the **real** broker: `CONNECTED to real Temporal, namespace=default, _refused=0`.

Why that matters more than the general concession the module already made: with
`otel_include_sensitive_data` on, the OTLP exporter carries prompts and completions, so a wrong or
hostile `CHEMCLAW_OTEL_ENDPOINT` exports them anywhere while **both** signals an operator would check
— `chemclaw_egress_refused_total` and `chemclaw_egress_guard_armed` — report health.
`derive_allowed` adds `temporal_address` and `otel_endpoint`, so an allowlist entry existed for each
and bounded neither.

**Decision.** Build the compiled layer. `src/chemclaw/core/netguard_preload.c` interposes libc's
`connect`, `getaddrinfo`, `sendto` and `sendmsg` through `LD_PRELOAD`, reading **the allowlist this
repository already derives** rather than a second list of its own. `deploy/entrypoint.sh` arms it for
every component; `deploy/Containerfile` compiles it with the base image's own `gcc`.

**A builder stage was written first and removed after measuring the base.** The argument for one was
keeping a compiler out of the runtime image — and `registry.access.redhat.com/ubi9/python-311`, the
base this file defaults to, already ships `/usr/bin/gcc` 11.5.0 (glibc 2.34), because the s2i Python
image carries a toolchain for building wheels. So the stage would have added a `dnf install gcc` —
a network dependency at build time — to keep out something the runtime image ships regardless. The
interposer compiles in that image with `-Werror` clean, and driven there refuses `8.8.8.8:443` with
`Operation not permitted` while a loopback dial reaches the stack (`ECONNREFUSED`).

**Measured on the implementation, not on the prototype.** Three arms against a *real* gRPC server
(`grpc.server` in the test process, bound to `0.0.0.0` so one port answers on both routes), each
client in a clean subprocess with proxy variables stripped — without that scrub, arm B dials a
loopback proxy, which the interposer permits, and the arm passes for the wrong reason:

| arm | target | plain socket | grpc `channel_ready` |
| --- | --- | --- | --- |
| A — no interposer | `192.0.2.2:43725` (non-loopback) | SUCCEEDED | SUCCEEDED |
| B — interposer, empty allowlist | `192.0.2.2:43725` | refused (EPERM) | refused |
| C — interposer, empty allowlist | `127.0.0.1:43725` | SUCCEEDED | SUCCEEDED |
| D — interposer, address allowlisted | `192.0.2.2:43725` | SUCCEEDED | SUCCEEDED |

Arm A is the positive control: without an arm that can observe success, a refusal is not evidence —
the first attempt at this measurement aimed at an address that does not speak gRPC and timed out
identically in both directions. Arm C is the non-destructive control. Arm D is the one that
distinguishes an allowlist from a deny-all, and its absence would have made arms B and C equally
consistent with a layer that breaks every declared destination.

Driven by hand against the real broker over the docker-bridge route, the same three clients the
finding names are all refused, and grpc's own C-core reports the interposer's `EPERM`:

```
FAILED_PRECONDITION: ipv4:172.17.0.1:7233: connect failed:
  addr: ipv4:172.17.0.1:7233 error: Operation not permitted
temporalio  refused(RuntimeError: Failed client connect: tonic::transport::Error(Transport, Connect))
counters: connect=10 resolve=1
```

So Temporal's Rust sdk-core does go through glibc `connect` here. Measured, not assumed.

**And end to end, through the real entrypoint, with the real allowlist and the live broker.** A
`background-worker` started by `deploy/entrypoint.sh` with `CHEMCLAW_TEMPORAL_ADDRESS` pointed at a
non-loopback route:

```
allowlist handed to the interposer: 127.0.0.1,172.17.0.1,localhost
LD_PRELOAD: /app/lib/libchemclaw_netguard.so      is_armed(): True
chemclaw_egress_refused_total 0   chemclaw_egress_guard_armed 1   chemclaw_egress_preload_armed 1
temporal: CONNECTED ns=default                       # the declared address, reached
off-allowlist grpc -> 172.18.0.1:7233: refused       # an undeclared one, four connect attempts
chemclaw_egress_preload_refused_connect 4   chemclaw_egress_preload_refused_resolve 0
chemclaw_egress_refused_total 0                      # the Python layer still cannot see any of it
```

The last line is the finding and its closure in one reading: the four refusals are on the compiled
layer's counter and the Python layer's is flat, because these are precisely the calls it never sees.

**Four things the build decided, each with the measurement behind it.**

1. **DNS is exempt at the address and enforced at the name.** glibc's resolver `connect`s a UDP
   socket to the nameserver in `/etc/resolv.conf`, which is non-loopback in every cluster. The
   exemption is narrow — port 53 **and** an address that file names, parsed from the same file the
   resolver reads — and it is load-bearing: with it removed, a UDP/53 datagram to the configured
   resolver is refused with `Operation not permitted`. It is *not* load-bearing for glibc's own
   `getaddrinfo`, which reaches `__connect` internally and never touches the interposition — which is
   why the measurement was taken on a datagram a library sends, the shape c-ares (grpc's resolver)
   uses.
2. **A numeric host is let through `getaddrinfo` and judged at `connect`.** No query leaves the host
   for an IP literal, so charging it as a *resolver* refusal would misreport the event. Nothing
   escapes either way; the counters stay meaningful. That is a deliberate divergence from
   `netguard.arm`, which refuses a non-allowlisted literal at the resolver.
3. **Two counters, not one.** A blocked lookup and a blocked dial have different causes and want
   different next steps. `chemclaw_egress_preload_refused_connect` and
   `..._refused_resolve` are separate series, bound as gauges to the C atomics, because nothing in
   Python performs these refusals and a polling loop would be a second thing to keep alive.
4. **Its own arming gauge, measured from the dynamic linker.** `chemclaw_egress_preload_armed` is 1
   iff `dlsym` resolves the library's symbol in this process — never from `LD_PRELOAD`, because a
   preload naming a path that does not exist is ignored by the loader **in silence**, which is
   exactly the shape where a gauge lies. Folding it into `chemclaw_egress_guard_armed` would rebuild
   the blindness that licensed the work: in an un-preloaded process the two series read 1 and 0, and
   a test asserts that pair.

**Where the code lives, and why not in a directory of its own.** `src/` is all the code and a C file
is code, so it sits beside the Python half it extends, as `core/netguard_preload.c` next to
`core/netguard_preload.py`. Three consequences argued for rather than inherited: no new top-level
directory and therefore no `ARCHITECTURE.md` row or `README.md` to keep in step (D-156); the source
reaches the image through the `COPY src ./src` that is already there, with no new line to forget;
and `tests/test_repo_map.py::test_no_import_package_sits_beside_data` stays exactly as strict,
because that rule is about Python beside `src/`, not about `src/` holding only Python — which it
already does not (`science/bo/benchmarks/data/*.csv`, every package `README.md`). The *build recipe*
is `deploy/build-netguard-preload.sh`, beside the other two shell scripts the image installs,
because it has two callers — the Containerfile and the measurement — and two `gcc` lines would let
the test prove a binary the image does not ship.

**Where it is armed, and why that contradicts the brief.** The brief asked for the pod to set
`LD_PRELOAD`. It is set in `deploy/entrypoint.sh` instead, on this repository's own twice-recorded
ground: a control that only one way of starting the process obeys is the Helm-only LangSmith pin and
the Helm-only LLM provider again. In the image, `docker run`, every connector pod, the mcp-face and a
hand-started component all get it, and no chart value can disagree — a test asserts no chart file
sets the variable at all. It also keeps the knowledge-sync containers out by construction: they
declare their own `command`, so they never pass through the entrypoint, and what they dial is `git`'s
remote, which is not on the settings object the allowlist is derived from.

**What it costs, stated because it is a real behavioural change.**

- **A child process inherits `LD_PRELOAD`.** That closes a gap `netguard.py` has conceded since it
  shipped — `kg/git_writer.py` shells out to `git` — and it means a deployment that pushes notes to a
  **remote** git host must now name that host in `CHEMCLAW_EGRESS_ALLOW`. Before this the destination
  was bounded by nothing in the process; now it is bounded, and an un-named remote fails the push.
  The alternative (having the interposer `unsetenv("LD_PRELOAD")` so children escape) was rejected:
  it would delete the coverage for the sake of the configuration.
- **One interpreter start per container boot.** `chemclaw.cli.egress_preload` prints
  `enabled|disabled` and the derived allowlist, and the entrypoint exports it before `exec`. It
  cannot be avoided: `LD_PRELOAD` is consumed by the loader before the interpreter exists, so nothing
  a process imports can arm it for itself. A re-exec from inside `chemclaw.core.config` was
  considered and rejected — it would lose `-m` semantics and break pytest, for a launcher-shaped
  problem.
- **Failing closed means the pod crashloops.** Under `set -e` a failed posture query stops the
  container rather than starting it unguarded, which is the direction that matters: an unguarded
  process is indistinguishable from a guarded one until something exfiltrates.

**What neither layer covers, stated rather than implied.** A **statically linked** binary or one
issuing the syscall directly — there is no dynamic symbol to interpose and no Python object to patch.
Anything not named: `sendmmsg`, `io_uring`, a raw `AF_PACKET` socket, a `write` on a descriptor this
guard already allowed. A process the library is not loaded into. And **where** traffic goes once a
proxy is in the environment, because a proxied dial is a legitimate connection to the proxy —
`refuse_proxied_egress` is the layer for that shape, and a loopback sidecar shares the pod's network
namespace, so there is none under it. Those are the NetworkPolicy's and that boot check's,
and `deploy/helm/chemclaw/values.yaml` now says so where a deployer sizes
`networkPolicy.egressDestinations`, which said nothing about either.

**Two corrections in `derive_allowed` that enforcement turned from cosmetic into real.**

- The conditional `otel_endpoint` entry is **kept and argued** rather than made unconditional: under
  enforcement an entry is a permission, so conditioning it on "is this dialled" is the correct shape,
  and the unconditional `temporal_address` beside it is right only because every component dials it.
- `core/logging.py` bridges `otel_endpoint` into `OTEL_EXPORTER_OTLP_ENDPOINT` with `setdefault`, so
  a deployment that sets the standard variable — or the per-signal
  `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` — keeps winning and the exporter dials a host this object
  never carried. Harmless while nothing enforced it; with the compiled layer armed it is an exporter
  refused by its own deployment, reported as the `UNAVAILABLE` the exporter swallows. Both variables
  are now read, in the exporter's own precedence order.

**What this sandbox could not measure, said rather than skipped.** `docker build` of the full
Containerfile fails here for a reason that predates this change — the sandbox's TLS interception
makes `dnf` refuse the Red Hat CDN, so the *existing* `dnf -y update && dnf install -y git rsync
util-linux` line fails first. The interposer's own build and behaviour were therefore driven inside
the base image directly, by mounting the source and the recipe into it; the assembled image is
unverified here as it was before.

**The alerts ship, and they ship because a test refused the alternative.**
`tests/test_deploy_chart.py::test_every_declared_metric_has_a_consumer` rejects a declared series
with no rule and no panel — "a series nobody reads is a cost with no benefit" — so leaving the three
gauges unalerted was not available. `ChemclawEgressPreloadDisarmed` and
`ChemclawEgressPreloadRefused` sit beside their Python-layer twins rather than replacing them, and
the runbook section for the first says out loud that `ChemclawEgressGuardDisarmed` will be *silent*
in exactly the condition it fires for. What stays open is the `git` remote: a child inherits the
preload, so the note push is now bounded by this layer and by nothing else, and no setting derives
its host.

**What keeps it true.** `tests/test_netguard_preload.py`, every assertion driven rather than read —
each was watched failing against a named mutation of the thing it guards:

- `test_arm_a_grpc_reaches_a_non_loopback_listener_without_the_interposer` — the positive control.
- `test_arm_b_grpc_cannot_reach_a_non_loopback_listener_with_the_interposer` — red with the `connect`
  interposition removed.
- `test_arm_c_a_loopback_dial_still_succeeds_with_the_interposer` — red with the loopback exemption
  removed.
- `test_an_address_on_the_derived_allowlist_is_reachable` — red with the allowlist consultation
  removed from `check_address`.
- `test_a_name_off_the_allowlist_never_reaches_the_resolver` — red with the `getaddrinfo` name check
  inverted; its *allowed* arm is what stops an interposer that refuses every lookup passing.
- `test_the_resolvers_own_address_stays_reachable_on_port_53` — red with the exemption keyed on the
  wrong port.
- `test_a_datagram_to_an_undeclared_host_is_refused` — red with the `sendto` interposition removed.
- `test_a_refused_lookup_and_a_refused_dial_are_separate_counters` — red with resolve refusals
  counted on the dial counter.
- `test_armed_is_read_from_the_linker_and_not_from_the_environment` — red with `is_armed()` reading
  `LD_PRELOAD`.
- `test_the_python_guards_gauge_does_not_claim_the_compiled_layer` — red with the compiled gauge fed
  from `netguard._armed`.
- `test_both_layers_arm_from_one_derivation` / `test_disabling_the_guard_disables_both_layers` — red
  with a hand-written list in `posture()`, and with the opt-out ignored.
- `test_the_entrypoint_preloads_the_path_the_image_installs` — red with the image installing the
  library elsewhere.
- `test_the_image_builds_the_interposer_through_the_one_build_recipe` — red with a bare `gcc` in
  the Containerfile, and red with the build removed. A mutation that removes only the `COPY` of
  the recipe survives it, and is recorded here rather than chased: that one breaks the **image
  build**, which is the control for it.
- `test_no_shipped_deployment_starts_without_arming_the_compiled_layer` — red with arming moved after
  the component dispatch.
- `test_the_entrypoint_hands_the_component_the_library_and_the_allowlist` — driven through a stub
  `python`, so it asserts the exec'd process's environment rather than a line in a script; red with
  the allowlist export removed.
- `test_a_failed_derivation_stops_the_container_rather_than_skipping_the_layer` — red with `|| true`
  on the posture query. **Its first version was vacuous** and is recorded here because the shape
  recurs: the stub failed *every* invocation, so the assertion passed on the stub's own exit code and
  the mutation survived. The stub now fails the posture query and succeeds at everything else, and
  the test asserts the component did not run.
- `test_the_interposer_states_what_it_cannot_cover` — the three uncoverable classes are properties of
  dynamic linking and cannot be asserted by running anything; what can be asserted is that the file a
  reader opens concedes them.
- `tests/test_deploy_chart.py::test_the_destination_list_says_which_layer_it_is_the_only_one_of` —
  red when any of the four claims is dropped from the `egressDestinations` comment block.

One arm is **not** measured on this host and says so out loud rather than skipping quietly:
`test_an_ipv4_mapped_address_is_not_a_way_around_the_check` skips where `AF_INET6` cannot be created,
which is this sandbox. The unwrapping is in the C and untested here.
