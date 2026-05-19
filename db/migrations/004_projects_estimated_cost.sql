-- 004 — projects.estimated_cost_usd
--
-- Corpus rows usually carry a final / actual cost in `cost_usd`.
-- Public-data rows usually carry an estimate (procurement budget,
-- appropriation line, STIP programmed amount). Keep them in separate
-- columns so we can show both on a merged entity page.

ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS estimated_cost_usd BIGINT;
