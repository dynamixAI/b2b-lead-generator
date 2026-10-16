import re
import urllib.parse
from functools import lru_cache

import dns.resolver
import requests
from bs4 import BeautifulSoup

EMAIL_RE = re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", re.I)
VALID_RE = re.compile(r"^[a-z0-9._+-]+@[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}$")
AT_DOT_RE = re.compile(
    r"([a-z0-9._+-]+)\s*[\[\(]\s*at\s*[\]\)]\s*([a-z0-9-]+(?:\s*[\[\(]\s*dot\s*[\]\)]\s*[a-z0-9-]+)+)",
    re.I,
)
DOT_RE = re.compile(r"\s*[\[\(]\s*dot\s*[\]\)]\s*", re.I)

ASSET_EXTS = (".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp", ".css", ".js", ".ico", ".pdf")
JUNK_DOMAINS = (
    "sentry.io", "wixpress.com", "example.com", "example.co.uk", "domain.com",
    "yourdomain.com", "yoursite.com", "email.com", "godaddy.com", "wix.com",
    "squarespace.com", "schema.org", "w3.org", "bing.com", "microsoft.com",
    "sentry-next.wixpress.com", "mysite.com", "company.com",
)
JUNK_LOCALS = {
    "example", "name", "your", "yourname", "youremail", "email", "user",
    "username", "test", "noreply", "no-reply", "donotreply", "do-not-reply",
}
FREE_PROVIDERS = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.co.uk", "outlook.com",
    "live.com", "live.co.uk", "yahoo.com", "yahoo.co.uk", "btinternet.com",
    "sky.com", "icloud.com", "me.com", "aol.com", "talktalk.net",
    "virginmedia.com", "protonmail.com", "mail.com",
}

MX_LABELS = {
    "mx": "Mail server found",
    "a_only": "No MX record (may still work)",
    "none": "No mail server",
    "nodomain": "Domain doesn't exist",
    "unknown": "Couldn't check",
}

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

STOP_WORDS = {
    "ltd", "limited", "the", "and", "services", "service", "company", "group",
    "contractors", "contractor", "solutions", "specialists", "specialist",
    "north", "south", "east", "west", "local", "home", "homes",
}


def clean_email(raw):
    if not raw:
        return ""
    e = urllib.parse.unquote(raw).strip().lower()
    e = e.strip(".,;:'\"()<>[]{} ")
    e = re.sub(r"^(u003e|u003c|x3e|x3c)", "", e)
    if not VALID_RE.match(e) or ".." in e or len(e) > 80:
        return ""
    local, domain = e.split("@", 1)
    if e.endswith(ASSET_EXTS) or local in JUNK_LOCALS:
        return ""
    if any(domain == d or domain.endswith("." + d) for d in JUNK_DOMAINS):
        return ""
    return e


def decode_cfemail(code):
    try:
        key = int(code[:2], 16)
        return "".join(chr(int(code[i:i + 2], 16) ^ key) for i in range(2, len(code), 2))
    except (ValueError, IndexError):
        return ""


def extract_emails(html_text):
    if not html_text:
        return set()
    soup = BeautifulSoup(html_text, "html.parser")
    found = set()

    for a in soup.select("a[href]"):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            for part in href[7:].split("?")[0].split(","):
                found.add(part)
        elif "/cdn-cgi/l/email-protection#" in href:
            found.add(decode_cfemail(href.split("#", 1)[1]))

    for el in soup.select("[data-cfemail]"):
        found.add(decode_cfemail(el["data-cfemail"]))

    for script in soup.select('script[type="application/ld+json"]'):
        found.update(EMAIL_RE.findall(script.get_text()))

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ")
    found.update(EMAIL_RE.findall(text))
    for local, dom in AT_DOT_RE.findall(text):
        found.add(local + "@" + DOT_RE.sub(".", dom))

    return {c for c in (clean_email(f) for f in found) if c}


def domains_match(email_domain, site_domain):
    if not email_domain or not site_domain:
        return False
    site_domain = site_domain.removeprefix("www.")
    return (
        email_domain == site_domain
        or email_domain.endswith("." + site_domain)
        or site_domain.endswith("." + email_domain)
    )


def rank_emails(emails, site_domain=None):
    def key(e):
        domain = e.split("@", 1)[1]
        if domains_match(domain, site_domain):
            group = 0
        elif domain in FREE_PROVIDERS:
            group = 1
        else:
            group = 2
        return (group, len(e), e)
    return sorted(set(emails), key=key)


def name_tokens(name, trade="", location=""):
    trade_stems = {w[:4] for w in re.findall(r"[a-z]+", trade.lower()) if len(w) >= 4}
    loc_words = set(re.findall(r"[a-z]+", location.lower()))
    tokens = set()
    for w in re.findall(r"[a-z0-9]+", name.lower()):
        if len(w) < 4 or w in STOP_WORDS or w in loc_words or w[:4] in trade_stems:
            continue
        tokens.add(w)
    return tokens


def email_belongs(email, tokens, site_domain=None):
    domain = email.split("@", 1)[1]
    if domains_match(domain, site_domain):
        return True
    compact = re.sub(r"[^a-z0-9]", "", email)
    return any(t in compact for t in tokens)


def search_emails_online(name, location, trade="", site_domain=None):
    tokens = name_tokens(name, trade, location)
    queries = [f'"{name}" {location} email']
    if site_domain:
        queries.append(f'"@{site_domain.removeprefix("www.")}"')

    found = set()
    for q in queries:
        url = "https://www.bing.com/search?q=" + urllib.parse.quote(q)
        try:
            r = requests.get(url, headers=BROWSER_HEADERS, timeout=(5, 8))
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue
        for e in extract_emails(r.text):
            if email_belongs(e, tokens, site_domain):
                found.add(e)
    return rank_emails(found, site_domain)


@lru_cache(maxsize=1024)
def mail_server_status(domain):
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5)
        hosts = [str(r.exchange).rstrip(".") for r in answers]
        return "mx" if any(hosts) else "none"
    except dns.resolver.NXDOMAIN:
        return "nodomain"
    except dns.resolver.NoAnswer:
        try:
            dns.resolver.resolve(domain, "A", lifetime=5)
            return "a_only"
        except Exception:
            return "none"
    except Exception:
        return "unknown"


# stops calling Abstract once the key is rejected or the quota runs out
class AbstractChecker:

    def __init__(self, api_key):
        self.api_key = api_key
        self.enabled = bool(api_key)

    def check(self, email):
        if not self.enabled:
            return None
        try:
            r = requests.get(
                "https://emailvalidation.abstractapi.com/v1/",
                params={"api_key": self.api_key, "email": email},
                timeout=8,
            )
        except requests.RequestException:
            return None
        if r.status_code in (401, 402, 429):
            self.enabled = False
            return None
        if r.status_code != 200:
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        disposable = data.get("is_disposable_email")
        if isinstance(disposable, dict):
            disposable = disposable.get("value")
        if disposable:
            return "Disposable address"
        deliverability = str(data.get("deliverability", "")).upper()
        if deliverability == "DELIVERABLE":
            return "Deliverable"
        if deliverability == "UNDELIVERABLE":
            return "Undeliverable"
        return None


def check_email(email, abstract=None):
    if not email:
        return ""
    domain = email.split("@", 1)[1]
    mx = mail_server_status(domain)
    if mx in ("none", "nodomain"):
        return MX_LABELS[mx]
    if abstract is not None:
        result = abstract.check(email)
        if result:
            return result
    return MX_LABELS[mx]
