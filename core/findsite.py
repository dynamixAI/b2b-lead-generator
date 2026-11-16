"""Find a business's own website from a web search, when the source didn't give one."""
import urllib.parse

import requests
from bs4 import BeautifulSoup

from core.emails import BROWSER_HEADERS, name_tokens
from core.website import host_of, is_social

# sites that list companies but aren't the company's own website
LISTING_HOSTS = (
    "company-information.service.gov.uk", "companieshouse.gov.uk", "gov.uk",
    "endole.co.uk", "opencorporates.com", "companycheck.co.uk", "bizdb.co.uk",
    "companiesintheuk.co.uk", "check-business.co.uk", "dnb.com", "zoominfo.com",
    "192.com", "thomsonlocal.com", "scoot.co.uk", "freeindex.co.uk", "cylex-uk.co.uk",
    "hotfrog.co.uk", "yelp.co.uk", "bing.com", "microsoft.com", "wikipedia.org",
    "google.com", "youtube.com", "companieslist.co.uk", "find-and-update.company-information.service.gov.uk",
)


def is_listing(host):
    return is_social(host) or any(host == h or host.endswith("." + h) for h in LISTING_HOSTS)


def result_links(html_text):
    # links from the search results, in order
    soup = BeautifulSoup(html_text, "html.parser")
    links = [a.get("href", "") for a in soup.select("li.b_algo h2 a")]
    return [l for l in links if l.startswith("http")]


def pick_site(links, name, trade="", location=""):
    # first result that isn't a directory and whose address contains part of the name
    tokens = name_tokens(name, trade, location)
    for link in links:
        host = host_of(link)
        if not host or is_listing(host):
            continue
        compact = host.replace("-", "").replace(".", "")
        if any(t in compact for t in tokens):
            parts = urllib.parse.urlsplit(link)
            return f"{parts.scheme}://{parts.netloc}"
    return ""


def find_website(name, location, trade="", session=None):
    query = f'"{name}" {location}'
    url = "https://www.bing.com/search?q=" + urllib.parse.quote(query)
    get = (session or requests).get
    try:
        r = get(url, headers=BROWSER_HEADERS, timeout=(5, 8))
    except requests.RequestException:
        return ""
    if r.status_code != 200:
        return ""
    return pick_site(result_links(r.text), name, trade, location)
