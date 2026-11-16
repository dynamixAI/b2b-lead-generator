import re
import time

import requests

SEARCH_URL = "https://api.company-information.service.gov.uk/search/companies"

# company types that count as a company in their own right, so cold B2B email is allowed
CORPORATE_TYPES = {
    "ltd",
    "plc",
    "llp",
    "private-limited-guarant-nsc",
    "private-limited-guarant-nsc-limited-exemption",
    "private-limited-shares-section-30-exemption",
    "private-unlimited",
    "private-unlimited-nsc",
}

TYPE_NAMES = {"ltd": "Ltd", "plc": "PLC", "llp": "LLP"}

# words that don't help tell two business names apart
FILLER = {
    "ltd", "limited", "llp", "plc", "the", "and", "co", "company", "uk", "of",
    "services", "service", "group", "t/a",
}

POSTCODE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", re.I)

YES = "Yes"
NO = "No"
CHECK = "Check"


def name_words(name):
    words = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower().replace("&", " and ")).split()
    return {w for w in words if w not in FILLER}


def postcode_of(text):
    # returns (outward, full), e.g. ("WN1", "WN1 2AB"); empty strings if none found
    m = POSTCODE.search(text or "")
    if not m:
        return "", ""
    outward, inward = m.group(1).upper(), m.group(2).upper()
    return outward, f"{outward} {inward}"


def match_score(name, address, item):
    # 0 = no match; higher is a better match
    ours, theirs = name_words(name), name_words(item.get("title", ""))
    if not ours or not theirs:
        return 0
    overlap = len(ours & theirs) / len(ours | theirs)

    their_address = (item.get("address") or {}).get("postal_code") or item.get("address_snippet", "")
    our_out, our_full = postcode_of(address)
    their_out, their_full = postcode_of(their_address)
    same_full = bool(our_full) and our_full == their_full
    same_area = bool(our_out) and our_out == their_out

    if overlap == 1:
        return 3 if (same_full or same_area) else 2
    if overlap >= 0.5 and same_full:
        return 2
    if overlap >= 0.5 and same_area:
        return 1
    return 0


def describe(item):
    kind = item.get("company_type", "")
    status = item.get("company_status", "")
    label = TYPE_NAMES.get(kind, kind.replace("-", " "))
    if status != "active":
        return f"{label}, {status or 'not active'}", NO
    if kind in CORPORATE_TYPES:
        return f"{label}, active", YES
    return f"{label}, active", NO


def check_company(name, address, api_key, session=None, retries=2):
    """Look a business up on Companies House.

    Returns company_check (what was found), company_number, and ok_to_email:
    Yes for an active company or LLP, No if not found or not a company,
    Check if the lookup failed.
    """
    empty = {"company_check": "", "company_number": "", "ok_to_email": ""}
    if not api_key or not name:
        return empty

    get = (session or requests).get
    for attempt in range(retries + 1):
        try:
            r = get(SEARCH_URL, params={"q": name, "items_per_page": 10},
                    auth=(api_key, ""), timeout=15)
        except requests.RequestException:
            return {**empty, "company_check": "Lookup failed", "ok_to_email": CHECK}
        if r.status_code == 429 and attempt < retries:
            # over the rate limit (600 requests per 5 minutes), wait and try again
            time.sleep(5 * (attempt + 1))
            continue
        break

    if r.status_code == 401:
        return {**empty, "company_check": "Companies House key not accepted", "ok_to_email": CHECK}
    if r.status_code != 200:
        return {**empty, "company_check": f"Lookup failed ({r.status_code})", "ok_to_email": CHECK}

    try:
        items = r.json().get("items", [])
    except ValueError:
        return {**empty, "company_check": "Lookup failed", "ok_to_email": CHECK}

    scored = sorted(((match_score(name, address, i), i) for i in items),
                    key=lambda pair: pair[0], reverse=True)
    if not scored or scored[0][0] == 0:
        # most often a sole trader or partnership, but trading names can differ
        return {**empty, "company_check": "Not found on Companies House", "ok_to_email": NO}

    score, best = scored[0]
    text, ok = describe(best)
    if score == 1:
        # name only partly matches, so ask for a human look before emailing
        text += f" (possible match: {best.get('title', '')})"
        ok = CHECK if ok == YES else ok
    return {
        "company_check": text,
        "company_number": best.get("company_number", ""),
        "ok_to_email": ok,
    }
