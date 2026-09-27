"""The read half of a stored skills tier: narrowed on every reach path, refused on every write.

**Two tiers are stored rather than filed, and this is the one backend behind both.** A chemist's own
skills (`agent/local_skills.py`) and the organisation's (`agent/org_skills.py`) differ in who writes
them, who reads them and what a load is booked against — and not at all in how a turn reaches them.
`agent/skill_backend.NarrowedSkillsBackend` is the same shape over `FilesystemBackend` for the
reviewed tree in git; this is its `StoreBackend` twin, and the two are deliberately not one class,
because the base classes disagree about which verbs are native and that disagreement is the whole
reason each needs its own override list (see `write`/`awrite` below).

**The predicate is the correction this module exists for.** The personal tier shipped narrowed *in
the prompt only*: `_skills_middleware` decided whether to advertise `/mine`, and the backend under
it would hand over any body anybody asked for. Driven, with `skill_names: []` — the eval control arm
that is supposed to remove every skill — one `ls("/mine/")` returned the names and `read_file`
returned the bodies. `agent/skill_backend.py`'s class docstring had already written the argument
this violated: "Listing is therefore only half the gate: a model told about
`/deep-research/SKILL.md` can ask for `/anything-else/SKILL.md`". The same sentence was true here
and nothing applied it.

That is a narrowing hole rather than a widening of authority — nothing reaches a tier belonging to
somebody else, because the *namespace* closes over the actor and a mount is per turn. What it
falsified is the control arm (`data/evals/profiles/skills-removed.yaml`), which is an experiment
this repository runs to find out whether skills help at all, and which was silently measuring a
system that still had them.

**Both halves of the gate, because upstream splits them across two base classes.** `ls`, `read`,
`grep`, `glob` and `download_files` are gated here; `als`, `aglob`, `agrep` and `adownload_files`
are `asyncio.to_thread` wrappers in `BackendProtocol` and so dispatch through these overrides, while
`aread` is native on `StoreBackend` and is therefore overridden explicitly. That asymmetry is the
one `agent/local_skills.py` was caught by on the *write* half — four sync refusals left three async
verbs open — and it is why the coverage test derives its method list from the protocol *and* from
`StoreBackend` rather than from a list anybody maintains.

**`download_files` is gated and was the reach path nobody had closed on this side.** The reviewed
tree gates it (`agent/skill_backend.py:282`) with a docstring saying it "returns a file's **full
bytes**" and that nothing binds it today, so it is "a latent hole rather than a live one" that
becomes live the moment upstream fetches a body that way. The stored tiers inherited the same
latency and not the same fix.
"""

import dataclasses
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar, cast

from deepagents.backends import StoreBackend
from deepagents.backends.protocol import GlobResult, GrepResult, LsResult

from chemclaw.agent.session_store import _session_connection, _session_dsn
from chemclaw.agent.skill_backend import (
    SkillsReadOnlyRefusal,
    is_a_skill_body,
    path_of,
    skill_of,
)
from chemclaw.core.config import settings

#: What a mounted tier answers for a path outside what this turn may reach.
#:
#: The same words `agent/skill_backend.REFUSED` uses, and for its reason: it does not say whether
#: the skill *exists*, because "not available to you" has to be the same answer for a gated skill
#: and for a typo, or the gate becomes an enumeration oracle.
REFUSED = "This path is not part of the skills available to you."


#: How many rows one page of a namespace walk asks for.
#:
#: `BaseStore.asearch` defaults to `limit=10`, which is not a page size a caller chose — it is a
#: default a caller who passed nothing inherited. Un-paged, a listing answers ten and reads as the
#: whole tier: measured on the personal tier, a chemist with twelve saved skills was listed ten
#: while the prompt carried twelve, and the two beyond the page were undeletable through the route
#: that exists to remove them.
LISTING_PAGE = 100


async def paged_items(store: Any, namespace: tuple[str, ...]) -> dict[str, Any]:
    """Every item in one namespace, keyed by store key — the whole tier rather than one page.

    **One walk because the loop had reached two copies and a third caller** (Rule of Three). The
    callers are each tier's own listing, which answers a route's "what is acting on my turns", the
    organisation tier's version listing and its version-cap walk, and `agent/stored_skill_tools.py`,
    which reads the bodies for the capability narrowing — five call sites in three modules. A paging
    walk is exactly the kind of thing that stays in step until one copy is edited.

    A fourth copy survives in `agent/scratchpad.py`'s eviction walk (`_EVICTION_PAGE`) and is not
    collapsed here: that one pages in order to *evict*, so it writes under a different cap as it
    goes, and folding it in would give this function a second purpose its other callers do not have.
    Named rather than left for a reader to find.

    The walk terminates on a page that adds nothing, which also ends it against a store that ignores
    `offset` — the same guard `scratchpad.BoundedStoreBackend` carries for the same reason.

    Args:
        store: The process's store.
        namespace: The namespace tuple to walk.

    Returns:
        `{store key: item}`, where an item carries both `.key` and `.value`, so a caller wanting the
        bodies does not need a second round trip.
    """
    held: dict[str, Any] = {}
    while True:
        page = await store.asearch(namespace, limit=LISTING_PAGE, offset=len(held))
        fresh = {item.key: item for item in page if item.key not in held}
        if not fresh:
            break
        held.update(fresh)
    return held


#: The document inside a skill directory that makes it a skill, mirroring the reviewed tree's
#: shape so both stored tiers are discovered by the same rule as `skills/` and read the same to a
#: model: `/org/<name>/SKILL.md` and `/mine/<name>/SKILL.md` are one convention, not two.
SKILL_FILENAME = "SKILL.md"

#: The transaction-scoped advisory lock a stored tier's writes serialize on — see
#: `advisory_writer_lock`.
_WRITER_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"


def storable_name(name: str) -> bool:
    r"""Whether this name could ever have been written, which is what a read of it may assume.

    **The writer's rule, asked by the readers, because the readers are reachable with anything.**
    `GET /skills/mine/{name}` and its `DELETE` take a path parameter: any byte a URL can carry
    reaches the store. Measured against the shipped Postgres store, `a\x00b` raised
    `psycopg.DataError: PostgreSQL text fields cannot contain NUL (0x00) bytes` out of both — a
    **500** on a name that cannot exist, where the honest answer is 404 and is what the in-memory
    store already gave. A 500 on caller input is an availability question dressed as a bug report.

    It is a predicate rather than a second copy of the check for the reason the whole tier is
    arranged around: a rule stated at one surface is a rule the other surface does not have, which
    is exactly the hole `local_skills.validated_skill`'s docstring measures for the two write doors.
    """
    return all(not character.isspace() and character.isprintable() for character in name)


def skill_key(name: str) -> str:
    """The store key one stored skill's body lives under, in either tier.

    Leading slash and trailing `SKILL.md`, because that is the shape `StoreBackend` writes and reads
    — measured rather than assumed, since a key this module invented would be a second definition
    that upstream could walk away from on a bump.
    """
    return f"/{name}/{SKILL_FILENAME}"


def name_of_key(key: str) -> str | None:
    """The skill a store key names, or `None` for a key that is not a skill body.

    The inverse of `skill_key`, and strict: a key with no leading segment (`/SKILL.md`) or no
    leading slash is not a skill. A suffix test alone listed `/SKILL.md` as a skill named `""` —
    a row a route shows and cannot address — and, in the capability narrowing, would have made a
    declaration under a nonsense name, where the entry that matters is the one that goes *missing*.
    """
    suffix = f"/{SKILL_FILENAME}"
    if not key.startswith("/") or not key.endswith(suffix) or len(key) <= len(suffix) + 1:
        return None
    return key[1 : -len(suffix)]


def store_writer(store: Any, namespace: tuple[str, ...]) -> StoreBackend:
    """A writable backend over one stored tier's namespace, for the routes that change it.

    **Plain `StoreBackend`, deliberately.** The turn's backend is a `PermittedStoreBackend` over the
    same namespace; this is the other side of the tier, reached from an HTTP route a person calls
    rather than from anything a model holds. Upstream's writer rather than `store.aput` keeps the
    stored shape — the key, the `content`/`encoding` pair, the two timestamps — with exactly one
    definition, and
    `tests/test_scratchpad.py::test_no_first_party_module_writes_to_a_store_directly` holds
    that no first-party module reaches past a backend to a store's own write verbs.
    """
    return StoreBackend(namespace=lambda _runtime: namespace, store=store)


@asynccontextmanager
async def advisory_writer_lock(lock_key: str) -> AsyncIterator[None]:
    """Serialize a stored tier's writes under `lock_key`, so a row cap is a bound, not a suggestion.

    **The caps were check-then-write with nothing between the two.** Measured against a real
    `AsyncPostgresStore` at a cap of 3: twelve concurrent `POST /skills/mine` calls all read the
    same pre-write count, all passed, and all twelve were written. Each tier keys the lock on what
    its cap is per (a chemist, an org skill), so writers that cannot race never contend.

    A transaction-scoped advisory lock on the session-store database rather than through the
    `store` backend, because the count and the write are two calls through somebody else's object
    and cannot be one transaction; it releases on commit.

    **Conditioned on the backend rather than guarded by an `except`.** A stored tier is only
    reachable when `turn_store()` answers, which needs `session_store="postgres"`; a test driving a
    writer over an in-memory store has no database to lock on and no concurrency to lose, so the
    lock is skipped explicitly rather than attempted and swallowed.
    """
    if settings.session_store != "postgres":
        yield
        return
    async with _session_connection(_session_dsn()) as conn:
        await conn.execute(_WRITER_LOCK, (lock_key,))
        try:
            yield
        finally:
            await conn.commit()


async def list_skill_names(store: Any, namespace: tuple[str, ...]) -> list[str]:
    """The names of every skill one stored tier holds, sorted — **all** of them.

    Paged through `paged_items` for the reason it gives (un-paged, a listing answers ten and reads
    as the whole tier), and parsed through `name_of_key`, so a key that is not a skill body lists as
    nothing rather than as an empty name.
    """
    held = await paged_items(store, namespace)
    return sorted(name for key in held if (name := name_of_key(key)) is not None)


async def read_skill_body(store: Any, namespace: tuple[str, ...], name: str) -> str | None:
    """One stored skill's body, verbatim, or `None` if the tier has no skill by that name.

    A name the writer would have refused is answered as absent rather than passed to the store —
    see `storable_name`, which measured a 500 on the shipped backend for a name that cannot exist.
    """
    if not storable_name(name):
        return None
    item = await store.aget(namespace, skill_key(name))
    if item is None:
        return None
    content = item.value.get("content")
    return content if isinstance(content, str) else None


class PermittedStoreBackend(StoreBackend):
    """A stored skills tier: every read bounded by `permits`, every write refused.

    Args:
        namespace: The store namespace factory this tier lives under, per upstream's signature.
        store: The store, passed explicitly because these backends are built outside a graph run.
        permits: Whether a skill *name* is reachable on this turn. Called per reach rather than
            once at construction, for `NarrowedSkillsBackend`'s reason: the role gate reads the
            turn's ambient identity and one backend can serve concurrent turns.
        refusal: What a refused write says, worded for the tier — the personal one names its owner
            and their route, the organisation's names an administrator and theirs. Required rather
            than defaulted, so a third tier cannot inherit a sentence written about the second.
        on_load: Booked once per *delivered* body, with the skill's name. The two tiers count
            differently and must: a personal skill's name is one person's words and gets a bare
            counter, while an organisation's is deployment configuration and can carry a label.

    Every argument is keyword-only and required. `permits` especially: a default would make the
    hole this class exists to close re-openable by omission, which is how it was open in the first
    place.
    """

    def __init__(
        self,
        *,
        namespace: Any,
        store: Any,
        permits: Callable[[str], bool],
        refusal: str,
        on_load: Callable[[str], None],
    ) -> None:
        """Hold the namespace, the predicate, the refusal text and the counting hook."""
        super().__init__(namespace=namespace, store=store)
        self._permits = permits
        self._refusal = refusal
        self._on_load = on_load

    def _allows(self, path: str) -> bool:
        """Whether this turn may reach `path` at all — the one question every read asks."""
        skill = skill_of(path)
        return not skill or self._permits(skill)

    def _permitted(self, hits: list[Any]) -> list[Any]:
        """The hits naming a path this turn may reach."""
        return [hit for hit in hits if self._allows(path_of(hit))]

    def _delivered(self, result: Any, path: str) -> None:
        """Book one delivered body, or nothing.

        Derived from the *result* rather than from the path, so the two conditions
        `agent/skill_backend.py` had to learn by measurement are answered by the object that knows
        them: a read that failed delivered nothing, and a read that asked for no lines delivered
        nothing either while still resolving.
        """
        if getattr(result, "error", None) is not None:
            return
        if getattr(result, "no_lines_requested", False):
            return
        if is_a_skill_body(path) and (name := skill_of(path)):
            self._on_load(name)

    def ls(self, path: str) -> LsResult:
        """List the tier, keeping only entries belonging to a skill this turn may reach."""
        result = super().ls(path)
        if not result.entries:
            return result
        kept = [entry for entry in result.entries if self._allows(str(entry.get("path", "")))]
        return LsResult(error=result.error, entries=kept)

    def read(self, *args: Any, **kwargs: Any) -> Any:
        """Read one body, refusing anything outside what this turn may reach, and count it.

        Forwarded with `*args, **kwargs` rather than upstream's signature, for the reason
        `_first_argument` gives: the coverage test derives from upstream's protocol, so a signature
        written here would be a second copy of one a bump could invalidate.
        """
        path = _first_argument(args, kwargs)
        if not self._allows(path):
            return _refused_read()
        result = super().read(*args, **kwargs)
        self._delivered(result, path)
        return result

    async def aread(self, *args: Any, **kwargs: Any) -> Any:
        """The async twin, overridden rather than inherited.

        `StoreBackend.aread` is native against the store rather than a `to_thread` wrapper, so a
        gate placed on `read` alone would bound the path nothing takes and leave the one an async
        agent actually takes wide open. That is the exact shape the write half of this tier was
        caught by.
        """
        path = _first_argument(args, kwargs)
        if not self._allows(path):
            return _refused_read()
        result = await super().aread(*args, **kwargs)
        self._delivered(result, path)
        return result

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        """Match files, dropping every hit outside what this turn may reach.

        Narrowed with `dataclasses.replace` rather than rebuilt, so `truncated` survives: a rebuild
        naming only `matches` reports a partial walk to the model as the whole tier, which is the
        defect `agent/skill_backend.glob` records. This gate only ever removes, so nothing it does
        can make a complete result partial or the reverse.
        """
        result = super().glob(pattern, path)
        if not result.matches:
            return result
        return _replaced(result, self._permitted(result.matches))

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
        context_lines: int = 0,
    ) -> GrepResult:
        """Search the tier, dropping every hit outside what this turn may reach.

        The keyword-only arguments are forwarded rather than accepted-and-ignored because upstream
        *introspects* for them (`protocol._method_accepts_max_count`) to decide whether to push the
        cap down to the backend or apply it itself — so an override that dropped them would change
        how many matches a caller gets depending on which class is underneath.
        """
        result = _call_grep(
            super().grep, pattern, path, glob, max_count=max_count, context_lines=context_lines
        )
        if not result.matches:
            return result
        return _replaced(result, self._permitted(result.matches))

    def download_files(self, paths: list[str]) -> Any:
        """Return the bodies of the permitted paths only.

        Filtered rather than refused outright, as `glob` and `grep` are: this returns per-path
        results, so a caller asking for five paths of which one is gated should get the four.
        """
        return super().download_files([path for path in paths if self._allows(path)])

    def write(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse: a skill is judgment somebody approved, never something a turn may rewrite."""
        raise SkillsReadOnlyRefusal(self._refusal)

    def edit(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `write` gives."""
        raise SkillsReadOnlyRefusal(self._refusal)

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `write` gives.

        Deleting is the verb most plausibly wanted and least plausibly wanted *by a turn*: a turn
        that could would be one bad inference away from removing judgment somebody still relies on.
        """
        raise SkillsReadOnlyRefusal(self._refusal)

    def upload_files(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `write` gives."""
        raise SkillsReadOnlyRefusal(self._refusal)

    async def awrite(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse. Overridden because `StoreBackend.awrite` is native rather than a thread wrapper.

        This is the one that mattered when the personal tier was measured: an async agent takes the
        async path, so before this override existed the refusal was true of a path nothing used and
        false of the path everything does.
        """
        raise SkillsReadOnlyRefusal(self._refusal)

    async def aedit(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `awrite` gives."""
        raise SkillsReadOnlyRefusal(self._refusal)

    async def adelete(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse, for the reason `awrite` gives."""
        raise SkillsReadOnlyRefusal(self._refusal)

    async def aupload_files(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse.

        Stated rather than inherited even though the base class's own implementation already
        refuses by falling through to `upload_files`: relying on that is relying on one of the four
        async verbs being a wrapper while the other three are not, which is the accident this class
        was caught by.
        """
        raise SkillsReadOnlyRefusal(self._refusal)


def _refused_read() -> Any:
    """What a gated read answers — a result the model can read, never a raised error.

    A refusal it reads keeps the turn going, where a raised error surfaces as a tool failure it may
    retry. Built through upstream's own `ReadResult` rather than a shape invented here, imported at
    call time for the reason the module's other upstream imports are not: `ReadResult` is the one
    name here that a bump could move, and a module-scope import would make that an import error at
    startup rather than a red test.
    """
    from deepagents.backends.protocol import ReadResult

    return ReadResult(error=REFUSED, file_data=None)


#: A glob or grep result, narrowed in place so its other fields survive — see `_replaced`.
_Result = TypeVar("_Result", GlobResult, GrepResult)


def _replaced(result: _Result, matches: list[Any]) -> _Result:
    """`result` with its matches narrowed and every other field carried over.

    `dataclasses.replace` rather than a constructor call naming two fields of three, so `truncated`
    — and whatever upstream adds next — survives the narrowing.
    """
    return dataclasses.replace(result, matches=matches)


def _call_grep(
    grep: Any,
    pattern: str,
    path: str | None,
    glob: str | None,
    *,
    max_count: int | None,
    context_lines: int,
) -> GrepResult:
    """Call `grep` with the two keyword arguments upstream may or may not accept.

    `StoreBackend.grep` does not take `context_lines` today and `FilesystemBackend.grep` does, and
    upstream introspects for `max_count` rather than declaring it everywhere. Rather than transcribe
    which class accepts what — a list that is right on the day it is written — the call is attempted
    with both and retried without the one that is refused.
    """
    try:
        result = grep(pattern, path, glob, max_count=max_count, context_lines=context_lines)
    except TypeError:
        result = grep(pattern, path, glob, max_count=max_count)
    return cast(GrepResult, result)


def _first_argument(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """The path a `read`/`aread` was given, whichever way upstream's caller spelled the call.

    `file_path` is upstream's own keyword, pinned for the write verbs by
    `tests/test_upstream_surface.py`.
    """
    if args:
        return str(args[0])
    return str(kwargs.get("file_path", ""))
