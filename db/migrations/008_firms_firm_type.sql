-- 008 — firms.firm_type
--
-- Plan §2.1 chose firm_type-on-firms over separate owners/developers
-- tables. A single firm can play multiple roles across projects (the
-- same entity can own one project and develop another), so type is
-- attached to the firm record itself, not to the role.
--
-- 'unknown' is the default until the resolver classifies. Multiple
-- types per firm (e.g. owner + developer) live in the JSONB array
-- `firm_type_aux`; `firm_type` holds the dominant / canonical one.

ALTER TABLE firms
    ADD COLUMN IF NOT EXISTS firm_type     TEXT NOT NULL DEFAULT 'unknown'
        CHECK (firm_type IN (
            'architect', 'engineer', 'contractor',
            'owner', 'developer', 'consultant', 'subcontractor',
            'other', 'unknown'
        )),
    ADD COLUMN IF NOT EXISTS firm_type_aux JSONB NOT NULL DEFAULT '[]';

CREATE INDEX IF NOT EXISTS idx_firms_firm_type ON firms(firm_type);
