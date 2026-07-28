# Reuma-evidens — Weekly Digest Routine

## Role
Weekly rheumatology evidence digest. Runs autonomously, no prompts needed once set up.

## Architecture
- **fetch_pubmed.py** (deterministic script): runs all 50 PubMed queries, parses XML —
  title/authors/dates/abstracts extracted verbatim, no AI involved
- **AI**: reads each fetched hit and assigns `disease_group` — a label only, never touches
  the fetched content
- **update_library.py** (deterministic script): dedups, buckets, writes `docs/data/library.json`
  (falls back to its own regex classifier only if AI didn't label a hit)
- **docs/index.html** (static page): reads `docs/data/library.json` client-side via JS — no regeneration needed per run
- **digests/YYYY-MM-DD.md** (optional weekly snapshot): audit trail of that week's new items only

This split matters: AI's only touch on the data is assigning a category label — it never
generates, paraphrases, or edits a title, author, date, or abstract. Everything else is
pure code.

## STEP 1 — SEARCH (PubMed)
- Date window: past 7 days (Entrez EDAT)
- `fetch_pubmed.py` runs all 50 queries (10 disease groups × 5 evidence types) via PubMed
  E-utilities and parses results in code — title, journal, authors, PMID, date, and
  **structured abstract** (PubMed's labeled `AbstractText` sections — Background/Methods/
  Results/Conclusion — kept as separate `{label, text}` pairs) are extracted verbatim,
  untouched by AI. Preprints & errata discarded automatically.
- **AI classifies** each hit into the most specific disease group it matches — a label
  assignment only, never a change to the fetched title/authors/abstract — and attaches
  it as `disease_group` on the hit before handing it to `update_library.py`.
  `update_library.py`'s regex patterns (`DISEASE_PATTERNS`) are a fallback only, used
  if AI leaves a hit unclassified; General is the fallback category, not the first choice.

### Disease groups (10)
RA, PsA/SpA, SLE, IIM, Vasculitis, Sjögren, SSc, PMR/crystal, Autoinflammatory, General

Query strings and evidence-type filters live in `fetch_pubmed.py`
(`DISEASE_QUERIES`, `EVIDENCE_FILTERS`) — edit there, not here, to keep one source of truth.

Evidence types (5): RCT, Guideline/consensus, Evidence synthesis, Observational, Clinical case/survey

## STEP 2 — UPDATE LIBRARY (run update_library.py)
Pass the classified hits into `update_library(new_hits, "docs/data/library.json")`. The script:
- Dedups by PMID against existing records
- Prepends new records (newest first)
- Drops records >24 months old
- Recomputes time-bucket for **all** records every run (buckets shift as weeks pass):
  - `recent` = last month
  - `mid` = 1–6 months
  - `older` = 6–24 months
- Writes `docs/data/library.json`

## STEP 3 — VIEWER (static, no regeneration)
`docs/index.html` + `docs/assets/style.css` read `docs/data/library.json` at load time via fetch
(cache-busted). Layout:
- Left sidebar: disease-group tabs, each with a live hit count
- Top of viewer: evidence-type tabs (only types with hits shown)
- Within each disease × evidence-type combo: three collapsible time-bucket sections
  ("Seneste måned" open by default, "1–6 måneder" and "Ældre" collapsed), each with a count
- Each item: title + tier badge (T1–T4, color-coded, sort-only — no explanation shown to user),
  authors ("A" / "A & B" / "First...Last" for 3+), journal, PMID link, collapsible full abstract
  rendered as labeled paragraphs (not flattened text)
- Empty combo: "Ingen nye."

No AI involvement in this step — it's pure JS reading JSON.

## STEP 4 — MARKDOWN ARCHIVE (optional, weekly audit trail)
This week's new items only, same grouping logic.
Format: `**{title}** — {first_author}...{last_author}, {journal} [T{tier}], PMID {pmid}`
Commit to `digests/YYYY-MM-DD.md`.

## Retention
24 months. Older records are pruned automatically by `update_library.py` on every run.

## DONE criteria
`docs/data/library.json` updated AND `docs/index.html` unchanged (static, no regen needed) AND
(optionally) `digests/YYYY-MM-DD.md` committed. Report: records added + total + hit count per
disease group.

## Notes for whoever (AI or human) runs this weekly
- Run order each week: `fetch_pubmed.fetch_all_hits()` → AI assigns `disease_group` to each
  hit (reading only, never rewriting title/authors/abstract) → `update_library.update_library()`.
- If PubMed's structured-abstract labels are missing for a record, don't invent them —
  store as a single unlabeled section (`fetch_pubmed.py` already does this automatically).
- Tier is looked up automatically in `fetch_pubmed.py` by journal name (`TIER_MAP`) — no
  manual or AI judgment needed:
  - T1: NEJM, Lancet, JAMA, Nature Medicine, BMJ
  - T2: Lancet Rheumatology, Ann Rheum Dis, Arthritis & Rheumatology, Nat Rev Rheumatol
  - T3: Rheumatology (Oxford), Arthritis Res Ther, RMD Open, Semin Arthritis Rheum,
        J Rheumatol, Arthritis Care Res
  - T4: everything else
