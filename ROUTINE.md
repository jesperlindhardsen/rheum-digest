# Reuma-evidens — Weekly Digest Routine

## Role
Weekly rheumatology evidence digest. Runs autonomously, no prompts needed once set up.

## Architecture
- **fetch_pubmed.py** (deterministic script): runs all 55 PubMed queries, parses XML —
  title/authors/dates/abstracts extracted verbatim, no AI involved
- **AI**: reads each fetched hit and assigns `disease_groups` — labels only, never touches
  the fetched content. While it's already reading the hit, it also sanity-checks
  `evidence_type` and flags suspected off-topic hits (see Step 1) — same labels-only
  discipline, just a second field
- **update_library.py** (deterministic script): dedups, buckets, writes `docs/data/library.json`
  (falls back to its own regex classifier only if AI didn't label a hit)
- **docs/index.html** (static page): reads `docs/data/library.json` client-side via JS — no regeneration needed per run
- **digests/YYYY-MM-DD.md** (optional weekly snapshot): audit trail of that week's new items only

This split matters: AI's only touch on the data is assigning a category label — it never
generates, paraphrases, or edits a title, author, date, or abstract. Everything else is
pure code.

## STEP 0 — APPLY PENDING CORRECTIONS (GitHub issues, before fetching anything new)
`docs/index.html` has a "Foreslå rettelse" form on every card (disease group(s),
evidence type, or "ekskludér helt"; a flagged card also gets "afvis flag"). The site is
static and can't write to `library.json` itself, so the form instead opens a
pre-filled **GitHub issue**, labelled `rettelse`, that the user creates deliberately.
That issue is the only way a correction exists until the routine picks it up here.

Each run, before Step 1:
- Try `gh issue list --repo jesperlindhardsen/rheum-digest --label rettelse --state open --json number,title,body`.
  If `gh` isn't available/authenticated for the Issues API in this environment, say so
  in the final report and skip this step entirely — don't fail the run over it.
- For each open issue, the body is a short key/value list, e.g.:
  ```
  PMID: 42601766
  Nuværende evidenstype: Clinical case/survey
  Foreslået evidenstype: RCT
  ```
  or `Ekskludér artiklen: Ja`, or `Afvis eksisterende flag: Ja`. Find the record by
  PMID in `docs/data/library.json` and apply exactly what's asked:
  - **Ekskludér**: remove the record.
  - **Sygdomsgrupper/evidenstype**: overwrite `disease_groups`/`evidence_type` with
    the proposed value(s).
  - **Afvis flag**: delete the `flagged` field, change nothing else.
  - If the PMID isn't found (already removed, typo'd), don't guess — leave the issue
    open and note it in the report instead of closing it silently.
- Close each applied issue with `gh issue close --repo jesperlindhardsen/rheum-digest
  <number> --comment "<what was changed>"`.
- If any records were touched, that's part of the same commit this run makes in Step
  2/6 (don't create a separate commit) — but do it *before* the new fetch, so this
  week's dedup and bucket recompute already reflect the correction.
- **This is batch, not instant.** A correction submitted any day of the week sits as
  an open issue until the *next* Monday run. There's no other automation that checks
  it sooner — say so if the user asks why nothing happened yet.

## STEP 1 — SEARCH (PubMed)
- Date window: past 7 days (Entrez EDAT)
- `fetch_pubmed.py` runs all 55 queries (11 disease groups × 5 evidence types) via PubMed
  E-utilities and parses results in code — title, journal, authors, PMID, date, and
  **structured abstract** (PubMed's labeled `AbstractText` sections — Background/Methods/
  Results/Conclusion — kept as separate `{label, text}` pairs) are extracted verbatim,
  untouched by AI. Preprints, errata, and comments/correspondence are discarded
automatically (see below).
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

**Bare acronyms are ambiguous and need disambiguation, in the PubMed query and in
`DISEASE_PATTERNS` alike.** `"TRAPS"[tiab]` alone also matches the plain word
"traps" wherever it appears — PubMed's `[tiab]` is case-insensitive — which pulled
in basic-science papers about "neutrophil extracellular traps" (NETs) with no
rheumatology content at all (PMID 42598105, an obesity/diabetes review). The
query now excludes the exact phrase `"extracellular traps"[tiab]`; the fallback
regex requires TRAPS to appear *without* that phrase anywhere in the text (an
anchored `^(?=.*\bTRAPS\b)(?!.*extracellular traps)` — the anchor matters, since
an unanchored negative lookahead lets `re.search` retry from a later position and
the exclusion evaporates the moment the disqualifying phrase is only spelled out
once and referred to as "traps" or "NETs" afterward, which is normal writing
style). `CAPS` has the opposite problem — it's ambiguous even *within*
rheumatology, meaning either Cryopyrin-Associated Periodic Syndrome or
Catastrophic Antiphospholipid Syndrome (PMID 42559394, an SLE case correctly
classified as SLE despite using "CAPS" for the latter). The fallback regex
requires "cryopyrin" nearby before accepting a bare CAPS match.

**Comments and correspondence are excluded, not classified.** A `Comment` on
someone else's article, or a `Letter` that opens "Comment on:" / "Correspondence
on:" / "Reply to:" / "Response to:", is secondary literature about another paper —
it was never evidence in its own right, however the finding query happened to tag
it. `EXCLUDE_TYPES` in `fetch_pubmed.py` drops any hit whose `PublicationType`
includes `Comment`; `COMMENT_TITLE` drops the title-pattern cases PubMed's
structured fields don't otherwise flag. `Editorial` alone is *not* excluded — a
society sometimes publishes its own recommendations in that format (e.g. PMID
41652650, the Qazaq College of Rheumatology's guidance), which is real content.
Known gap: a critique with neither a recognisable title nor a `Comment` tag (e.g.
PMID 42170841, "'More guidelines than rules': reconsidering key gaps...") slips
through and needs manual removal — there's no PubMed field to catch it by.

Query strings and evidence-type filters live in `fetch_pubmed.py`
(`DISEASE_QUERIES`, `EVIDENCE_FILTERS`) — edit there, not here, to keep one source of truth.

Evidence types (5), by stored key: RCT, Guideline/consensus, Evidence synthesis,
Observational, Clinical case/survey. The viewer relabels these for display
(`EVIDENCE_LABEL` for the tabs, `EVIDENCE_BADGE` for the shorter on-card chip, both
in `docs/index.html`) — notably Evidence synthesis reads "Meta/reviews". The keys
are what lives in the data; don't rename them there.

**An article is normally found by several queries.** `fetch_all_hits` searches
first and fetches second: phase 1 records *every* evidence query that returned each
PMID, phase 2 fetches each PMID once. So a hit is stored once, and when the queries
disagree the most specific match wins (`most_specific_type`) rather than whichever
query the loop reached first. Disease queries overlapping doesn't matter — the
disease groups are assigned afterwards by reading the article, not by which query
found it.

**Which query found a hit does not decide its type.** The `[tiab]` clauses in
`EVIDENCE_FILTERS` are deliberately loose — a systematic review that merely mentions
"randomized controlled trial" in its abstract is returned by the RCT query — and
`fetch_all_hits` dedups first-wins, so that label would stick. `classify_evidence_type()`
therefore derives the type from PubMed's own `PublicationType`, most specific first
(`EVIDENCE_PRECEDENCE`: Guideline → Evidence synthesis (Systematic Review/Meta-Analysis)
→ RCT → Observational → Case → Evidence synthesis again, this time for a bare `Review`).
The finding query is only the fallback, used when a record carries none of those tags —
common for ahead-of-print records not yet MeSH-indexed.

**A bare `Review` (narrative, not systematic) sits last in `EVIDENCE_PRECEDENCE`, on
purpose.** PubMed commonly tags a "case report and review of the literature" with both
`Case Reports` and `Review` — that's a case report, so the more specific tag has to win;
`Review` only decides when nothing more specific applies. Skipping `Review` entirely
isn't safe either: it was the gap that let PMID 42598105 (an obesity/diabetes review
PubMed itself calls a plain `Review`, no other type) fall back to the finding query,
which had matched it via the RCT filter's `[tiab]` clause — its abstract's one mention
of "randomized controlled trial" was actually *"no completed RCT... has yet validated
it"*, the opposite of being one. A phrase search can't tell a claim from its negation.

**A trial protocol/feasibility/pilot paper describes a trial that hasn't produced
results yet — a plan to generate evidence, not evidence itself — so it's excluded,
not classified.** `PROTOCOL_TITLE` in `fetch_pubmed.py` matches titles containing
"feasibility study/trial", "study protocol", "protocol for", "pilot study/trial", or
"rationale and design". PubMed rarely tags these with any decisive `PublicationType`,
so nothing previously stopped the RCT query's `[tiab]` clause — which fires on the
mere phrase, future tense included — from mislabelling them as completed RCTs, e.g.
PMID 42592900, a gout self-management app whose abstract says it tests "the overall
feasibility of a **future** randomized controlled trial." When the actual trial is
later published with results, that record arrives on its own merits; this only
excludes the preparatory paper.

**A title asserting what the article is outranks the finding-query fallback.**
`TITLE_ASSERTS_SYNTHESIS` catches titles ending "...: a systematic review" / "...: a
meta-analysis" — e.g. PMID 42228337, "Split vs. single-dose oral methotrexate in
rheumatoid arthritis: a meta-analysis", whose abstract's RCT-flavoured wording had
outvoted its own title. Only applies when `PublicationType` carries no decisive tag;
PubMed's own type still wins whenever both exist and disagree.

Each record stores its `publication_types` so this stays auditable without re-fetching.

**AI review of new hits, each run — the second line of defence.** The deterministic
filters above catch every *known* pattern, but each one was found by hand, after the
fact, from a record that had already reached the library. New failure modes will keep
surfacing. So while AI is already reading each new hit's title and abstract to assign
`disease_groups`, it also:
- **Sanity-checks `evidence_type`** whenever the hit has no decisive `PublicationType`
  (i.e. it rests on the finding-query fallback) — does the abstract actually support
  being an RCT, a synthesis, etc., or does it read like PMID 42592900 or 42228035
  (a claim about trial evidence, not the trial itself)? If AI disagrees, it overrides
  `evidence_type` directly — a label correction, same discipline as `disease_groups`:
  never touching title/authors/abstract. If `PublicationType` *is* decisive, don't
  second-guess it — PubMed's own tag stays authoritative.
- **Flags suspected off-topic or non-evidence hits** that slipped past every
  deterministic filter (an acronym collision nobody's written a regex for yet, a
  critique with no recognisable title pattern like PMID 42170841) — it does not
  drop them itself.

**Where a flag actually surfaces — not just a run log the user can't watch live.**
The routine runs unattended at 06:00 Monday; nobody is there to read a session
transcript or act on it mid-run. A flag is therefore never *only* text in the run's
final report — it's written onto the record itself, as `hit["flagged"] = "<short
reason>"`, which `update_library.py` carries into `docs/data/library.json` (omitted
entirely when there's nothing to flag, not set to `None`). Two places pick it up
from there, both places the user already checks on their own schedule:
- **`docs/index.html`** renders a `⚑ <reason>` line directly on the card, and a
  clickable "⚑ N til gennemsyn" badge in the header (rendered only when `N > 0`)
  that filters the whole page down to just the flagged records.
- **`digests/YYYY-MM-DD.md`** should list this week's flagged items in a short
  "## Til gennemsyn" section (PMID + reason) if any exist, alongside the normal
  per-group listing, so they surface in the file the user already reads weekly.

Flagged records are **not excluded** — they're included under AI's best-guess
classification, same as any other hit, just visibly marked. The flag is a pointer
for the user to look, not a verdict.

**How a flag actually gets resolved: the user tells whoever is operating the
digest (normally via a Claude Code chat) the PMID and the decision.** There is
no button on the site and no automatic resolution — a `flagged` field, once set,
sits on that record forever; `update_library.py` only ever touches new PMIDs, so
nothing revisits it. The resolution is always a manual edit to
`docs/data/library.json`, one of:
- **Exclude it** — remove the record entirely (same as any other bad hit in this
  project's history, e.g. PMID 42598105).
- **Reclassify it** — fix `disease_groups`/`evidence_type` and clear `flagged`.
- **Dismiss it** — it was a false alarm; just clear `flagged`, record unchanged.

If the same shape of problem recurs across several flags, that's the signal to
write a permanent deterministic rule (a new `PROTOCOL_TITLE`-style pattern, a
`TIER`/`EXCLUDE_TYPES` addition, etc.) rather than resolving each one by hand
forever — every fix in this file's history started as exactly that kind of
one-off before becoming a rule.

This only ever runs against **this week's new hits** (dozens, not the full library) —
the existing 797 records were corrected by hand as each fix above landed, and won't be
silently re-touched by a routine run.

**Records settle over time.** `update_library.refresh_pending_types()` re-checks
PubMed each run for records still carrying no decisive type, and upgrades them once
they're indexed. Without it nothing would ever revisit them — `update_library` only
touches new PMIDs. Run it after `update_library()` each week.

Known gap: `EVIDENCE_FILTERS["Evidence synthesis"]` matches on `[pt]` tags only, so
an ahead-of-print systematic review cannot be found by that query at all, and can't
fall back to it either. Such records sit under whichever other query caught them
(often Observational or RCT via their `[tiab]` clauses) until PubMed indexes them and
`refresh_pending_types` corrects them.

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

If any of this week's hits carry a `flagged` reason (see Step 1), open with a
"## Til gennemsyn" section listing them — `**{title}** — {flagged reason}, PMID
{pmid}` — before the normal per-group listing. Omit the section entirely when
nothing was flagged.

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
