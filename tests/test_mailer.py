"""Tests for writing and sending outreach emails, and reading the replies."""
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from core import sendqueue, store
from core.mailer import UNSUBSCRIBE_LINE, build_message, company_short, fill, first_name_for

SENDER = {"email": "pius@tradereports.co.uk", "name": "Pius Ajamma"}


@pytest.fixture
def con(tmp_path):
    return store.connect(tmp_path / "test.db")


def lead(name, email, number, director="", ok="Yes", status="Has website"):
    return {"name": name, "website_status": status, "email": email, "company_number": number,
            "address": "Wigan WN1 2AB", "ok_to_email": ok, "phone": "", "website": "", "other_emails": ""}


def make_list(con, rows, purpose="TradeReports", directors=None):
    lid = store.create_list(con, f"L{len(store.get_lists(con))}", purpose)
    store.add_leads(con, lid, rows, trade="electricians", town="wigan", directors=directors or {})
    return lid


# --- names and wording ---

def test_first_name():
    assert first_name_for("adrian.barnett@jouleelectrical.co.uk") == "Adrian"
    assert first_name_for("chris@cbelectricalsolutions.co.uk") == "Chris"
    # a shared inbox: use the director
    assert first_name_for("info@wiswellbuilding.co.uk", "Craig Wiswell") == "Craig"
    # not a person's name, and no director: no guess
    assert first_name_for("journey.electrical.service@gmail.com") == ""
    # an unusual name still counts when it matches the director
    assert first_name_for("aimee@aimeedaltonelectrical.co.uk", "Aimee Dalton") == "Aimee"
    assert first_name_for("", "") == ""
    # names the library knows, from anywhere
    assert first_name_for("pius@x.co.uk") == "Pius"
    # a name the library doesn't know still works when it's the director's
    assert first_name_for("kwame@x.co.uk", "Kwame Mensah") == "Kwame"
    assert first_name_for("kwame@x.co.uk") == ""


def test_fill_template():
    l = {"name": "Jon Wright Electrical Ltd", "email": "info@jw.co.uk", "director": "Jon Wright",
         "trade": "electricians", "town": "wigan"}
    assert company_short("Jon Wright Electrical Ltd") == "Jon Wright Electrical"
    assert fill("Hi {first_name}, {company} in {town}", l) == "Hi Jon, Jon Wright Electrical in Wigan"
    # no name: "Hi there"; an unknown placeholder is left blank instead of breaking
    assert fill("Hi {first_name}, {mystery}!", {"name": "X Ltd", "email": "info@x.co.uk"}) == "Hi there, !"


def test_one_person_per_email():
    msg = build_message("pius@tradereports.co.uk", "Pius Ajamma", "chris@cb.co.uk", "Hello", "Hi Chris,\n\nLine two")
    assert msg["To"] == "chris@cb.co.uk" and msg["Cc"] is None and msg["Bcc"] is None
    plain = msg.get_body(("plain",)).get_content()
    html = msg.get_body(("html",)).get_content()
    assert UNSUBSCRIBE_LINE in plain and "No thanks, don&#x27;t email me again" in html
    assert "#e2784a" in html and "TradeReports" in html  # branded signature
    assert "<p" in html and "Line two" in html


# --- who gets an email, and when ---

def test_only_safe_new_leads_get_a_first_email(con):
    lid = make_list(con, [
        lead("A Ltd", "a@a.co.uk", "1"),
        lead("B Ltd", "", "2"),                     # no email
        lead("C", "c@gmail.com", "", ok="No"),      # not a limited company
        lead("D Ltd", "d@d.co.uk", "4"),
    ])
    d_id = [l["id"] for l in store.load_leads(con, lid) if l["name"] == "D Ltd"][0]
    store.update_lead(con, d_id, status="Opted out")
    due = store.due_first_emails(con, lid, 10)
    assert [l["name"] for l in due] == ["A Ltd"]


def test_send_run_marks_contacted_and_waits_between(con):
    lid = make_list(con, [lead("A Ltd", "a@a.co.uk", "1"), lead("B Ltd", "b@b.co.uk", "2"),
                          lead("C Ltd", "c@c.co.uk", "3")])
    sent, waits = [], []
    result = sendqueue.run(con, lid, SENDER, sent.append, limit=2, gap_minutes=3, sleep=waits.append)
    assert result == (2, 0)
    assert [m["To"] for m in sent] == ["a@a.co.uk", "b@b.co.uk"]  # limit respected
    assert len(waits) == 1 and 120 <= waits[0] <= 240              # about 3 minutes apart
    statuses = {l["name"]: l["status"] for l in store.load_leads(con, lid)}
    assert statuses == {"A Ltd": "Contacted", "B Ltd": "Contacted", "C Ltd": "Not contacted"}
    # running again only picks up the one left
    sent.clear()
    sendqueue.run(con, lid, SENDER, sent.append, limit=5, sleep=lambda s: None)
    assert [m["To"] for m in sent] == ["c@c.co.uk"]


def test_same_business_not_emailed_twice_from_two_lists(con):
    web = make_list(con, [lead("A Ltd", "a@a.co.uk", "1")], purpose="Web design")
    tr = make_list(con, [lead("A Ltd", "a@a.co.uk", "1")], purpose="TradeReports")
    sendqueue.run(con, web, SENDER, lambda m: None, limit=5, sleep=lambda s: None)
    assert store.due_first_emails(con, tr, 5) == []


def test_follow_up_after_a_week_only_without_a_reply(con):
    lid = make_list(con, [lead("A Ltd", "a@a.co.uk", "1"), lead("B Ltd", "b@b.co.uk", "2")])
    sendqueue.run(con, lid, SENDER, lambda m: None, limit=5, sleep=lambda s: None)
    a, b = store.load_leads(con, lid)
    store.update_lead(con, b["id"], status="Replied")
    assert store.due_follow_ups(con, lid) == []                    # too soon
    later = date.today() + timedelta(days=7)
    due = store.due_follow_ups(con, lid, later)
    assert [l["name"] for l in due] == ["A Ltd"]                   # B replied, so no follow-up
    sent = []
    sendqueue.run(con, lid, SENDER, sent.append, limit=5, sleep=lambda s: None, today=later)
    assert sent[0]["Subject"].startswith("Re:")
    assert store.load_leads(con, lid)[0]["status"] == "Followed up"
    assert store.due_follow_ups(con, lid, later + timedelta(days=7)) == []  # only one follow-up


def test_daily_cap_across_lists(con, monkeypatch):
    monkeypatch.setattr(store, "DAILY_CAP", 3)
    rows = [lead(f"{i} Ltd", f"x{i}@x{i}.co.uk", str(i)) for i in range(5)]
    lid = make_list(con, rows)
    sent = []
    sendqueue.run(con, lid, SENDER, sent.append, limit=10, sleep=lambda s: None)
    assert len(sent) == 3


def test_bad_login_stops_the_run(con):
    lid = make_list(con, [lead("A Ltd", "a@a.co.uk", "1"), lead("B Ltd", "b@b.co.uk", "2")])

    def refuse(msg):
        raise Exception("(535, b'Authentication credentials invalid')")

    assert sendqueue.run(con, lid, SENDER, refuse, limit=5, sleep=lambda s: None) == (0, 1)
    assert all(l["status"] == "Not contacted" for l in store.load_leads(con, lid))


def test_templates_save_and_default(con):
    lid = make_list(con, [lead("A Ltd", "a@a.co.uk", "1")], purpose="Web design")
    from core.mailer import DEFAULT_TEMPLATES
    assert store.get_template(con, lid, DEFAULT_TEMPLATES)["subject"] == "A website for {company}?"
    store.save_template(con, lid, "S", "B", "FS", "FB", daily_limit=7, auto_send=True)
    t = store.get_template(con, lid, DEFAULT_TEMPLATES)
    assert (t["subject"], t["daily_limit"], t["auto_send"]) == ("S", 7, 1)
    assert store.auto_send_lists(con) == [lid]


# --- the daily sender script ---

def test_send_daily_script(tmp_path, monkeypatch):
    import send_daily
    monkeypatch.setattr(send_daily, "LOG", tmp_path / "send.log")
    monkeypatch.setattr(send_daily, "LOCK", tmp_path / "send.lock")
    secrets = tmp_path / "secrets.toml"
    secrets.write_text('SMTP_USER = "pius@tradereports.co.uk"\nSMTP_PASSWORD = "x"\n')
    monkeypatch.setattr(send_daily, "SECRETS", secrets)
    monkeypatch.setenv("OUTREACH_DB", str(tmp_path / "d.db"))
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "d.db")
    sent = []
    monkeypatch.setattr(send_daily, "smtp_sender", lambda *a: sent.append)
    monkeypatch.setattr(sendqueue.time, "sleep", lambda s: None)
    monkeypatch.setattr(send_daily, "check_replies", lambda *a, **k: {"opted_out": 0, "interested": 0, "replied": 0})

    con = store.connect()
    lid = make_list(con, [lead("A Ltd", "a@a.co.uk", "1")])
    assert send_daily.main(["--list", str(lid), "--gap", "0"]) == 0
    assert [m["To"] for m in sent] == ["a@a.co.uk"]
    assert "1 sent" in (tmp_path / "send.log").read_text()
    assert not (tmp_path / "send.lock").exists()

    # if the inbox can't be read, nothing is sent (an unsubscribe might be waiting)
    con2 = store.connect()
    lid2 = make_list(con2, [lead("B Ltd", "b@b.co.uk", "2")])

    def no_inbox(*a, **k):
        raise OSError("IMAP down")

    monkeypatch.setattr(send_daily, "check_replies", no_inbox)
    sent.clear()
    assert send_daily.main(["--list", str(lid2), "--gap", "0"]) == 1
    assert sent == [] and "nothing was sent" in (tmp_path / "send.log").read_text()

    # a second sender can't start while one holds the lock
    import os
    (tmp_path / "send.lock").write_text(str(os.getpid()))
    assert send_daily.take_lock() is False


# --- warnings on the list ---

def test_flags():
    import lists_ui
    df = pd.DataFrame({
        "address": ["7 Bolton Road, Wigan", "7 Bolton Road,  Wigan", "1 High St"],
        "founded": ["2010-01-01", "2026-06-01", ""],
    })
    flags = lists_ui.flags_for(df, df, today=date(2026, 11, 16))
    assert flags[0] == "Shared address (likely accountant)"
    assert flags[1] == "Shared address (likely accountant); New company"
    assert flags[2] == ""


# --- buttons ---

def test_tradereports_buttons_link_to_the_site():
    from core.mailer import NO_THANKS, buttons_for
    b = buttons_for("TradeReports", {"name": "Jon Wright Electrical Ltd"}, "pius@tradereports.co.uk")
    assert [label for label, _ in b] == ["Try it free", "See an example report", NO_THANKS]
    assert b[0][1] == "https://tradereports.co.uk/?utm_campaign=email-try"
    assert b[1][1].startswith("https://tradereports.co.uk/example")
    # "No thanks" opens their email app with the unsubscribe already written
    assert b[2][1].startswith("mailto:pius@tradereports.co.uk?subject=Unsubscribe")
    assert "Jon%20Wright%20Electrical" in b[2][1]


def test_web_design_button_is_a_ready_written_reply():
    from core.mailer import buttons_for
    label, url = buttons_for("Web design", {"name": "Sparks Ltd"}, "pius@tradereports.co.uk")[0]
    assert label == "Yes, show me a free mock-up"
    assert url.startswith("mailto:pius@tradereports.co.uk?subject=Yes%20please")
    assert "Sparks" in url and "Ltd" not in url


def test_email_shows_the_buttons():
    from core.mailer import buttons_for
    buttons = buttons_for("TradeReports", {"name": "A Ltd"}, "pius@tradereports.co.uk")
    msg = build_message("pius@tradereports.co.uk", "Pius Ajamma", "a@a.co.uk", "Hi", "Hello", buttons)
    html = msg.get_body(("html",)).get_content()
    plain = msg.get_body(("plain",)).get_content()
    assert "Try it free" in html and "utm_campaign=email-try" in html and "See an example report" in html
    assert "Try it free: https://tradereports.co.uk/?utm_campaign=email-try" in plain
    assert msg["List-Unsubscribe"].startswith("<mailto:pius@tradereports.co.uk?subject=Unsubscribe")


def test_sent_email_keeps_its_id_and_buttons(con):
    lid = make_list(con, [lead("A Ltd", "a@a.co.uk", "1")], purpose="TradeReports")
    sent = []
    sendqueue.run(con, lid, SENDER, sent.append, limit=5, sleep=lambda s: None)
    assert "Try it free" in sent[0].get_body(("html",)).get_content()
    row = con.execute("SELECT message_id FROM send_log").fetchone()
    assert row["message_id"] == sent[0]["Message-ID"]


# --- reading replies ---

def reply(frm, subject, text, in_reply_to="", mid=None, **headers):
    from email.message import EmailMessage
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, "pius@tradereports.co.uk", subject
    m["Message-ID"] = mid or f"<{abs(hash((frm, subject, text)))}@x>"
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    m.set_content(text)
    return m


def test_classify_replies():
    from core.inbox import classify
    assert classify("Unsubscribe", "Please don't email A again.") == "opted_out"
    assert classify("Re: Quick question", "No thanks mate") == "opted_out"
    assert classify("Yes please: a free mock-up for A", "") == "interested"
    assert classify("Re: Quick question", "Looks handy, how does it work with Excel?") == "replied"


def test_quoted_email_is_ignored():
    from core.inbox import classify, fresh_text
    # your own "unsubscribe" line is quoted under their reply; it mustn't count as theirs
    m = reply("a@a.co.uk", "Re: Hi", "Sounds good, tell me more\n\nOn Mon, 16 Nov 2026, Pius wrote:\n"
              "> Not for you? Reply \"unsubscribe\" and I won't contact you again.")
    assert fresh_text(m) == "Sounds good, tell me more"
    assert classify(m["Subject"], fresh_text(m)) == "replied"


def test_replies_update_every_list(con):
    from core.inbox import handle_message
    web = make_list(con, [lead("A Ltd", "a@a.co.uk", "1"), lead("B Ltd", "b@b.co.uk", "2"),
                          lead("C Ltd", "c@c.co.uk", "3")], purpose="Web design")
    tr = make_list(con, [lead("A Ltd", "a@a.co.uk", "1")], purpose="TradeReports")
    sent = []
    sendqueue.run(con, web, SENDER, sent.append, limit=5, sleep=lambda s: None)

    # A unsubscribes: opted out in both lists, and never emailed again
    assert handle_message(con, reply("a@a.co.uk", "Unsubscribe", "Please don't email A again."),
                          SENDER["email"]) == ("opted_out", ["A Ltd"])
    assert {l["status"] for l in store.load_leads(con, web) + store.load_leads(con, tr)
            if l["name"] == "A Ltd"} == {"Opted out"}
    assert store.due_first_emails(con, tr, 5) == []

    # B answers from a different address: matched by the email it replies to
    b_id = sent[1]["Message-ID"]
    assert handle_message(con, reply("owner@bmail.com", "Re: A website", "How much?", in_reply_to=b_id),
                          SENDER["email"]) == ("replied", ["B Ltd"])

    # C clicks the mock-up button
    assert handle_message(con, reply("c@c.co.uk", "Yes please: a free mock-up for C", "Yes please"),
                          SENDER["email"])[0] == "interested"

    status = {l["name"]: l["status"] for l in store.load_leads(con, web)}
    assert status == {"A Ltd": "Opted out", "B Ltd": "Replied", "C Ltd": "Interested"}
    assert "replied" in store.load_leads(con, web)[1]["notes"]
    # replied leads get no follow-up
    assert store.due_follow_ups(con, web, date.today() + timedelta(days=8)) == []


def test_out_of_office_and_repeats_are_ignored(con):
    from core.inbox import handle_message
    lid = make_list(con, [lead("A Ltd", "a@a.co.uk", "1")])
    sendqueue.run(con, lid, SENDER, lambda m: None, limit=5, sleep=lambda s: None)
    assert handle_message(con, reply("a@a.co.uk", "Automatic reply: away until Monday", "I'm away"),
                          SENDER["email"]) is None
    assert handle_message(con, reply("a@a.co.uk", "Re: Hi", "Hi", Auto_Submitted="auto-replied"),
                          SENDER["email"]) is None
    m = reply("a@a.co.uk", "Re: Hi", "Interesting", mid="<once@x>")
    assert handle_message(con, m, SENDER["email"]) == ("replied", ["A Ltd"])
    assert handle_message(con, m, SENDER["email"]) is None          # read once only
    # someone who isn't on any list
    assert handle_message(con, reply("friend@x.com", "Hello", "Hi Pius"), SENDER["email"]) is None


def test_check_replies_reads_the_inbox_without_changing_it(con):
    from core.inbox import check_replies
    lid = make_list(con, [lead("A Ltd", "a@a.co.uk", "1")])
    sendqueue.run(con, lid, SENDER, lambda m: None, limit=5, sleep=lambda s: None)
    raw = reply("a@a.co.uk", "Unsubscribe", "Please stop").as_bytes()

    class FakeImap:
        calls = []

        def login(self, u, p): self.calls.append("login")
        def select(self, box, readonly=False): self.calls.append(("select", readonly))
        def search(self, charset, *criteria): return "OK", [b"1"]
        def fetch(self, num, what): self.calls.append(what); return "OK", [(b"1 (BODY[] {1}", raw), b")"]
        def logout(self): self.calls.append("logout")

    imap = FakeImap()
    counts = check_replies(con, "imap.x", "pius@tradereports.co.uk", "pw", connect=lambda: imap)
    assert counts == {"opted_out": 1, "interested": 0, "replied": 0}
    assert ("select", True) in imap.calls and "(BODY.PEEK[])" in imap.calls  # read-only, stays unread
    assert imap.calls[-1] == "logout"
    assert store.load_leads(con, lid)[0]["status"] == "Opted out"
