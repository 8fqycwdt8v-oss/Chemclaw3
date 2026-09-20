"""`model_copy(update=…)` assigns past validation, and a fixture that does it may own its subject.

**The defect this file exists for, and the measurement that reshaped it.** A guard for two new
model fields was green with both fields *deleted*, because its fixture built the answer with
`model_copy(update=…)` — which writes straight into `__dict__` and therefore supplies a key whether
or not the model declares it. `tasks/lessons.md` records the rule ("a fixture built past validation
is a fixture that supplies the subject"), and the review that found it proposed flagging every
`model_copy(update=…)` in a test whose subject model declares `extra="ignore"` or `extra="forbid"`.

**That heuristic was measured and it separates nothing, in both directions.** Every one of the 149
call sites the suite actually executes was instrumented: the copy was re-validated through
`model_validate` and compared with what `model_copy` produced. Not one site injects a key the model
does not declare, not one produces an object `model_validate` refuses, and exactly one produces an
object that differs from the validated form (`test_structure.py`'s deliberate
`Structure(**noisy.model_dump())`, which re-crosses the boundary on purpose). Meanwhile
`extra="ignore"`/`"forbid"` selects 65 of those 149 — and `extra="ignore"` is *pydantic's default*,
so the 80 sites on models that declare nothing behave identically to the 6 that declare `"ignore"`.
A property shared by four fifths of the tree, held by every harmless site and by no dangerous one,
is not a discriminator.

**So "dangerous" is not a property of the object.** No fixture here builds an impossible one. What
the found defect actually was is a *relation* between the fixture and the assertion: the fixture
supplied the very thing whose treatment was under test. That relation is not readable from the call
site — nothing in `x.model_copy(update={"version": …})` says whether the test is about `version`.

What *is* readable is the narrower proposition that makes the relation possible, and this file
derives two halves of it from the live tree rather than asserting either:

* **A check the constructor runs and the copy does not.** The subject model's own
  `model_validator(mode="after")` bodies are read, and a site is flagged when the key it updates is
  one a validator inspects. `Structure._normalize_and_validate` rounds `positions` and refuses an
  impossible `multiplicity`; a copy that sets either has skipped that, so the object is inside the
  model's type but outside its rules.
* **A boundary production actually crosses.** `src/` is read for `Model.model_validate(` — if the
  system obtains this model by validating, then a fixture that does not validate is not what the
  system receives, which is the lesson's own criterion ("the fixture has to cross that boundary the
  way production crosses it") with the crossing *found* instead of assumed.

Flagged sites are argued in `_ARGUED`, held in both directions so an entry that stops being flagged
fails too — the same shape as `tests/test_claude_md_figures.py`. A flag is not a verdict: most of
these are fine, and the entry says why.

**The admissibility arm carries no allowlist**, because an object the model would refuse cannot be
argued for. It is empty today and that is the measurement above, not an absence of checking:
`test_the_rule_can_fail` drives both arms over a model defined here.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pathlib
import re
from typing import Any

import pydantic

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_TESTS = _ROOT / "tests"
_SRC = _ROOT / "src"

#: `Model.model_validate(` / `.model_validate_json(` anywhere in `src/` — the evidence that
#: production obtains that model by crossing a validation boundary rather than by constructing it.
_VALIDATED_IN_SRC = re.compile(r"\b([A-Z][A-Za-z0-9_]*)\s*\.\s*model_validate(?:_json)?\s*\(")

#: `self.<field>` inside a validator body: the fields that validator inspects.
_SELF_FIELD = re.compile(r"self\.([a-z_][a-z0-9_]*)")

#: Every flagged site, keyed `<file>::<test>::<Model>.<field>` so the entry survives an edit above
#: it, with the reason the fixture may build its subject that way. Held in both directions.
_ARGUED: dict[str, str] = {}


def _innermost_name(annotation: ast.expr | None) -> str | None:
    """The class name inside an annotation such as `list[Foo] | None`, or None."""
    if annotation is None:
        return None
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Attribute):
        return annotation.attr
    if isinstance(annotation, ast.Subscript):
        return _innermost_name(annotation.slice) or _innermost_name(annotation.value)
    if isinstance(annotation, ast.BinOp):
        return _innermost_name(annotation.left) or _innermost_name(annotation.right)
    if isinstance(annotation, ast.Tuple) and annotation.elts:
        return _innermost_name(annotation.elts[0])
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            return _innermost_name(ast.parse(annotation.value, mode="eval").body)
        except SyntaxError:
            return None
    return None


class _Module:
    """One test module, indexed for the three questions this file asks of it."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self.tree = ast.parse(path.read_text(encoding="utf-8"))
        self.parent: dict[int, ast.AST] = {}
        for node in ast.walk(self.tree):
            for child in ast.iter_child_nodes(node):
                self.parent[id(child)] = node
        self.functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions.setdefault(node.name, node)
        self.imported: dict[str, str] = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    self.imported[alias.asname or alias.name] = node.module

    def scopes(self, node: ast.AST) -> list[ast.AST]:
        """Enclosing scopes of `node`, innermost first."""
        out: list[ast.AST] = []
        current: ast.AST | None = self.parent.get(id(node))
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.append(current)
            current = self.parent.get(id(current))
        return out

    def subject(self, receiver: ast.expr, node: ast.AST, depth: int = 0) -> str | None:
        """The class name of `receiver`, followed through the shapes this suite writes.

        Deliberately partial: a receiver this cannot name is reported by
        `test_the_walk_names_most_of_the_subjects_it_finds` rather than silently dropped, because a
        resolver that goes blind turns this whole file green.
        """
        if depth > 6:
            return None
        if isinstance(receiver, ast.Call):
            func = receiver.func
            if isinstance(func, ast.Attribute) and func.attr.startswith("model_"):
                return self.subject(func.value, node, depth + 1)
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name is None:
                return None
            if name[:1].isupper():
                return name
            helper = self.functions.get(name)
            return _innermost_name(helper.returns) if helper is not None else None
        if isinstance(receiver, ast.Subscript):
            return self.subject(receiver.value, node, depth + 1)
        if isinstance(receiver, ast.Attribute):
            owner = self.subject(receiver.value, node, depth + 1)
            return f"{owner}.{receiver.attr}" if owner else None
        if not isinstance(receiver, ast.Name):
            return None
        for scope in [*self.scopes(node), self.tree]:
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = scope.args
                for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                    if arg.arg == receiver.id and arg.annotation is not None:
                        named = _innermost_name(arg.annotation)
                        if named:
                            return named
            bound: ast.expr | None = None
            for sub in ast.walk(scope):
                if isinstance(sub, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == receiver.id for t in sub.targets
                ):
                    bound = sub.value
                elif (
                    isinstance(sub, ast.AnnAssign)
                    and isinstance(sub.target, ast.Name)
                    and sub.target.id == receiver.id
                ):
                    named = _innermost_name(sub.annotation)
                    if named:
                        return named
                elif isinstance(sub, (ast.For, ast.comprehension)) and (
                    isinstance(sub.target, ast.Name) and sub.target.id == receiver.id
                ):
                    element = self.subject(sub.iter, node, depth + 1)
                    if element and "." not in element:
                        return element
            if bound is not None:
                return self.subject(bound, node, depth + 1)
        return None


class Site:
    """One `model_copy(update=…)` call in a test, with what the live model says about it."""

    def __init__(
        self,
        module: _Module,
        node: ast.Call,
        model: type[pydantic.BaseModel],
        updates: dict[str, ast.expr],
        scope: str,
    ) -> None:
        self.file = module.path.relative_to(_ROOT).as_posix()
        self.line = node.lineno
        self.model = model
        self.updates = updates
        self.scope = scope

    def key(self, field: str) -> str:
        """The stable identity of one flagged field on one fixture."""
        return f"{self.file}::{self.scope}::{self.model.__name__}.{field}"


def _update_keys(node: ast.Call) -> dict[str, ast.expr] | None:
    """The literal string keys of the `update=` mapping, or None if it is not a literal dict."""
    for keyword in node.keywords:
        if keyword.arg != "update":
            continue
        if not isinstance(keyword.value, ast.Dict):
            return None
        out: dict[str, ast.expr] = {}
        for key, value in zip(keyword.value.keys, keyword.value.values, strict=True):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                return None
            out[key.value] = value
        return out
    return None


def _load(module: _Module, name: str) -> type[pydantic.BaseModel] | None:
    """The live model class behind a name the test module imports from `chemclaw`."""
    where = module.imported.get(name)
    if where is None or not where.startswith("chemclaw"):
        return None
    try:
        found = getattr(importlib.import_module(where), name, None)
    except ImportError:
        return None
    if isinstance(found, type) and issubclass(found, pydantic.BaseModel):
        return found
    return None


def _sites() -> tuple[list[Site], int]:
    """Every `model_copy(update=…)` call under `tests/`, and how many there are in total."""
    found: list[Site] = []
    total = 0
    for path in sorted(_TESTS.rglob("*.py")):
        module = _Module(path)
        for node in ast.walk(module.tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "model_copy"
                and any(keyword.arg == "update" for keyword in node.keywords)
            ):
                continue
            total += 1
            updates = _update_keys(node)
            if updates is None:
                continue
            named = module.subject(node.func.value, node)
            model = _load(module, named) if named and "." not in named else None
            if model is None:
                continue
            scopes = module.scopes(node)
            scope = scopes[-1].name if scopes else "<module>"
            found.append(Site(module, node, model, updates, scope))
    return found, total


def _validator_reads(model: type[pydantic.BaseModel]) -> frozenset[str]:
    """The fields the model's own `model_validator`s inspect — checks a copy walks past."""
    read: set[str] = set()
    for validator in model.__pydantic_decorators__.model_validators.values():
        try:
            source = inspect.getsource(validator.func)
        except (OSError, TypeError):
            continue
        read.update(_SELF_FIELD.findall(source))
    return frozenset(read)


def _validated_in_src() -> frozenset[str]:
    """Model names `src/` obtains by validating, read off `src/` rather than assumed."""
    names: set[str] = set()
    for path in _SRC.rglob("*.py"):
        names.update(_VALIDATED_IN_SRC.findall(path.read_text(encoding="utf-8")))
    return frozenset(names)


def _flagged(sites: list[Site]) -> dict[str, str]:
    """Flagged site keys mapped to the derivation that flagged them."""
    wire = _validated_in_src()
    out: dict[str, str] = {}
    for site in sites:
        reads = _validator_reads(site.model)
        crossed = site.model.__name__ in wire
        for field in site.updates:
            why = []
            if field in reads:
                why.append(f"a model_validator on {site.model.__name__} inspects {field!r}")
            if crossed:
                why.append(f"src/ obtains {site.model.__name__} through model_validate")
            if why:
                out[site.key(field)] = " and ".join(why)
    return out


def _inadmissible(sites: list[Site]) -> list[str]:
    """Sites whose literal update value the field's own validation refuses."""
    out: list[str] = []
    for site in sites:
        for field, value in site.updates.items():
            declared = site.model.model_fields.get(field)
            if declared is None:
                if site.model.model_config.get("extra", "ignore") != "allow":
                    out.append(
                        f"{site.file}:{site.line} sets {site.model.__name__}.{field}, which the "
                        "model does not declare — no constructor could have put it there"
                    )
                continue
            try:
                literal = ast.literal_eval(value)
            except (SyntaxError, TypeError, ValueError):
                continue
            try:
                adapter: pydantic.TypeAdapter[Any] = pydantic.TypeAdapter(
                    declared.rebuild_annotation()
                )
            except Exception:  # noqa: BLE001 - an unbuildable annotation is not a finding
                continue
            try:
                adapter.validate_python(literal)
            except pydantic.ValidationError as refusal:
                out.append(
                    f"{site.file}:{site.line} sets {site.model.__name__}.{field}={literal!r}, "
                    f"which that field refuses: {refusal.errors()[0]['msg']}"
                )
    return out


def test_no_model_copy_fixture_builds_an_object_its_own_model_would_refuse() -> None:
    """A copy that assigns a value the field rejects is an object production cannot deliver.

    There is nothing to argue here and so no allowlist: a guard whose subject could not have
    reached the code under test is measuring the fixture. Empty today — instrumented, no site in
    this suite builds one — and `test_the_rule_can_fail` is what proves the arm still looks.
    """
    sites, _ = _sites()
    refused = _inadmissible(sites)
    assert not refused, (
        f"{len(refused)} model_copy(update=…) fixture(s) build an object the model itself would "
        "refuse. Build it through the constructor or `model_validate`, so the object under test is "
        "one the system could produce:\n  " + "\n  ".join(refused)
    )


def test_every_model_copy_fixture_that_skips_a_check_its_model_applies_is_argued() -> None:
    """A copy that walks past a check the constructor runs has to say why that is safe here.

    Flagged from the live tree, never from a list in this file: the model's own
    `model_validator` bodies say which fields they inspect, and `src/` says which models it obtains
    by validating. A flag is not a defect — it is the set a reader has to be able to check.
    """
    sites, total = _sites()
    flagged = _flagged(sites)
    unargued = {key: why for key, why in sorted(flagged.items()) if key not in _ARGUED}
    assert not unargued, (
        f"{len(unargued)} of {len(flagged)} flagged model_copy(update=…) fixture(s) (out of "
        f"{total} call sites) skip a check their model applies and are not argued. Either rebuild "
        "the fixture through the real constructor or `model_validate`, or add a row to _ARGUED "
        "saying why the bypass cannot affect what the test concludes:\n  "
        + "\n  ".join(f"{key}: {why}" for key, why in unargued.items())
    )


def test_no_argument_outlives_the_fixture_it_argues() -> None:
    """An exemption that can no longer be tripped is a claim that a control exists.

    The other direction of the same list, and the one that makes the guard above self-proving: if
    the walk stops naming subjects, or a fixture is rebuilt through its constructor, the entry here
    stops matching and this fails rather than the tree going quietly green.
    """
    sites, _ = _sites()
    flagged = _flagged(sites)
    orphaned = sorted(key for key in _ARGUED if key not in flagged)
    assert not orphaned, (
        f"_ARGUED holds {len(orphaned)} entry/entries for fixtures that are no longer flagged — "
        "rebuilt, moved or renamed. Delete them:\n  " + "\n  ".join(orphaned)
    )


def test_the_rule_can_fail() -> None:
    """Both arms, driven over a model defined here, because neither fires on the tree today.

    The admissibility arm is empty and the flagged arm is fully argued, which is exactly the state
    in which a broken derivation is indistinguishable from a clean tree. So the derivations are
    driven directly: a field a validator inspects is flagged, a field nothing checks is not, and a
    value the field refuses is refused.
    """

    class _Subject(pydantic.BaseModel):
        """A stand-in with one checked field and one unchecked one."""

        count: int = pydantic.Field(ge=0)
        label: str = ""

        @pydantic.model_validator(mode="after")
        def _checked(self) -> "_Subject":
            if self.count > 10:
                raise ValueError("count is capped at 10")
            return self

    assert _validator_reads(_Subject) == frozenset({"count"}), (
        "the flagged arm reads a model_validator's body for the fields it inspects; it no longer "
        "sees `self.count`, so every cross-field bypass in the tree would go unflagged"
    )
    assert "label" not in _validator_reads(_Subject), (
        "a field no validator inspects must not be flagged, or the list is every call site"
    )
    assert _validated_in_src(), (
        "no model in src/ appears as `Model.model_validate(` — the boundary half of the derivation "
        "is reading nothing, so it flags nothing"
    )
    assert "Structure" in _validated_in_src(), (
        "`Structure` is obtained from the calc server by validating; the src/ scan no longer sees "
        "it, which means the pattern or the tree moved"
    )
