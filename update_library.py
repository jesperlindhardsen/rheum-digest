"""
update_library.py

Takes raw PubMed search hits (list of dicts, one per article) and:
- classifies into the right disease group (most specific match wins)
- dedups against existing data/library.json by PMID
- prepends new records (newest first)
- drops records >24 months old
- recomputes time-bucket for ALL records (buckets shift as weeks pass)
- writes data/library.json

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
    ("Vasculitis", r"vasculitis|giant cell arteritis|\bANCA\b|\bGPA\b|\bEGPA\b|"
                   r"eosinophilic granulomatosis|takayasu|IgA vasculitis|"
                   r"henoch-schonlein|cryoglobulin|small vessel vasculitis"),
    ("Sjögren", r"sj[oö]gren"),
    ("SSc", r"systemic sclerosis|scleroderma"),
    ("PMR/crystal", r"polymyalgia rheumatica|\bgout\b|calcium pyrophosphate"),
    ("Autoinflammatory", r"VEXAS|autoinflammatory disease|familial mediterranean fever|"
                          r"adult-onset still|\bTRAPS\b|cryopyrin-associated periodic syndrome|"
                          r"\bCAPS\b"),
    ("General", r"rheumatic disease|inflammatory rheumatic|"
                r"rheumatology.*(vaccin|pregnan|comorbid)"),
]

TIME_WINDOW_DAYS = 24 * 30  # ~24 months retention


def classify_disease_group(raw_text: str):
    """Return the most specific matching disease group, or None if no match."""
    text = raw_text.lower()
    for group, pattern in DISEASE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return group
    return None


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
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


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

        # Prefer an AI-assigned group (a label, not a content change) if given;
        # fall back to regex only when the hit wasn't pre-classified.
        group = hit.get("disease_group") or classify_disease_group(hit.get("raw_text", hit["title"]))
        if group is None:
            continue  # doesn't match any group, skip

        record = {
            "pmid": hit["pmid"],
            "disease_group": group,
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

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(library, f, ensure_ascii=False, indent=2)

    by_group = {}
    for rec in library:
        by_group[rec["disease_group"]] = by_group.get(rec["disease_group"], 0) + 1

    return {"added": added, "total": len(library), "by_group": by_group}


if __name__ == "__main__":
    print("update_library.py loaded. Import update_library() and call it with your hits.")
