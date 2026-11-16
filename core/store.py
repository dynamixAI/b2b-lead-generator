"""Outreach lists, kept in a small SQLite file on this computer (data/outreach.db).

A list has a purpose (web design, TradeReports...). Leads are copied into a list
from a search, without duplicates. An opt-out belongs to the business, so it
shows in every list, now and later.
"""
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

# OUTREACH_DB lets tests use a throwaway file
DB_PATH = Path(os.environ.get("OUTREACH_DB") or Path(__file__).resolve().parent.parent / "data" / "outreach.db")

PURPOSES = ["Web design", "TradeReports", "Data service", "Other"]
STATUSES = ["Not contacted", "Contacted", "Followed up", "Replied", "Interested", "Not interested", "Opted out"]
FOLLOW_UP_DAYS = 7

# what the app's website statuses mean for outreach
GROUP_NO_SITE = "No website"
GROUP_WEAK = "Weak website"
GROUP_HAS_SITE = "Has website"
GROUPS = [GROUP_NO_SITE, GROUP_WEAK, GROUP_HAS_SITE]

SCHEMA = """
CREATE TABLE IF NOT EXISTS lists (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    purpose TEXT NOT NULL,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    list_id INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    name TEXT NOT NULL,
    grp TEXT NOT NULL,
    contact_by TEXT,
    email TEXT,
    other_emails TEXT,
    phone TEXT,
    director TEXT,
    address TEXT,
    website TEXT,
    company_number TEXT,
    ok_to_email TEXT,
    trade TEXT,
    town TEXT,
    job_software TEXT,
    vat_registered TEXT,
    founded TEXT,
    tradereports_fit TEXT,
    follow_up_sent TEXT,
    status TEXT NOT NULL DEFAULT 'Not contacted',
    date_contacted TEXT,
    notes TEXT,
    added TEXT NOT NULL,
    UNIQUE (list_id, key)
);
CREATE TABLE IF NOT EXISTS templates (
    list_id INTEGER PRIMARY KEY REFERENCES lists(id) ON DELETE CASCADE,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    follow_subject TEXT NOT NULL,
    follow_body TEXT NOT NULL,
    daily_limit INTEGER NOT NULL DEFAULT 15,
    auto_send INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS send_log (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER,
    lead_key TEXT,
    email TEXT,
    kind TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    ok INTEGER NOT NULL,
    error TEXT,
    message_id TEXT
);
CREATE TABLE IF NOT EXISTS inbox_seen (
    message_id TEXT PRIMARY KEY,
    seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS opt_outs (
    key TEXT PRIMARY KEY,
    name TEXT,
    email TEXT,
    since TEXT NOT NULL
);
"""

# the columns the app shows and lets you edit, in order
LEAD_COLUMNS = [
    "id", "name", "grp", "contact_by", "email", "phone", "director", "address", "status",
    "date_contacted", "notes", "website", "other_emails", "company_number", "ok_to_email",
    "trade", "town", "added", "job_software", "vat_registered", "founded", "tradereports_fit",
    "follow_up_sent",
]

# columns added after the first version; older databases get them on connect
NEW_COLUMNS = ["job_software", "vat_registered", "founded", "tradereports_fit", "follow_up_sent"]


def connect(path=None):
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    have = {r["name"] for r in con.execute("PRAGMA table_info(leads)")}
    for col in NEW_COLUMNS:
        if col not in have:
            con.execute(f"ALTER TABLE leads ADD COLUMN {col} TEXT")
    if "message_id" not in {r["name"] for r in con.execute("PRAGMA table_info(send_log)")}:
        con.execute("ALTER TABLE send_log ADD COLUMN message_id TEXT")
    con.commit()
    return con


def group_of(website_status):
    # the finder's five website statuses folded into three outreach groups
    if website_status == "No website":
        return GROUP_NO_SITE
    if website_status == "Has website":
        return GROUP_HAS_SITE
    return GROUP_WEAK


def lead_key(row):
    # the same business gets the same key in every search: company number if known,
    # otherwise its name and postcode
    number = (row.get("company_number") or "").strip()
    if number:
        return f"ch:{number}"
    name = re.sub(r"[^a-z0-9]", "", (row.get("name") or "").lower())
    name = re.sub(r"(limited|ltd|llp)$", "", name)
    postcode = re.findall(r"[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}", (row.get("address") or "").upper())
    return f"nm:{name}:{postcode[-1].replace(' ', '') if postcode else ''}"


def contact_route(row):
    # the best way to reach this business, for the "Contact by" column
    if row.get("email"):
        # cold email is only allowed to a limited company or LLP
        return "Email" if row.get("ok_to_email") == "Yes" else "Email (check: may need consent)"
    if row.get("phone"):
        return "Phone"
    if row.get("address"):
        return "Letter"
    return "Find on Facebook / Google"


def create_list(con, name, purpose):
    name = (name or "").strip()
    if not name:
        raise ValueError("Give the list a name.")
    if purpose not in PURPOSES:
        raise ValueError(f"Purpose must be one of: {', '.join(PURPOSES)}")
    try:
        cur = con.execute("INSERT INTO lists (name, purpose, created) VALUES (?, ?, ?)",
                          (name, purpose, date.today().isoformat()))
    except sqlite3.IntegrityError:
        raise ValueError(f'There is already a list called "{name}".')
    con.commit()
    return cur.lastrowid


def get_lists(con):
    rows = con.execute("""
        SELECT l.id, l.name, l.purpose, l.created, COUNT(d.id) AS leads
        FROM lists l LEFT JOIN leads d ON d.list_id = l.id
        GROUP BY l.id ORDER BY l.created DESC, l.id DESC
    """).fetchall()
    return [dict(r) for r in rows]


def delete_list(con, list_id):
    con.execute("DELETE FROM lists WHERE id = ?", (list_id,))
    con.commit()


def opted_out_keys(con):
    rows = con.execute("SELECT key, email FROM opt_outs").fetchall()
    keys = {r["key"] for r in rows}
    emails = {(r["email"] or "").lower() for r in rows if r["email"]}
    return keys, emails


def add_leads(con, list_id, rows, trade="", town="", directors=None):
    """Copy search results into a list.

    rows: dicts from the finder (name, email, website_status, ...).
    directors: optional {key: "Jane Smith"} looked up beforehand.
    Returns (added, already_there, opted_out).
    """
    directors = directors or {}
    out_keys, out_emails = opted_out_keys(con)
    added = already = opted = 0
    today = date.today().isoformat()
    for row in rows:
        key = lead_key(row)
        email = (row.get("email") or "").strip().lower()
        is_out = key in out_keys or (email and email in out_emails)
        try:
            con.execute("""
                INSERT INTO leads (list_id, key, name, grp, contact_by, email, other_emails, phone,
                    director, address, website, company_number, ok_to_email, trade, town,
                    job_software, vat_registered, founded, tradereports_fit, status, added)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                list_id, key, row.get("name", ""), group_of(row.get("website_status", "")),
                contact_route(row), email, row.get("other_emails", ""), row.get("phone", ""),
                directors.get(key, ""), row.get("address", ""), row.get("website", ""),
                row.get("company_number", ""), row.get("ok_to_email", ""), trade, town,
                row.get("job_software", ""), row.get("vat_registered", ""), row.get("founded", ""),
                row.get("tradereports_fit", ""),
                "Opted out" if is_out else "Not contacted", today,
            ))
        except sqlite3.IntegrityError:
            already += 1
            continue
        added += 1
        opted += bool(is_out)
    con.commit()
    return added, already, opted


def load_leads(con, list_id):
    rows = con.execute(f"SELECT key, {', '.join(LEAD_COLUMNS)} FROM leads WHERE list_id = ? ORDER BY id",
                       (list_id,)).fetchall()
    # an opt-out made in another list shows here too
    out_keys, out_emails = opted_out_keys(con)
    leads = []
    for r in rows:
        lead = dict(r)
        key = lead.pop("key")
        if key in out_keys or (lead["email"] and lead["email"].lower() in out_emails):
            lead["status"] = "Opted out"
        lead["follow_up"] = follow_up_date(lead["status"], lead["date_contacted"])
        leads.append(lead)
    return leads


def follow_up_date(status, date_contacted):
    # one follow-up, a week after first contact, and only if they haven't answered
    if status != "Contacted" or not date_contacted:
        return ""
    try:
        return (date.fromisoformat(str(date_contacted)[:10]) + timedelta(days=FOLLOW_UP_DAYS)).isoformat()
    except ValueError:
        return ""


def update_lead(con, lead_id, status=None, date_contacted=None, notes=None):
    """Save what you changed on one lead. Marking it Opted out records it for every list."""
    row = con.execute("SELECT key, name, email FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if row is None:
        return
    if status is not None:
        if status not in STATUSES:
            raise ValueError(f"Unknown status: {status}")
        con.execute("UPDATE leads SET status = ? WHERE id = ?", (status, lead_id))
        if status == "Opted out":
            con.execute("INSERT OR IGNORE INTO opt_outs (key, name, email, since) VALUES (?, ?, ?, ?)",
                        (row["key"], row["name"], row["email"], date.today().isoformat()))
        else:
            # changed by mistake: take the opt-out back off
            con.execute("DELETE FROM opt_outs WHERE key = ?", (row["key"],))
    if date_contacted is not None:
        con.execute("UPDATE leads SET date_contacted = ? WHERE id = ?", (date_contacted or None, lead_id))
    if notes is not None:
        con.execute("UPDATE leads SET notes = ? WHERE id = ?", (notes, lead_id))
    con.commit()


def missing_directors(con, list_id):
    # company numbers in a list that have no director name yet
    rows = con.execute("""SELECT id, company_number FROM leads
                          WHERE list_id = ? AND company_number != '' AND (director IS NULL OR director = '')""",
                       (list_id,)).fetchall()
    return [(r["id"], r["company_number"]) for r in rows]


def set_director(con, lead_id, director):
    con.execute("UPDATE leads SET director = ? WHERE id = ?", (director, lead_id))
    con.commit()


# ---------------------------------------------------------
# emailing a list
# ---------------------------------------------------------

DAILY_CAP = 40          # most emails sent in one day across every list, to protect your domain
RECENT_DAYS = 30        # don't email the same business again from another list within this many days


def get_template(con, list_id, defaults):
    """The list's email template, or the starting one for its purpose."""
    row = con.execute("SELECT * FROM templates WHERE list_id = ?", (list_id,)).fetchone()
    if row:
        return dict(row)
    purpose = con.execute("SELECT purpose FROM lists WHERE id = ?", (list_id,)).fetchone()["purpose"]
    base = defaults.get(purpose) or defaults["Other"]
    return {"list_id": list_id, **base, "daily_limit": 15, "auto_send": 0}


def save_template(con, list_id, subject, body, follow_subject, follow_body, daily_limit=15, auto_send=False):
    con.execute("""
        INSERT INTO templates (list_id, subject, body, follow_subject, follow_body, daily_limit, auto_send)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(list_id) DO UPDATE SET subject=excluded.subject, body=excluded.body,
            follow_subject=excluded.follow_subject, follow_body=excluded.follow_body,
            daily_limit=excluded.daily_limit, auto_send=excluded.auto_send
    """, (list_id, subject, body, follow_subject, follow_body, int(daily_limit), int(bool(auto_send))))
    con.commit()


def auto_send_lists(con):
    return [r["list_id"] for r in con.execute("SELECT list_id FROM templates WHERE auto_send = 1")]


def sent_today(con, today=None):
    today = (today or date.today()).isoformat()
    return con.execute("SELECT COUNT(*) FROM send_log WHERE ok = 1 AND sent_at LIKE ?",
                       (today + "%",)).fetchone()[0]


def _recently_emailed(con, today):
    # businesses emailed from any list in the last RECENT_DAYS days
    since = (today - timedelta(days=RECENT_DAYS)).isoformat()
    rows = con.execute("SELECT lead_key, email FROM send_log WHERE ok = 1 AND sent_at >= ?", (since,)).fetchall()
    return {r["lead_key"] for r in rows}, {(r["email"] or "").lower() for r in rows}


def due_first_emails(con, list_id, limit, today=None):
    """Leads ready for a first email: not contacted, have an email, safe to email, not opted out,
    and not emailed from another list recently."""
    today = today or date.today()
    out_keys, out_emails = opted_out_keys(con)
    recent_keys, recent_emails = _recently_emailed(con, today)
    rows = con.execute("""SELECT * FROM leads WHERE list_id = ? AND status = 'Not contacted'
                          AND email != '' AND ok_to_email = 'Yes' ORDER BY id""", (list_id,)).fetchall()
    due = []
    for r in rows:
        email = r["email"].lower()
        if r["key"] in out_keys or email in out_emails or r["key"] in recent_keys or email in recent_emails:
            continue
        due.append(dict(r))
        if len(due) >= limit:
            break
    return due


def due_follow_ups(con, list_id, today=None):
    """Contacted at least FOLLOW_UP_DAYS ago, no reply marked, no follow-up sent yet."""
    today = today or date.today()
    cutoff = (today - timedelta(days=FOLLOW_UP_DAYS)).isoformat()
    out_keys, out_emails = opted_out_keys(con)
    rows = con.execute("""SELECT * FROM leads WHERE list_id = ? AND status = 'Contacted'
                          AND email != '' AND (follow_up_sent IS NULL OR follow_up_sent = '')
                          AND date_contacted IS NOT NULL AND date_contacted <= ? ORDER BY id""",
                       (list_id, cutoff)).fetchall()
    return [dict(r) for r in rows if r["key"] not in out_keys and r["email"].lower() not in out_emails]


def record_send(con, lead, kind, ok, error="", now=None, message_id=""):
    """Log one email and move the lead on: first email -> Contacted, follow-up -> Followed up."""
    now = now or datetime.now()
    con.execute("""INSERT INTO send_log (lead_id, lead_key, email, kind, sent_at, ok, error, message_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (lead["id"], lead["key"], lead["email"], kind, now.isoformat(timespec="seconds"), int(ok), error,
                 message_id))
    if ok and kind == "first":
        con.execute("UPDATE leads SET status = 'Contacted', date_contacted = ? WHERE id = ?",
                    (now.date().isoformat(), lead["id"]))
    elif ok and kind == "follow_up":
        con.execute("UPDATE leads SET status = 'Followed up', follow_up_sent = ? WHERE id = ?",
                    (now.date().isoformat(), lead["id"]))
    con.commit()


def send_log(con, list_id, limit=50):
    rows = con.execute("""SELECT s.sent_at, d.name, s.email, s.kind, s.ok, s.error
                          FROM send_log s JOIN leads d ON d.id = s.lead_id
                          WHERE d.list_id = ? ORDER BY s.id DESC LIMIT ?""", (list_id, limit)).fetchall()
    return [dict(r) for r in rows]


def list_purpose(con, list_id):
    row = con.execute("SELECT purpose FROM lists WHERE id = ?", (list_id,)).fetchone()
    return row["purpose"] if row else "Other"


# ---------------------------------------------------------
# replies in your inbox
# ---------------------------------------------------------

def already_seen(con, message_id):
    return con.execute("SELECT 1 FROM inbox_seen WHERE message_id = ?", (message_id,)).fetchone() is not None


def mark_seen(con, message_id):
    con.execute("INSERT OR IGNORE INTO inbox_seen (message_id, seen_at) VALUES (?, ?)",
                (message_id, datetime.now().isoformat(timespec="seconds")))
    con.commit()


def leads_for_reply(con, from_email, reply_to_ids=()):
    """Every lead (in any list) a reply belongs to: the email it was sent to, one of the
    business's other addresses, or the email it answers (when someone replies from another address)."""
    from_email = (from_email or "").strip().lower()
    keys = set()
    if from_email:
        for r in con.execute("""SELECT key FROM leads WHERE lower(email) = ?
                                OR (',' || replace(lower(other_emails), ' ', '') || ',') LIKE ?""",
                             (from_email, f"%,{from_email},%")):
            keys.add(r["key"])
    for mid in reply_to_ids:
        r = con.execute("SELECT lead_key FROM send_log WHERE message_id = ?", (mid,)).fetchone()
        if r:
            keys.add(r["lead_key"])
    if not keys:
        return []
    marks = ",".join("?" * len(keys))
    return [dict(r) for r in con.execute(f"SELECT * FROM leads WHERE key IN ({marks}) ORDER BY id", tuple(keys))]
