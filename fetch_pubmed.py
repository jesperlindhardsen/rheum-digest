"""
fetch_pubmed.py

Deterministic PubMed fetcher. Runs all 55 queries (11 disease groups x 5 evidence
types) against NCBI E-utilities for the past 7 days (by Entrez date), parses the
XML, and returns hits as plain dicts. Title, authors, dates, journal, keywords,
and the structured abstract are extracted verbatim -- nothing here paraphrases
or edits fetched content. Preprints, errata/retractions, and anything not in
English or Danish are discarded.

disease_group is deliberately NOT set here -- that's the AI labeling step
(see ROUTINE.md Step 1). update_library.py falls back to its own regex
classifier if a hit reaches it unlabeled.

Usage: import this module and call fetch_all_hits()
"""

import os
import re
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
    # GCA lives with PMR, not here -- the two are one clinical spectrum.
    "Vasculitis": '("vasculitis"[tiab] OR "ANCA"[tiab] OR "granulomatosis with polyangiitis"[tiab] OR "eosinophilic granulomatosis"[tiab] OR "takayasu arteritis"[tiab] OR "IgA vasculitis"[tiab] OR "henoch-schonlein"[tiab] OR "cryoglobulinemic vasculitis"[tiab])',
    "PMR/GCA": '("polymyalgia rheumatica"[tiab] OR "giant cell arteritis"[tiab] OR "temporal arteritis"[tiab])',
    "Sjögren": '("sjogren syndrome"[tiab] OR "sjögren"[tiab])',
    "SSc": '("systemic sclerosis"[tiab] OR "scleroderma"[tiab])',
    "Crystal": '("gout"[tiab] OR "gouty arthritis"[tiab] OR "calcium pyrophosphate"[tiab] OR "pseudogout"[tiab] OR "monosodium urate"[tiab])',
    # "TRAPS"[tiab] alone also matches the plain word "traps" wherever it
    # appears -- PubMed's [tiab] is case-insensitive -- which pulled in
    # unrelated basic-science papers about "neutrophil extracellular traps"
    # (PMID 42598105, an obesity/diabetes review with no rheumatology content
    # at all). Excluding that exact phrase removes the collision without
    # narrowing genuine TRAPS-disease hits, which essentially never contain it.
    "Autoinflammatory": '("VEXAS syndrome"[tiab] OR "autoinflammatory disease"[tiab] OR "familial mediterranean fever"[tiab] OR "adult-onset Still"[tiab] OR ("TRAPS"[tiab] NOT "extracellular traps"[tiab]) OR "cryopyrin-associated periodic syndrome"[tiab])',
    "General": '("rheumatic disease"[tiab] OR "inflammatory rheumatic disease"[tiab] OR "rheumatology"[ti])',
}

EVIDENCE_FILTERS = {
    "RCT": '("Randomized Controlled Trial"[pt] OR "randomized controlled trial"[tiab])',
    "Guideline/consensus": '("Guideline"[pt] OR "Practice Guideline"[pt] OR "Consensus Development Conference"[pt] OR "recommendations"[ti])',
    "Evidence synthesis": '("Systematic Review"[pt] OR "Meta-Analysis"[pt])',
    "Observational": '("Observational Study"[pt] OR "cohort study"[tiab] OR "case-control"[tiab] OR registry[tiab])',
    "Clinical case/survey": '("Case Reports"[pt] OR "case series"[tiab] OR survey[tiab])',
}

# Which query found a hit is a poor guide to what it is: the [tiab] clauses in
# EVIDENCE_FILTERS are deliberately loose, so a systematic review that merely
# mentions "randomized controlled trial" in its abstract is returned by the RCT
# query -- and since fetch_all_hits dedups first-wins, that label stuck.
# PubMed's own PublicationType is authoritative, so prefer it, most specific
# first. Falls back to the finding query only when the record carries none of
# these (common for ahead-of-print records not yet MeSH-indexed).
EVIDENCE_PRECEDENCE = [
    ("Guideline/consensus", {"Guideline", "Practice Guideline", "Consensus Development Conference"}),
    ("Evidence synthesis", {"Systematic Review", "Meta-Analysis"}),
    ("RCT", {"Randomized Controlled Trial"}),
    ("Observational", {"Observational Study"}),
    ("Clinical case/survey", {"Case Reports"}),
]


DECISIVE_TYPES = set().union(*(tags for _, tags in EVIDENCE_PRECEDENCE))


def has_decisive_type(publication_types) -> bool:
    """Whether PubMed has assigned a type we can classify on."""
    return bool(set(publication_types) & DECISIVE_TYPES)


def fetch_publication_types(pmids: list) -> dict:
    """
    PMID -> its PublicationType list, for records already in the library.
    Used to re-check records that reached us before PubMed indexed them.
    """
    session = _session()
    out = {}
    for i in range(0, len(pmids), 200):
        root = _efetch(session, pmids[i:i + 200])
        time.sleep(REQUEST_DELAY)
        for pubmed_article in root.findall(".//PubmedArticle"):
            pmid = _text(pubmed_article.find(".//PMID"))
            if not pmid:
                continue
            out[pmid] = sorted({
                t for t in (_text(x) for x in
                            pubmed_article.findall(".//PublicationTypeList/PublicationType"))
                if t
            })
    return out


def classify_evidence_type(publication_types, fallback: str) -> str:
    types = set(publication_types)
    for name, tags in EVIDENCE_PRECEDENCE:
        if types & tags:
            return name
    return fallback


# "Comment" is secondary literature about someone else's article, not evidence
# in its own right -- e.g. PMID 42587046, an editorial on a losartan/uric-acid
# trial, which otherwise fell to the RCT query's [tiab] fallback and displayed
# as an RCT. "Editorial" alone is NOT excluded: a society's own recommendations
# are sometimes published in that format (PMID 41652650, the Qazaq College of
# Rheumatology's guidance), and that is real content, not commentary.
EXCLUDE_TYPES = {"Preprint", "Published Erratum", "Retraction of Publication",
                 "Retracted Publication", "Comment"}

# Comment letters carry PublicationType "Letter", identical to a letter with
# real content (a case report, a consensus statement) -- PubMed's structured
# fields don't distinguish them. Title wording is the only signal available
# without reading the abstract, and MEDLINE's own convention for a comment
# letter is to open with one of these phrases.
COMMENT_TITLE = re.compile(r"^(comment on|correspondence on|reply to|response to)\s*[:\s]", re.IGNORECASE)

# Only English- and Danish-language articles. Applied twice: as a PubMed search
# filter (LANGUAGE_FILTER, so we don't fetch what we'd discard) and again on the
# parsed <Language> tags (ALLOWED_LANGUAGES), which are authoritative.
ALLOWED_LANGUAGES = {"eng", "dan"}
LANGUAGE_FILTER = '(english[la] OR danish[la])'

# How far ahead of today an article date can sit before it's treated as a
# typo rather than a post-dated issue. See _parse_date.
FUTURE_DATE_TOLERANCE_DAYS = 120

# Four journal classes, in the viewer's words: top medicine / top rheumatology /
# rheumatology / everything else. Names below are PubMed's ISO abbreviation
# (MedlineJournalInfo/ISOAbbreviation), the canonical short form PubMed itself
# uses, so exact lookups need no fuzzy matching.
TIER1_JOURNALS = {
    "N Engl J Med", "Lancet", "JAMA", "BMJ", "Ann Intern Med", "Nat Med",
    "NEJM Evid", "JAMA Intern Med", "JAMA Netw Open", "EClinicalMedicine",
}
TIER2_JOURNALS = {
    "Ann Rheum Dis", "Arthritis Rheumatol", "Lancet Rheumatol", "Nat Rev Rheumatol",
    "Rheumatology (Oxford)", "RMD Open", "Semin Arthritis Rheum", "J Rheumatol",
    "Arthritis Care Res (Hoboken)", "Arthritis Res Ther",
}

# The Lancet's regional titles only ever appear with a suffix -- Lancet Reg
# Health Eur, ... Am, ... West Pac -- so an exact key would never match.
TIER1_PREFIXES = ("Lancet Reg Health",)

# Tier 3 is open-ended by design: new rheumatology journals appear constantly,
# and an explicit list would always lag behind them. Matching the name keeps it
# self-maintaining. \brheum (not rheumat) so "Int J Rheum Dis" is caught, and
# no bare "arthro" -- that pulled in arthroplasty, which is orthopaedics.
RHEUM_JOURNAL = re.compile(
    r"\brheum|reumat|arthrit|lupus|scleroder|sj[oö]gren|myositis|vasculit|"
    r"spondyl|musculoskelet|osteoarthr|\bgout\b|connective tissue", re.IGNORECASE)
NON_RHEUM_JOURNAL = re.compile(r"radiol|arthroplast|surg|orthop", re.IGNORECASE)

# Rheumatology journals whose names carry none of the keywords above.
TIER3_JOURNALS = {"Joint Bone Spine"}


def journal_tier(iso_abbrev: str) -> int:
    """1 top medicine, 2 top rheumatology, 3 rheumatology, 4 everything else."""
    if iso_abbrev in TIER1_JOURNALS or iso_abbrev.startswith(TIER1_PREFIXES):
        return 1
    if iso_abbrev in TIER2_JOURNALS:
        return 2
    if iso_abbrev in TIER3_JOURNALS:
        return 3
    if RHEUM_JOURNAL.search(iso_abbrev) and not NON_RHEUM_JOURNAL.search(iso_abbrev):
        return 3
    return 4


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


def _date_parts(el) -> datetime:
    """Build a datetime from an element holding Year/Month/Day children."""
    if el is None:
        return None
    year = _text(el.find("Year"))
    month = _text(el.find("Month")) or "01"
    day = _text(el.find("Day")) or "01"
    if not year:
        return None
    try:
        month_num = int(month) if month.isdigit() else datetime.strptime(month[:3], "%b").month
        return datetime(int(year), month_num, int(day))
    except (ValueError, TypeError):
        return None


def _parse_date(pubmed_article: ET.Element):
    """
    Publication date, preferring the article's own date. Journals legitimately
    post-date issues by a month or two (a September issue out in August), but
    anything past FUTURE_DATE_TOLERANCE_DAYS is a publisher metadata typo --
    e.g. PMID 42557168, stamped 2028 where every other field says 2026. In that
    case fall back to PubMed's own indexing date, which is also what the Entrez
    date window in _esearch already keys on.
    """
    cutoff = datetime.now() + timedelta(days=FUTURE_DATE_TOLERANCE_DAYS)
    candidates = (
        ".//ArticleDate",
        ".//PubDate",
        ".//History/PubMedPubDate[@PubStatus='pubmed']",
        ".//History/PubMedPubDate[@PubStatus='entrez']",
    )

    implausible = None
    for path in candidates:
        parsed = _date_parts(pubmed_article.find(path))
        if parsed is None:
            continue
        if parsed <= cutoff:
            return parsed.strftime("%Y-%m-%d")
        if implausible is None:
            implausible = parsed

    # Every candidate was far-future: keep the first rather than drop the hit.
    return implausible.strftime("%Y-%m-%d") if implausible else None


def _parse_article(pubmed_article: ET.Element, evidence_type: str) -> dict:
    article = pubmed_article.find(".//Article")
    if article is None:
        return None

    pub_types = {_text(pt) for pt in article.findall(".//PublicationTypeList/PublicationType")}
    if pub_types & EXCLUDE_TYPES:
        return None

    title = _text(article.find("ArticleTitle"))
    if title and COMMENT_TITLE.search(title):
        return None

    # An article can carry several <Language> tags; keep it if any is allowed.
    languages = [t.lower() for t in (_text(l) for l in article.findall(".//Language")) if t]
    if not set(languages) & ALLOWED_LANGUAGES:
        return None

    # The finding query is only a fallback; PubMed's own type wins.
    evidence_type = classify_evidence_type(pub_types, evidence_type)

    pmid = _text(pubmed_article.find(".//PMID"))
    date = _parse_date(pubmed_article)
    if not (pmid and title and date):
        return None

    authors = article.findall(".//AuthorList/Author")
    last_names = [_text(a.find("LastName")) for a in authors if _text(a.find("LastName"))]
    first_author = last_names[0] if last_names else "Unknown"
    last_author = last_names[-1] if last_names else first_author

    iso_abbrev = _text(article.find(".//Journal/ISOAbbreviation")) or "Unknown"
    tier = journal_tier(iso_abbrev)

    abstract_sections = _parse_abstract(article)
    if not abstract_sections:
        abstract_sections = [{"label": None, "text": ""}]

    keywords = _parse_keywords(pubmed_article)

    raw_text = title + " " + " ".join(s["text"] for s in abstract_sections)

    return {
        "pmid": pmid,
        "languages": languages,
        "publication_types": sorted(pub_types),
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


def most_specific_type(evidence_types) -> str:
    """The most specific of several matching evidence types (EVIDENCE_PRECEDENCE)."""
    for name, _ in EVIDENCE_PRECEDENCE:
        if name in evidence_types:
            return name
    return sorted(evidence_types)[0]


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

    # Phase 1 -- search only, recording every evidence query that returned each
    # PMID. Articles routinely match several: the RCT filter's [tiab] clause
    # catches reviews that discuss trials, "survey" catches all sorts. Keeping
    # the whole set means the fallback below can pick the most specific one
    # rather than whichever query the loop happened to reach first.
    matched_types = {}
    for disease_term in DISEASE_QUERIES.values():
        for evidence_type in evidence_types:
            term = f"{disease_term} AND {EVIDENCE_FILTERS[evidence_type]} AND {LANGUAGE_FILTER}"
            for pmid in _esearch(session, term, mindate, maxdate):
                matched_types.setdefault(pmid, set()).add(evidence_type)
            time.sleep(REQUEST_DELAY)

    # Phase 2 -- fetch each PMID once. _parse_article prefers PubMed's own
    # PublicationType; the query-derived type is only its fallback.
    hits = []
    pmids = list(matched_types)
    for i in range(0, len(pmids), 200):
        root = _efetch(session, pmids[i:i + 200])
        time.sleep(REQUEST_DELAY)
        for pubmed_article in root.findall(".//PubmedArticle"):
            pmid = _text(pubmed_article.find(".//PMID"))
            fallback = most_specific_type(matched_types.get(pmid) or evidence_types)
            hit = _parse_article(pubmed_article, fallback)
            if hit is not None:
                hits.append(hit)

    return hits


if __name__ == "__main__":
    results = fetch_all_hits()
    print(f"Fetched {len(results)} hits.")
    for h in results[:5]:
        print(f"  [{h['evidence_type']}] {h['journal']} (T{h['tier']}) — {h['title'][:80]}")
