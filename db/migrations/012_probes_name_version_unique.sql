-- 012 — fix probes uniqueness
--
-- The schema declared `probes.name TEXT NOT NULL UNIQUE`, which makes
-- it impossible to have v1 and v2 of the same probe coexist — and the
-- whole point of the versioning system (plan §2.4) is that historical
-- probe_runs keep their FK target after a version bump. Surfaced when
-- attempting to seed project_panel_v1 v2 alongside v1.
--
-- Replace the per-name UNIQUE with (name, version) UNIQUE.

ALTER TABLE probes DROP CONSTRAINT IF EXISTS probes_name_key;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uniq_probes_name_version'
    ) THEN
        ALTER TABLE probes
            ADD CONSTRAINT uniq_probes_name_version
            UNIQUE (name, version);
    END IF;
END $$;
