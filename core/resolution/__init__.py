"""
Entity resolution for firms, projects, and people.

Each module is also a CLI entry point (`python -m core.resolution.<name>`),
so this package's __init__ deliberately does NOT eagerly import them —
that would trigger Python's "module loaded twice" RuntimeWarning when
the same module is then run via `-m`.

Import the public API from the specific submodule:

    from core.resolution.resolve_firms    import resolve_firms, deterministic_match
    from core.resolution.resolve_people   import resolve_people, upsert_person
    from core.resolution.resolve_projects import resolve_project, merge_projects
    from core.resolution.classify_firms   import classify_role
"""
