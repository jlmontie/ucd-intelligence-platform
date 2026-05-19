-- 011 — child-row uniqueness on roles, claims, quotes
--
-- Plan §2.6. Encodes the natural keys in the schema so neither legacy
-- ingest nor the merge_projects primitive can produce duplicate child
-- rows silently. Surfaced by Q2 of notebooks/schema_smoke.ipynb: HOK
-- appeared 3× as architect on Delta Sky Club after merge_projects
-- re-pointed legacy duplicates whose existence the schema didn't
-- forbid.
--
-- Natural keys:
--   roles  — (project_id, firm_id, role, team)
--   claims — (project_id, article_id, text)
--   quotes — (project_id, article_id, text, speaker_name)
--
-- claims.text and quotes.text can grow long; using md5() in a unique
-- expression index keeps the index size bounded regardless of content
-- length. roles columns are all short, so a plain UNIQUE works.

-- ── Dedup pass ───────────────────────────────────────────────────────────
-- Keep MIN(id) per natural key; the constraint creation below would
-- otherwise fail on existing duplicates.

DELETE FROM roles a
USING roles b
WHERE a.project_id = b.project_id
  AND a.firm_id    = b.firm_id
  AND a.role       = b.role
  AND a.team       = b.team
  AND a.id         > b.id;

DELETE FROM claims a
USING claims b
WHERE a.project_id IS NOT DISTINCT FROM b.project_id
  AND a.article_id = b.article_id
  AND a.text       = b.text
  AND a.id         > b.id;

DELETE FROM quotes a
USING quotes b
WHERE a.project_id   IS NOT DISTINCT FROM b.project_id
  AND a.article_id   = b.article_id
  AND a.text         = b.text
  AND a.speaker_name IS NOT DISTINCT FROM b.speaker_name
  AND a.id           > b.id;

-- ── Constraints ──────────────────────────────────────────────────────────

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uniq_roles_natural_key'
    ) THEN
        ALTER TABLE roles
            ADD CONSTRAINT uniq_roles_natural_key
            UNIQUE (project_id, firm_id, role, team);
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uniq_claims_natural_key
    ON claims (project_id, article_id, md5(text));

CREATE UNIQUE INDEX IF NOT EXISTS uniq_quotes_natural_key
    ON quotes (project_id, article_id, md5(text), speaker_name);
