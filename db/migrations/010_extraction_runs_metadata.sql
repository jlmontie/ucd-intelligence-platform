-- 010 — extraction run metadata on probe_runs
--
-- §2.4 requires tracking extraction runs by
-- (article_id, model, prompt_version, ran_at). probe_runs already
-- carries probe_id, article_id, probe_version (= prompt_version) and
-- ran_at; this migration adds the missing `model` column and a
-- `content_hash` column so the runner can cheaply check whether an
-- article's text has changed since the last successful run.
--
-- The existing UNIQUE (probe_id, article_id, probe_version) is dropped
-- and replaced with UNIQUE (probe_id, article_id, probe_version,
-- content_hash). That way a re-OCR'd article (new content_hash) gets
-- a new probe_run row instead of a conflict, and history is preserved.

ALTER TABLE probe_runs
    ADD COLUMN IF NOT EXISTS model        TEXT,
    ADD COLUMN IF NOT EXISTS content_hash TEXT;

ALTER TABLE probe_runs
    DROP CONSTRAINT IF EXISTS probe_runs_probe_id_article_id_probe_version_key;

CREATE UNIQUE INDEX IF NOT EXISTS idx_probe_runs_unique
    ON probe_runs (
        probe_id,
        article_id,
        probe_version,
        COALESCE(content_hash, '')
    );

CREATE INDEX IF NOT EXISTS idx_probe_runs_article ON probe_runs(article_id);
