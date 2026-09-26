-- `experiment_arm_results.value` is finite, and the database says so rather than only the model.
--
-- **Why the database has to say it too.** 107 created the table with `value DOUBLE PRECISION NOT
-- NULL` and no check, and the model in front of it (`protocols/results.ArmResult`) accepted `NaN`
-- and `±inf` until the hardening wave that added `allow_inf_nan=False` — including the tool-call
-- strings "NaN" and "inf", which pydantic parses to exactly that. The table is append-only by
-- grant, so a non-finite value that lands is permanent: it reads as a disagreement with itself
-- (`nan != nan`) and reaches a surrogate through `observations_for` as a measured value. The model
-- is the one writer today; the database is what holds when a second writer, a manual `INSERT` or a
-- restore from another system is not.
--
-- Postgres treats `'NaN'::float8` as equal to itself (unlike IEEE), so `NOT IN` refuses it along
-- with both infinities; `tests/test_protocol_results.py` drives all three against the table.
--
-- **Guarded by `pg_constraint` rather than dropped and re-added**, which is the other re-runnable
-- spelling `tests/test_migrations_are_additive.py` accepts, and the reason is what a replay does to
-- an operator's work. A drop-then-add on replay silently re-creates the constraint `NOT VALID`,
-- discarding a `VALIDATE CONSTRAINT` somebody ran on purpose; the guard leaves an existing
-- constraint exactly as it found it. It also keeps `DROP CONSTRAINT` out of the file, a statement
-- the rollback check has to flag because it cannot tell a widening from a key replacement.
--
-- **The previous image keeps writing.** Every row it writes goes through `ArmResult`, whose `value`
-- already refuses non-finite input, so this constraint rejects nothing it would send — the
-- rollback is still "deploy the previous image".
--
-- **`NOT VALID`, then validated only when the rows already satisfy it.** An image from before that
-- hardening wave could have stored a non-finite value, and `core.migrate` applies the whole run in
-- one transaction — an unconditional `VALIDATE` would abort every migration after this one on such
-- a database, to protect rows that are already there. So new writes are refused from here on in
-- every case, and the existing rows are proven by the database whenever they can be. Which arm a
-- database took is `pg_constraint.convalidated`; an operator who finds `false` there has a
-- pre-hardening non-finite row to look at before running `VALIDATE CONSTRAINT` by hand. That arm
-- is not silent: it raises a WARNING naming the count, which `core.migrate` logs as
-- `migrate.server_warning` (psycopg drops a notice nobody registered a handler for). The scan
-- runs under the lock the `ADD` already holds, and the table is small by construction (one row per
-- measured outcome per arm), unlike `session_messages` in 046.
--
-- Applied by `make db-migrate`.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'experiment_arm_results'::regclass
           AND conname = 'experiment_arm_results_value_finite'
    ) THEN
        ALTER TABLE experiment_arm_results
            ADD CONSTRAINT experiment_arm_results_value_finite
            CHECK (value NOT IN ('NaN'::float8, 'Infinity'::float8, '-Infinity'::float8))
            NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM experiment_arm_results
         WHERE value IN ('NaN'::float8, 'Infinity'::float8, '-Infinity'::float8)
    ) THEN
        ALTER TABLE experiment_arm_results
            VALIDATE CONSTRAINT experiment_arm_results_value_finite;
    ELSE
        RAISE WARNING 'experiment_arm_results_value_finite left NOT VALID: % non-finite row(s) '
            'in experiment_arm_results; inspect them, then run VALIDATE CONSTRAINT by hand',
            (SELECT count(*) FROM experiment_arm_results
              WHERE value IN ('NaN'::float8, 'Infinity'::float8, '-Infinity'::float8));
    END IF;
END
$$;
