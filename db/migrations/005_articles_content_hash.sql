-- 005 — articles.content_hash
--
-- Stable hash of the article's source pages (concatenated extracted text).
-- The probe runner uses this as the cache key: a probe only re-runs when
-- (probe_id, article_hash, probe_version) is unseen. If page text changes
-- (e.g. re-OCR), the hash changes and probes re-run automatically.

ALTER TABLE articles
    ADD COLUMN IF NOT EXISTS content_hash TEXT;

CREATE INDEX IF NOT EXISTS idx_articles_content_hash ON articles(content_hash);
