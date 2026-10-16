import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

from core.emails import check_email, mail_server_status, rank_emails, search_emails_online
from core.website import BROKEN, HAS_SITE, HOLDING, NO_SITE, SOCIAL, analyse_website

STATUS_ORDER = {NO_SITE: 0, SOCIAL: 1, BROKEN: 2, HOLDING: 3, HAS_SITE: 4}

COLUMNS = [
    "name", "phone", "website_status", "website", "email", "email_source",
    "email_check", "other_emails", "rating", "reviews", "address",
    "google_maps", "note", "lat", "lng", "place_id",
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
        "note": note,
        "lat": loc.get("latitude"),
        "lng": loc.get("longitude"),
        "place_id": place.get("id", ""),
    }


def build_lead(place, trade, location, abstract=None, use_web_search=True):
    row = base_row(place)
    site = analyse_website(row["website"])
    row["website_status"] = site["status"]
    row["website"] = strip_tracking(site["url"])
    if site["note"]:
        row["note"] = "; ".join(filter(None, [row["note"], site["note"]]))

    emails = rank_emails(site["emails"], site["domain"])
    source = "Website" if emails else ""

    if not emails and use_web_search:
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


def is_closed(place):
    return place.get("businessStatus") == "CLOSED_PERMANENTLY"


def build_leads(places, trade, location, abstract=None, use_web_search=True,
                workers=8, on_progress=None):
    rows = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(build_lead, p, trade, location, abstract, use_web_search): p
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
