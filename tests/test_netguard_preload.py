"""The compiled egress layer, driven rather than inspected.

Every assertion here about *enforcement* runs a real client in a real subprocess against a real
listener, because the defect that licensed this layer is exactly the shape an inspecting test cannot
see: `netguard.py` patches `socket.socket`, every unit test of it passed, and grpc's C-core walked
past all of it. A test that read the source, or asserted that the chart sets `LD_PRELOAD`, would
have been green throughout.

Three arms, and the two controls are the point:

  A  no interposer, non-loopback target  → SUCCEEDS. Without this the probe cannot observe success,
     and a refusal proves nothing — the first attempt at this measurement aimed at an address that
     does not speak gRPC and timed out identically in both directions.
  B  interposer, non-loopback target     → refused.
  C  interposer, loopback target         → SUCCEEDS. The layer must not break the dials this
     deployment makes to Postgres, Temporal and the calc backend.

The listener is a **real gRPC server** in the test process, bound to `0.0.0.0` so the same port is
reachable by both a loopback and a non-loopback address. `grpc.channel_ready_future` only resolves
once the HTTP/2 transport is up, so "SUCCEEDED" means grpc's C-core actually connected.

**The subprocess environment is scrubbed of proxy variables.** grpc reads `grpc_proxy`,
`https_proxy` and `http_proxy` regardless of the target's scheme, and a sandbox with an ambient
proxy would have arm B dialling a loopback proxy — which the interposer permits — so the arm would
pass for the wrong reason and arm B would be measuring nothing.
"""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
from collections.abc import Iterator
from concurrent import futures
from pathlib import Path

import pytest

from chemclaw.cli.egress_preload import posture
from chemclaw.core import netguard_preload
from chemclaw.core.config import Settings
from chemclaw.core.netguard import derive_allowed

_ROOT = Path(__file__).resolve().parents[1]
_BUILD_SCRIPT = _ROOT / "deploy" / "build-netguard-preload.sh"
_ENTRYPOINT = _ROOT / "deploy" / "entrypoint.sh"
_CONTAINERFILE = _ROOT / "deploy" / "Containerfile"

#: `SIOCGIFADDR`. Used to read an interface's own address without a connect, a DNS lookup or any
#: other operation the guard under test might refuse.
_SIOCGIFADDR = 0x8915


@pytest.fixture(scope="module")
def interposer() -> Path:
    """The built `.so`, compiled by the **same script the image build runs**.

    Through `deploy/build-netguard-preload.sh` rather than a `gcc` line of its own: two invocations
    of the compiler would let this file prove a binary the image does not ship, which is the
    second-declaration defect the script exists to prevent. Building it here also means `-Werror`
    covers the interposer on every run of this suite.
    """
    if shutil.which(os.environ.get("CC", "gcc")) is None:  # pragma: no cover - toolchain-dependent
        pytest.skip("no C compiler: the interposer cannot be built, so nothing here is measured")
    target = Path(os.environ.get("PYTEST_DEBUG_TEMPROOT", "/tmp")) / netguard_preload.LIBRARY_NAME
    subprocess.run(
        ["sh", str(_BUILD_SCRIPT), str(netguard_preload.SOURCE), str(target)],
        check=True,
        capture_output=True,
    )
    return target


def _non_loopback_address() -> str | None:
    """This host's own non-loopback IPv4 address, read from an interface rather than resolved."""
    for _, name in socket.if_nameindex():
        if name == "lo":
            continue
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            packed = fcntl.ioctl(
                probe.fileno(), _SIOCGIFADDR, struct.pack("256s", name.encode()[:15])
            )
        except OSError:
            continue
        finally:
            probe.close()
        address = socket.inet_ntoa(packed[20:24])
        if not address.startswith("127."):
            return address
    return None


@pytest.fixture(scope="module")
def grpc_server() -> Iterator[int]:
    """A real gRPC server on every interface, so one port answers on loopback and off it."""
    grpc = pytest.importorskip("grpc")
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    port = server.add_insecure_port("0.0.0.0:0")
    server.start()
    try:
        yield int(port)
    finally:
        server.stop(grace=None)


_CLIENT = """
import socket, sys
target = sys.argv[1]
host, port = target.rsplit(":", 1)
try:
    s = socket.create_connection((host, int(port)), timeout=5); s.close(); print("socket SUCCEEDED")
except Exception as exc:
    print(f"socket refused {type(exc).__name__}")
import grpc
channel = grpc.insecure_channel(target)
try:
    grpc.channel_ready_future(channel).result(timeout=8); print("grpc SUCCEEDED")
except Exception as exc:
    print(f"grpc refused {type(exc).__name__}")
"""


def _drive(target: str, *, library: Path | None, allow: str = "", script: str = _CLIENT) -> str:
    """Run `script` against `target` in a clean subprocess, optionally with the interposer loaded.

    Proxy variables are stripped: with one set, grpc dials the proxy instead of the target and every
    arm would report success. `PYTHONPATH` is not set, so the child imports no first-party module
    and the in-process Python guard plays no part in what is measured.
    """
    environment = {
        key: value
        for key, value in os.environ.items()
        if "proxy" not in key.lower() and key != "LD_PRELOAD"
    }
    if library is not None:
        environment["LD_PRELOAD"] = str(library)
        environment[netguard_preload.ALLOWLIST_VARIABLE] = allow
    completed = subprocess.run(
        [sys.executable, "-c", script, target],
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
    )
    return completed.stdout + completed.stderr


def test_arm_a_grpc_reaches_a_non_loopback_listener_without_the_interposer(
    grpc_server: int,
) -> None:
    """The positive control. A probe that cannot see success cannot report a refusal as evidence."""
    address = _non_loopback_address()
    if address is None:  # pragma: no cover - single-interface host
        pytest.skip("no non-loopback interface: there is no off-box route to measure")
    output = _drive(f"{address}:{grpc_server}", library=None)
    assert "socket SUCCEEDED" in output, output
    assert "grpc SUCCEEDED" in output, output


def test_arm_b_grpc_cannot_reach_a_non_loopback_listener_with_the_interposer(
    grpc_server: int, interposer: Path
) -> None:
    """The finding, closed: grpc's C-core is refused at libc, where Python cannot reach it.

    `netguard.arm` patches `socket.socket` and the `socket` module resolvers, and measured against
    this same listener a `grpc.insecure_channel` connected with its refusal counter flat. Here the
    plain socket and grpc are refused by one mechanism, and the interposer's own line names the
    destination.
    """
    address = _non_loopback_address()
    if address is None:  # pragma: no cover - single-interface host
        pytest.skip("no non-loopback interface: there is no off-box route to measure")
    output = _drive(f"{address}:{grpc_server}", library=interposer)
    assert "socket refused" in output, output
    assert "grpc refused" in output, output
    assert f"refused connect to {address}:{grpc_server}" in output, output


def test_arm_c_a_loopback_dial_still_succeeds_with_the_interposer(
    grpc_server: int, interposer: Path
) -> None:
    """The non-destructive control.

    This deployment dials Postgres, Temporal and the calc backend on loopback in development; they
    must be untouched, or the layer is a broken deployment rather than a guard.
    """
    output = _drive(f"127.0.0.1:{grpc_server}", library=interposer)
    assert "socket SUCCEEDED" in output, output
    assert "grpc SUCCEEDED" in output, output


def test_an_address_on_the_derived_allowlist_is_reachable(
    grpc_server: int, interposer: Path
) -> None:
    """The layer is an allowlist, not a deny-all — the same shape as `netguard.derive_allowed`.

    Without this, arm B is equally consistent with an interposer that refuses everything off
    loopback, which would make the deployment's own declared destinations unreachable.
    """
    address = _non_loopback_address()
    if address is None:  # pragma: no cover - single-interface host
        pytest.skip("no non-loopback interface: there is no off-box route to measure")
    output = _drive(f"{address}:{grpc_server}", library=interposer, allow=address)
    assert "socket SUCCEEDED" in output, output
    assert "grpc SUCCEEDED" in output, output


_MAPPED_CLIENT = """
import socket, sys
target = sys.argv[1]
host, port = target.rsplit(":", 1)
try:
    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM); s.settimeout(5)
except OSError:
    print("socket unsupported"); raise SystemExit(0)
try:
    s.connect((f"::ffff:{host}", int(port))); print("socket SUCCEEDED")
except Exception as exc:
    print(f"socket refused {type(exc).__name__}")
"""


def test_an_ipv4_mapped_address_is_not_a_way_around_the_check(
    grpc_server: int, interposer: Path
) -> None:
    """`::ffff:1.2.3.4` is an IPv4 destination wearing an IPv6 `sockaddr`.

    Read as opaque v6 bytes it matches no v4 allowlist entry and no v4 loopback test, so a check
    that did not unwrap it would refuse the legitimate form and — worse — let a mapped form of a
    *refused* address through a table keyed on v4 text.
    """
    address = _non_loopback_address()
    if address is None:  # pragma: no cover - single-interface host
        pytest.skip("no non-loopback interface: there is no off-box route to measure")
    refused = _drive(f"{address}:{grpc_server}", library=interposer, script=_MAPPED_CLIENT)
    if "socket unsupported" in refused:  # pragma: no cover - host without IPv6
        pytest.skip("no AF_INET6 on this host: the mapped-address unwrapping is not measured here")
    assert f"refused connect to {address}" in refused, refused
    allowed = _drive(
        f"{address}:{grpc_server}", library=interposer, allow=address, script=_MAPPED_CLIENT
    )
    assert "socket SUCCEEDED" in allowed, allowed


_COUNTER_CLIENT = """
import ctypes, socket, sys
library = ctypes.CDLL(None)
read = library.chemclaw_netguard_preload_refused
read.restype, read.argtypes = ctypes.c_ulong, [ctypes.c_int]
host, port = sys.argv[1].rsplit(":", 1)
try:
    socket.create_connection((host, int(port)), timeout=5)
except Exception:
    pass
try:
    socket.getaddrinfo("collector.not-declared.invalid", 4317)
except Exception:
    pass
print(f"connect={read(0)} resolve={read(1)}")
"""


def test_a_refused_lookup_and_a_refused_dial_are_separate_counters(
    grpc_server: int, interposer: Path
) -> None:
    """Two events, two numbers.

    A blocked name and a blocked address have different causes and want different next steps, and
    one counter covering both cannot say which happened.
    """
    address = _non_loopback_address()
    if address is None:  # pragma: no cover - single-interface host
        pytest.skip("no non-loopback interface: there is no off-box route to measure")
    output = _drive(f"{address}:{grpc_server}", library=interposer, script=_COUNTER_CLIENT)
    assert "connect=1 resolve=1" in output, output


_RESOLVE_CLIENT = """
import socket, sys
name = sys.argv[1]
try:
    socket.getaddrinfo(name, 80, socket.AF_INET)
    print("resolve SUCCEEDED")
except Exception as exc:
    print(f"resolve refused {type(exc).__name__}")
"""


def test_a_name_off_the_allowlist_never_reaches_the_resolver(interposer: Path) -> None:
    """A DNS query is a round trip in its own right, and the name is the covert channel.

    Refused at `getaddrinfo`, so nothing leaves — and the *allowed* arm is what makes this mean
    something: an interposer that refused every lookup would pass the first assertion and break
    every pod.
    """
    own = socket.gethostname()
    refused = _drive("0:0", library=interposer, script=_RESOLVE_CLIENT)
    assert "resolve refused" in refused, refused
    allowed = _drive(own, library=interposer, allow=own.lower(), script=_RESOLVE_CLIENT)
    assert "resolve SUCCEEDED" in allowed, allowed


_NAMESERVER_CLIENT = """
import socket, sys
header = bytes.fromhex("abcd01000001000000000000")
query = header + b"\\x07example\\x03com\\x00" + bytes.fromhex("00010001")
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(4)
try:
    sock.sendto(query, (sys.argv[1], 53)); print("sendto SUCCEEDED")
except Exception as exc:
    print(f"sendto refused {type(exc).__name__}")
"""


def _first_nameserver() -> str | None:
    """The first `nameserver` in `/etc/resolv.conf`, the file the resolver itself reads."""
    try:
        lines = Path("/etc/resolv.conf").read_text(encoding="utf-8").splitlines()
    except OSError:  # pragma: no cover - no resolver configuration
        return None
    for line in lines:
        if line.startswith("nameserver"):
            candidate = line[len("nameserver") :].strip().split()[0].split("%")[0]
            if candidate and not candidate.startswith("127."):
                return candidate
    return None


def test_the_resolvers_own_address_stays_reachable_on_port_53(interposer: Path) -> None:
    """Without this exemption the interposer breaks every lookup in the cluster.

    Cluster DNS is non-loopback, and a resolver that uses libc's public `connect`/`sendto` — c-ares,
    which is what grpc resolves names with — would be refused by address before any name could be
    judged. Measured both ways: with the exemption removed from the C source, this same datagram is
    refused with `Operation not permitted`. The exemption is narrow on purpose — port 53 **and** an
    address `/etc/resolv.conf` names — so it is not a hole a library can dial through.
    """
    nameserver = _first_nameserver()
    if nameserver is None:  # pragma: no cover - loopback or absent resolver
        pytest.skip("no non-loopback nameserver configured: nothing to exempt")
    output = _drive(nameserver, library=interposer, script=_NAMESERVER_CLIENT)
    assert "sendto SUCCEEDED" in output, output


def test_a_datagram_to_an_undeclared_host_is_refused(interposer: Path) -> None:
    """A datagram is its own entry point, because `sendto` never calls `connect`.

    A compiled extension's datagram is past both `netguard.arm`'s patched `socket.sendto` and its
    patched `connect`.
    """
    address = _non_loopback_address()
    if address is None:  # pragma: no cover - single-interface host
        pytest.skip("no non-loopback interface: there is no off-box route to measure")
    output = _drive(address, library=interposer, script=_NAMESERVER_CLIENT)
    assert "sendto refused" in output, output
    assert f"refused sendto to {address}:53" in output, output


_ARMED_CLIENT = """
from chemclaw.core import netguard_preload
print(f"armed={netguard_preload.is_armed()}")
"""


def test_armed_is_read_from_the_linker_and_not_from_the_environment(interposer: Path) -> None:
    """An `LD_PRELOAD` naming a path that does not exist is ignored by the loader **in silence**.

    So a gauge fed from the variable would report a layer that is not there, which is the exact
    failure this whole ADR is about: a signal an operator checks, reading health over an open path.
    `is_armed()` asks `dlsym` instead.
    """
    loaded = _drive("unused", library=interposer, script=_ARMED_CLIENT)
    assert "armed=True" in loaded, loaded
    missing = _drive(
        "unused", library=Path("/nonexistent/libchemclaw_netguard.so"), script=_ARMED_CLIENT
    )
    assert "armed=False" in missing, missing


def test_the_python_guards_gauge_does_not_claim_the_compiled_layer() -> None:
    """The half of the finding that made it serious: one gauge read 1 over an open compiled path.

    This process has the Python guard armed (it imported `chemclaw.core.config`) and no interposer,
    and the two series say exactly that. Folding them into one would rebuild the blindness.
    """
    from chemclaw.core.metrics import METRICS

    exposition = METRICS.render()
    assert "chemclaw_egress_guard_armed 1" in exposition
    assert "chemclaw_egress_preload_armed 0" in exposition
    assert not netguard_preload.is_armed()


def test_both_layers_arm_from_one_derivation() -> None:
    """One allowlist, two enforcement points.

    A second list in C or in shell would drift silently — the two layers would refuse different
    hosts, and only one of them logs where anybody greps.
    """
    settings = Settings()
    word, _, allowlist = posture().partition(" ")
    assert word == "enabled"
    assert allowlist == ",".join(sorted(derive_allowed(settings)))


def test_disabling_the_guard_disables_both_layers(monkeypatch: pytest.MonkeyPatch) -> None:
    """One knob, not two.

    A deployment that takes the stated opt-out must not end up with a compiled layer refusing what
    the Python layer was configured to allow.
    """
    from chemclaw.cli import egress_preload
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "egress_guard_enabled", False)
    assert egress_preload.posture().split(" ")[0] == "disabled"


def test_the_entrypoint_preloads_the_path_the_image_installs() -> None:
    """An `LD_PRELOAD` pointing at nothing is ignored by the loader without a word.

    So the three spellings of this path — the Containerfile's build target, the entrypoint's export
    and `netguard_preload.LIBRARY_PATH` — are pinned against each other rather than kept equal by
    hand.
    """
    containerfile = _CONTAINERFILE.read_text(encoding="utf-8")
    entrypoint = _ENTRYPOINT.read_text(encoding="utf-8")
    build = next(
        line for line in containerfile.splitlines() if netguard_preload.LIBRARY_NAME in line
    )
    assert netguard_preload.LIBRARY_PATH in build, build
    assert f'export LD_PRELOAD="{netguard_preload.LIBRARY_PATH}' in entrypoint


def test_the_image_builds_the_interposer_through_the_one_build_recipe() -> None:
    """The image must compile the source this suite measures, with the flags this suite uses.

    A second `gcc` invocation is the whole risk: the suite would then prove a binary built one way
    while the image shipped one built another, and `-Werror` or `-fPIC` drifting apart is exactly
    the kind of difference that shows up only in the cluster.
    """
    containerfile = _CONTAINERFILE.read_text(encoding="utf-8")
    instructions = [
        line for line in containerfile.splitlines() if line.startswith(("RUN ", "COPY ", "    "))
    ]
    body = "\n".join(instructions)
    assert _BUILD_SCRIPT.name in body, "the image does not run the shared build recipe"
    assert netguard_preload.SOURCE.name in body, "the image does not compile the interposer"
    assert "gcc" not in body, (
        "the Containerfile invokes a compiler directly; the flags belong to "
        f"deploy/{_BUILD_SCRIPT.name}, which is what the measurement builds with"
    )


def test_no_shipped_deployment_starts_without_arming_the_compiled_layer() -> None:
    """The `MCP_EGRESS_GUARD=off` shape, refused: arming is unconditional in the entrypoint.

    Not inside a `case` branch, not behind a chart value, and not defaulted off — a component that
    did not pass through the arming block would run with the compiled path open while both of the
    Python layer's signals reported health.
    """
    entrypoint = _ENTRYPOINT.read_text(encoding="utf-8")
    arming = entrypoint.index("chemclaw_egress_posture=")
    dispatch = entrypoint.index('component="${CHEMCLAW_COMPONENT')
    assert arming < dispatch, "the interposer is armed after the component dispatch begins"
    # The *declaration*, not the word: the chart legitimately explains this layer at
    # `networkPolicy.egressDestinations` and names the variable in an alert description. What it
    # must not do is set it, which in a chart is a YAML key or a `name:` in an env list.
    chart = _ROOT / "deploy" / "helm" / "chemclaw"
    for document in [chart / "values.yaml", *sorted((chart / "templates").glob("*.yaml"))]:
        for line in document.read_text(encoding="utf-8").splitlines():
            assert not re.match(r"\s*(-\s*name:\s*)?LD_PRELOAD\s*:?\s*", line), (
                f"{document.name} sets LD_PRELOAD; arming belongs to the image so that `docker "
                "run`, the connector pods and a hand-started worker cannot differ from the chart"
            )


_STUB_PYTHON = """#!/bin/sh
case "$*" in
  *chemclaw.cli.egress_preload*) echo "enabled gateway.example,postgres.example" ;;
  *) echo "LD_PRELOAD=${LD_PRELOAD-unset}"; echo "ALLOW=${CHEMCLAW_NETGUARD_PRELOAD_ALLOW-unset}" ;;
esac
"""


def test_the_entrypoint_hands_the_component_the_library_and_the_allowlist(tmp_path: Path) -> None:
    """The effect, not the shape: what the `exec`ed component's environment actually holds.

    Driven with a stub `python` that answers the posture query and then reports its own environment,
    so this asserts the export reached the process rather than that the script contains a line.
    """
    stub = tmp_path / "python"
    stub.write_text(_STUB_PYTHON, encoding="utf-8")
    stub.chmod(0o755)
    completed = subprocess.run(
        ["bash", str(_ENTRYPOINT)],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "CHEMCLAW_COMPONENT": "background-worker",
        },
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert f"LD_PRELOAD={netguard_preload.LIBRARY_PATH}" in completed.stdout, completed.stdout
    assert "ALLOW=gateway.example,postgres.example" in completed.stdout, completed.stdout


#: Fails the posture query and *succeeds* at everything else. The distinction is the whole test: a
#: stub that failed both would make the assertion below pass for the stub's own exit code, which is
#: how the first version of this test came out vacuous under mutation.
_FAILING_POSTURE_STUB = """#!/bin/sh
case "$*" in
  *chemclaw.cli.egress_preload*) echo "boom" >&2; exit 3 ;;
  *) echo "COMPONENT RAN"; exit 0 ;;
esac
"""


def test_a_failed_derivation_stops_the_container_rather_than_skipping_the_layer(
    tmp_path: Path,
) -> None:
    """Fail closed.

    The one outcome that must not happen is a pod that starts unguarded because the posture query
    broke — an unguarded process is indistinguishable from a guarded one until something
    exfiltrates.
    """
    stub = tmp_path / "python"
    stub.write_text(_FAILING_POSTURE_STUB, encoding="utf-8")
    stub.chmod(0o755)
    completed = subprocess.run(
        ["bash", str(_ENTRYPOINT)],
        capture_output=True,
        text=True,
        env={"PATH": f"{tmp_path}:/usr/bin:/bin", "CHEMCLAW_COMPONENT": "background-worker"},
        timeout=60,
    )
    assert completed.returncode != 0, completed.stdout
    assert "COMPONENT RAN" not in completed.stdout, (
        "the component started even though the egress posture could not be derived"
    )


def test_the_interposer_states_what_it_cannot_cover() -> None:
    """A layer that implies more than it enforces is the defect, not the documentation of it.

    The three uncoverable classes are properties of dynamic linking rather than of this code, so
    they cannot be asserted by running anything — what can be asserted is that the module says so,
    in the file a reader opens.
    """
    source = netguard_preload.SOURCE.read_text(encoding="utf-8")
    header = source.split("*/", 1)[0]
    for concession in ("statically linked", "syscall directly", "sendmmsg"):
        assert concession in header, f"the header does not concede {concession!r}"


def test_every_preload_metric_this_module_binds_is_declared() -> None:
    """Every gauge this module binds must be one the registry declares.

    `bind_gauge` raises on an undeclared name and is called best-effort inside a bare `except`, so a
    renamed gauge would disappear from the exposition in total silence.
    """
    from chemclaw.core.metrics import declared_metric_names

    bound = set(
        re.findall(
            r'bind_gauge\(\s*"(chemclaw_egress_preload_\w+)"',
            Path(netguard_preload.__file__).read_text(encoding="utf-8"),
        )
    )
    assert len(bound) == 3
    assert bound <= declared_metric_names()
