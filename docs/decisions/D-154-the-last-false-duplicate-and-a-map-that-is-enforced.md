# D-154 — The last false duplicate, the corpora in one place, and a map that is enforced

A follow-up to the restructure of D-146…D-148, asked in a form worth recording as carefully as the
answer: *"Is there a way to make it even more consistent, or would I overshoot doing that, removing
needed granularity?"*

Both halves get an answer here. The three changes below are the consistency that was still
available; the four **non**-changes are the granularity that would have been lost by continuing, and
they are recorded because the next reader will find the same four candidates and needs to know they
were considered rather than missed.

## 1. `chemclaw.mcp` was the last false duplicate, and `science/` had already made it removable

D-148 introduced `science/` so that `science/calc` and `connectors/calc` would read as a **pair** —
engine and wrapper — rather than as a duplication. `chemclaw.mcp` was left alone in that pass, and
its own README explained why:

> Moving the bodies into the bundles would be churn with no behavioural change, so it has not been
> done.

That was a correct judgement about the choice available at the time, when the only destination was
`connectors/` and moving pure fingerprint code there would have buried computation inside a
transport bundle. It stopped being correct the moment `science/` existed, and nothing re-examined
it — the note stayed true-sounding while its premise expired.

So `mcp/molfp` sat one directory from `connectors/molfp`, looking exactly like the pair that *is*
principled while being nothing of the kind. It split cleanly along the line the repository already
had:

| From | To |
| --- | --- |
| `mcp/fpstore.py` | `science/fingerprints/store.py` |
| `mcp/{molfp,rxnfp}/{fingerprint,search}.py` | `science/fingerprints/{molfp,rxnfp}/` |
| `mcp/{molfp,rxnfp}/server.py` | `connectors/{molfp,rxnfp}/server/tools.py` |

`server/tools.py` beside `server/app.py` is what every other bundle already looks like, so `molfp`
and `rxnfp` stopped being the two bundles whose code lived elsewhere. The package is deleted, and
the rule is now exceptionless: **capability code lives in a connector bundle or in `science/`,
nowhere else.**

### The D-016 rule, stated accurately

The deleted README claimed the directory *"cannot be named `mcp`"* — the SDK owns that name, and a
sibling package shadows it (`from mcp.server.fastmcp import FastMCP`). It made that claim from
inside a directory named `mcp`, having been moved there by D-148 without anyone noticing the
contradiction.

Both facts were true. A **top-level** `mcp/` shadows the SDK; `chemclaw.mcp`, a submodule, never
could. The rule survives in `connectors/README.md` in the form that is actually load-bearing, and
the incident is worth its own sentence: a rule stated as an absolute outlives the condition that
made it one.

## 2. Runtime corpora into `data/`, with two deliberate exceptions

`evals/` and `templates/` existed twice — once as a code package under `src/chemclaw/`, once as a
root data directory — and `ARCHITECTURE.md` carried a paragraph explaining that the collision was
fine. Needing a paragraph is the defect. `evals/`, `templates/` and `profiles/` are now
`data/evals/`, `data/templates/` and `data/profiles/`, beside the `vendored/` and `eln-exports/`
already there. The root went from thirteen directories to ten and no root directory shares a name
with a code package.

**`knowledge/` and `skills/` deliberately did not move.** They are read at runtime like the others,
so the exceptionless version of this rule would have swallowed them — and they are architecture
layers 4 and 3, what the system knows and how it judges, authored by people rather than configured
by an operator. Their position at the root is what says so. The rule keeps a stated exception
instead: **`data/` holds every corpus the code reads, except the two directories that are layers.**

Five config defaults moved with them (`profiles_dir`, `templates_dir`, `eval_case_dir`,
`eval_baseline_path`, `eval_retrieval_corpus_dir`), and the Containerfile's seven data `COPY` lines
became four.

### What the move broke, and how it announced itself

`tests/test_retrieval_eval.py` pinned the gold corpus at a hardcoded `_REPO / "evals" /
"retrieval_corpus"`, so every gold case suddenly scored `0/2 expected sources retrieved` — which
reads as a retrieval regression, not as a missing directory. The literal is now derived from the
setting's own default so it cannot drift again, and the fixture asserts the directory exists,
because **an empty corpus and a wrong path produce identical numbers**. Same class as D-148's silent
`glob` over a moved migrations directory; the counter-measure is the same one, applied one layer
earlier.

## 3. The repository map is enforced rather than promised

`ARCHITECTURE.md` has closed with "adding a top-level directory means adding a row here" through two
restructures, with nothing checking it. A stale map is worse than none — it is the first thing a
newcomer reads, and it is believed.

`tests/test_repo_map.py` now checks the map against the tree in **both** directions (a row with no
directory, a directory with no row), that `src/` is still the only place code lives, and that every
directory has a `README.md`. That last one is not decoration: GitHub renders a folder's README the
instant you click it, which makes it the highest-leverage documentation in the repository, and it
existed in five of fourteen packages. The seventeen missing ones are written, each naming the
boundary against the neighbour it is most often confused with rather than restating its modules —
prose that restates code is prose that rots.

Each of the five assertions was broken on purpose and observed failing before being trusted. That
step is not ceremony here: **this class of test fails by finding nothing rather than by raising**,
and it has produced two green-but-vacuous tests in this repository already
(`test_image_ships_every_first_party_package` iterating an empty set, `make db-migrate` globbing a
moved directory). Every check in the new module asserts its input set is non-empty for the same
reason.

## What was deliberately not done

Four candidates that look like consistency and would each have cost a distinction:

- **Merging `science/*` into `connectors/*`.** Puts Temporal imports inside the physics and makes
  the engines untestable without a broker. The pairing *is* the layering rule.
- **Merging `memory/` into `retrieval/`.** They sound alike — both "read back out" — and are not:
  retrieval answers *what do we have on this*, memory answers *what did past work teach us*. Eleven
  modules would go a level deeper to save a word.
- **Uniform file sets across connector bundles.** `calc` has workflows, activities and a worker;
  `chem` has only a server. That variance says which capabilities own durable work — deleting it
  would delete information.
- **Burying `knowledge/` and `skills/` under `data/`** — see above.

Also raised and declined: splitting `core/config.py` (1726 lines) and `api/app.py` (1350). Both are
genuinely hard to browse, and file size is a different problem from repository structure; mixing
them into a change that is otherwise a set of moves would make the diff unreviewable. The config
split is cheap whenever it is wanted — all 118 import sites say `from chemclaw.core.config import
settings`, so the seam does not move.

## Two smaller corrections, both of the same kind

**`deploy/README.md` listed an `mcp-molfp`/`mcp-rxnfp` component** that `entrypoint.sh` has no case
for and the chart has never declared. This is the D-117 failure exactly — prose asserting a
deployable that does not exist — found in a file that the chart↔entrypoint test does not read. The
fingerprint capabilities deploy as `connector-molfp`/`connector-rxnfp` like every other bundle.

**A quotation of history had been rewritten.** `tests/test_deploy_chart.py` quotes the entrypoint
line that kept `mcp-calc` routable: `exec python -m mcp_servers.calc.server`. D-148's repository-wide
rewrite of `mcp_servers.…` paths caught that line too, so the quotation said something the file had
never said. Restored, with a note. The general rule, which D-148 also learned by corrupting other
branches' ADR citations: **a mechanical substitution cannot tell which occurrences are claims about
the present and which are quotations of the past.** Scope it, or read the diff.

## Also

The five finished `*-plan.md` moved to `docs/archive/plans/`, leaving `docs/planning/` with only the
four documents a session actually maintains. A completed plan reads exactly like a live one — same
imperative voice, same ticket numbers — so mixing them made the directory a guess.
