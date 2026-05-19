-- 003 — projects geo enrichment
--
-- Populated by core/geocode/. County is split out for cheap filtering
-- (Wasatch Front MVP scope is defined as 4 specific counties).

ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS lat    DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS lng    DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS county TEXT;

CREATE INDEX IF NOT EXISTS idx_projects_county ON projects(county);
