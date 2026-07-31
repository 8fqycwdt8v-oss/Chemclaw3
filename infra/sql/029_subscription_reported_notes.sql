-- What each subscriber has already been told, at the watermark's own date (DARK-7).
--
-- `durable/digest.py` decides a note is new with `valid_from >= last_seen_at::date`, and `>=` is
-- load-bearing in both directions: `>` would silently drop a note that appeared later on the same
-- day the digest ran — the common case, since a digest runs hourly and a note's `valid_from` is a
-- *date* — while `>=` re-qualifies every note dated that day on every subsequent run. At the
-- shipped hourly cadence one note is delivered up to 24 times, against `agent/subscriptions.py`'s
-- own promise that "asking twice does not double-notify".
--
-- The two failures are not symmetric — a missed note defeats the feature and a duplicate is a
-- nuisance — which is why the ordering elsewhere in that module deliberately favours re-reporting.
-- But the fix does not have to choose: keep `>=` and remember which ids were already sent *at that
-- date*. A note dated today is reported once; a note that arrives later today is still reported;
-- and the list resets when the date rolls over, so it is bounded by one day's matches rather than
-- by the corpus.
ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS last_seen_note_ids TEXT[] NOT NULL DEFAULT '{}';
