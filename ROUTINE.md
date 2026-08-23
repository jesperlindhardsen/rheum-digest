# Reuma-evidens — Weekly Digest Routine

## Role
Weekly rheumatology evidence digest. Runs autonomously, no prompts needed once set up.

## Architecture
- **fetch_pubmed.py** (deterministic script): runs all 55 PubMed queries, parses XML —
  title/authors/dates/abstracts extracted verbatim, no AI involved
- **AI**: reads each fetched hit and assigns `disease_groups` — labels only, never touches
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
- `fetch_pubmed.py` runs all 55 queries (11 disease groups × 5 evidence types) via PubMed
  E-utilities and parses results in code — title, journal, authors, PMID, date, and
  **structured abstract** (PubMed's labeled `AbstractText` sections — Background/Methods/
  Results/Conclusion — kept as separate `{label, text}` pairs) are extracted verbatim,
  untouched by AI. Preprints & errata discarded automatically.
- **Language**: English and Danish only. Enforced twice in `fetch_pubmed.py` — as a
  PubMed search filter (`LANGUAGE_FILTER`, so other languages are never fetched) and
  again on the parsed `<Language>` tags (`ALLOWED_LANGUAGES`), which are authoritative.
  An article tagged with several languages is kept if any one of them is allowed.
  Each record stores its `languages` list.
- **AI classifies** each hit into the most specific disease group it matches — a label
  assignment only, never a change to the fetched title/authors/abstract — and attaches
  it as `disease_groups` (a list) on the hit before handing it to `update_library.py`.
  `update_library.py`'s regex patterns (`DISEASE_PATTERNS`) are a fallback only, used
  if AI leaves a hit unclassified; General is the fallback category, not the first choice.
- **Cross-listing**: normally one group per hit. A second group is added only when the
  article genuinely covers both — the standing case being large-vessel vasculitis
  (GCA and Takayasu treated together), which belongs under Vasculitis *and* PMR/GCA.
  Not for passing mentions: a paper is cross-listed when both groups are its subject.
  Tab counts therefore sum to more than the record count; that's expected.

### Disease groups (11)
Display order (`DISEASE_ORDER` in `docs/index.html`, also used for digest grouping):
RA, PsA/SpA, PMR/GCA, Crystal, General, SLE, IIM, Vasculitis, Sjögren, SSc, Autoinflammatory

That order is for reading, not for classifying: `DISEASE_PATTERNS` in
`update_library.py` runs in its own precedence order (most specific first,
PMR/GCA ahead of Vasculitis, General last). Reordering one must not reorder the other.

`PMR/GCA` covers polymyalgia rheumatica *and* giant cell arteritis (incl. temporal
arteritis) — one clinical spectrum, so GCA sits here rather than under Vasculitis.
`Crystal` is gout and CPPD/pseudogout. Classification checks PMR/GCA before
Vasculitis, since GCA matches both.

Query strings and evidence-type filters live in `fetch_pubmed.py`
(`DISEASE_QUERIES`, `EVIDENCE_FILTERS`) — edit there, not here, to keep one source of truth.

Evidence types (5), by stored key: RCT, Guideline/consensus, Evidence synthesis,
Observational, Clinical case/survey. The viewer relabels these for display
(`EVIDENCE_LABEL` for the tabs, `EVIDENCE_BADGE` for the shorter on-card chip, both
in `docs/index.html`) — notably Evidence synthesis reads "Meta/reviews". The keys
are what lives in the data; don't rename them there.

**Which query found a hit does not decide its type.** The `[tiab]` clauses in
`EVIDENCE_FILTERS` are deliberately loose — a systematic review that merely mentions
"randomized controlled trial" in its abstract is returned by the RCT query — and
`fetch_all_hits` dedups first-wins, so that label would stick. `classify_evidence_type()`
therefore derives the type from PubMed's own `PublicationType`, most specific first
(`EVIDENCE_PRECEDENCE`: Guideline → Evidence synthesis → RCT → Observational → Case).
The finding query is only the fallback, used when a record carries none of those tags —
common for ahead-of-print records not yet MeSH-indexed (296 of the current 826).
Each record stores its `publication_types` so this stays auditable without re-fetching.

## STEP 2 — UPDATE LIBRARY (run update_library.py)
Pass the classified hits into `update_library(new_hits, "docs/data/library.json")`. The script:
- Dedups by PMID against existing records
- Prepends new records (newest first)
- Drops records >24 months old
- Recomputes `time_bucket` (`recent` ≤1mo / `mid` 1–6mo / `older` 6–24mo) for **all**
  records every run. Retained as metadata — the viewer groups by calendar month
  instead (see Step 3), so nothing on the page depends on it.
- Writes `docs/data/library.json` as `{"generated_at": "YYYY-MM-DD", "records": [...]}`.
  `generated_at` is the run date — that's what the viewer shows as "Sidst opdateret",
  deliberately *not* the newest record's publication date (journals post-date issues,
  so that would read as a future date).

## STEP 3 — VIEWER (static, no regeneration)
`docs/index.html` + `docs/assets/style.css` read `docs/data/library.json` at load time via fetch
(cache-busted). Layout:
- Left sidebar: evidence-type tabs, led by an **"Alt"** tab (all types), each with a
  live count
- Disease-group tabs across the top, led by an **"Alle"** tab (all groups)
- Default view is **Alt × Alle** — the whole library — and both pseudo-tabs come
  first in their row. They are viewer-only (`ALL_EVIDENCE` / `ALL_GROUP` in
  `docs/index.html`), never stored values, so they stay out of `DISEASE_ORDER`,
  out of `disease_groups`, and out of the digests. A cross-listed article appears
  once under "Alle", not twice.
- Tier filter: T1–T4 checkboxes, all on by default, each showing its count within
  the active evidence type. Applies to everything — tab counts and which disease
  tabs appear follow it, so a count never promises items the filter is hiding.
  With every tier unchecked the panel reads "Vælg mindst ét niveau."
- Within each disease × evidence-type combo: one collapsible section per calendar
  month, newest first, each with a count, at the same font size as the disease tabs.
  Every month within the last six of the run month is open on load; older ones are
  collapsed. A combo whose newest items already predate that window still opens its
  topmost section, so it never loads looking empty.
  - The section for the run's own month is labelled "Seneste måned"; every other
    section carries month + year ("Juli 2026"). A sparse combo whose newest items
    are months old therefore opens on a dated section, not on "Seneste måned".
  - Records post-dated past the run month (journals publish issues ahead) are
    folded into the "Seneste måned" section rather than sorting above it.
- Each item, in order: title + tier badge (T1–T4, color-coded, sort-only — no
  explanation shown to user); reference line (authors as "Name" when first and last
  author match, otherwise "First...Last"; journal in bold; date; PMID link); PubMed
  a chip row — the article's evidence type first (short form via `EVIDENCE_BADGE`:
  RCT / Retningslinje / Meta/review / Observationel / Case, in plum, the one accent
  the tier badges don't use), then its PubMed keywords; then the collapsible abstract,
  rendered as labeled paragraphs (not flattened text). The chip row sits above the
  toggle so it stays visible while the abstract is collapsed.
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
- Run order each week: `fetch_pubmed.fetch_all_hits()` → AI assigns `disease_groups` to each
  hit (reading only, never rewriting title/authors/abstract) → `update_library.update_library()`.
- If PubMed's structured-abstract labels are missing for a record, don't invent them —
  store as a single unlabeled section (`fetch_pubmed.py` already does this automatically).
- Tier is assigned automatically by `fetch_pubmed.journal_tier()` from the journal's
  ISO abbreviation — no manual or AI judgment needed. Stored as 1–4; the viewer names
  them (`TIER_LABEL` in `docs/index.html`):
  - **1 · Top medicin** — `TIER1_JOURNALS`: NEJM, Lancet, JAMA, BMJ, Ann Intern Med,
    Nature Medicine, NEJM Evidence, JAMA Intern Med, JAMA Netw Open, EClinicalMedicine.
    Plus `TIER1_PREFIXES` for the Lancet's regional titles, which only ever appear with
    a suffix (Lancet Reg Health Eur / Am / …) and so can't be matched exactly.
  - **2 · Top reumatologi** — `TIER2_JOURNALS`: Ann Rheum Dis, Arthritis & Rheumatology,
    Lancet Rheumatology, Nat Rev Rheumatol, Rheumatology (Oxford), RMD Open,
    Semin Arthritis Rheum, J Rheumatol, Arthritis Care Res, Arthritis Res Ther.
  - **3 · Reumatologi** — everything else rheumatological. Deliberately *not* a list:
    `RHEUM_JOURNAL` matches the journal name (rheum/reumat/arthrit/lupus/scleroder/
    sjögren/myositis/vasculit/spondyl/musculoskelet/osteoarthr/gout/connective tissue),
    so new rheumatology journals are caught without maintenance. `NON_RHEUM_JOURNAL`
    subtracts radiology/arthroplasty/surgery/orthopaedics — those are neighbouring
    fields, not rheumatology. `TIER3_JOURNALS` holds the ones whose names contain no
    such keyword at all (Joint Bone Spine); expect to add to it occasionally.
  - **4 · Øvrige** — everything else.
  - T4: everything else
