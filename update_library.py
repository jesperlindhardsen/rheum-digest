"""
update_library.py

Takes raw PubMed search hits (list of dicts, one per article) and:
- classifies into disease groups (most specific match wins; a second group only
  for a genuine overlap, e.g. large-vessel vasculitis under both Vasculitis
  and PMR/GCA)
- dedups against existing data/library.json by PMID
- prepends new records (newest first)
- drops records >24 months old
- recomputes time-bucket for ALL records (buckets shift as weeks pass)
- writes data/library.json as {"generated_at": "YYYY-MM-DD", "records": [...]}

Usage: import this module and call update_library(new_hits, library_path)
new_hits: list of dicts with keys:
    pmid, journal, first_author, last_author, date (YYYY-MM-DD),
    title, abstract_sections (list of {label, text}), keywords (list of str),
    evidence_type, tier (int 1-4), raw_text (title + abstract, used only for classification)
"""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

# Order matters: more specific groups checked before General
DISEASE_PATTERNS = [
    ("RA", r"rheumatoid arthritis"),
    ("PsA/SpA", r"psoriatic arthritis|spondyloarthritis|ankylosing spondylitis"),
    ("SLE", r"systemic lupus erythematosus|\blupus\b"),
    ("IIM", r"myositis|dermatomyositis|inclusion body myositis|antisynthetase"),
    # PMR/GCA is checked before Vasculitis: giant cell arteritis matches both,
    # and it belongs with polymyalgia rheumatica.
    ("PMR/GCA", r"polymyalgia rheumatica|giant cell arteritis|temporal arteritis|"
                r"\bGCA\b|\bPMR\b"),
    ("Vasculitis", r"vasculitis|\bANCA\b|\bGPA\b|\bEGPA\b|"
                   r"eosinophilic granulomatosis|takayasu|IgA vasculitis|"
                   r"henoch-schonlein|cryoglobulin|small vessel vasculitis"),
    ("Sjögren", r"sj[oö]gren"),
    ("SSc", r"systemic sclerosis|scleroderma"),
    ("Crystal", r"\bgout\b|\bgouty\b|calcium pyrophosphate|\bCPPD\b|pseudogout|"
                r"monosodium urate|crystal arthropathy|crystal arthritis"),
    # Bare acronyms need disambiguation: \bTRAPS\b alone also matches the plain
    # word "traps" (e.g. "extracellular traps" in NET biology, PMID 42598105),
    # and \bCAPS\b is ambiguous even within rheumatology -- Cryopyrin-Associated
    # Periodic Syndrome vs. Catastrophic Antiphospholipid Syndrome (both real,
    # completely different diseases; PMID 42559394 used the latter). This is
    # the fallback classifier only -- AI's disease_groups normally wins -- but
    # the same collision applies here, so require the disambiguating context.
    # The TRAPS branch is anchored with ^: an unanchored (?!.*extracellular
    # traps) lets re.search retry from later positions in the text and defeats
    # the exclusion the moment "traps" appears again anywhere after the
    # disqualifying phrase (common -- the phrase is usually only spelled out
    # once, then just "traps"/"NETs" afterward). Anchoring forces the whole
    # check to run once, against the full text, from position zero.
    ("Autoinflammatory", r"VEXAS|autoinflammatory disease|familial mediterranean fever|"
                          r"adult-onset still|cryopyrin-associated periodic syndrome|"
                          r"^(?=.*\bTRAPS\b)(?!.*extracellular traps)|"
                          r"cryopyrin.{0,30}\bCAPS\b|\bCAPS\b.{0,30}cryopyrin"),
    ("General", r"rheumatic disease|inflammatory rheumatic|"
                r"rheumatology.*(vaccin|pregnan|comorbid)"),
]

TIME_WINDOW_DAYS = 24 * 30  # ~24 months retention

# A record may belong to more than one group, but only where it genuinely
# covers both. The one case the fallback classifier can recognise on its own:
# large-vessel vasculitis papers treat GCA and Takayasu together, so they
# belong under Vasculitis and PMR/GCA alike.
#
# Matched against the TITLE only, deliberately. Against the abstract it also
# fires on Takayasu-only papers and on incidental mentions in unrelated case
# reports; in the title it means the paper is actually about LVV as a whole.
LVV_OVERLAP = re.compile(r"large[- ]vessel vasculitis", re.IGNORECASE)


def classify_disease_groups(raw_text: str, title: str = None) -> list:
    """
    Return every disease group the text belongs to, most specific first, or an
    empty list if none match. Normally one group; two only for a genuine
    overlap (see LVV_OVERLAP).
    """
    text = raw_text.lower()
    groups = []
    for group, pattern in DISEASE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            groups.append(group)
            break

    if LVV_OVERLAP.search(title if title is not None else raw_text):
        for group in ("Vasculitis", "PMR/GCA"):
            if group not in groups:
                groups.append(group)

    return groups


def compute_bucket(pub_date: str, today: datetime) -> str:
    """Return 'recent' (<=1mo), 'mid' (1-6mo), or 'older' (6-24mo)."""
    d = datetime.strptime(pub_date, "%Y-%m-%d")
    age_days = (today - d).days
    if age_days <= 30:
        return "recent"
    elif age_days <= 182:
        return "mid"
    else:
        return "older"


def load_library(path: Path) -> list:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # The file was a bare list of records before generated_at was added;
    # accept either shape so an old library.json still loads.
    if isinstance(data, list):
        return data
    return data.get("records", [])


def update_library(new_hits: list, library_path: str, today: datetime = None) -> dict:
    """
    Returns: {"added": int, "total": int, "by_group": {group: count}}
    """
    if today is None:
        today = datetime.now()

    path = Path(library_path)
    library = load_library(path)
    existing_pmids = {rec["pmid"] for rec in library}

    added = 0
    for hit in new_hits:
        if hit["pmid"] in existing_pmids:
            continue  # dedup

        # Prefer AI-assigned groups (labels, not a content change) if given;
        # fall back to regex only when the hit wasn't pre-classified. Accepts
        # either a disease_groups list or a single legacy disease_group.
        groups = hit.get("disease_groups")
        if not groups:
            single = hit.get("disease_group")
            groups = [single] if single else classify_disease_groups(
                hit.get("raw_text", hit["title"]), hit["title"]
            )
        if not groups:
            continue  # doesn't match any group, skip

        record = {
            "pmid": hit["pmid"],
            "languages": hit.get("languages", []),
            "publication_types": hit.get("publication_types", []),
            "disease_groups": groups,
            "evidence_type": hit["evidence_type"],
            "tier": hit["tier"],
            "journal": hit["journal"],
            "first_author": hit["first_author"],
            "last_author": hit["last_author"],
            "date": hit["date"],
            "title": hit["title"],
            "abstract_sections": hit["abstract_sections"],
            "keywords": hit.get("keywords", []),
        }
        # Set only when AI's weekly read flags something a deterministic
        # filter didn't catch (see ROUTINE.md) -- a short reason, shown on
        # the card and countable in the header banner. Omitted entirely
        # otherwise, not set to None, so it stays falsy in the viewer's JS.
        if hit.get("flagged"):
            record["flagged"] = hit["flagged"]
        library.insert(0, record)  # prepend, newest first
        existing_pmids.add(hit["pmid"])
        added += 1

    # Drop records older than retention window
    cutoff = today - timedelta(days=TIME_WINDOW_DAYS)
    library = [
        rec for rec in library
        if datetime.strptime(rec["date"], "%Y-%m-%d") >= cutoff
    ]

    # Recompute time bucket for every record (ages shift each run)
    for rec in library:
        rec["time_bucket"] = compute_bucket(rec["date"], today)

    # Sort: newest first overall (view logic re-groups by disease/type/bucket)
    library.sort(key=lambda r: r["date"], reverse=True)

    # generated_at is this run's date -- what the viewer shows as
    # "Sidst opdateret". Deliberately not the newest record's publication
    # date: journals post-date issues, so that would sit in the future.
    payload = {
        "generated_at": today.strftime("%Y-%m-%d"),
        "records": library,
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # A cross-listed record counts once per group it appears under, so these
    # sum to more than `total` -- that's the tab counts, not a record count.
    by_group = {}
    for rec in library:
        for group in rec["disease_groups"]:
            by_group[group] = by_group.get(group, 0) + 1

    return {"added": added, "total": len(library), "by_group": by_group}


def refresh_pending_types(library_path: str) -> dict:
    """
    Re-check PubMed for records that still carry no decisive PublicationType.

    Ahead-of-print records reach us before PubMed has indexed them, so their
    evidence_type is whatever the finding query guessed. PubMed assigns the real
    type weeks later, but nothing would revisit it -- update_library only ever
    touches new PMIDs. Run this each week to let those records settle.

    Returns: {"pending": int, "resolved": int, "retyped": int, "changes": {...}}
    """
    from fetch_pubmed import (fetch_publication_types, has_decisive_type,
                              classify_evidence_type)

    path = Path(library_path)
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    records = payload if isinstance(payload, list) else payload.get("records", [])

    pending = [r for r in records
               if not has_decisive_type(r.get("publication_types", []))]
    if not pending:
        return {"pending": 0, "resolved": 0, "retyped": 0, "changes": {}}

    fetched = fetch_publication_types([r["pmid"] for r in pending])

    resolved = 0
    changes = {}
    for rec in pending:
        types = fetched.get(rec["pmid"])
        if not types or not has_decisive_type(types):
            continue  # PubMed still hasn't indexed it; try again next week
        rec["publication_types"] = types
        resolved += 1
        new_type = classify_evidence_type(types, rec["evidence_type"])
        if new_type != rec["evidence_type"]:
            key = f"{rec['evidence_type']} -> {new_type}"
            changes[key] = changes.get(key, 0) + 1
            rec["evidence_type"] = new_type

    if isinstance(payload, list):
        payload = {"generated_at": datetime.now().strftime("%Y-%m-%d"), "records": records}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    return {"pending": len(pending), "resolved": resolved,
            "retyped": sum(changes.values()), "changes": changes}


if __name__ == "__main__":
    print("update_library.py loaded. Import update_library() and call it with your hits.")
