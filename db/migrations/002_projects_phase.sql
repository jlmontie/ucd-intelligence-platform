-- 002 — projects.phase
--
-- Distinct from the existing `status` column, which is a coarse
-- corpus-only label (completed / under_construction / announced).
-- `phase` is the lifecycle bucket the public-data feed cares about
-- (planning → design → approved → bidding → construction → completed).

ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS phase TEXT
        CHECK (phase IN (
            'planning', 'design', 'approved',
            'bidding', 'construction', 'completed'
        ));

CREATE INDEX IF NOT EXISTS idx_projects_phase ON projects(phase);
