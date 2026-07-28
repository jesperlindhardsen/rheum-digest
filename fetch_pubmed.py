"""
fetch_pubmed.py

Deterministic PubMed fetcher. Runs all 50 queries (10 disease groups x 5 evidence
types) against NCBI E-utilities for the past 7 days (by Entrez date), parses the
XML, and returns hits as plain dicts. Title, authors, dates, journal, keywords,
and the structured abstract are extracted verbatim -- nothing here paraphrases
or edits fetched content. Preprints and errata/retractions are discarded.

disease_group is deliberately NOT set here -- that's the AI labeling step
(see ROUTINE.md Step 1). update_library.py falls back to its own regex
classifier if a hit reaches it unlabeled.

Usage: import this module and call fetch_all_hits()
"""

import os
import time
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

import requests

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

# NCBI asks for a contact email for scripts hitting their API at scale / on a
# schedule, so they can reach you if something misbehaves instead of just
# blocking your IP. Set via env var (not hardcoded) so it never ends up in a
# public repo. Fine to leave unset for occasional manual/test runs.
CONTACT_EMAIL = os.environ.get("PUBMED_CONTACT_EMAIL", "")
TOOL_NAME = "reuma-evidens-digest"

REQUEST_DELAY = 0.35  # ~3 req/sec, the no-API-key rate limit

# Order matters only for readability -- search terms, not classification.
# (Final disease_group is assigned by AI reading the fetched hit; see ROUTINE.md.)
DISEASE_QUERIES = {
    "RA": '"rheumatoid arthritis"[tiab]',
    "PsA/SpA": '("psoriatic arthritis"[tiab] OR "spondyloarthritis"[tiab] OR "ankylosing spondylitis"[tiab])',
    "SLE": '("systemic lupus erythematosus"[tiab] OR "lupus"[tiab])',
    "IIM": '("myositis"[tiab] OR "dermatomyositis"[tiab] OR "inclusion body myositis"[tiab] OR "antisynthetase syndrome"[tiab])',
    "Vasculitis": '("vasculitis"[tiab] OR "giant cell arteritis"[tiab] OR "ANCA"[tiab] OR "granulomatosis with polyangiitis"[tiab] OR "eosinophilic granulomatosis"[tiab] OR "takayasu arteritis"[tiab] OR "IgA vasculitis"[tiab] OR "henoch-schonlein"[tiab] OR "cryoglobulinemic vasculitis"[tiab])',
    "Sjögren": '("sjogren syndrome"[tiab] OR "sjögren"[tiab])',
    "SSc": '("systemic sclerosis"[tiab] OR "scleroderma"[tiab])',
    "PMR/crystal": '("polymyalgia rheumatica"[tiab] OR "gout"[tiab] OR "calcium pyrophosphate"[tiab])',
    "Autoinflammatory": '("VEXAS syndrome"[tiab] OR "autoinflammatory disease"[tiab] OR "familial mediterranean fever"[tiab] OR "adult-onset Still"[tiab] OR "TRAPS"[tiab] OR "cryopyrin-associated periodic syndrome"[tiab])',
    "General": '("rheumatic disease"[tiab] OR "inflammatory rheumatic disease"[tiab] OR "rheumatology"[ti])',
}

EVIDENCE_FILTERS = {
    "RCT": '("Randomized Controlled Trial"[pt] OR "randomized controlled trial"[tiab])',
    "Guideline/consensus": '("Guideline"[pt] OR "Practice Guideline"[pt] OR "Consensus Development Conference"[pt] OR "recommendations"[ti])',
    "Evidence synthesis": '("Systematic Review"[pt] OR "Meta-Analysis"[pt])',
    "Observational": '("Observational Study"[pt] OR "cohort study"[tiab] OR "case-control"[tiab] OR registry[tiab])',
    "Clinical case/survey": '("Case Reports"[pt] OR "case series"[tiab] OR survey[tiab])',
}

EXCLUDE_TYPES = {"Preprint", "Published Erratum", "Retraction of Publication", "Retracted Publication"}

# Keys are PubMed's ISO abbreviation (MedlineJournalInfo/ISOAbbreviation) --
# the canonical short form PubMed itself uses, so lookups need no fuzzy matching.
TIER_MAP = {
    "N Engl J Med": 1, "Lancet": 1, "JAMA": 1, "Nat Med": 1, "BMJ": 1,
    "Lancet Rheumatol": 2, "Ann Rheum Dis": 2, "Arthritis Rheumatol": 2, "Nat Rev Rheumatol": 2,
    "Rheumatology (Oxford)": 3, "Arthritis Res Ther": 3, "RMD Open": 3,
    "Semin Arthritis Rheum": 3, "J Rheumatol": 3, "Arthritis Care Res (Hoboken)": 3,
}


def _session() -> requests.Session:
    s = requests.Session()
    s.params = {"tool": TOOL_NAME}
    if CONTACT_EMAIL:
        s.params["email"] = CONTACT_EMAIL
    return s


def _esearch(session: requests.Session, term: str, mindate: str, maxdate: str) -> list:
    resp = session.get(
        f"{EUTILS}/esearch.fcgi",
        params={
            "db": "pubmed",
            "term": term,
            "retmax": 200,
            "retmode": "json",
            "datetype": "edat",
            "mindate": mindate,
            "maxdate": maxdate,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("esearchresult", {}).get("idlist", [])


def _efetch(session: requests.Session, pmids: list) -> ET.Element:
    resp = session.get(
        f"{EUTILS}/efetch.fcgi",
        params={
            "db": "pubmed",
            "id": ",".join(pmids),
            "rettype": "abstract",
            "retmode": "xml",
        },
        timeout=60,
    )
    resp.raise_for_status()
    return ET.fromstring(resp.content)


def _text(el):
    return "".join(el.itertext()).strip() if el is not None else None


def _parse_abstract(article: ET.Element) -> list:
    sections = []
    for ab in article.findall(".//Abstract/AbstractText"):
        label = ab.get("Label")
        text = _text(ab)
        if text:
            sections.append({"label": label, "text": text})
    return sections


def _parse_keywords(pubmed_article: ET.Element) -> list:
    return [t for t in (_text(k) for k in pubmed_article.findall(".//KeywordList/Keyword")) if t]


def _parse_date(article: ET.Element):
    for path in (".//ArticleDate", ".//PubDate"):
        el = article.find(path)
        if el is None:
            continue
        year = _text(el.find("Year"))
        month = _text(el.find("Month")) or "01"
        day = _text(el.find("Day")) or "01"
        if not year:
            continue
        try:
            month_num = int(month) if month.isdigit() else datetime.strptime(month[:3], "%b").month
            return datetime(int(year), month_num, int(day)).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            continue
    return None


def _parse_article(pubmed_article: ET.Element, evidence_type: str) -> dict:
    article = pubmed_article.find(".//Article")
    if article is None:
        return None

    pub_types = {_text(pt) for pt in article.findall(".//PublicationTypeList/PublicationType")}
    if pub_types & EXCLUDE_TYPES:
        return None

    pmid = _text(pubmed_article.find(".//PMID"))
    title = _text(article.find("ArticleTitle"))
    date = _parse_date(pubmed_article)
    if not (pmid and title and date):
        return None

    authors = article.findall(".//AuthorList/Author")
    last_names = [_text(a.find("LastName")) for a in authors if _text(a.find("LastName"))]
    first_author = last_names[0] if last_names else "Unknown"
    last_author = last_names[-1] if last_names else first_author

    iso_abbrev = _text(article.find(".//Journal/ISOAbbreviation")) or "Unknown"
    tier = TIER_MAP.get(iso_abbrev, 4)

    abstract_sections = _parse_abstract(article)
    if not abstract_sections:
        abstract_sections = [{"label": None, "text": ""}]

    keywords = _parse_keywords(pubmed_article)

    raw_text = title + " " + " ".join(s["text"] for s in abstract_sections)

    return {
        "pmid": pmid,
        "journal": iso_abbrev,
        "first_author": first_author,
        "last_author": last_author,
        "date": date,
        "title": title,
        "abstract_sections": abstract_sections,
        "keywords": keywords,
        "evidence_type": evidence_type,
        "tier": tier,
        "raw_text": raw_text,
    }


def fetch_all_hits(days: int = 7, evidence_types: list = None) -> list:
    """
    Runs the disease/evidence queries against PubMed for the past `days`
    days (by Entrez date) and returns deduped hit dicts (no disease_group set --
    that's assigned afterward by AI, per ROUTINE.md).

    evidence_types: restrict to a subset of EVIDENCE_FILTERS' keys (e.g. for a
    backfill covering a longer window than the weekly 7-day production run).
    Defaults to all 5 types, i.e. the normal weekly behavior.
    """
    evidence_types = evidence_types or list(EVIDENCE_FILTERS.keys())

    today = datetime.now()
    mindate = (today - timedelta(days=days)).strftime("%Y/%m/%d")
    maxdate = today.strftime("%Y/%m/%d")

    session = _session()
    seen_pmids = set()
    hits = []

    for disease, disease_term in DISEASE_QUERIES.items():
        for evidence_type in evidence_types:
            evidence_term = EVIDENCE_FILTERS[evidence_type]
            term = f"{disease_term} AND {evidence_term}"
            pmids = _esearch(session, term, mindate, maxdate)
            time.sleep(REQUEST_DELAY)

            new_pmids = [p for p in pmids if p not in seen_pmids]
            if not new_pmids:
                continue

            for i in range(0, len(new_pmids), 200):
                chunk = new_pmids[i:i + 200]
                root = _efetch(session, chunk)
                time.sleep(REQUEST_DELAY)
                for pubmed_article in root.findall(".//PubmedArticle"):
                    hit = _parse_article(pubmed_article, evidence_type)
                    if hit is None:
                        continue
                    if hit["pmid"] in seen_pmids:
                        continue
                    seen_pmids.add(hit["pmid"])
                    hits.append(hit)

    return hits


if __name__ == "__main__":
    results = fetch_all_hits()
    print(f"Fetched {len(results)} hits.")
    for h in results[:5]:
        print(f"  [{h['evidence_type']}] {h['journal']} (T{h['tier']}) — {h['title'][:80]}")
