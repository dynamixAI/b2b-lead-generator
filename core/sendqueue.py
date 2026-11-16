"""Works through a list: follow-ups that are due first, then new first emails, one at a time."""
import random
import time
from datetime import date, datetime

from core import store
from core.mailer import DEFAULT_TEMPLATES, build_message, buttons_for, fill


def plan(con, list_id, limit, today=None):
    """What would be sent now: (follow-ups, first emails), within the list's and the day's limits."""
    today = today or date.today()
    room = max(0, store.DAILY_CAP - store.sent_today(con, today))
    follow = store.due_follow_ups(con, list_id, today)[:room]
    room -= len(follow)
    first = store.due_first_emails(con, list_id, min(limit, room), today) if room > 0 else []
    return follow, first


def render(template, lead, kind):
    subject = template["follow_subject"] if kind == "follow_up" else template["subject"]
    body = template["follow_body"] if kind == "follow_up" else template["body"]
    return fill(subject, lead), fill(body, lead)


def make_message(purpose, template, lead, kind, sender):
    """The finished email for one lead, with the buttons for the list's purpose."""
    subject, body = render(template, lead, kind)
    return build_message(sender["email"], sender["name"], lead["email"], subject, body,
                         buttons_for(purpose, lead, sender["email"]))


def run(con, list_id, sender, send_fn, limit, gap_minutes=3.0, sleep=time.sleep, on_event=None, today=None):
    """Send what's due for one list. sender: {"email", "name"}. Returns (sent, failed).

    Waits gap_minutes (give or take a little) between emails, like a person sending them.
    Stops early if the mailbox refuses, so a wrong password doesn't fail every email.
    """
    template = store.get_template(con, list_id, DEFAULT_TEMPLATES)
    purpose = store.list_purpose(con, list_id)
    follow, first = plan(con, list_id, limit, today)
    queue = [(lead, "follow_up") for lead in follow] + [(lead, "first") for lead in first]
    sent = failed = 0
    for i, (lead, kind) in enumerate(queue):
        if i:
            sleep(max(30.0, gap_minutes * 60 * random.uniform(0.7, 1.3)))
        msg = make_message(purpose, template, lead, kind, sender)
        try:
            send_fn(msg)
        except Exception as e:  # login refused, connection dropped, address rejected...
            store.record_send(con, lead, kind, False, str(e)[:300])
            failed += 1
            if on_event:
                on_event(f"Failed: {lead['name']} ({e})")
            if "auth" in str(e).lower() or "login" in str(e).lower() or "535" in str(e):
                if on_event:
                    on_event("Stopped: the mailbox refused the login. Check SMTP_PASSWORD.")
                break
            continue
        # the Message-ID lets a reply from a different address be matched back to this lead
        store.record_send(con, lead, kind, True, now=datetime.now(), message_id=msg["Message-ID"])
        sent += 1
        if on_event:
            on_event(f"Sent {'follow-up' if kind == 'follow_up' else 'email'} to {lead['name']} <{lead['email']}>")
    return sent, failed
