-- 009 — promote people to first-class usage
--
-- The people table existed but was never populated. Add the columns
-- and supporting tables the resolver needs:
--   - person_mentions parallels firm_mentions for entity resolution.
--   - confidence on people captures resolver certainty.
--   - (name, firm_id) is the natural key (same name at different firms
--     is a different person until proven otherwise).

ALTER TABLE people
    ADD COLUMN IF NOT EXISTS confidence REAL NOT NULL DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS notes      TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_people_name_firm
    ON people(name, COALESCE(firm_id, 0));

CREATE TABLE IF NOT EXISTS person_mentions (
    id           SERIAL PRIMARY KEY,
    raw_name     TEXT NOT NULL,
    raw_title    TEXT,
    raw_firm     TEXT,
    canonical_id INTEGER REFERENCES people(id),
    confidence   REAL,
    corrected    BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_person_mentions_raw  ON person_mentions(raw_name);
CREATE INDEX IF NOT EXISTS idx_person_mentions_trgm ON person_mentions USING GIN (raw_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_people_name_trgm     ON people USING GIN (name gin_trgm_ops);
