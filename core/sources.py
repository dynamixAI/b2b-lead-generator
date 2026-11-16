"""Free places to find businesses: Companies House and OpenStreetMap.

Both return "place" dicts in the same shape as Google Places results, so the
rest of the app (website check, email search, table, Excel) works unchanged.
"""
import re

import requests

CH_ADVANCED_URL = "https://api.company-information.service.gov.uk/advanced-search/companies"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "DynamixAI-lead-finder/1.0 (https://github.com/dynamixAI/b2b-lead-generator)"

# trade words -> Companies House SIC codes and OpenStreetMap tags
TRADES = {
    "electrician": (["43210"], [("craft", "electrician")]),
    "plumber": (["43220"], [("craft", "plumber"), ("craft", "hvac")]),
    "heating": (["43220"], [("craft", "hvac"), ("craft", "plumber")]),
    "roofer": (["43910"], [("craft", "roofer")]),
    "builder": (["41202", "43999"], [("craft", "builder")]),
    "plasterer": (["43310"], [("craft", "plasterer")]),
    "joiner": (["43320"], [("craft", "carpenter"), ("craft", "joiner")]),
    "carpenter": (["43320"], [("craft", "carpenter"), ("craft", "joiner")]),
    "painter": (["43341"], [("craft", "painter")]),
    "decorator": (["43341"], [("craft", "painter")]),
    "tiler": (["43330"], [("craft", "tiler"), ("craft", "floorer")]),
    "flooring": (["43330"], [("craft", "floorer")]),
    "glazier": (["43342"], [("craft", "glaziery"), ("craft", "window_construction")]),
    "window": (["43342"], [("craft", "window_construction"), ("craft", "glaziery")]),
    "scaffold": (["43991"], [("craft", "scaffolder")]),
    "landscap": (["81300"], [("craft", "gardener")]),
    "gardener": (["81300"], [("craft", "gardener")]),
    "cleaner": (["81210", "81229"], [("shop", "dry_cleaning")]),
    "locksmith": (["80200", "95290"], [("craft", "locksmith"), ("shop", "locksmith")]),
}

TRADE_LIST = "electricians, plumbers, heating engineers, roofers, builders, plasterers, " \
             "joiners, painters and decorators, tilers, glaziers, scaffolders, landscapers, " \
             "cleaners, locksmiths"


class SourceError(Exception):
    pass


def resolve_trade(text):
    """Turn what the user typed into (sic_codes, osm_tags).

    Accepts a trade word ("roofers") or SIC codes typed directly ("43210, 43220").
    Returns ([], []) if nothing matches.
    """
    text = (text or "").lower()
    codes = re.findall(r"\b\d{5}\b", text)
    if codes:
        return codes, []
    sic, tags = [], []
    for word, (s, t) in TRADES.items():
        if word in text:
            sic += [c for c in s if c not in sic]
            tags += [x for x in t if x not in tags]
    return sic, tags


def _place(pid, name, address, phone="", website="", lat=None, lng=None, maps="", company=None, email=""):
    # same keys the Google Places results use, so base_row() can read it
    place = {
        "id": pid,
        "displayName": {"text": name},
        "formattedAddress": address,
        "nationalPhoneNumber": phone,
        "websiteUri": website,
        "googleMapsUri": maps,
        "userRatingCount": 0,
        "_find_site": not website,
    }
    if lat is not None and lng is not None:
        place["location"] = {"latitude": lat, "longitude": lng}
    if company:
        place["_company"] = company
    if email:
        place["_email"] = email
    return place


# --- Companies House ---

def _ch_address(a):
    parts = [a.get("address_line_1"), a.get("address_line_2"), a.get("locality"), a.get("postal_code")]
    return ", ".join(p for p in parts if p)


def search_companies_house(sic_codes, location, api_key, max_results=100, session=None):
    """Active limited companies and LLPs in a place, by SIC code.

    The location matches the registered office address, which is sometimes an
    accountant's office rather than where the business works.
    """
    if not api_key:
        raise SourceError("No Companies House key. Add COMPANIES_HOUSE_API_KEY to .streamlit/secrets.toml.")
    if not sic_codes:
        raise SourceError(f"Couldn't match that trade. Try one of: {TRADE_LIST}, or type SIC codes.")

    get = (session or requests).get
    places, start = [], 0
    while len(places) < max_results:
        params = {
            "company_status": "active",
            "company_type": "ltd,llp",
            "location": location,
            "sic_codes": ",".join(sic_codes),
            "size": min(100, max_results - len(places)),
            "start_index": start,
        }
        try:
            r = get(CH_ADVANCED_URL, params=params, auth=(api_key, ""), timeout=20)
        except requests.RequestException as e:
            if places:
                break
            raise SourceError(f"Couldn't reach Companies House: {e}")
        if r.status_code == 401:
            raise SourceError("Companies House didn't accept the key. Check COMPANIES_HOUSE_API_KEY.")
        if r.status_code == 404:
            break  # no (more) results
        if r.status_code != 200:
            if places:
                break
            raise SourceError(f"Companies House returned an error ({r.status_code})")

        items = r.json().get("items", [])
        for item in items:
            kind = item.get("company_type", "")
            places.append(_place(
                pid=f"ch:{item.get('company_number', '')}",
                name=item.get("company_name", "Unknown").title(),
                address=_ch_address(item.get("registered_office_address") or {}),
                company={
                    "company_check": f"{'LLP' if kind == 'llp' else 'Ltd'}, active",
                    "company_number": item.get("company_number", ""),
                    "ok_to_email": "Yes",
                },
            ))
        if len(items) < params["size"]:
            break
        start += len(items)
    return places


# --- OpenStreetMap ---

def _osm_address(tags):
    street = " ".join(p for p in (tags.get("addr:housenumber"), tags.get("addr:street")) if p)
    parts = [street, tags.get("addr:city") or tags.get("addr:town"), tags.get("addr:postcode")]
    return ", ".join(p for p in parts if p)


def overpass_query(osm_tags, location):
    # businesses with any of the tags, inside the named town or district
    safe = location.replace('"', "")
    lines = "".join(f'  nwr["{k}"="{v}"](area.a);\n' for k, v in osm_tags)
    return (
        "[out:json][timeout:25];\n"
        f'area["name"~"^{safe}$",i]["boundary"="administrative"]->.a;\n'
        f"(\n{lines});\n"
        "out center tags 200;"
    )


def search_osm(osm_tags, location, session=None):
    """Businesses mapped on OpenStreetMap in a town. Free, but coverage of trades is patchy."""
    if not osm_tags:
        raise SourceError(f"Couldn't match that trade. Try one of: {TRADE_LIST}.")
    if re.search(r"\d", location):
        raise SourceError("OpenStreetMap needs a town name, not a postcode.")

    post = (session or requests).post
    try:
        r = post(OVERPASS_URL, data={"data": overpass_query(osm_tags, location)},
                 headers={"User-Agent": USER_AGENT}, timeout=40)
    except requests.RequestException as e:
        raise SourceError(f"Couldn't reach OpenStreetMap: {e}")
    if r.status_code != 200:
        raise SourceError(f"OpenStreetMap returned an error ({r.status_code}). Try again in a minute.")

    places = []
    for el in r.json().get("elements", []):
        tags = el.get("tags") or {}
        name = tags.get("name")
        if not name:
            continue
        centre = el.get("center") or el
        lat, lng = centre.get("lat"), centre.get("lon")
        places.append(_place(
            pid=f"osm:{el.get('type')}/{el.get('id')}",
            name=name,
            address=_osm_address(tags),
            phone=tags.get("phone") or tags.get("contact:phone", ""),
            website=tags.get("website") or tags.get("contact:website", ""),
            lat=lat, lng=lng,
            maps=f"https://www.openstreetmap.org/{el.get('type')}/{el.get('id')}",
            email=tags.get("email") or tags.get("contact:email", ""),
        ))
    return places
