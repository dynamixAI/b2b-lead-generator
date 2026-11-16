import urllib.parse
from datetime import date
from concurrent.futures import ThreadPoolExecutor, as_completed

from core.companies import check_company
from core.findsite import find_website
from core.emails import (
    check_email, email_belongs, mail_server_status, name_tokens, rank_emails, search_emails_online,
)
from core.website import BROKEN, HAS_SITE, HOLDING, NO_SITE, SOCIAL, analyse_website

STATUS_ORDER = {NO_SITE: 0, SOCIAL: 1, BROKEN: 2, HOLDING: 3, HAS_SITE: 4}

COLUMNS = [
    "name", "phone", "website_status", "website", "email", "email_source",
    "email_check", "other_emails", "rating", "reviews", "address",
    "google_maps", "ok_to_email", "company_check", "company_number",
    "job_software", "vat_registered", "founded", "tradereports_fit",
    "note", "lat", "lng", "place_id",
]


def strip_tracking(url):
    if not url:
        return url
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if not k.lower().startswith("utm_")]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def base_row(place):
    loc = place.get("location") or {}
    note = "Temporarily closed" if place.get("businessStatus") == "CLOSED_TEMPORARILY" else ""
    return {
        "name": (place.get("displayName") or {}).get("text", "Unknown"),
        "phone": place.get("nationalPhoneNumber", ""),
        "website_status": NO_SITE,
        "website": place.get("websiteUri", ""),
        "email": "",
        "email_source": "",
        "email_check": "",
        "other_emails": "",
        "rating": place.get("rating"),
        "reviews": place.get("userRatingCount", 0),
        "address": place.get("formattedAddress", ""),
        "google_maps": place.get("googleMapsUri", ""),
        "ok_to_email": "",
        "company_check": "",
        "company_number": "",
        "job_software": "",
        "vat_registered": "",
        "founded": "",
        "tradereports_fit": "",
        "note": note,
        "lat": loc.get("latitude"),
        "lng": loc.get("longitude"),
        "place_id": place.get("id", ""),
    }


def build_lead(place, trade, location, abstract=None, use_web_search=True, ch_key=None):
    row = base_row(place)
    # Ltd or LLP? only those can be cold emailed under PECR
    # (Companies House results already know, so no second lookup)
    row.update(place.get("_company") or check_company(row["name"], row["address"], ch_key))
    if not row["website"] and place.get("_find_site"):
        row["website"] = find_website(row["name"], location, trade, address=row["address"])
        found = "Website found automatically, check it's theirs" if row["website"] else "No website found online"
        row["note"] = "; ".join(filter(None, [row["note"], found]))
    site = analyse_website(row["website"])
    row["website_status"] = site["status"]
    row["job_software"] = ", ".join(site.get("software", []))
    row["vat_registered"] = "Yes" if site.get("vat") else ""
    row["tradereports_fit"] = tradereports_fit(row)
    row["website"] = strip_tracking(site["url"])
    if site["note"]:
        row["note"] = "; ".join(filter(None, [row["note"], site["note"]]))

    emails = rank_emails(site["emails"], site["domain"])
    # put emails that match the business first, so a web designer's address
    # in the footer doesn't get picked over the company's own
    tokens = name_tokens(row["name"], trade, location)
    emails.sort(key=lambda e: not email_belongs(e, tokens, site["domain"]))
    source = "Website" if emails else ""

    # email listed on the map entry itself
    if not emails and place.get("_email"):
        emails = [place["_email"].strip().lower()]
        source = "OpenStreetMap"

    # no website found for a Companies House or map lead: web search results for the
    # bare name are too often someone else's, so don't guess an email from them
    unsure = place.get("_find_site") and not row["website"]
    if not emails and use_web_search and not unsure:
        emails = search_emails_online(row["name"], location, trade, site["domain"] or None)
        source = "Web search" if emails else ""

    if not emails and site["status"] in (HAS_SITE, HOLDING) and site["domain"]:
        if mail_server_status(site["domain"]) == "mx":
            emails = [f"info@{site['domain']}"]
            source = "Guessed"

    if emails:
        row["email"] = emails[0]
        row["email_source"] = source
        row["other_emails"] = ", ".join(emails[1:4])
        row["email_check"] = check_email(emails[0], abstract)
    return row


def years_trading(founded, today=None):
    try:
        start = date.fromisoformat(str(founded)[:10])
    except ValueError:
        return None
    today = today or date.today()
    return today.year - start.year - ((today.month, today.day) < (start.month, start.day))


def tradereports_fit(row, today=None):
    """How likely TradeReports is to help: High, Medium or Low.

    High: their site shows job software (they have exports to check).
    Medium: VAT registered, or trading 2+ years with a working website.
    """
    if row.get("job_software"):
        return "High"
    years = years_trading(row.get("founded", ""), today)
    if row.get("vat_registered") == "Yes" or (years is not None and years >= 2 and row.get("website_status") == HAS_SITE):
        return "Medium"
    return "Low"


def is_closed(place):
    return place.get("businessStatus") == "CLOSED_PERMANENTLY"


def build_leads(places, trade, location, abstract=None, use_web_search=True,
                workers=8, on_progress=None, ch_key=None):
    rows = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(build_lead, p, trade, location, abstract, use_web_search, ch_key): p
            for p in places
        }
        for done, future in enumerate(as_completed(futures), start=1):
            place = futures[future]
            try:
                row = future.result()
            except Exception as e:
                row = base_row(place)
                if row["website"]:
                    row["website_status"] = HAS_SITE
                row["note"] = f"Check failed: {e}"
            rows.append(row)
            if on_progress:
                on_progress(done, len(places), row["name"])

    rows.sort(key=lambda r: (STATUS_ORDER.get(r["website_status"], 9), -(r["reviews"] or 0)))
    return rows
