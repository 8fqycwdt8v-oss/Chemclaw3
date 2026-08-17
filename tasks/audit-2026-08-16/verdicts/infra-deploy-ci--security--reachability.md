# Verdicts — `infra-deploy-ci--security.md`, reachability lens

Scope: findings marked **critical** or **high** only. The source file contains exactly one —
the `db-grants` finding. The remaining five are two medium and three low and were not examined.

---

## `db-grants` reports success while reconciling nothing — the append-only audit grant fails open on any role not literally named `chemclaw_app`

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

### What I did

Every mechanical claim reproduces. I ran the whole chain against the live Postgres, in a scratch
database (`ccverify`) so I did not have to drop the `chemclaw_app` role another agent's experiment
left in the cluster. Cluster-wide roles mean "the role is absent" cannot be simulated in the shared
database, so I ran the *same file* with only the constant substituted.

1. The silent no-op, straight from the shipped file:

```
$ sed "s/'chemclaw_app'/'chemclaw_app_absent'/" infra/sql/grants/app_privileges.sql > /tmp/grants_absent.sql
$ psql -d ccverify -f /tmp/grants_absent.sql
NOTICE:  role chemclaw_app_absent does not exist; this deployment runs a single database principal ...
DO
psql exit=0
AFTER: DELETE INSERT REFERENCES SELECT TRIGGER TRUNCATE UPDATE   # chemclaw_runtime, unchanged
```

2. The NOTICE really is invisible through the Python path. `apply_grants` with `logging.basicConfig(level=DEBUG)`
   on the root logger (so any `psycopg` logger record would print):

```
$ uv run python /tmp/run_grants.py      # CHEMCLAW_SQL_MIGRATIONS_DIR=/tmp/sqlv (absent-role copy)
applied: ['app_privileges.sql']
exit=0
```

   Not one line of diagnostic output. psycopg installs no default notice handler and
   `core/grants.py` adds none.

3. The tamper itself:

```
$ psql -U chemclaw   -d ccverify -c "insert into audit_events (correlation_id, actor, tool, arguments, outcome, latency_ms)
                                     values ('c1','victim','safety_screen','{}','allowed',5)"
$ psql -U chemclaw_runtime -d ccverify -c "update audit_events set actor='someone-else', outcome='denied' where actor='victim';" \
                                       -c "delete from audit_events where actor='someone-else';" \
                                       -tAc "select count(*) from audit_events;"
UPDATE 1
DELETE 1
0
```

4. Nothing downstream notices. `grep -rn "has_table_privilege\|pg_roles\|current_user\|session_user" src/ --include=*.py`
   → **no matches**: no process ever checks its own privileges at startup or anywhere else.
   `grep -n "psycopg\|connect\|dsn" tests/test_database_privileges.py` → no matches; the test is
   file-vs-file, as the finding says.

5. The two claims I went looking for that the finding does not make:

```
$ grep -rn "CREATE ROLE\|GRANT ALL" --include=*.sql --include=*.sh --include=*.yaml --include=*.yml \
       --include=*.py --include=Makefile .    # (excluding .git)
   <no output>
```

   Nothing in this repository — not the chart, not `docker-compose.yml`, not `infra/live/bootstrap.sh`,
   not a migration — creates a runtime role or grants it anything. And:

```
$ grep -rn "chemclaw_app" .env.example deploy/helm/chemclaw/values.yaml
.env.example:76:   Set it (plus a `chemclaw_app` role and `make db-grants`) and the runtime credential loses ...
values.yaml:541:   ... Set it, create a `chemclaw_app` role, and the credential that can issue DDL ...
```

6. The other branch of the same misconfiguration, for comparison — role created but *not*
   pre-granted, which is what "create a `chemclaw_app` role and run `make db-grants`" reads as:

```
$ psql -U chemclaw_bare -d ccverify -c "select count(*) from audit_events;"
ERROR:  permission denied for table audit_events
```

Cleanup: `ccverify`, `chemclaw_runtime` and `chemclaw_bare` dropped; `chemclaw_app` (not mine)
left alone; `git status` shows no source file touched.

### Why

The mechanism is exactly as described and I would not argue with a word of it: a hardcoded SQL
literal, a `RETURN` that reports nothing anyone can read, an exit code of 0, and a runner that
prints `applied grants:` for a file that applied zero statements. The "no way to tell a supported
no-op from a switched-off control" observation is correct and is the genuinely valuable part of
this finding. Two things around the mechanism do not hold as stated, and together they move it off
**high**.

**Reachability is narrower than "a deployment that splits the principal."** The finding treats the
role name as an undocumented internal — "not a chart value, not a setting, and appears nowhere in
`values.yaml` except inside a prose comment on line 541." That comment is not incidental: it is the
operator instruction attached to the *very setting* that turns this mode on, it names the exact
literal, and `.env.example:76` — which the finding does not cite — repeats it verbatim beside
`CHEMCLAW_POSTGRES_MIGRATION_DSN`. So the trigger is not "an operator picks a plausible name"; it
is "an operator enables an opt-in mode and ignores the one sentence documenting how to enable it."
That is a real gap (prose is not a validator, and I agree it should be a settings key), but it is a
deployment-time deviation from a written instruction, not a path any caller or input can walk.

**The consequence needs a second operator act that this repository never performs.** The chain
"grants no-op → role keeps `GRANT ALL` → the chat credential rewrites `audit_events`" depends
entirely on the parenthetical *"typically `GRANT ALL ON ALL TABLES`"*. Nothing in the artifact under
review does that — grep finds no `CREATE ROLE` and no `GRANT ALL` anywhere in the repo, so whatever
the runtime role holds is SQL the operator wrote by hand. If they wrote the blanket grant, the
finding's outcome follows exactly (step 3 above). If they did what the instruction implies — create
the role and let `db-grants` supply the privileges — the misconfiguration is not silent at all: the
first query fails with `permission denied for table audit_events` (step 6), the pods crash-loop, and
the operator finds the naming mistake in minutes. The finding presents the silent branch as the
only branch.

**Severity: medium.** What is lost when the control fails open is a defense-in-depth narrowing whose
absence is *the shipped default for every single-principal deployment* — dev, CI, `make up`, and any
production that never sets the migration DSN. The failure state equals the supported baseline, so it
is not a new capability granted to anyone: it requires already holding the runtime DSN, and the
adversary who holds that is an insider or an already-compromised pod. No untrusted input reaches it,
no chemist is shown a wrong safety, impurity or structure answer, and no secret is disclosed. What is
genuinely lost is the *belief* that the trail is append-only in a deployment that paid for the split —
which is worth fixing, and the finding's own minimal fix (a notice handler plus a hard failure when a
distinct migration DSN is configured and the named role is missing) is the right size for it. I would
add one thing the reporter missed, which makes the fix cheaper rather than the defect worse: the file
already knows how to distinguish "absent" from "present" per object — the eight `to_regclass` guards
in the LangGraph block do exactly that, deliberately, with the reasoning written out. The role guard
is the one place in the file where absence is treated as success instead of as a decision.
