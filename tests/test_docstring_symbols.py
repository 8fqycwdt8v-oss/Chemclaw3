"""Two narrow guards over the *bare names* this tree's prose cites, and the wide one it declines.

`tests/test_docstring_paths.py` guards two shapes — a file path and a fully-qualified dotted name
— and its own comment records why it stops there: every backticked bare identifier was measured at
3,351 distinct with 1,077 resolving to no first-party definition, a 32% false-positive rate. A
later review resolved all 31,981 backticked references in `src/chemclaw/**/*.py` against a layered
symbol table (first-party definitions, class attributes, module-level assignments, parameters,
modules, imports, string literals, YAML/Helm/SQL declarations and every installed distribution).
216 names resolved to nothing; hand-checking 35 of them measured an **82.9% false-positive rate**.

That number is a property of this tree rather than of the technique, and it is the intended
property: prose here deliberately names things that do not exist, because the absence *is* the
subject. "There is deliberately no `labels_enabled`." "The alternative — `dipole_averaged` — would
put one calculator's schema in the cache." A gate that fails the build on those sentences would be
answered by deleting them, which costs the reader the argument and buys nothing. **So the general
rule is declined, deliberately and on measurement**, and this file holds only the two narrowings
that survived it.

**B1 — the near-miss inside the declaring file.** A dangling name is flagged only when the *same
module* defines a name differing from it by a leading underscore, a trailing `s`, or a difflib
ratio of at least `_NEAR_MISS`. That conjunction is the whole guard: a sentence about something
that does not exist does not accidentally near-miss a definition one screen above it, while a
local rename that left its own docstring behind does so by construction. Measured over `src/` the
day it was written: four flags, three of them real (`_identity_label` for `_identity_labels`,
`skills_middleware` for `_skills_middleware`, `_degradation_findings` for `degradation_findings`
— each a rename that moved the definition and not the sentence beside it), and one historical,
allowlisted below.

**B2 — a metric name in source prose.** `chemclaw.cli.validate_prose_contract`'s rules 8 and 9
already resolve metric citations against the registry, and their corpus is `docs/`: the runbook,
the ADRs, the chart's alerts. `src/**/*.py` is simply outside it. That is the more consequential
half of the two, because a module docstring naming a series is an *alerting* claim — and the
defect it caught was exactly that: `durable/retention.py` told an operator to alert on
`chemclaw_retention_rows_deleted_total` and `chemclaw_retention_bytes_reclaimed_total`, two
counters written, measured, and deliberately removed one screen below the sentence that kept
promising them. A name that does not exist *renders*, as an alert that matches nothing and
therefore never fires.

**Both guards were watched refusing before they were kept.** `_flag_near_misses` and
`_undeclared_metrics` are driven against a synthetic defect by the two `..._refuses_...` tests
below, so a change that makes either mechanism vacuous fails here rather than passing quietly —
which is the failure mode `D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`
is about, one level up.

**What B1's resolution layer does not include is upstream.** It is first-party code tokens (every
`NAME` the tokenizer sees in `src/` and `tests/`, which covers definitions, parameters, attributes
and imports), plus the identifier-shaped words inside string literals, plus the builtins. A name
that exists only in a dependency and *also* near-misses a definition in the file citing it would
be a false positive; there is none today, and one would land in `_NEAR_MISS_ALLOWED` with its
reason, which is the same friction `test_docstring_paths._REMOVED` creates on purpose. Walking
site-packages to close a hole nothing occupies would cost every run of this suite.
"""

import ast
import builtins
import io
import keyword
import re
import tokenize
from difflib import SequenceMatcher
from pathlib import Path

import pytest

from chemclaw.cli.validate_prose_contract import (
    _NON_METRIC_NAMES,
    _declared_including_histogram_series,
)

_SELF = Path(__file__).resolve()
_REPO_ROOT = _SELF.parents[1]
_SRC = _REPO_ROOT / "src"
_TESTS = _REPO_ROOT / "tests"

# A backticked bare identifier: no dot, no slash, no call parentheses. The qualified and path forms
# are `test_docstring_paths.py`'s, and overlapping with it would mean two answers to one question.
_BARE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")

# A metric citation, in the spelling `validate_prose_contract._METRIC` already defines for the
# `docs/` corpus — the span must *end* at the name or its label matcher, so a module path is not
# read as a series.
_METRIC = re.compile(r"`(chemclaw_[a-z0-9_]+)(?:\{[^`}]*\})?`")

# How alike two names have to be before one is read as a misspelling of the other. 0.88 is where
# the measurement put it: `qm_activity_timeout_seconds`/`activity_timeout_seconds` scores 0.941 and
# is the one historical flag, while the near-miss pairs this tree's prose produces legitimately —
# `deleted`/`skipped`, `permits`/`_permits` — sit below it or are resolved by the code scan first.
_NEAR_MISS = 0.88

# Dangling names whose near twin in the same file is a coincidence rather than a rename. One entry,
# and it earns its line: the sentence's whole subject is the *old* spelling.
_NEAR_MISS_ALLOWED: dict[str, str] = {
    # `core/config/temporal.py` — "It used to be spelled `qm_activity_timeout_seconds`", a rename
    # the sentence is *about*. The near twin it scores 0.941 against is the current field, which is
    # exactly what makes the sentence useful and this flag wrong.
    "qm_activity_timeout_seconds": "the retired spelling the surviving field's comment is about",
}

# `chemclaw_*` spans in source prose that no registry declares and none should. Every one of them
# contains its own negation — the sentence exists because the series does not — so the entry here
# is not an exemption granted to a mistake, it is the mistake being the point.
_METRIC_ALLOWED: dict[str, str] = {
    # `agent/checkpointer.py` — a checkpoint *metadata* key the first version of that guard used,
    # not a series. It shares the prefix with the package and nothing else.
    "chemclaw_state_schema": "a retired checkpoint metadata key, never a metric",
    # `api/app.py` — the sentence says in so many words that neither the watermark nor this
    # counter "exists anywhere in `src/`", and names it to stop D-119 §42 being read as live.
    "chemclaw_rollback_watermark_unavailable_total": "named as the dead consumer it is",
    # `core/netguard.py` — emitted by the sibling fleet (`Chemclaw3-mcp`), which this registry does
    # not declare and this repository does not build. The citation is the parallel, not a claim.
    "chemclaw_mcp_egress_refused_total": "a series the sibling fleet emits, not this one",
    # `core/metrics.py` — the spelling `declared_histogram_names()` exists to *refuse*: a blanket
    # suffix fold would grant a counter a `_bucket` series. Quoting it is how that is explained.
    "chemclaw_turns_started_total_bucket": "the derived spelling the exact fold refuses",
    # The four below are all in `cli/validate_prose_contract.py`, whose docstrings are the worked
    # examples of this very question. Each is deliberately a name nothing declares: the counter an
    # alert named for a year and never had, a log marker, the stale metric an ADR is *about*, and
    # a PromQL placeholder that is not a name at all.
    "chemclaw_degradations_total": "rule 8's worked example of an alert naming nothing",
    "chemclaw_plans_consumed": "a log marker quoted as a correct non-metric",
    "chemclaw_tool_latency_seconds": "the stale metric D-2026-08-01 is about, quoted by that rule",
    "chemclaw_x": "the placeholder in rule 9's own statement, not a series",
}


def _prose_of(path: Path) -> list[str]:
    """Every docstring and comment in one file — the text a reader navigates by.

    Both kinds, because both carry pointers and this tree writes as many arguments in comments as
    in docstrings, and the two defects that started this file were one of each:
    `core/config/calculators.py` documented a setting that never existed in a comment, and
    `durable/retention.py` promised two counters in its module docstring.
    """
    text = path.read_text(encoding="utf-8")
    prose = [
        doc
        for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        if (doc := ast.get_docstring(node, clean=False))
    ]
    prose += [
        token.string
        for token in tokenize.generate_tokens(io.StringIO(text).readline)
        if token.type == tokenize.COMMENT
    ]
    return prose


def _code_names(path: Path) -> set[str]:
    """Every identifier this file uses as code, plus the identifier-shaped words in its strings.

    `NAME` tokens rather than an `ast` walk, because the question is "does this name appear
    anywhere in the tree's code" and a token stream answers it without a case per binding form.
    Non-docstring string contents are folded in for the layer a symbol table alone misses: SQL
    identifiers, metric names, tool names and settings keys are all real names that exist only
    inside a literal.

    **Docstrings are excluded, and that is the difference between a guard and a mirror.** They are
    `STRING` tokens like any other, so folding them in would let prose resolve prose: the first
    version of this function did, and both refusal probes below passed vacuously because *this
    file's own module docstring* quotes the two dangling names it exists to catch.
    """
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        if node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    names = {
        token.string
        for token in tokenize.generate_tokens(io.StringIO(text).readline)
        if token.type == tokenize.NAME
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                names.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", node.value))
    return names


def _bound_names(path: Path) -> set[str]:
    """Every name this module itself binds: definitions, assignments, imports and parameters.

    The near-miss candidates, and deliberately the *binding* set rather than the token set: a name
    this file merely mentions is not a name its own prose could have been renamed away from.
    """
    bound: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            bound.add(node.name)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                args = node.args
                bound.update(
                    arg.arg
                    for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)
                    if arg is not None
                )
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bound.add(node.target.id)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            bound.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
    return bound


def _near_miss(cited: str, defined: str) -> str | None:
    """Why `defined` reads as what `cited` was meant to say, or `None` if it does not.

    Three relations, and the first two are named rather than folded into the ratio because they are
    the two ways a rename actually happens here: privatising a helper (a leading underscore) and
    pluralising one whose job changed from a thing to the set of them (a trailing `s`).
    """
    if cited == defined:
        return None
    if cited == defined.lstrip("_") or defined == cited.lstrip("_"):
        return f"`{defined}` differs only by a leading underscore"
    if cited == f"{defined}s" or defined == f"{cited}s":
        return f"`{defined}` differs only by a trailing 's'"
    ratio = SequenceMatcher(None, cited, defined).ratio()
    if ratio >= _NEAR_MISS:
        return f"`{defined}` is {ratio:.2f} similar"
    return None


def _source_files() -> list[Path]:
    """Every module whose prose these two rules cover."""
    return sorted(_SRC.rglob("*.py"))


def _resolvable() -> frozenset[str]:
    """Every bare name that exists as code anywhere in this repository's own packages.

    Computed once for the whole session — it tokenizes the tree, which is cheap compared with
    doing it per parametrized file.

    **This file is excluded from its own universe**, for the reason its docstrings already are: the
    two refusal probes below hold a dangling name in a plain string literal, which would otherwise
    make that name resolve and both probes pass while asserting nothing. A guard whose fixtures
    feed its own symbol table cannot fail.
    """
    universe = set(dir(builtins)) | set(keyword.kwlist)
    for path in (*_SRC.rglob("*.py"), *_TESTS.rglob("*.py")):
        if path.resolve() != _SELF:
            universe |= _code_names(path)
    return frozenset(universe)


_RESOLVABLE = _resolvable()


def _flag_near_misses(prose: list[str], bound: set[str], resolvable: frozenset[str]) -> list[str]:
    """The cited names in `prose` that resolve nowhere and near-miss something in `bound`."""
    flags: list[str] = []
    cited = {name for chunk in prose for name in _BARE.findall(chunk)}
    for name in sorted(cited - resolvable - set(_NEAR_MISS_ALLOWED)):
        reasons = [why for candidate in sorted(bound) if (why := _near_miss(name, candidate))]
        if reasons:
            flags.append(f"`{name}` resolves to nothing, and {reasons[0]}")
    return flags


def _undeclared_metrics(prose: list[str], declared: frozenset[str]) -> list[str]:
    """The `chemclaw_*` series named in `prose` that nothing in this process emits.

    A name that also exists as first-party code is not a citation of a series — `chemclaw_agent` is
    a module — which is the same exclusion `validate_prose_contract` states beside its own pattern.
    """
    cited = {name for chunk in prose for name in _METRIC.findall(chunk)}
    return sorted(
        cited - declared - _NON_METRIC_NAMES - set(_METRIC_ALLOWED) - _RESOLVABLE,
    )


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: str(p.relative_to(_REPO_ROOT)))
def test_a_cited_name_that_near_misses_one_this_file_defines_is_a_rename_the_prose_missed(
    path: Path,
) -> None:
    """A dangling name whose twin sits in the same file is a rename that left its sentence."""
    flags = _flag_near_misses(_prose_of(path), _bound_names(path), _RESOLVABLE)
    assert not flags, (
        f"{path.relative_to(_REPO_ROOT)} cites a name that does not exist while defining its near "
        f"twin: {'; '.join(flags)}. Say the current name; if the old spelling is the subject of "
        f"the sentence, it belongs in _NEAR_MISS_ALLOWED with the reason."
    )


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: str(p.relative_to(_REPO_ROOT)))
def test_every_metric_named_in_source_prose_is_one_this_registry_declares(path: Path) -> None:
    """A series named in a docstring is an alerting claim, and an alert on nothing never fires."""
    undeclared = _undeclared_metrics(_prose_of(path), _declared_including_histogram_series())
    assert not undeclared, (
        f"{path.relative_to(_REPO_ROOT)} names {len(undeclared)} metric series that "
        f"`core/metrics.py` does not declare: {', '.join(undeclared)}. Name what is emitted, or "
        f"emit it; a series whose absence is the point of the sentence goes in _METRIC_ALLOWED."
    )


def test_the_near_miss_rule_refuses_a_rename_that_left_its_docstring_behind() -> None:
    """Drive B1 against the defect it was built from, so a vacuous version fails here.

    The shape is `science/bo/campaign_record.py`'s exactly: the function was pluralised, the three
    sentences citing it were not, and nothing in the tree could see it.
    """
    prose = ["Reduced by `_identity_label`, which folds a label list as chemistry."]
    assert _flag_near_misses(prose, {"_identity_labels"}, _RESOLVABLE) == [
        "`_identity_label` resolves to nothing, and `_identity_labels` differs only by a "
        "trailing 's'"
    ]
    # And the conjunction is what makes it usable: the same dangling name with no twin beside it is
    # the 82.9%-false-positive case, and passes.
    assert _flag_near_misses(prose, {"canonical_text"}, _RESOLVABLE) == []


def test_the_metric_rule_refuses_an_alert_target_nothing_emits() -> None:
    """Drive B2 against the two counters `durable/retention.py` promised an operator for months."""
    declared = _declared_including_histogram_series()
    prose = [
        "`chemclaw_table_bytes` and `chemclaw_retention_rows_deleted_total` are what an operator "
        "can alert on."
    ]
    assert _undeclared_metrics(prose, declared) == ["chemclaw_retention_rows_deleted_total"]


def test_both_rules_have_something_to_check() -> None:
    """Guard the guards' own vacuity: a corpus that stopped matching would pass every file above."""
    prose = [chunk for path in _source_files() for chunk in _prose_of(path)]
    cited = {name for chunk in prose for name in _BARE.findall(chunk)}
    series = {name for chunk in prose for name in _METRIC.findall(chunk)}
    assert len(cited) > 2_000, f"only {len(cited)} backticked bare names found; the scan is broken"
    assert len(series) > 50, f"only {len(series)} metric citations found; the scan is broken"
