"""Find a business's own website when the source didn't give one.

Guesses the domain from the name (jonwrightelectrical.co.uk) and only accepts it
if the site is live and its home page mentions the business.

Search engines were tried and dropped: from a script they return unrelated
pages (Tate Security -> tate.org.uk), which would put wrong sites on leads.
"""
import re
import socket

import requests

from core.emails import BROWSER_HEADERS, name_tokens
from core.website import host_of, is_holding_page, is_social, visible_text

# sites that list companies but aren't the company's own website
LISTING_HOSTS = (
    "company-information.service.gov.uk", "companieshouse.gov.uk", "gov.uk",
    "endole.co.uk", "opencorporates.com", "companycheck.co.uk", "bizdb.co.uk",
    "companiesintheuk.co.uk", "check-business.co.uk", "dnb.com", "zoominfo.com",
    "192.com", "thomsonlocal.com", "scoot.co.uk", "freeindex.co.uk", "cylex-uk.co.uk",
    "hotfrog.co.uk", "yelp.co.uk", "bing.com", "microsoft.com", "wikipedia.org",
    "google.com", "youtube.com", "companieslist.co.uk", "duckduckgo.com",
    "find-and-update.company-information.service.gov.uk", "northdata.com",
    "companieshousedata.co.uk", "ukcompanylist.co.uk", "electricalcompetentperson.co.uk",
)

# words left out when guessing a domain
LEGAL_WORDS = {"ltd", "limited", "llp", "plc", "the", "uk", "and", "co"}

TLDS = (".co.uk", ".com", ".uk")
TIMEOUT = (4, 6)


def is_listing(host):
    return is_social(host) or any(host == h or host.endswith("." + h) for h in LISTING_HOSTS)


# --- 1. guess the domain ---

def guess_domains(name, with_short=True):
    # "Jon Wright Electrical (Wigan) Ltd" -> jonwrightelectrical.co.uk, jon-wright-electrical.co.uk, ...
    # with_short adds the first two words only (jonwright.co.uk), which needs a stricter check
    clean = re.sub(r"\(.*?\)", " ", (name or "").lower()).replace("&", " ")
    words = [w for w in re.findall(r"[a-z0-9]+", clean) if w not in LEGAL_WORDS]
    if not words:
        return []
    stems = ["".join(words), "-".join(words)]
    if with_short and len(words) > 2:
        stems.append("".join(words[:2]))
    seen, out = set(), []
    for stem in stems:
        for tld in TLDS:
            d = stem + tld
            if d not in seen and len(stem) >= 4:
                seen.add(d)
                out.append(d)
    return out


def _resolves(domain):
    try:
        socket.getaddrinfo(domain, 443)
        return True
    except (socket.gaierror, UnicodeError, OSError):
        return False


def _page_mentions(text, name, trade, location):
    # the home page should mention the business: most of its distinctive name words,
    # or one of them plus the town
    tokens = name_tokens(name, trade, location) or set(re.findall(r"[a-z]{4,}", name.lower()))
    if not tokens:
        return False
    text = text.lower()
    found = sum(t in text for t in tokens)
    if found >= max(1, (len(tokens) + 1) // 2) and (len(tokens) > 1 or location.lower() in text):
        return True
    return found >= 1 and bool(location) and location.lower() in text


def guess_website(name, trade="", location="", session=None):
    get = (session or requests).get
    full = set(guess_domains(name, with_short=False))
    for domain in guess_domains(name):
        # a shortened name (homeimprovement.co.uk) is often someone else's site,
        # so only accept it if the page also mentions the town
        short = domain not in full
        if session is None and not _resolves(domain):
            continue
        for scheme in ("https://", "http://"):
            try:
                r = get(scheme + domain, headers=BROWSER_HEADERS, timeout=TIMEOUT)
            except requests.RequestException:
                continue
            if r.status_code >= 400:
                break
            if is_listing(host_of(getattr(r, "url", "") or scheme + domain)):
                break
            text = visible_text(r.text)
            if is_holding_page(text) or not _page_mentions(text, name, trade, location):
                break
            if short and not (location and location.lower() in text.lower()):
                break
            return scheme + domain
    return ""


def find_website(name, location, trade="", session=None):
    return guess_website(name, trade, location, session)
