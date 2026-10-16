from types import SimpleNamespace

import core.leads as leads
import core.website as website
from core.emails import (
    clean_email, decode_cfemail, email_belongs, extract_emails, name_tokens, rank_emails,
)
from core.website import (
    BROKEN, HAS_SITE, HOLDING, NO_SITE, SOCIAL, analyse_website, contact_pages, is_holding_page,
)


def encode_cf(email, key=0x42):
    return f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in email)


def test_clean_email_filters_junk():
    assert clean_email("Info@Smith-Roofing.co.uk.") == "info@smith-roofing.co.uk"
    assert clean_email("%20info@smith.co.uk") == "info@smith.co.uk"
    assert clean_email("u003einfo@smith.co.uk") == "info@smith.co.uk"
    assert clean_email("abc123@sentry.wixpress.com") == ""
    assert clean_email("example@domain.com") == ""
    assert clean_email("logo@2x.png") == ""
    assert clean_email("noreply@smith.co.uk") == ""


def test_decode_cfemail():
    assert decode_cfemail(encode_cf("jo@hart.co.uk")) == "jo@hart.co.uk"
    assert decode_cfemail("zz") == ""


def test_extract_emails_from_all_sources():
    page = f"""
    <html><body>
      <a href="mailto:Office@Hart.co.uk?subject=Quote">Email</a>
      <a href="/cdn-cgi/l/email-protection#{encode_cf('hidden@hart.co.uk')}">[email protected]</a>
      <span data-cfemail="{encode_cf('also@hart.co.uk')}"></span>
      <script type="application/ld+json">{{"email": "ld@hart.co.uk"}}</script>
      <script>var x = "js@tracker.io";</script>
      <p>Write to jo [at] hart [dot] co [dot] uk</p>
      <p>Error report: 123abc@sentry.wixpress.com</p>
    </body></html>
    """
    assert extract_emails(page) == {
        "office@hart.co.uk", "hidden@hart.co.uk", "also@hart.co.uk",
        "ld@hart.co.uk", "jo@hart.co.uk",
    }


def test_rank_emails_prefers_own_domain():
    ranked = rank_emails(["bob@gmail.com", "x@other.com", "info@hart.co.uk"], "www.hart.co.uk")
    assert ranked == ["info@hart.co.uk", "bob@gmail.com", "x@other.com"]


def test_search_results_only_keep_matching_emails():
    tokens = name_tokens("J Hart Roofing Ltd", trade="roofers", location="Wigan")
    assert tokens == {"hart"}
    assert email_belongs("jhart.roofs@gmail.com", tokens)
    assert not email_belongs("someoneelse@gmail.com", tokens)
    assert email_belongs("info@hart.co.uk", set(), "hart.co.uk")


def test_holding_page_detection():
    assert is_holding_page("this domain is for sale")
    assert is_holding_page("our new website is coming soon")
    long_real_page = ("roofing repairs in wigan " * 60) + "new gallery coming soon"
    assert not is_holding_page(long_real_page)


def test_contact_pages_ignore_tracking_params():
    page = '<a href="/contact-us?ref=nav">Contact</a><a href="https://facebook.com/x">About us</a>'
    pages = contact_pages(page, "https://hart.co.uk")
    assert pages[0] == "https://hart.co.uk/contact-us?ref=nav"
    assert all(p.startswith("https://hart.co.uk") for p in pages)


def fake_response(url, text="", status=200):
    return SimpleNamespace(url=url, text=text, status_code=status, headers={"Content-Type": "text/html"})


def test_analyse_website_uses_origin_not_tracking_url(monkeypatch):
    requested = []

    def fake_fetch(session, url):
        requested.append(url)
        if url.startswith("https://hart.co.uk/?utm"):
            return fake_response(url, "<p>" + "roofing " * 300 + '</p><a href="/contact">Contact</a>')
        if url == "https://hart.co.uk/contact":
            return fake_response(url, '<a href="mailto:info@hart.co.uk">Email</a>')
        return fake_response(url, status=404)

    monkeypatch.setattr(website, "fetch", fake_fetch)
    result = analyse_website("https://hart.co.uk/?utm_source=gmb")
    assert result["status"] == HAS_SITE
    assert result["emails"] == {"info@hart.co.uk"}
    assert "https://hart.co.uk/contact" in requested


def test_analyse_website_statuses(monkeypatch):
    assert analyse_website("")["status"] == NO_SITE
    assert analyse_website("https://www.facebook.com/hartroofing")["status"] == SOCIAL

    monkeypatch.setattr(website, "fetch", lambda s, u: None)
    assert analyse_website("https://dead.co.uk")["status"] == BROKEN

    monkeypatch.setattr(website, "fetch", lambda s, u: fake_response(u, status=500))
    assert analyse_website("https://error.co.uk")["status"] == BROKEN

    monkeypatch.setattr(website, "fetch", lambda s, u: fake_response("https://www.facebook.com/x"))
    assert analyse_website("https://redirects.co.uk")["status"] == SOCIAL

    monkeypatch.setattr(website, "fetch", lambda s, u: fake_response(u, "<h1>Coming soon</h1>"))
    assert analyse_website("https://new.co.uk")["status"] == HOLDING


def test_build_lead_marks_guessed_email(monkeypatch):
    monkeypatch.setattr(leads, "analyse_website", lambda url: {
        "status": HAS_SITE, "url": url, "domain": "hart.co.uk", "emails": set(), "note": "",
    })
    monkeypatch.setattr(leads, "search_emails_online", lambda *a, **k: [])
    monkeypatch.setattr(leads, "mail_server_status", lambda d: "mx")
    monkeypatch.setattr(leads, "check_email", lambda e, a=None: "Mail server found")

    place = {"displayName": {"text": "Hart Roofing"}, "websiteUri": "https://hart.co.uk", "id": "p1"}
    row = leads.build_lead(place, "roofers", "Wigan")
    assert row["email"] == "info@hart.co.uk"
    assert row["email_source"] == "Guessed"


def test_no_guess_for_broken_sites(monkeypatch):
    monkeypatch.setattr(leads, "analyse_website", lambda url: {
        "status": BROKEN, "url": url, "domain": "", "emails": set(), "note": "Site didn't load",
    })
    monkeypatch.setattr(leads, "search_emails_online", lambda *a, **k: [])
    row = leads.build_lead({"displayName": {"text": "X"}, "websiteUri": "https://x.co.uk"}, "roofers", "Wigan")
    assert row["email"] == ""
    assert row["website_status"] == BROKEN


def test_closed_businesses_detected():
    assert leads.is_closed({"businessStatus": "CLOSED_PERMANENTLY"})
    assert not leads.is_closed({"businessStatus": "OPERATIONAL"})


def test_strip_tracking():
    assert leads.strip_tracking("https://hart.co.uk/?utm_source=gmb&utm_medium=x") == "https://hart.co.uk/"
    assert leads.strip_tracking("https://hart.co.uk/p?id=3&utm_source=gmb") == "https://hart.co.uk/p?id=3"
