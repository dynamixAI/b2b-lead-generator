"""Tests for the Companies House check and the free lead sources."""
import pytest

import core.leads as leads
from core import sources
from core.companies import check_company, match_score, name_words, postcode_of
from core.website import HAS_SITE


# --- Companies House check ---

class FakeResponse:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data or {}

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


def ch_item(title, kind="ltd", status="active", postcode="WN1 2AB", number="01234567"):
    return {"title": title, "company_type": kind, "company_status": status,
            "company_number": number, "address": {"postal_code": postcode}}


def test_name_words_drops_filler():
    assert name_words("Smith & Sons Plumbing Ltd") == {"smith", "sons", "plumbing"}
    assert name_words("THE SMITH PLUMBING COMPANY LIMITED") == {"smith", "plumbing"}


def test_postcode_of():
    assert postcode_of("12 High St, Wigan WN1 2AB, UK") == ("WN1", "WN1 2AB")
    assert postcode_of("wn12ab") == ("WN1", "WN1 2AB")
    assert postcode_of("Wigan, UK") == ("", "")


def test_match_score():
    addr = "12 High St, Wigan WN1 2AB, UK"
    assert match_score("Smith Plumbing", addr, ch_item("SMITH PLUMBING LTD")) == 3
    assert match_score("Smith Plumbing", addr, ch_item("SMITH PLUMBING LTD", postcode="M1 1AA")) == 2
    assert match_score("Smith Plumbing Wigan", addr, ch_item("SMITH PLUMBING LTD", postcode="WN1 9ZZ")) == 1
    assert match_score("Smith Plumbing", addr, ch_item("JONES ROOFING LTD")) == 0


def test_check_company_active_ltd_is_ok():
    s = FakeSession(FakeResponse(200, {"items": [ch_item("SMITH PLUMBING LTD")]}))
    out = check_company("Smith Plumbing", "Wigan WN1 2AB", "key", session=s)
    assert out == {"company_check": "Ltd, active", "company_number": "01234567", "ok_to_email": "Yes"}


def test_check_company_not_found_is_not_ok():
    s = FakeSession(FakeResponse(200, {"items": [ch_item("JONES ROOFING LTD")]}))
    out = check_company("Smith Plumbing", "Wigan WN1 2AB", "key", session=s)
    assert out["ok_to_email"] == "No"
    assert out["company_check"] == "Not found on Companies House"


def test_check_company_dissolved_is_not_ok():
    s = FakeSession(FakeResponse(200, {"items": [ch_item("SMITH PLUMBING LTD", status="dissolved")]}))
    out = check_company("Smith Plumbing", "Wigan WN1 2AB", "key", session=s)
    assert out["ok_to_email"] == "No"
    assert "dissolved" in out["company_check"]


def test_check_company_partial_match_needs_a_look():
    s = FakeSession(FakeResponse(200, {"items": [ch_item("SMITH PLUMBING LTD", postcode="WN1 9ZZ")]}))
    out = check_company("Smith Plumbing Wigan", "Wigan WN1 2AB", "key", session=s)
    assert out["ok_to_email"] == "Check"


def test_check_company_bad_key_and_no_key():
    s = FakeSession(FakeResponse(401))
    assert check_company("Smith", "WN1 2AB", "bad", session=s)["ok_to_email"] == "Check"
    assert check_company("Smith", "WN1 2AB", "")["ok_to_email"] == ""


def test_check_company_retries_after_rate_limit(monkeypatch):
    monkeypatch.setattr("core.companies.time.sleep", lambda s: None)
    s = FakeSession(FakeResponse(429), FakeResponse(200, {"items": [ch_item("SMITH PLUMBING LTD")]}))
    out = check_company("Smith Plumbing", "Wigan WN1 2AB", "key", session=s)
    assert s.calls == 2 and out["ok_to_email"] == "Yes"


# --- free sources: Companies House search, OpenStreetMap, website finder ---

class FakePoster(FakeSession):
    def post(self, url, **kwargs):
        self.calls += 1
        self.last = kwargs
        return self.responses.pop(0)


def test_resolve_trade():
    assert sources.resolve_trade("Electricians") == (["43210"], [("craft", "electrician")])
    sic, tags = sources.resolve_trade("painters and decorators")
    assert sic == ["43341"] and ("craft", "painter") in tags
    assert sources.resolve_trade("43210, 43220") == (["43210", "43220"], [])
    assert sources.resolve_trade("astronauts") == ([], [])


def test_companies_house_search_builds_places():
    item = {"company_name": "SMITH ELECTRICAL LTD", "company_number": "0123", "company_type": "ltd",
            "registered_office_address": {"address_line_1": "1 High St", "locality": "Wigan",
                                          "postal_code": "WN1 2AB"}}
    s = FakeSession(FakeResponse(200, {"items": [item]}))
    places = sources.search_companies_house(["43210"], "Wigan", "key", session=s)
    assert len(places) == 1
    p = places[0]
    assert p["displayName"]["text"] == "Smith Electrical Ltd"
    assert p["formattedAddress"] == "1 High St, Wigan, WN1 2AB"
    assert p["_company"]["ok_to_email"] == "Yes" and p["_find_site"] is True


def test_companies_house_search_errors():
    with pytest.raises(sources.SourceError):
        sources.search_companies_house(["43210"], "Wigan", "")
    with pytest.raises(sources.SourceError):
        sources.search_companies_house([], "Wigan", "key")
    with pytest.raises(sources.SourceError):
        sources.search_companies_house(["43210"], "Wigan", "bad", session=FakeSession(FakeResponse(401)))
    # no results comes back as 404 from the API, which is just an empty list
    assert sources.search_companies_house(["43210"], "Nowhere", "key", session=FakeSession(FakeResponse(404))) == []


def test_osm_search_builds_places():
    data = {"elements": [
        {"type": "node", "id": 5, "lat": 53.5, "lon": -2.6,
         "tags": {"name": "Sparks R Us", "craft": "electrician", "phone": "01942 000000",
                  "addr:street": "Market St", "addr:city": "Wigan", "email": "hi@sparks.co.uk"}},
        {"type": "way", "id": 6, "center": {"lat": 1, "lon": 2}, "tags": {"craft": "electrician"}},
    ]}
    s = FakePoster(FakeResponse(200, data))
    places = sources.search_osm([("craft", "electrician")], "Wigan", session=s)
    assert [p["displayName"]["text"] for p in places] == ["Sparks R Us"]  # unnamed one skipped
    p = places[0]
    assert p["nationalPhoneNumber"] == "01942 000000" and p["_email"] == "hi@sparks.co.uk"
    assert p["googleMapsUri"] == "https://www.openstreetmap.org/node/5"
    assert 'nwr["craft"="electrician"]' in s.last["data"]["data"]


def test_osm_rejects_postcode():
    with pytest.raises(sources.SourceError):
        sources.search_osm([("craft", "electrician")], "WN1 2AB")


def test_build_lead_uses_known_company_and_finds_site(monkeypatch):
    place = sources._place("ch:1", "Hartley Roofing Ltd", "Wigan WN1 2AB",
                           company={"company_check": "Ltd, active", "company_number": "1", "ok_to_email": "Yes"})
    monkeypatch.setattr(leads, "check_company", lambda *a: (_ for _ in ()).throw(AssertionError("no lookup")))
    monkeypatch.setattr(leads, "find_website", lambda *a: "https://hartleyroofing.co.uk")
    monkeypatch.setattr(leads, "analyse_website", lambda url: {"status": HAS_SITE, "url": url,
                        "domain": "hartleyroofing.co.uk", "emails": {"info@hartleyroofing.co.uk"}, "note": ""})
    row = leads.build_lead(place, "roofers", "Wigan", use_web_search=False)
    assert row["ok_to_email"] == "Yes" and row["website"] == "https://hartleyroofing.co.uk"
    assert row["email"] == "info@hartleyroofing.co.uk"
    assert "found automatically" in row["note"]


# --- website finder: domain guessing ---

from core.findsite import guess_domains, guess_website


class Page:
    def __init__(self, text, status=200, url=""):
        self.text, self.status_code, self.url = text, status, url


class PageSession:
    # answers each URL from a dict; anything else fails like a missing site
    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def get(self, url, **kwargs):
        self.asked.append(url)
        if url in self.pages:
            return self.pages[url]
        import requests
        raise requests.ConnectionError("no such site")


def test_guess_domains():
    d = guess_domains("Jon Wright Electrical (Wigan) Ltd")
    assert d[:3] == ["jonwrightelectrical.co.uk", "jonwrightelectrical.com", "jonwrightelectrical.uk"]
    assert "jon-wright-electrical.co.uk" in d and "jonwright.co.uk" in d
    assert guess_domains("Ltd") == []


def test_guess_website_checks_the_page_mentions_the_business():
    s = PageSession({
        "https://jonwrightelectrical.co.uk": Page("<h1>Jon Wright Electrical</h1> Electricians in Wigan"),
    })
    assert guess_website("Jon Wright Electrical Ltd", "electricians", "Wigan", session=s) == \
        "https://jonwrightelectrical.co.uk"
    # a domain that exists but is someone else's or parked is rejected
    s = PageSession({"https://jonwrightelectrical.co.uk": Page("This domain is for sale")})
    assert guess_website("Jon Wright Electrical Ltd", "electricians", "Wigan", session=s) == ""


def test_guess_website_rejects_a_page_about_someone_else():
    # live site, but it's a different business that shares one word
    s = PageSession({"https://tatesecuritytechnology.co.uk": Page("Visit Tate Modern, London's gallery")})
    assert guess_website("Tate Security Technology Limited", "electricians", "Wigan", session=s) == ""
    s = PageSession({"https://tatesecuritytechnology.co.uk": Page("Tate Security: CCTV and alarms")})
    assert guess_website("Tate Security Technology Limited", "electricians", "Wigan", session=s) == \
        "https://tatesecuritytechnology.co.uk"


def test_no_email_guessing_when_no_site_was_found(monkeypatch):
    place = sources._place("ch:9", "Tate Security Technology Limited", "Wigan",
                           company={"company_check": "Ltd, active", "company_number": "9", "ok_to_email": "Yes"})
    monkeypatch.setattr(leads, "find_website", lambda *a: "")
    monkeypatch.setattr(leads, "search_emails_online",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not search")))
    row = leads.build_lead(place, "electricians", "Wigan")
    assert row["email"] == "" and row["note"] == "No website found online"


def test_short_guess_needs_the_town():
    # homeimprovement.co.uk is a shortened guess, so it must mention the town too
    page = Page("Home Improvement ideas and offers across the UK")
    s = PageSession({"https://homeimprovement.co.uk": page})
    assert guess_website("Home Improvement Woolton Ltd", "builders", "Wigan", session=s) == ""
    s = PageSession({"https://homeimprovement.co.uk": Page("Home Improvement Woolton, builders in Wigan")})
    assert guess_website("Home Improvement Woolton Ltd", "builders", "Wigan", session=s) == \
        "https://homeimprovement.co.uk"


def test_business_email_beats_web_designer_email(monkeypatch):
    place = sources._place("ch:5", "Journey Electrical Ltd", "Wigan",
                           company={"company_check": "Ltd, active", "company_number": "5", "ok_to_email": "Yes"})
    monkeypatch.setattr(leads, "find_website", lambda *a: "https://journeyelectrical.co.uk")
    monkeypatch.setattr(leads, "analyse_website", lambda url: {
        "status": HAS_SITE, "url": url, "domain": "journeyelectrical.co.uk", "note": "",
        "emails": {"info.tisodigital@gmail.com", "journey.electrical.service@gmail.com"}})
    monkeypatch.setattr(leads, "check_email", lambda e, a=None: "ok")
    row = leads.build_lead(place, "electricians", "Wigan", use_web_search=False)
    assert row["email"] == "journey.electrical.service@gmail.com"
