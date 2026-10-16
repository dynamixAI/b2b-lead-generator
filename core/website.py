import urllib.parse

import requests
from bs4 import BeautifulSoup

from core.emails import BROWSER_HEADERS, extract_emails

NO_SITE = "No website"
SOCIAL = "Social or directory only"
BROKEN = "Broken website"
HOLDING = "Holding page"
HAS_SITE = "Has website"

SOCIAL_HOSTS = (
    "facebook.com", "fb.com", "fb.me", "instagram.com", "linktr.ee", "tiktok.com",
    "twitter.com", "x.com", "linkedin.com", "yell.com", "checkatrade.com",
    "mybuilder.com", "trustatrade.com", "ratedpeople.com", "nextdoor.com",
    "bark.com", "business.site", "g.page", "wa.me", "whatsapp.com",
)

STRONG_HOLDING = (
    "domain for sale", "this domain is for sale", "buy this domain",
    "domain is parked", "parked domain", "parked free", "this domain has expired",
    "domain has been registered", "is registered by",
)
SOFT_HOLDING = (
    "coming soon", "under construction", "check back soon", "launching soon",
    "website under maintenance", "site is under maintenance", "new website on its way",
)

TIMEOUT = (5, 8)


def host_of(url):
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return host.lower().removeprefix("www.")


def is_social(host):
    return any(host == h or host.endswith("." + h) for h in SOCIAL_HOSTS)


def origin_of(url):
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def fetch(session, url):
    try:
        return session.get(url, timeout=TIMEOUT, allow_redirects=True)
    except requests.exceptions.SSLError:
        if url.startswith("https://"):
            try:
                return session.get("http://" + url[8:], timeout=TIMEOUT, allow_redirects=True)
            except requests.RequestException:
                return None
        return None
    except requests.RequestException:
        return None


def visible_text(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return " ".join(soup.get_text(" ").split()).lower()


def is_holding_page(text):
    if any(p in text for p in STRONG_HOLDING):
        return True
    # a real site can mention "coming soon" somewhere, so only count it on thin pages
    return len(text.split()) < 200 and any(p in text for p in SOFT_HOLDING)


def contact_pages(html_text, origin):
    soup = BeautifulSoup(html_text, "html.parser")
    site_host = host_of(origin)
    pages = []
    for a in soup.select("a[href]"):
        href = a["href"].strip()
        label = (a.get_text() + " " + href).lower()
        if "contact" not in label and "about" not in label:
            continue
        full = urllib.parse.urljoin(origin + "/", href).split("#")[0]
        if host_of(full) == site_host and full.startswith("http"):
            pages.append(full)
    pages += [origin + "/contact", origin + "/contact-us"]
    seen, unique = set(), []
    for p in pages:
        key = p.rstrip("/")
        if key not in seen and key != origin:
            seen.add(key)
            unique.append(p)
    return unique[:3]


def analyse_website(url):
    result = {"status": NO_SITE, "url": url or "", "domain": "", "emails": set(), "note": ""}
    if not url:
        return result
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
        result["url"] = url

    if is_social(host_of(url)):
        result["status"] = SOCIAL
        return result

    with requests.Session() as session:
        session.headers.update(BROWSER_HEADERS)
        home = fetch(session, url)

        if home is None:
            result.update(status=BROKEN, note="Site didn't load")
            return result
        if home.status_code >= 400:
            result.update(status=BROKEN, note=f"Site returned error {home.status_code}")
            return result

        final_host = host_of(home.url)
        if is_social(final_host):
            result.update(status=SOCIAL, note="Redirects to a social or directory page")
            return result

        result["domain"] = final_host
        if "html" not in home.headers.get("Content-Type", "html").lower():
            result["status"] = HAS_SITE
            return result

        text = visible_text(home.text)
        result["status"] = HOLDING if is_holding_page(text) else HAS_SITE

        emails = extract_emails(home.text)
        if not emails:
            for page in contact_pages(home.text, origin_of(home.url)):
                r = fetch(session, page)
                if r is not None and r.status_code == 200:
                    emails |= extract_emails(r.text)
                if emails:
                    break
        result["emails"] = emails
    return result
