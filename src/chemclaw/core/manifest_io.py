"""One reader for every manifest this system loads, and one rule for the fields that execute.

**Why this module exists at all.** Six seams — `connector.yaml`, `datasource.yaml`, `sink.yaml`,
`channel.yaml`, a template and a profile — each read a YAML file the same way and each got it
slightly differently wrong. The 2026-09 re-review drove all six against the same corpus and found
one defect class shared by every one of them:

- **No expansion budget.** A 375-byte `connector.yaml` using YAML anchors cost 687 MB of RSS and
  3.2 s, and at one more level of nesting drove the process past 9 GB. `yaml.safe_load` itself is
  cheap and innocent here — it shares aliased nodes, so the parse is 93 objects — but the *shape*
  it hands to pydantic expands 43 million times during validation. Bounding the parse would have
  measured 93 and passed; the bound has to be on the **expanded** size, which is what
  `_expanded_size` computes: memoised per node, so a 43-million-node document is counted in 93
  steps of arithmetic and never materialised.
- **`RecursionError` is not a `YAMLError`.** Every loader caught `(OSError, yaml.YAMLError)`, and
  2000-deep nesting raises neither — so the failure escaped naming no file, was not the seam's own
  error type, and slipped past the `except ValueError` startup handlers this tree relies on.
- **Duplicate keys were silently last-wins.** PyYAML takes the last of a repeated key with no
  diagnostic, and `extra="forbid"` offers nothing because the key is not extra. On an endpoint that
  matters exactly as much as the classification partition does: measured, a manifest declaring
  `state_changing: [a, b]` and then repeating `state_changing: []` / `read_only: [a, b]` loaded
  with **two write tools classified as reads**, which is the plan gate's input (D-167) failing open.

Folding them here fixes each once instead of six times, and gives the "must be a mapping" rule its
one home — five of the six loaders had it, written five slightly different ways, and the sixth
(`publish/registry._load`) did not have it at all.

**`error` is a parameter rather than a new exception class**, deliberately. Temporal matches
`non_retryable_error_types` by exact class *name*, so a shared `ManifestError` would have to be
registered in `durable.publish._BAD_DATA_TYPES` and every seam would lose the type its own callers
and CLIs already catch (`connector-validate` reports a `ConnectorError`, not a traceback). Passing
the seam's own error type keeps one reader and six contracts.
"""

from importlib import import_module
from pathlib import Path
from typing import Any, TypeVar

import yaml

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError

# The largest manifest this tree ships is 27,623 bytes, so ~36x headroom. The cap exists to make a
# hostile file cheap to refuse *before* it is parsed; it is not the control that stops the alias
# bomb, which is 375 bytes on disk.
MAX_MANIFEST_BYTES = 1_000_000
# The deepest shipped manifest nests 11 levels (`eln-databricks`), so ~6x headroom. Checked during
# composition, which is where PyYAML's own recursion happens, so the bound is hit before the C
# stack is.
MAX_MANIFEST_DEPTH = 64
# The largest shipped manifest is 163 nodes, so ~600x headroom. Counted *expanded* — every alias
# reference counts again — because that is the number pydantic pays and the number a bomb inflates.
MAX_MANIFEST_NODES = 100_000
# The ceiling on one model-facing prose field: a job's summary or description, a template's, a
# parameter's. ~1,000 estimated tokens at this tree's chars/4 estimator, against a largest shipped
# field of 1,418 characters.
#
# **What this does and does not bound.** It closes "one field, unbounded": measured, a 5.4 MB
# `connector.yaml` produced a 5,200,664-character tool docstring, which is the text the model is
# sent on every call, from one file that every validator passed. It does *not* bound a bundle's
# total contribution — a manifest with a hundred jobs is still a hundred bounded tools — because
# the total prefix is a property of the whole bound surface and `tests/test_context_floor.py` is
# the ratchet that owns it. That ratchet measures the graph built from the *shipped* bundles, so
# an out-of-tree bundle on `connectors_dir` is outside it by construction; this bound is what an
# out-of-tree bundle is held to instead.
MAX_MANIFEST_TEXT_CHARS = 4_000

_E = TypeVar("_E", bound=ChemclawError)


class _ManifestLoader(yaml.SafeLoader):
    """`SafeLoader` plus a depth bound and a duplicate-key refusal.

    Subclassed rather than configured because neither rule is expressible as an option. It stays a
    `SafeLoader`, so `!!python/` remains refused — the one property of these loaders the review
    found already sound, and the one this class must not weaken.
    """

    def __init__(self, stream: Any) -> None:
        super().__init__(stream)
        self._depth = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        """Compose one node, refusing a document nested deeper than `MAX_MANIFEST_DEPTH`.

        The check is here rather than on the constructed object because PyYAML's composer is the
        recursive step: past ~1,000 levels it raises `RecursionError` from inside the C stack, and
        an error raised here names the manifest instead.
        """
        self._depth += 1
        if self._depth > MAX_MANIFEST_DEPTH:
            raise yaml.constructor.ConstructorError(
                None,
                None,
                f"nested deeper than {MAX_MANIFEST_DEPTH} levels; a manifest is a declaration, "
                "not a data structure",
                getattr(parent, "start_mark", None),
            )
        try:
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        """Build a mapping, refusing a key that appears twice.

        PyYAML's own behaviour is last-wins with no diagnostic, which is wrong everywhere and
        dangerous in one place: a repeated `state_changing:` silently discards the first list, and
        that partition is the plan gate's input, so the mistake fails *open* while the file still
        reads as if the tools were gated.
        """
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                continue
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    "while building a mapping",
                    node.start_mark,
                    f"key {key!r} appears more than once; PyYAML would silently keep the last "
                    "one, which is how a classification list gets discarded without a diagnostic",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _expanded_size(value: Any, error: type[_E], path: Path) -> int:
    """How many nodes `value` has once every alias reference is counted again.

    Memoised on `id()`, so the classic billion-laughs — whose parse is a handful of shared objects
    — is counted by arithmetic rather than by walking it. Recursion is safe because
    `MAX_MANIFEST_DEPTH` has already bounded the composed document; a *cycle* (`&a [*a]`) is the
    one shape depth cannot bound, so it is refused by name rather than counted forever.
    """
    memo: dict[int, int] = {}
    active: set[int] = set()

    def walk(node: Any) -> int:
        key = id(node)
        cached = memo.get(key)
        if cached is not None:
            return cached
        if isinstance(node, dict | list):
            if key in active:
                raise error(f"{path}: manifest refers to itself; a manifest is a flat declaration")
            active.add(key)
            items = node.values() if isinstance(node, dict) else node
            total = 1 + sum(walk(item) for item in items)
            active.discard(key)
        else:
            total = 1
        memo[key] = total
        return total

    return walk(value)


def read_manifest(path: Path, error: type[_E]) -> dict[str, Any]:
    """Read one manifest file into a mapping, raising `error` naming the file on any problem.

    The four checks, in the order a hostile file meets them: the file is not absurdly large, it
    parses under a depth bound with duplicate keys refused, it does not expand past
    `MAX_MANIFEST_NODES`, and its root is a mapping. Every one of them raises the caller's own
    error type, so a validator that catches `ConnectorError` keeps reporting a line instead of a
    traceback.
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise error(f"{path}: unreadable manifest: {exc}") from exc
    if size > MAX_MANIFEST_BYTES:
        raise error(
            f"{path}: manifest is {size} bytes, over the {MAX_MANIFEST_BYTES}-byte ceiling; "
            "a manifest declares a capability, it does not carry a corpus"
        )
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_ManifestLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise error(f"{path}: unreadable or malformed YAML: {exc}") from exc
    except RecursionError as exc:
        # Not a `YAMLError`, so every loader here used to let it escape untranslated — naming no
        # file, and not the seam's own type, so the `except ValueError` startup handlers and
        # Temporal's name-matched non-retryable set both missed it.
        raise error(f"{path}: YAML is nested too deeply to parse") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise error(f"{path}: must contain a YAML mapping, got {type(raw).__name__}")
    nodes = _expanded_size(raw, error, path)
    if nodes > MAX_MANIFEST_NODES:
        raise error(
            f"{path}: expands to {nodes} nodes, over the {MAX_MANIFEST_NODES}-node ceiling. "
            "YAML aliases multiply: this file is small on disk and is not small once expanded"
        )
    return raw


def check_driver_module(reference: str, error: type[_E], field: str) -> None:
    """Refuse a `module:callable` whose top-level package no operator has allowed.

    **A manifest is data** — the sentence `connectors/registry.py` already uses to refuse
    `transport: stdio` and `core/config/connectors.py` uses to refuse an unfunded wall-clock
    ceiling. This is the same rule applied to the other field family that executes: `params_model`,
    `precondition`, `ingest`, `retrieve`, `commitments` and `driver` are imported, and the sink and
    channel seams then *call* what they resolve, with the manifest's own `config:` as keyword
    arguments. Measured, a manifest naming a module wrote a file at import time inside
    `job_tools()` — the per-turn agent-build path — with `connector-validate`, `sink-validate` and
    `datasource-validate` all exiting 0.

    **Why a package allow-list is the proportionate control, and what it does not claim.** The
    threat this seam actually has is a manifest arriving on a discovery path that is *not* the
    installed package — a mounted ConfigMap, a CI job syncing a sibling repo — because discovery is
    enablement. Such a directory is not on `sys.path`, so the module it names has to already be
    importable, and `chemclaw` plus whatever the operator deliberately added is exactly the set of
    things that are. It does **not** defend against someone who can write a `.py` inside the
    installed `chemclaw` package: that is the code, and a manifest rule is not what stops it.

    The D-118/D-120 property survives intact: a bundle whose driver lives in this tree needs no
    setting, and a third-party driver is one env var, set once by the operator who mounted the
    directory — the same "an operator turns it on" shape `connector_stdio_enabled` has, so there is
    one idiom here rather than two.
    """
    module_name = reference.partition(":")[0]
    top = module_name.partition(".")[0]
    if top in settings.manifest_driver_package_list:
        return
    raise error(
        f"{field} {reference!r} names package {top!r}, which is not on "
        f"CHEMCLAW_MANIFEST_DRIVER_PACKAGES ({sorted(settings.manifest_driver_package_list)}). "
        "This field is imported and called in this process, and a manifest is data: add the "
        "package deliberately, as an operator, or move the driver into `chemclaw`."
    )


def resolve_driver(reference: str, error: type[_E], field: str) -> Any:
    """Import `module:callable` from an allowed package and return it, or raise `error`.

    The one resolver the sink, channel and data-source seams share. Late-bound on purpose — a
    process that never delivers never imports a delivery client — so the allow-list check runs
    here, at the moment of import, and again at parse time in each manifest model so
    `make sink-validate` and its siblings refuse the file without importing anything.
    """
    check_driver_module(reference, error, field)
    module_name, _, attribute = reference.partition(":")
    try:
        module = import_module(module_name)
    except ImportError as exc:
        raise error(
            f"cannot import {module_name!r} for {field} {reference!r}: {exc}. A driver's client "
            "package is installed only where that seam is actually used."
        ) from exc
    resolved = getattr(module, attribute, None)
    if resolved is None:
        raise error(f"{module_name!r} has no attribute {attribute!r} (from {reference!r})")
    if not callable(resolved):
        raise error(f"{reference!r} is not callable")
    return resolved


def within_root(root: Path, candidate: Path) -> bool:
    """Whether `candidate` still resolves inside `root` once every symlink is followed.

    Discovery is enablement: any subdirectory of a discovery root holding a manifest is loaded, and
    both `iterdir()` and `is_file()` follow symlinks, so a `linked -> /anywhere/else` entry inside
    the root loaded that other directory's manifest as a bundle. Measured, it did.

    **Resolution rather than "refuse a symlink"**, because a symlink inside a discovery root is
    ordinary: a Kubernetes ConfigMap volume presents every key as a symlink into its own `..data`
    directory, and refusing the link outright would refuse the shipped delivery mechanism. What
    matters is where it lands — a ConfigMap's link lands inside the mount, and the escape does not.
    """
    try:
        return candidate.resolve().is_relative_to(root.resolve())
    except OSError:
        # A broken or looping link resolves to nothing that can be compared; that is not inside.
        return False
