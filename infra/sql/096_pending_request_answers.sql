-- Where an answer goes when the question is asked again
-- (D-2026-09-13-an-answer-is-archived-so-the-question-can-be-asked-again).
--
-- `pending_requests` is keyed on `request_id`, which `request_id_for` derives from
-- `(kind, subject, asked_of)` — deliberately, so an at-least-once redelivery of the opening activity
-- coalesces onto one row instead of asking a person twice. The cost of that key is that a *second*
-- cycle of the same question is the same row, and reopening it NULLs `answered_at`, `answered_by`
-- and `answer`. Migration 079 closed that by refusing the reopen: the guard admits only the terminal
-- states in which nobody answered.
--
-- **Refusing is the right direction and the wrong outcome.** A legitimate re-ask — the same question
-- put to the same people in a later campaign round, a re-launched approval — meets an `answered` row,
-- writes nothing, and `durable/awaiting.py` raises a non-retryable `ApplicationError`, so the
-- workflow fails rather than waiting. The question cannot be asked again for as long as the old
-- answer stands, which for this table is for ever: it is in `retention._NOT_PRUNED`.
--
-- So the answer moves somewhere it cannot be overwritten, and the reopen is allowed.
-- `(request_id, run_id)` is the key, because a run is what a cycle is: the run that answered owns
-- the archived row, and the run that re-asks owns the live one. `ON CONFLICT DO NOTHING` on the
-- archive write, so a retried open that has already archived is a no-op rather than a failure.
--
-- **Refused by retention and retained through erasure, inheriting `pending_requests`' argument
-- rather than getting a new one.** These rows *are* that table's attribution — who asked somebody to
-- run, review or deliver something and who answered — moved aside so the question can be asked
-- again. The growth bound is the same and tighter: one row per *answered* cycle that was later
-- re-asked, which is human-paced twice over. `agent/leaver.py` scrubs `requested_by` and
-- `answered_by` here exactly as it does there; `asked_of` stays, for the same reason it stays there
-- (advisory routing, possibly an entitlement rather than a person).
--
-- INSERT only in `infra/sql/grants/app_privileges.sql`: an archived answer is written once and never
-- revised. The application holds no UPDATE and no DELETE on it, which is what makes "an answer that
-- has been moved aside cannot be edited" enforced rather than intended.
CREATE TABLE IF NOT EXISTS pending_request_answers (
    request_id   TEXT        NOT NULL,
    run_id       TEXT        NOT NULL,
    kind         TEXT        NOT NULL,
    subject      TEXT        NOT NULL,
    asked_of     TEXT        NOT NULL DEFAULT '',
    requested_by TEXT        NOT NULL DEFAULT '',
    session_id   TEXT        NOT NULL DEFAULT '',
    answered_at  TIMESTAMPTZ NOT NULL,
    answered_by  TEXT        NOT NULL,
    answer       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    -- When the archive row was written, which is when the question was asked again. Distinct from
    -- `answered_at`: the gap between them is how long the old answer stood.
    archived_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (request_id, run_id),
    -- The same claim `076`'s `pending_requests_answer_is_attributed` makes one table over: a row
    -- here exists *because* somebody answered, so an unattributed one is a row asserting that
    -- nobody did.
    CONSTRAINT pending_request_answers_is_attributed CHECK (answered_by <> '')
);

-- Every read of this table is "what was answered for this request", which is the key's own prefix,
-- so no index beyond the primary key is needed. An erasure scan is by `answered_by`/`requested_by`
-- and runs once per departing person against a table bounded by how often a human is asked
-- something twice — a sequential scan there is cheaper than an index nobody else reads.
