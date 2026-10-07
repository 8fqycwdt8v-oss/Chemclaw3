"""The compiled egress layer, driven rather than inspected.

Enforcement is asserted by running a real gRPC client in a subprocess against a real listener,
because grpc's C-core bypasses the Python `socket` patch and only a driven test can see that.
Arms: A no interposer → succeeds (the probe can observe success); B interposer, non-loopback →
refused; C interposer, loopback → succeeds; D interposer, allowlisted → succeeds. The listener binds
`0.0.0.0` so one port serves both address kinds, and the subprocess environment is scrubbed of
proxy variables so arm B cannot pass by dialling a loopback proxy.
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

#: The loader variable the entrypoint exports and no chart file may set.
_PRELOAD_VARIABLE = "LD_PRELOAD"


def test_the_entrypoint_puts_the_project_venv_back_in_front_of_the_base_images() -> None:
    """The entrypoint restores the project venv to the front of PATH before running any interpreter.

    The UBI base sets `BASH_ENV=/opt/app-root/bin/activate`, which bash sources on non-interactive
    startup and which prepends an interpreter without `chemclaw`. CI's smoke run uses
    `--entrypoint python` and never triggers it, so this text assertion is the suite's guard.
    """
    entrypoint = _ENTRYPOINT.read_text(encoding="utf-8")
    restore = 'export PATH="/app/.venv/bin:${PATH}"'
    assert restore in entrypoint, (
        "entrypoint.sh does not put the project venv back in front of the base image's; every "
        "`exec python -m chemclaw...` it dispatches will resolve to an interpreter without chemclaw"
    )
    # The first line that runs an interpreter at all, located by line so prose in comments cannot
    # match. The PATH restore must precede it: the arming block already runs `python -m ...` under
    # `set -e`.
    lines = entrypoint.split("\n")
    offsets: list[int] = []
    at = 0
    for line in lines:
        stripped = line.lstrip()
        # Comment lines are prose about interpreters, not uses of one. Excluding them is the whole
        # reason this is a line scan rather than a substring search.
        if stripped and not stripped.startswith("#") and re.search(r"\b(python|uvicorn)\b", line):
            offsets.append(at)
        at += len(line) + 1
    first_interpreter = min(offsets, default=-1)
    assert first_interpreter > 0, (
        "entrypoint.sh runs no interpreter; this test is watching the wrong file"
    )
    assert entrypoint.index(restore) < first_interpreter, (
        "the PATH is restored after something has already run an interpreter, which is after it "
        "could matter — under `set -e` the first such line is where every component dies"
    )


@pytest.fixture(scope="module")
def interposer() -> Path:
    """The built `.so`, compiled by the same script the image build runs.

    One build recipe means the suite proves the binary the image ships, and `-Werror` covers the
    interposer on every run.
    """
    if shutil.which(os.environ.get("CC", "gcc")) is None:  # pragma: no cover - toolchain-dependent
        pytest.skip("no C compiler: the interposer cannot be built, so nothing here is measured")
    # Build into a directory that does not exist yet, as the image does (`/app/lib`), so the
    # script's own directory creation is exercised.
    target = (
        Path(os.environ.get("PYTEST_DEBUG_TEMPROOT", "/tmp"))
        / "netguard-preload-build"
        / netguard_preload.LIBRARY_NAME
    )
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

    Proxy variables are stripped so grpc dials the target itself; `PYTHONPATH` is unset so the
    in-process Python guard plays no part.
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
    """Grpc's C-core is refused at libc, where the Python guard cannot reach it.

    The plain socket and grpc are refused by one mechanism, and the interposer's log line names the
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
    """`::ffff:1.2.3.4` is an IPv4 destination in an IPv6 `sockaddr` and must be unwrapped.

    Otherwise it matches no v4 allowlist or loopback entry, refusing the legitimate form and letting
    a mapped form of a refused address through.
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
    """A name off the allowlist is refused at `getaddrinfo`, so no DNS query leaves.

    The allowed arm proves the interposer does not simply refuse every lookup.
    """
    own = socket.gethostname()
    refused = _drive("0:0", library=interposer, script=_RESOLVE_CLIENT)
    assert "resolve refused" in refused, refused
    allowed = _drive(own, library=interposer, allow=own.lower(), script=_RESOLVE_CLIENT)
    assert "resolve SUCCEEDED" in allowed, allowed


_RESOLVER_FAMILY_CLIENT = """
import ctypes, sys

class hostent(ctypes.Structure):
    _fields_ = [
        ("h_name", ctypes.c_char_p),
        ("h_aliases", ctypes.POINTER(ctypes.c_char_p)),
        ("h_addrtype", ctypes.c_int),
        ("h_length", ctypes.c_int),
        ("h_addr_list", ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))),
    ]

libc = ctypes.CDLL(None, use_errno=True)
name = sys.argv[1].encode()
AF_INET = 2

def report(label, pointer, extra=""):
    if not pointer:
        print(f"{label} refused{extra}")
        return
    entry = pointer.contents
    raw = bytes(entry.h_addr_list[0][i] for i in range(entry.h_length))
    print(f"{label} SUCCEEDED " + ".".join(str(octet) for octet in raw))

plain = libc.gethostbyname
plain.restype, plain.argtypes = ctypes.POINTER(hostent), [ctypes.c_char_p]
report("gethostbyname", plain(name))

two = libc.gethostbyname2
two.restype, two.argtypes = ctypes.POINTER(hostent), [ctypes.c_char_p, ctypes.c_int]
report("gethostbyname2", two(name, AF_INET))

buffer = ctypes.create_string_buffer(8192)
for label, reentrant, leading in (
    ("gethostbyname_r", libc.gethostbyname_r, [ctypes.c_char_p]),
    ("gethostbyname2_r", libc.gethostbyname2_r, [ctypes.c_char_p, ctypes.c_int]),
):
    entry, result, herr = hostent(), ctypes.POINTER(hostent)(), ctypes.c_int(-1)
    reentrant.restype = ctypes.c_int
    reentrant.argtypes = leading + [
        ctypes.POINTER(hostent), ctypes.c_char_p, ctypes.c_size_t,
        ctypes.POINTER(ctypes.POINTER(hostent)), ctypes.POINTER(ctypes.c_int),
    ]
    head = [name] if len(leading) == 1 else [name, AF_INET]
    status = reentrant(*head, ctypes.byref(entry), buffer, len(buffer),
                       ctypes.byref(result), ctypes.byref(herr))
    report(label, result, extra=f" status={status} h_errno={herr.value}")

read = libc.chemclaw_netguard_preload_refused
read.restype, read.argtypes = ctypes.c_ulong, [ctypes.c_int]
print(f"resolve={read(1)} connect={read(0)}")
"""

#: The four entry points the `getaddrinfo`-only check walked past.
_RESOLVER_FAMILY = ("gethostbyname", "gethostbyname2", "gethostbyname_r", "gethostbyname2_r")


def test_the_whole_resolver_family_is_refused_and_not_only_getaddrinfo(interposer: Path) -> None:
    """The whole resolver family is refused, not only `getaddrinfo`.

    The port-53 exemption lets datagrams to the configured nameserver through, so any resolver entry
    point left uninterposed (`gethostbyname` and friends) would carry an arbitrary name to it.
    Driven through `ctypes.CDLL(None)` because CPython's `socket.gethostbyname` goes via
    `getaddrinfo`. The host's own name keeps queries local, and the allowed arm stops a
    refuse-everything interposer passing.
    """
    own = socket.gethostname()
    refused = _drive(own, library=interposer, script=_RESOLVER_FAMILY_CLIENT)
    for entry_point in _RESOLVER_FAMILY:
        assert f"{entry_point} refused" in refused, refused
    # glibc's NXDOMAIN contract: the `_r` forms return 0 with a NULL result and `HOST_NOT_FOUND`
    # (a nonzero status means ERANGE to a caller). Asserted per entry point.
    for entry_point in ("gethostbyname_r", "gethostbyname2_r"):
        assert f"{entry_point} refused status=0 h_errno=1" in refused, refused
    assert "resolve=4 connect=0" in refused, refused
    allowed = _drive(own, library=interposer, allow=own.lower(), script=_RESOLVER_FAMILY_CLIENT)
    for entry_point in _RESOLVER_FAMILY:
        assert f"{entry_point} SUCCEEDED" in allowed, allowed


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
    """The resolver's own address stays reachable on port 53, or every cluster lookup breaks.

    Cluster DNS is non-loopback and c-ares (grpc's resolver) uses libc's `connect`/`sendto`. The
    exemption is narrow: port 53 and an address `/etc/resolv.conf` names.
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


_BATCH_DATAGRAM_CLIENT = """
import ctypes, socket, sys

class iovec(ctypes.Structure):
    _fields_ = [("iov_base", ctypes.c_void_p), ("iov_len", ctypes.c_size_t)]

class msghdr(ctypes.Structure):
    _fields_ = [("msg_name", ctypes.c_void_p), ("msg_namelen", ctypes.c_uint32),
                ("msg_iov", ctypes.POINTER(iovec)), ("msg_iovlen", ctypes.c_size_t),
                ("msg_control", ctypes.c_void_p), ("msg_controllen", ctypes.c_size_t),
                ("msg_flags", ctypes.c_int)]

class mmsghdr(ctypes.Structure):
    _fields_ = [("msg_hdr", msghdr), ("msg_len", ctypes.c_uint)]

host, port = sys.argv[1].rsplit(":", 1)
address = ctypes.create_string_buffer(16)
ctypes.memmove(
    address,
    socket.AF_INET.to_bytes(2, "little") + int(port).to_bytes(2, "big") + socket.inet_aton(host),
    8,
)
payload = ctypes.create_string_buffer(b"probe")
vector = iovec(ctypes.cast(payload, ctypes.c_void_p), 5)
batch = (mmsghdr * 2)()
for message in batch:
    message.msg_hdr.msg_name = ctypes.cast(address, ctypes.c_void_p)
    message.msg_hdr.msg_namelen = 16
    message.msg_hdr.msg_iov = ctypes.pointer(vector)
    message.msg_hdr.msg_iovlen = 1
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
libc = ctypes.CDLL(None, use_errno=True)
libc.sendmmsg.restype = ctypes.c_int
libc.sendmmsg.argtypes = [ctypes.c_int, ctypes.POINTER(mmsghdr), ctypes.c_uint, ctypes.c_int]
sent = libc.sendmmsg(sock.fileno(), batch, 2, 0)
print(f"sendmmsg SUCCEEDED {sent}" if sent > 0 else f"sendmmsg refused {ctypes.get_errno()}")
"""


def test_a_batched_datagram_to_an_undeclared_host_is_refused(interposer: Path) -> None:
    """A `sendmmsg` batch to an undeclared host is refused.

    It carries one destination per message, so it must be checked like `sendto`/`sendmsg`. One
    refused address refuses the whole batch, since a partial send would report success for traffic
    this layer did not permit.
    """
    address = _non_loopback_address()
    if address is None:  # pragma: no cover - single-interface host
        pytest.skip("no non-loopback interface: there is no off-box route to measure")
    refused = _drive(f"{address}:53", library=interposer, script=_BATCH_DATAGRAM_CLIENT)
    assert "sendmmsg refused" in refused, refused
    assert f"refused sendmmsg to {address}:53" in refused, refused
    allowed = _drive(
        f"{address}:53", library=interposer, allow=address, script=_BATCH_DATAGRAM_CLIENT
    )
    assert "sendmmsg SUCCEEDED" in allowed, allowed


_ARMED_CLIENT = """
from chemclaw.core import netguard_preload
print(f"armed={netguard_preload.is_armed()}")
"""


def test_armed_is_read_from_the_linker_and_not_from_the_environment(interposer: Path) -> None:
    """`is_armed()` asks `dlsym`, not the environment.

    The loader silently ignores an `LD_PRELOAD` naming a missing path, so a gauge fed from the
    variable would report a layer that is not there.
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
    """The entrypoint preloads the path the image installs.

    The loader ignores a missing `LD_PRELOAD` target silently, so the Containerfile's build target,
    the entrypoint's export and `netguard_preload.LIBRARY_PATH` are pinned against each other.
    """
    containerfile = _CONTAINERFILE.read_text(encoding="utf-8")
    entrypoint = _ENTRYPOINT.read_text(encoding="utf-8")
    build = next(
        line for line in containerfile.splitlines() if netguard_preload.LIBRARY_NAME in line
    )
    assert netguard_preload.LIBRARY_PATH in build, build
    assert f'export LD_PRELOAD="{netguard_preload.LIBRARY_PATH}' in entrypoint


def test_the_image_builds_the_interposer_through_the_one_build_recipe() -> None:
    """The image compiles the interposer through the same build script the suite uses.

    A second `gcc` invocation would let the suite prove one binary while the image ships another.
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
    """Arming is unconditional in the entrypoint.

    Not inside a `case` branch, behind a chart value, or defaulted off: a component skipping it
    would run with the compiled path open while the Python layer's signals reported health.
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
            assert not re.match(rf"\s*(-\s*name:\s*)?{_PRELOAD_VARIABLE}\s*:?\s*", line), (
                f"{document.name} sets {_PRELOAD_VARIABLE}; arming belongs to the image so that "
                "`docker run`, the connector pods and a hand-started worker cannot differ from "
                "the chart"
            )


#: The image's `ENTRYPOINT`, which is the only thing that arms the compiled layer. A Kubernetes
#: `command:` **replaces** it rather than prefixing it, which is the whole of the finding below.
_IMAGE_ENTRYPOINT = "/usr/local/bin/chemclaw-entrypoint"

#: Containers that run this image without reaching the entrypoint, each with its argument. The test
#: derives the real set from the templates, so a new workload with its own `command:` fails until
#: it is argued here.
_CONTAINERS_THAT_BYPASS_THE_ENTRYPOINT: dict[str, str] = {
    # `git clone`/`fetch` against the note remote, which is not on the settings `derive_allowed`
    # reads, so arming would refuse the sync. Bounded by the NetworkPolicy instead.
    "knowledge-sync-init": "git against the note remote, which no setting derives",
    "knowledge-sync": "git against the note remote, which no setting derives",
    "note-repo-init": "git against the note remote, which no setting derives",
}


def _image_containers() -> dict[str, list[str]]:
    """Every container in the chart that runs `chemclaw.image`, mapped to its `command`.

    Derived from template text (this suite is offline; `make helm-validate` renders). `command:` is
    matched at the container's own field indentation, so a nested `lifecycle` hook command is not
    mistaken for it; both the inline JSON list and the block list forms are read.
    """
    chart = _ROOT / "deploy" / "helm" / "chemclaw" / "templates"
    found: dict[str, list[str]] = {}
    for document in [*sorted(chart.glob("*.yaml")), chart / "_helpers.tpl"]:
        lines = document.read_text(encoding="utf-8").splitlines()
        starts = [
            (index, len(match.group(1)), match.group(2))
            for index, line in enumerate(lines)
            if (match := re.match(r"^(\s*)- name: ([A-Za-z0-9-]+(?:\{\{[^}]*\}\})?)\s*$", line))
        ]
        for position, (index, indent, name) in enumerate(starts):
            end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
            block = lines[index + 1 : end]
            field = " " * (indent + 2)
            if not any(
                line.startswith(f"{field}image:") and "chemclaw.image" in line for line in block
            ):
                continue
            command: list[str] = []
            for offset, line in enumerate(block):
                if not line.startswith(f"{field}command:"):
                    continue
                inline = line.split("command:", 1)[1].strip()
                if inline:
                    command = re.findall(r'"([^"]*)"', inline)
                else:
                    for item in block[offset + 1 :]:
                        entry = re.match(rf"{field}  - (.*)$", item)
                        if entry is None:
                            break
                        command.append(entry.group(1).strip().strip('"'))
                break
            found[name] = command
    return found


def test_every_container_running_this_image_reaches_the_entrypoint_that_arms_it() -> None:
    """Every container running this image reaches the entrypoint that arms it.

    A Kubernetes `command:` replaces the image `ENTRYPOINT`, and arming happens only inside
    `deploy/entrypoint.sh`, so a container with its own `command:` runs without the compiled layer.
    Hook Jobs declare no port, so the armed gauge cannot reveal this either.
    """
    containers = _image_containers()
    assert len(containers) > 5, f"the container derivation found almost nothing: {containers}"
    bypassing = {
        name: command
        for name, command in containers.items()
        if command and command[0] != _IMAGE_ENTRYPOINT
    }
    unargued = {
        name: command
        for name, command in bypassing.items()
        if name not in _CONTAINERS_THAT_BYPASS_THE_ENTRYPOINT
    }
    assert not unargued, (
        "these containers run the Chemclaw image with their own `command:`, which replaces the "
        f"ENTRYPOINT that arms the compiled egress layer: {unargued}. Either drop the `command:` "
        "and name the component in `CHEMCLAW_COMPONENT`, or add the container to "
        "`_CONTAINERS_THAT_BYPASS_THE_ENTRYPOINT` with the argument for it"
    )
    assert set(_CONTAINERS_THAT_BYPASS_THE_ENTRYPOINT) <= set(containers), (
        "the exemption list names a container this chart no longer has: "
        f"{sorted(set(_CONTAINERS_THAT_BYPASS_THE_ENTRYPOINT) - set(containers))}"
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
    """Fail closed: a broken posture derivation stops the container.

    An unguarded pod is indistinguishable from a guarded one until something exfiltrates.
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
    """The interposer's source states what dynamic linking lets past it.

    Raw `syscall(SYS_connect, …)`, a `dlopen`ed libc and `res_query` cannot be interposed, so the
    assertion is that the module says so. `sendmmsg` is interposed and must be absent from that
    list, since a stale concession reads as an open gap.
    """
    source = netguard_preload.SOURCE.read_text(encoding="utf-8")
    header = source.split("*/", 1)[0]
    for concession in ("statically linked", "syscall directly", "res_query", "dlopen"):
        assert concession in header, f"the header does not concede {concession!r}"
    assert "sendmmsg" not in header.split("WHAT IT DOES NOT COVER", 1)[1], (
        "the header still concedes `sendmmsg`, which this layer now interposes"
    )


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
