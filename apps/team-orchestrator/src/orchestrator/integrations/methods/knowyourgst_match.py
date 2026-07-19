"""[FAZAL-PROVIDED 2026-06-30 — VT-495 source of truth for the name-matching layer.
Build the KnowYourGSTScraper.search() (ScrapingBee scrape+parse of knowyourgst.com)
to satisfy the GSTSearcher Protocol below; keep this matching layer's semantics exactly.]

Company-name normalization and similarity matching for KnowYourGST.
Examples:
    "RKECOM Services Pvt Ltd"
        -> cleaned query: "RKECOM"
    "RKECOM SERVICES OPC PRIVATE LIMITED"
        -> normalized comparison key: "rkecom"
This code assumes `scraper.search(query)` is the search method from the
previous KnowYourGSTScraper implementation and returns:
[
    {
        "company_name": "...",
        "state": "...",
        "gst_number": "..."
    }
]
"""
from __future__ import annotations
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Protocol
# Legal entity terms generally do not identify the actual business.
LEGAL_ENTITY_STOPWORDS = frozenset(
    {
        "pvt",
        "private",
        "ltd",
        "limited",
        "opc",
        "llp",
        "llc",
        "inc",
        "incorporated",
        "corporation",
        "corp",
        "company",
        "co",
        "plc",
    }
)
# Generic business words may differ between the typed name and the registered
# name. Keep this list deliberately conservative to avoid overly broad queries.
GENERIC_BUSINESS_STOPWORDS = frozenset(
    {
        "service",
        "services",
        "and",
        "the",
    }
)
COMPANY_STOPWORDS = (
    LEGAL_ENTITY_STOPWORDS | GENERIC_BUSINESS_STOPWORDS
)
# KnowYourGST's form declares a minimum query length of five characters.
MIN_SEARCH_LENGTH = 5
MAX_SEARCH_LENGTH = 50
# Results below this score are treated as unrelated.
DEFAULT_MIN_SIMILARITY = 0.72
class GSTSearcher(Protocol):
    """Interface expected from the KnowYourGST scraper."""
    def search(self, query: str) -> list[dict[str, str]]:
        ...
def tokenize_company_name(company_name: str) -> list[str]:
    """
    Convert a company name into normalized word tokens.
    Normalization includes:
    - Unicode normalization.
    - Case normalization.
    - Punctuation removal.
    - Whitespace normalization.
    - Removal of legal and generic company words.
    Stopwords are removed only as complete words. For example, removing
    "service" does not alter a brand such as "ServiceNow".
    """
    normalized = unicodedata.normalize("NFKC", company_name).casefold()
    # Remove the expanded form of OPC before individual token processing.
    normalized = re.sub(
        r"\bone[\s-]+person[\s-]+company\b",
        " ",
        normalized,
    )
    # Convert "&" into whitespace. "and" is already a stopword.
    normalized = normalized.replace("&", " ")
    # Treat punctuation, hyphens, periods and underscores as word separators.
    normalized = re.sub(r"[^\w\s]", " ", normalized)
    normalized = normalized.replace("_", " ")
    raw_tokens = normalized.split()
    return [
        token
        for token in raw_tokens
        if token not in COMPANY_STOPWORDS
    ]
def normalized_company_key(company_name: str) -> str:
    """
    Build the normalized value used for local similarity comparisons.
    Examples:
        RKECOM Services Pvt Ltd -> rkecom
        RKECOM SERVICES OPC PRIVATE LIMITED -> rkecom
    """
    return " ".join(tokenize_company_name(company_name))
def build_search_queries(company_name: str) -> list[str]:
    """
    Create progressively broader KnowYourGST search queries.
    Query order:
    1. All distinctive words after removing stopwords.
    2. Individual distinctive words, longest first.
    The individual-word fallback handles punctuation or wording differences.
    For example, the cleaned phrase "digital prodigy india" may not match
    "DIGITAL-PRODIGY INDIA", but "prodigy" will.
    Duplicate queries are removed while preserving their order.
    """
    tokens = tokenize_company_name(company_name)
    if not tokens:
        raise ValueError(
            "The company name contains no distinctive words after "
            "normalization."
        )
    queries: list[str] = []
    # Build the most specific query that fits the site's 50-character limit.
    phrase_tokens: list[str] = []
    phrase_length = 0
    for token in tokens:
        added_length = len(token) + (1 if phrase_tokens else 0)
        if phrase_length + added_length > MAX_SEARCH_LENGTH:
            break
        phrase_tokens.append(token)
        phrase_length += added_length
    cleaned_phrase = " ".join(phrase_tokens)
    if len(cleaned_phrase) >= MIN_SEARCH_LENGTH:
        queries.append(cleaned_phrase)
    # If the complete cleaned phrase fails, try distinctive tokens one by one.
    # Longer words are generally more selective than short words.
    indexed_tokens = list(enumerate(tokens))
    indexed_tokens.sort(key=lambda item: (-len(item[1]), item[0]))
    for _, token in indexed_tokens:
        if MIN_SEARCH_LENGTH <= len(token) <= MAX_SEARCH_LENGTH:
            queries.append(token)
    # Remove duplicates while retaining the intended search order.
    unique_queries: list[str] = []
    seen_queries: set[str] = set()
    for query in queries:
        query_key = query.casefold()
        if query_key not in seen_queries:
            seen_queries.add(query_key)
            unique_queries.append(query)
    if not unique_queries:
        raise ValueError(
            "No distinctive company-name token meets KnowYourGST's "
            f"{MIN_SEARCH_LENGTH}-character minimum."
        )
    return unique_queries
def calculate_company_similarity(
    requested_name: str,
    candidate_name: str,
) -> float:
    """
    Calculate similarity after removing legal and generic company words.
    The score combines:
    - Token coverage:
      How many distinctive requested words appear in the candidate?
    - Sequence similarity:
      How similar are the normalized company names overall?
    Returns a score between 0.0 and 1.0.
    """
    requested_key = normalized_company_key(requested_name)
    candidate_key = normalized_company_key(candidate_name)
    if not requested_key or not candidate_key:
        return 0.0
    requested_tokens = set(requested_key.split())
    candidate_tokens = set(candidate_key.split())
    token_coverage = (
        len(requested_tokens & candidate_tokens)
        / len(requested_tokens)
    )
    sequence_similarity = SequenceMatcher(
        None,
        requested_key,
        candidate_key,
    ).ratio()
    # Token coverage gets more weight because word overlap is usually more
    # meaningful for registered company names than punctuation or formatting.
    return (0.70 * token_coverage) + (0.30 * sequence_similarity)
def search_company_by_similar_name(
    scraper: GSTSearcher,
    company_name: str,
    *,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
) -> list[dict[str, str]]:
    """
    Search KnowYourGST using a normalized company name.
    The function:
    1. Removes legal and generic company-name words.
    2. Searches the cleaned phrase.
    3. If zero results are returned, tries distinctive-word fallbacks.
    4. Deduplicates results by GST number.
    5. Filters and sorts candidates using local similarity.
    6. Returns a JSON-compatible object list.
    The output contains only:
    - company_name
    - state
    - gst_number
    """
    if not 0.0 <= min_similarity <= 1.0:
        raise ValueError("min_similarity must be between 0.0 and 1.0.")
    company_name = " ".join(company_name.split())
    if not company_name:
        raise ValueError("Company name cannot be empty.")
    search_queries = build_search_queries(company_name)
    collected_results: dict[str, dict[str, str]] = {}
    for search_query in search_queries:
        query_results = scraper.search(search_query)
        for result in query_results:
            gst_number = result.get("gst_number", "").strip().upper()
            result_company_name = result.get(
                "company_name",
                "",
            ).strip()
            state = result.get("state", "").strip()
            if not gst_number or not result_company_name or not state:
                continue
            similarity = calculate_company_similarity(
                company_name,
                result_company_name,
            )
            if similarity < min_similarity:
                continue
            existing = collected_results.get(gst_number)
            # Internally retain the similarity score for sorting. It will be
            # removed from the final JSON output.
            if existing is None or similarity > existing["_similarity"]:
                collected_results[gst_number] = {
                    "company_name": result_company_name,
                    "state": state,
                    "gst_number": gst_number,
                    "_similarity": similarity,
                }
        # Stop after the first query that produces acceptable matches.
        # This avoids unnecessary ScrapingBee requests and overly broad results.
        if collected_results:
            break
    ranked_results = sorted(
        collected_results.values(),
        key=lambda result: (
            -result["_similarity"],
            result["company_name"],
            result["gst_number"],
        ),
    )
    # Remove the internal score so the result has the required JSON shape.
    return [
        {
            "company_name": result["company_name"],
            "state": result["state"],
            "gst_number": result["gst_number"],
        }
        for result in ranked_results
    ]
# Integration example:
#
# scraper = KnowYourGSTScraper(
#     os.environ["SCRAPINGBEE_API_KEY"]
# )
#
# results = search_company_by_similar_name(
#     scraper,
#     "RKECOM Services Pvt Ltd",
# )
#
# print(json.dumps(results, ensure_ascii=False, indent=2))
#
# Expected matching result:
#
# [
#   {
#     "company_name": "RKECOM SERVICES OPC PRIVATE LIMITED",
#     "state": "...",
#     "gst_number": "..."
#   }
# ]
