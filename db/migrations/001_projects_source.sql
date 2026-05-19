-- 001 — projects.source
--
-- Tracks where a project record came from. Lets the merger inflection
-- (Part 4) join corpus and public-data rows on the same physical table.
--   corpus       — extracted from a UCD magazine article
--   public_data  — scraped from UP3 / DFCM / STIP / etc.
--   merged       — manually or programmatically reconciled

ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'corpus'
        CHECK (source IN ('corpus', 'public_data', 'merged'));

CREATE INDEX IF NOT EXISTS idx_projects_source ON projects(source);
