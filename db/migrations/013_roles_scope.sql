-- 013 — roles.scope
--
-- Plan §2.7 (Tier 3 of the consolidation cleanup). The probe panel
-- often emits role qualifiers in parenthetical form on either the
-- firm name or the role string, e.g.:
--
--     {"firm": "Flynn Companies",          "role": "Roofing (patching)"}
--     {"firm": "Flynn Companies (patching)", "role": "Roofing"}
--     {"firm": "FW Specialties",          "role": "Flooring (terrazzo)"}
--
-- Tier 1 + Tier 2 of consolidate.py collapsed these into a single
-- canonical role row but lost the "(patching)" / "(terrazzo)" detail.
-- A future reingest would re-introduce the same scope text in the
-- role string, dedupe-via-natural-key would keep one variant, and the
-- scope info would silently disappear again. Promoting `scope` to a
-- first-class column lets materialize_from_probes peel parenthetical
-- detail off `role` and `firm` and stash it here. Existing rows keep
-- NULL — they predate the column.
--
-- The natural-key UNIQUE on `roles` stays
-- (project_id, firm_id, role, team) — `scope` is metadata, not part
-- of identity. Two extractions of the same (project, firm, role,
-- team) with different scopes will still dedupe; whichever scope
-- writes first wins. Update with care if scope quality matters.

ALTER TABLE roles
    ADD COLUMN IF NOT EXISTS scope TEXT;

COMMENT ON COLUMN roles.scope IS
    'Subscope qualifier originally extracted as a parenthetical on '
    'firm or role (e.g. "patching", "subtier ductwork", '
    '"exterior/curtain wall"). NULL when the probe output had no '
    'qualifier. Not part of the role''s natural key — first writer '
    'wins on conflicting scopes for the same (project, firm, role, team).';
