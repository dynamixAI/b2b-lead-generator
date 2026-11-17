"""Reads replies in your mailbox and updates your lists, so you don't have to.

  - "Unsubscribe", "no thanks", "remove me" and the like -> Opted out, in every list
  - a "Yes please" from one of the email's buttons    -> Interested
  - any other reply                                    -> Replied (so no follow-up goes out)
  - "address not found" bounces                        -> Bad email, in every list (never emailed again)

Out-of-office messages are ignored. It only reads; nothing in your inbox changes,
and emails stay unread.
"""
import email
import email.policy
import hashlib
import imaplib
import re
from datetime import date, timedelta
from email.utils import parseaddr

from core import store
from core.mailer import UNSUBSCRIBE_SUBJECT, YES_PREFIX

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

STOP_WORDS = re.compile(
    r"\b(unsubscribe|no thanks|no thank you|remove me|take me off|stop emailing|stop sending|"
    r"not interested|don'?t (?:email|contact)|do not (?:email|contact))\b", re.I)
AUTO_SUBJECT = re.compile(
    r"out of (?:the )?office|automatic reply|auto.?reply|autoreply|away from|on holiday|annual leave|"
    r"undeliver|delivery status|delivery failure|returned mail|mail delivery", re.I)
BOUNCE_SUBJECT = re.compile(
    r"undeliver|delivery status notification|delivery failure|failure notice|returned mail|"
    r"mail delivery|address not found|could not be delivered|wasn't delivered|not delivered", re.I)
DOMAIN_DEAD = re.compile(
    r"domain not found|host not found|no such domain|host or domain name not found|nxdomain|"
    r"unrouteable|unroutable|dns error|domain does not exist", re.I)
EMAIL_IN_TEXT = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
QUOTE_START = re.compile(
    r"^\s*(>|on .{5,200}wrote:|-{2,}\s*original message|from:\s|sent from my|_{5,})", re.I)

# a reply only moves on leads that were actually emailed
EMAILED = ("Contacted", "Followed up", "Replied")


def _imap_date(d):
    # IMAP wants 16-Nov-2026 whatever language the computer uses
    return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"


def fresh_text(msg):
    """What they wrote, without the quoted copy of your email underneath."""
    part = msg.get_body(("plain",)) or msg.get_body(("html",))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except (LookupError, ValueError):
        return ""
    if part.get_content_type() == "text/html":
        text = re.sub(r"<(br|/p|/div)[^>]*>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    lines = []
    for line in text.splitlines():
        if QUOTE_START.match(line):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def classify(subject, text):
    """"opted_out", "interested" or "replied"."""
    subject = (subject or "").strip()
    if subject.lower().startswith(UNSUBSCRIBE_SUBJECT.lower()) or STOP_WORDS.search(text or ""):
        return "opted_out"
    if subject.lower().startswith(YES_PREFIX.lower()) or (text or "").lower().startswith(YES_PREFIX.lower()):
        return "interested"
    return "replied"


def _is_automatic(msg, from_email):
    if (msg.get("Auto-Submitted") or "no").lower() != "no":
        return True
    if re.match(r"(mailer-daemon|postmaster|no-?reply)@", from_email):
        return True
    return bool(AUTO_SUBJECT.search(msg.get("Subject") or ""))


def _is_bounce(msg, from_email):
    if re.match(r"(mailer-daemon|postmaster)@", from_email):
        return True
    if msg.get_content_type() == "multipart/report":
        return True
    return bool(BOUNCE_SUBJECT.search(msg.get("Subject") or ""))


def bounced_addresses(con, msg):
    """Addresses in a bounce that you actually emailed (from the send log)."""
    try:
        raw = msg.as_bytes().decode("utf-8", "ignore")
    except Exception:
        raw = str(msg.get_payload())
    found = {e.lower().rstrip(".") for e in EMAIL_IN_TEXT.findall(raw)}
    if not found:
        return []
    marks = ",".join("?" * len(found))
    rows = con.execute(f"SELECT DISTINCT lower(email) AS e FROM send_log WHERE ok = 1 AND lower(email) IN ({marks})",
                       tuple(found))
    return sorted(r["e"] for r in rows)


def _mark_bounced(con, addresses, day, domain_dead=False):
    names = []
    for address in addresses:
        # it never arrived, so it doesn't count as contacting them
        con.execute("UPDATE send_log SET ok = 0, error = 'bounced' WHERE lower(email) = ?", (address,))
        for lead in con.execute("SELECT * FROM leads WHERE lower(email) = ?", (address,)).fetchall():
            lead = dict(lead)
            if lead["status"] in ("Opted out", "Bad email"):
                continue
            tried = {r["e"] for r in con.execute(
                "SELECT DISTINCT lower(email) AS e FROM send_log WHERE lead_key = ?", (lead["key"],))} | {address}
            others = [e.strip().lower() for e in (lead["other_emails"] or "").split(",") if e.strip()]
            if domain_dead:
                # the whole domain is gone: only an address somewhere else could work
                dead = address.split("@")[1]
                others = [e for e in others if e.split("@")[-1] != dead]
            nxt = next((e for e in others if e not in tried), "")
            if nxt:
                # another address on their website: use that one next time instead
                rest = ", ".join(e for e in others if e != nxt)
                con.execute("""UPDATE leads SET email = ?, other_emails = ?, status = 'Not contacted',
                               date_contacted = '', follow_up_sent = '' WHERE id = ?""", (nxt, rest, lead["id"]))
                con.commit()
                _add_note(con, lead, f"{day}: {address} bounced, switched to {nxt}")
            else:
                store.update_lead(con, lead["id"], status="Bad email")
                _add_note(con, lead, f"{day}: email bounced, {address} doesn't exist")
            names.append(lead["name"])
    return sorted(set(names))


def _message_id(msg):
    mid = (msg.get("Message-ID") or "").strip()
    if mid:
        return mid
    # no ID: make one from what's there, so it's still only read once
    raw = f"{msg.get('From')}|{msg.get('Date')}|{msg.get('Subject')}".encode()
    return "<" + hashlib.sha1(raw).hexdigest() + "@no-id>"


def _add_note(con, lead, note):
    old = (lead.get("notes") or "").strip()
    store.update_lead(con, lead["id"], notes=f"{old}\n{note}".strip() if old else note)


def handle_message(con, msg, own_address, today=None):
    """Act on one inbox email. Returns (what it was, [business names]) or None if it wasn't a reply."""
    mid = _message_id(msg)
    if store.already_seen(con, mid):
        return None
    store.mark_seen(con, mid)
    from_email = parseaddr(msg.get("From") or "")[1].strip().lower()
    day = (today or date.today()).strftime("%d %b")
    if from_email and _is_bounce(msg, from_email):
        try:
            dead = bool(DOMAIN_DEAD.search(msg.as_bytes().decode("utf-8", "ignore")))
        except Exception:
            dead = False
        names = _mark_bounced(con, bounced_addresses(con, msg), day, domain_dead=dead)
        return ("bounced", names) if names else None
    if not from_email or from_email == (own_address or "").lower() or _is_automatic(msg, from_email):
        return None

    refs = re.findall(r"<[^>]+>", f"{msg.get('In-Reply-To') or ''} {msg.get('References') or ''}")
    leads = store.leads_for_reply(con, from_email, refs)
    if not leads:
        return None

    subject = (msg.get("Subject") or "").strip()
    kind = classify(subject, fresh_text(msg))
    changed = []
    for lead in leads:
        if kind == "opted_out":
            store.update_lead(con, lead["id"], status="Opted out")
            _add_note(con, lead, f"{day}: asked not to be emailed again")
        elif lead["status"] in EMAILED:
            store.update_lead(con, lead["id"], status="Interested" if kind == "interested" else "Replied")
            _add_note(con, lead, f"{day}: replied ({subject[:60]})")
        else:
            continue
        changed.append(lead["name"])
    return kind, sorted(set(changed))


def check_replies(con, host, user, password, days=30, connect=None, on_event=None):
    """Read the last few weeks of your inbox and update the lists. Returns counts of what changed.

    connect: for tests, a function returning an IMAP-like object.
    """
    counts = {"opted_out": 0, "interested": 0, "replied": 0, "bounced": 0}
    box = connect() if connect else imaplib.IMAP4_SSL(host, 993, timeout=30)
    try:
        box.login(user, password)
        box.select("INBOX", readonly=True)  # read-only: nothing is moved or marked read
        since = _imap_date(date.today() - timedelta(days=days))
        _, data = box.search(None, "SINCE", since)
        for num in (data[0] or b"").split():
            _, parts = box.fetch(num, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if not raw:
                continue
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            result = handle_message(con, msg, user)
            if result and result[1]:
                kind, names = result
                counts[kind] += 1
                if on_event:
                    what = {"opted_out": "opted out", "interested": "said yes", "replied": "replied",
                            "bounced": "bounced (bad email address)"}[kind]
                    on_event(f"{', '.join(names)} {what}.")
    finally:
        try:
            box.logout()
        except Exception:
            pass
    return counts
