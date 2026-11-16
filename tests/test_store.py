"""Tests for the outreach lists (core/store.py) and the director lookup."""
import pytest

from core import store
from core.companies import get_director, tidy_officer_name


@pytest.fixture
def con(tmp_path):
    return store.connect(tmp_path / "test.db")


def lead(name, status="No website", email="", number="", address="Wigan WN1 2AB", ok="Yes", phone=""):
    return {"name": name, "website_status": status, "email": email, "company_number": number,
            "address": address, "ok_to_email": ok, "phone": phone, "website": "", "other_emails": ""}


def test_groups_and_contact_routes():
    assert store.group_of("No website") == "No website"
    assert store.group_of("Broken link") == "Weak website"
    assert store.group_of("Has website") == "Has website"
    assert store.contact_route(lead("A", email="a@a.co.uk")) == "Email"
    assert store.contact_route(lead("A", email="a@gmail.com", ok="No")).startswith("Email (check")
    assert store.contact_route(lead("A", phone="01942")) == "Phone"
    assert store.contact_route(lead("A")) == "Letter"
    assert store.contact_route(lead("A", address="")) == "Find on Facebook / Google"


def test_same_business_same_key():
    assert store.lead_key(lead("X", number="0123")) == "ch:0123"
    a = store.lead_key(lead("Smith Roofing Ltd", address="1 High St, Wigan, WN1 2AB"))
    b = store.lead_key(lead("SMITH ROOFING", address="Wigan wn1 2ab"))
    assert a == b


def test_new_list_and_no_duplicates(con):
    lid = store.create_list(con, "Roofers in Wigan", "Web design")
    rows = [lead("A Ltd", number="1"), lead("B Ltd", number="2")]
    assert store.add_leads(con, lid, rows, directors={"ch:1": "Jane Smith"}) == (2, 0, 0)
    # a second search with one repeat: only the new one goes in
    assert store.add_leads(con, lid, [lead("B Ltd", number="2"), lead("C Ltd", number="3")]) == (1, 1, 0)
    leads = store.load_leads(con, lid)
    assert [l["name"] for l in leads] == ["A Ltd", "B Ltd", "C Ltd"]
    assert leads[0]["director"] == "Jane Smith" and leads[0]["status"] == "Not contacted"
    with pytest.raises(ValueError):
        store.create_list(con, "Roofers in Wigan", "Web design")
    with pytest.raises(ValueError):
        store.create_list(con, "  ", "Web design")


def test_opt_out_applies_to_every_list(con):
    web = store.create_list(con, "Web", "Web design")
    tr = store.create_list(con, "TR", "TradeReports")
    store.add_leads(con, web, [lead("A Ltd", number="1", email="a@a.co.uk")])
    store.add_leads(con, tr, [lead("A Ltd", number="1", email="a@a.co.uk")])
    a_web = store.load_leads(con, web)[0]["id"]
    store.update_lead(con, a_web, status="Opted out")
    # shows as opted out in the other list straight away
    assert store.load_leads(con, tr)[0]["status"] == "Opted out"
    # and in any list made later
    later = store.create_list(con, "Later", "Other")
    assert store.add_leads(con, later, [lead("A Ltd", number="1")]) == (1, 0, 1)
    assert store.load_leads(con, later)[0]["status"] == "Opted out"
    # deleting a list keeps the opt-out
    store.delete_list(con, web)
    assert store.load_leads(con, tr)[0]["status"] == "Opted out"


def test_follow_up_a_week_after_contact(con):
    lid = store.create_list(con, "L", "Web design")
    store.add_leads(con, lid, [lead("A Ltd", number="1")])
    lead_id = store.load_leads(con, lid)[0]["id"]
    store.update_lead(con, lead_id, status="Contacted", date_contacted="2026-11-16", notes="Sent the letter")
    row = store.load_leads(con, lid)[0]
    assert row["follow_up"] == "2026-11-23" and row["notes"] == "Sent the letter"
    # once they reply, no follow-up is due
    store.update_lead(con, lead_id, status="Replied")
    assert store.load_leads(con, lid)[0]["follow_up"] == ""
    with pytest.raises(ValueError):
        store.update_lead(con, lead_id, status="Maybe")


def test_missing_directors(con):
    lid = store.create_list(con, "L", "Web design")
    store.add_leads(con, lid, [lead("A Ltd", number="1"), lead("B", number="")])
    missing = store.missing_directors(con, lid)
    assert [n for _, n in missing] == ["1"]
    store.set_director(con, missing[0][0], "Jo Bloggs")
    assert store.missing_directors(con, lid) == []


class FakeResponse:
    def __init__(self, status, data=None):
        self.status_code, self._data = status, data or {}

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, response):
        self.response = response

    def get(self, url, **kwargs):
        return self.response


def test_director_lookup():
    assert tidy_officer_name("SMITH, John Paul") == "John Smith"
    data = {"items": [
        {"name": "OLD, Gone", "officer_role": "director", "resigned_on": "2020-01-01"},
        {"name": "WRIGHT, Jon", "officer_role": "director"},
    ]}
    assert get_director("123", "key", session=FakeSession(FakeResponse(200, data))) == "Jon Wright"
    assert get_director("123", "key", session=FakeSession(FakeResponse(404))) == ""
    assert get_director("", "key") == ""


def test_table_edits_are_saved(con, monkeypatch):
    # what the My lists table does when you change a cell
    import lists_ui

    class FakeSt:
        session_state = {}

    monkeypatch.setattr(lists_ui, "st", FakeSt)
    lid = store.create_list(con, "L", "Web design")
    store.add_leads(con, lid, [lead("A Ltd", number="1"), lead("B Ltd", number="2")])
    ids = [l["id"] for l in store.load_leads(con, lid)]

    FakeSt.session_state = {"ed": {"edited_rows": {"0": {"status": "Contacted"},
                                                   "1": {"notes": "Rang, call back Monday",
                                                         "date_contacted": "2026-11-10"}}}}
    lists_ui._save_edits(con, "ed", ids, "v")
    a, b = store.load_leads(con, lid)
    assert a["status"] == "Contacted" and a["date_contacted"]  # today filled in
    assert b["notes"] == "Rang, call back Monday" and b["date_contacted"] == "2026-11-10"
    assert FakeSt.session_state["v"] == 1  # table redrawn from the database


def test_tradereports_signals():
    from datetime import date
    from core.leads import tradereports_fit, years_trading
    from core.website import detect_job_software, has_vat_number

    html = '<a href="https://clienthub.getjobber.com/booking/abc">Book online</a>'
    assert detect_job_software(html) == ["Jobber"]
    assert detect_job_software('<script src="https://book.servicem8.com/x.js"></script>') == ["ServiceM8"]
    assert detect_job_software("<p>We fix boilers</p>") == []
    assert has_vat_number("<footer>VAT Reg No: GB 123 4567 89</footer>")
    assert has_vat_number("<p>VAT number 123456789</p>")
    assert not has_vat_number("<p>All prices include VAT</p>")

    today = date(2026, 11, 16)
    assert years_trading("2020-03-01", today) == 6 and years_trading("", today) is None
    assert tradereports_fit({"job_software": "Tradify"}, today) == "High"
    assert tradereports_fit({"vat_registered": "Yes"}, today) == "Medium"
    assert tradereports_fit({"founded": "2021-01-01", "website_status": "Has website"}, today) == "Medium"
    assert tradereports_fit({"founded": "2026-01-01", "website_status": "Has website"}, today) == "Low"


def test_old_database_gets_new_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(store.SCHEMA.replace("    job_software TEXT,\n    vat_registered TEXT,\n"
                                           "    founded TEXT,\n    tradereports_fit TEXT,\n", ""))
    old.close()
    con = store.connect(path)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(leads)")}
    assert {"job_software", "vat_registered", "founded", "tradereports_fit"} <= cols
