"""One reader for every manifest this system loads, and one rule for the fields that execute.

Every seam (`connector.yaml`, `datasource.yaml`, `sink.yaml`, `channel.yaml`, templates,
profiles) reads YAML through `read_manifest`, which enforces:

- an expansion budget on the alias-expanded size, since anchors make a tiny file expand by many
  orders of magnitude during validation while the parse itself stays small;
- a depth bound, because deep nesting raises `RecursionError`, which is not a `YAMLError`;
- duplicate-key refusal, since PyYAML is silently last-wins and a repeated `state_changing:` would
  classify write tools as reads, failing the plan gate open;
- a mapping root.

`error` is a parameter rather than a shared exception class so each seam keeps the error type its
callers and CLIs already catch, and Temporal (which matches non-retryable types by exact name)
needs no new registration.
"""

from importlib import import_module
from pathlib import Path
from typing import Any, TypeVar

import yaml

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError

# Makes a hostile file cheap to refuse before parsing, with wide headroom over shipped manifests. It
# is not what stops an alias bomb, which is tiny on disk.
MAX_MANIFEST_BYTES = 1_000_000
# Several times the deepest shipped manifest. Checked during composition, where PyYAML recurses, so
# it
# trips before the C stack does.
MAX_MANIFEST_DEPTH = 64
# The largest shipped manifest is 163 nodes, so ~600x headroom. Counted *expanded* — every alias
# reference counts again — because that is the number pydantic pays and the number a bomb inflates.
MAX_MANIFEST_NODES = 100_000
# Ceiling on one model-facing prose field (a job's, template's or parameter's summary or
# description), about 1,000 estimated tokens. It bounds a single field sent to the model on every
# call, not a bundle's total contribution: the shipped total is ratcheted by
# `tests/test_context_floor.py`, and this bound is what an out-of-tree bundle is held to.
MAX_MANIFEST_TEXT_CHARS = 4_000

_E = TypeVar("_E", bound=ChemclawError)


class _ManifestLoader(yaml.SafeLoader):
    """`SafeLoader` plus a depth bound and a duplicate-key refusal.

    Subclassed because neither rule is an option. It must stay a `SafeLoader` so `!!python/` tags
    are
    refused.
    """

    def __init__(self, stream: Any) -> None:
        super().__init__(stream)
        self._depth = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        """Compose one node, refusing a document nested deeper than `MAX_MANIFEST_DEPTH`.

        Checked in the composer because that is PyYAML's recursive step, so the error names the
        manifest
        instead of surfacing as `RecursionError`.
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

        PyYAML is last-wins with no diagnostic; a repeated `state_changing:` would silently discard
        the
        first list and fail the plan gate open.
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

    Memoised on `id()`, so a billion-laughs document is counted by arithmetic rather than walked.
    Recursion is safe because depth is already bounded; a cycle (`&a [*a]`) is refused by name.
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

    Checks, in the order a hostile file meets them: size, parse under the depth bound with duplicate
    keys refused, expanded size against `MAX_MANIFEST_NODES`, and a mapping root. Every failure
    raises
    the caller's own error type.
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
        # Not a `YAMLError`: translated so it names the file and is the seam's own type.
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

    A manifest is data, yet fields like `params_model`, `precondition`, `ingest`, `retrieve` and
    `driver` are imported and some are called with the manifest's own `config:`; importing an
    arbitrary
    module runs its code on the agent-build path. The threat is a manifest arriving on a discovery
    path
    outside the installed package (a mounted ConfigMap, a synced repo), so the allow-list is
    `chemclaw`
    plus packages an operator adds explicitly. It does not defend against writing code into the
    installed package itself.
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

    Shared by the sink, channel and data-source seams. Late-bound so a process that never delivers
    never imports a delivery client; the allow-list is checked here and again at parse time in each
    manifest model, so validators refuse a file without importing anything.
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

    Discovery is enablement, and `iterdir()`/`is_file()` follow symlinks, so a link out of the root
    would load a foreign manifest. Resolved rather than refusing symlinks outright, because a
    Kubernetes ConfigMap volume presents every key as a symlink into its own `..data` directory.
    """
    try:
        return candidate.resolve().is_relative_to(root.resolve())
    except OSError:
        # A broken or looping link resolves to nothing that can be compared; that is not inside.
        return False
