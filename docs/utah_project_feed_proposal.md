# Utah Construction Project Intelligence Feed
## Project Proposal

*Prepared April 2026*

---

## Executive Summary

No product currently does for Utah what DATABEX does for Arizona — surface early-stage construction projects months before formal bidding, organized by sector, owner, and phase. This proposal evaluates the feasibility of building that product as a parallel effort to the UCD Intelligent Research Platform, with a planned merger into a unified platform.

The public data infrastructure in Utah is strong. The technical foundation being built for the UCD Platform applies directly. A focused MVP covering public-sector projects across the Wasatch Front is achievable in 3–4 months. Combined with the UCD corpus intelligence layer, the merged product would be meaningfully more capable than DATABEX — and would have no direct regional competitor.

---

## The Market Gap

Construction professionals in Utah currently have no dedicated regional tool for early-stage project discovery. The national platforms — Dodge Data & Analytics and ConstructConnect — technically cover Utah, but their regional depth is thin. They surface large projects that reach national visibility; they routinely miss the $3M school addition approved at a January board meeting in Tooele County, or the $12M municipal facility authorized in a December capital appropriations bill.

DATABEX fills this gap in Arizona by monitoring local government sources continuously and surfacing projects 2–6 months before formal bidding begins. That head start gives subscribers time to build relationships with owners and design teams before bid lists are set — which is where pursuits are won or lost.

Utah has equivalent or better public data infrastructure. The gap is not data availability. It is the absence of a product that monitors, structures, and delivers it.

---

## What This Product Does

A Utah construction project lead discovery platform. Core capabilities at launch:

- **Early-stage project alerts** — projects surfaced at the planning and design phase, months before GC/sub solicitation
- **Sector classification** — organized across construction typologies (K-12, higher education, healthcare, industrial, multifamily, civic, transportation, etc.)
- **Owner and developer profiles** — project history, delivery method preferences, contact context
- **Saved searches and daily digests** — natural-language filters; email alerts when matching projects appear or update
- **Geographic filtering** — county, city, Wasatch Front vs. statewide
- **Project phase tracking** — planning → design → approval → procurement → construction

This is a project-lead discovery tool. It answers: *"What should I be pursuing, and how early can I know about it?"*

---

## Data Sources

Utah's public data infrastructure is genuinely strong, particularly for public-sector projects. The following sources form the monitoring stack:

### Tier 1 — Structured, High-Quality, Directly Ingestible

| Source | What It Provides | Signal Timing |
|---|---|---|
| **UP3 (Utah Public Procurement Place)** | All state agency design and construction solicitations | 12–18 months pre-construction |
| **transparent.utah.gov** | State contract awards, including design firm selection | 12–18 months pre-construction |
| **UDOT STIP** | Multi-year highway and infrastructure project pipeline | 1–3 years out |
| **Utah Legislature capital appropriations** | Line-item project authorizations in annual budget bills | 1–3 years out |
| **DFCM project lists** | State building and facilities projects | 12–24 months pre-construction |

### Tier 2 — Accessible, Requires Extraction

| Source | What It Provides | Signal Timing |
|---|---|---|
| **Salt Lake City and County planning portals** | Design review, conditional use permits, zoning applications | 6–18 months pre-construction |
| **Utah County, Davis, Weber, Washington County portals** | Same as above for major counties | 6–18 months pre-construction |
| **School district board meeting minutes** | Bond project resolutions, facility approvals (41 districts) | 12–24 months pre-construction |
| **Municipal planning commission agendas** | Provo, Ogden, St. George, Logan, Lehi, etc. | 6–12 months pre-construction |

### Tier 3 — Network and Monitoring

| Source | What It Provides | Signal Timing |
|---|---|---|
| **County recorder land transactions** | Developer land acquisitions; intent signals | 12–36 months pre-construction |
| **Regional business press** | Developer announcements, design award press releases | 6–18 months pre-construction |
| **Utility interconnection requests** | Large commercial and industrial development signals | 12–24 months pre-construction |

Tier 1 is clean, structured, and fully automatable. Tier 2 requires LLM-based extraction from PDFs and inconsistently formatted web sources — the same pipeline being built for UCD corpus ingestion applies directly here. Tier 3 requires ongoing monitoring and some human triage for quality.

---

## Technical Feasibility

The UCD Intelligent Research Platform is already planning to build:

- A document ingestion pipeline with multimodal LLM extraction
- An entity resolution layer for firms, owners, and people
- A canonical project database (`projects`, `firms`, `owners`, `roles`)
- A web serving and query layer

The Utah project feed reuses all of this. The incremental build is the monitoring and ingestion layer on top of a shared data model — not a parallel codebase.

### Estimated Build Effort

| Component | Estimated Effort |
|---|---|
| UP3, transparent.utah.gov, DFCM scrapers | 1–2 weeks |
| UDOT STIP and legislature appropriations parsers | 1 week |
| Major county and city planning portal scrapers | 2–3 weeks |
| School district board minute ingestion (PDF → LLM extract) | 2–3 weeks |
| Regional press and news monitoring | 1–2 weeks |
| County recorder land transaction monitoring | 2 weeks |
| Internal triage interface (human review queue) | 2 weeks |
| Subscriber-facing project feed, search, and alerts | 3–4 weeks |
| **Total — public-sector MVP, Wasatch Front** | **10–14 weeks** |

Adding comprehensive private development tracking (county recorder depth, developer network) adds approximately 4–6 weeks of build plus ongoing operational overhead.

### Automation vs. Human Triage

A core operational question for any project intelligence product is how much ongoing human attention quality requires. An honest breakdown:

**Fully automatable:**
- Ingesting and parsing structured government portals (UP3, transparent.utah.gov, UDOT)
- LLM extraction from structured PDFs (appropriations bills, board minutes)
- Sector classification and entity resolution
- Alert matching and delivery

**Requires human judgment:**
- Distinguishing significant projects from routine permits (a $200K church bathroom addition vs. a $15M campus expansion)
- Identifying private development projects before a permit is filed
- Validating early-stage cost estimates (often absent or approximate)
- Catching errors in public filings

DATABEX operates with a small editorial team doing ongoing data triage. A Utah equivalent, focused primarily on public-sector projects at launch, could run leaner — but private development tracking at DATABEX's depth requires sustained editorial investment.

---

## The Merger with UCD Platform

The two products share a data model. The UCD Platform already defines:

- `projects` — canonical project records
- `firms` / `owners` / `people` — canonical entities with alias resolution
- `roles` — firm-project relationships (the teaming graph)

The Utah project feed populates those same tables, with a `source` field distinguishing `public_data` from `corpus`. When the UCD Platform later ingests an article covering a project already in the database from public monitoring, the record enriches automatically: the project stub becomes a full entry with historical context, team details, narrative claims, and quotes.

This enrichment moment is itself a product feature. A subscriber following a tracked project receives not just bid-phase updates but: *"New UCD coverage available for this project — read the team profile and comparable projects."* No current product offers that combination.

The merged platform answers two questions that today require entirely separate research efforts:
1. *What projects are coming?* (from the monitoring feed)
2. *Who are the players, what have they done, and how should I position?* (from the UCD corpus intelligence layer)

---

## Competitive Position

| | Dodge / ConstructConnect | DATABEX (AZ only) | Utah Project Feed + UCD Platform |
|---|---|---|---|
| **Utah project coverage** | National; thin locally | Not available | Utah-dedicated |
| **Early-stage signals** | Moderate | Strong | Strong |
| **Local government sources** | Limited | Strong (AZ) | Strong |
| **Firm / teaming intelligence** | Minimal | Minimal | Deep (UCD corpus) |
| **Synthesized answers** | None | None | Conversational agent with citations |
| **Historical corpus depth** | None | None | Full UCD archive |
| **Regional entity graph** | None | None | Firms, people, owners, teaming history |

The Utah project feed alone is competitive with Dodge and ConstructConnect on regional depth. Combined with the UCD corpus intelligence layer, the merged product has no direct competitor in the Mountain West.

---

## Open Questions Before Committing

The following decisions shape scope, timeline, and ongoing operating cost significantly:

1. **Minimum project size** — $1M+, $5M+ (DATABEX threshold), or higher? A lower floor surfaces more opportunities but increases data volume and triage burden.

2. **Public vs. private development balance at launch** — A public-sector-only MVP (state agencies, school districts, municipalities) is cleanly buildable and requires minimal ongoing human triage. Adding private development at DATABEX's depth requires editorial/network investment from day one.

3. **Geographic scope at launch** — Wasatch Front only (Salt Lake, Utah, Davis, Weber counties) captures approximately 80% of Utah construction volume and is the natural starting point. Washington County (St. George) and Cache County (Logan) are logical Phase 2 additions. Full-state from day one is feasible but widens the monitoring surface.

4. **Standalone product or UCD-bundled** — Does the project feed launch as a separate subscription product (the DATABEX model) or as a feature tier within the UCD Platform? This is primarily a pricing and go-to-market decision, not a technical one.

5. **Operating model for data quality** — Who owns ongoing triage and data validation? A dedicated part-time editor, a shared responsibility within the team, or an AI-first approach with quality metrics? This determines ongoing operating cost more than any technical choice.

---

## Recommendation

This is feasible and strategically well-timed. The technical foundation is being built anyway; the marginal cost of adding a Utah project monitoring layer is modest relative to the standalone value it delivers. The regional market has no dedicated competitor. And the merger path — where a tracked project gains UCD corpus enrichment the moment UCD covers it — is a genuine product differentiator that neither Dodge, ConstructConnect, nor a standalone DATABEX equivalent can offer.

**Suggested approach:** Scope the MVP to public-sector projects on the Wasatch Front, targeting a 12–14 week build running in parallel with UCD Platform Stage 0–1. Validate subscriber interest and data quality before expanding to private development tracking or additional geographies. Merge the project feed data model with the UCD Platform at Stage 2 or Stage 3, when entity pages and the teaming graph are ready to enrich the project records.

The two products are stronger together than either is alone.
