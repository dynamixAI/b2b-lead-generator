"""Send outreach emails without the app open.

Two ways it runs:
    .venv/bin/python send_daily.py --list 3 --limit 10   # the app's "Send now" button: one list, straight away
    .venv/bin/python send_daily.py                       # the timer, every 5 minutes: sends any list whose
                                                         # chosen time (set in the app) has come
It reads the mailbox details from .streamlit/secrets.toml and writes what it did to data/send.log.
Before sending, it reads replies in your inbox, so anyone who unsubscribed or replied is never emailed again.
"""
import argparse
import os
import sys
import tomllib
from datetime import date, datetime
from pathlib import Path

import core  # noqa: F401  (IPv4 fix)
from core import sendqueue, store
from core.inbox import check_replies
from core.mailer import smtp_sender

HERE = Path(__file__).resolve().parent
SECRETS = HERE / ".streamlit" / "secrets.toml"
LOG = HERE / "data" / "send.log"
LOCK = HERE / "data" / "send.lock"


def log(line):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with LOG.open("a") as f:
        f.write(f"{stamp}  {line}\n")
    # only echo to the screen when someone is watching, so the timer's log doesn't fill with copies
    if sys.stdout.isatty():
        print(f"{stamp}  {line}", flush=True)


def mailbox_settings(secrets):
    missing = [k for k in ("SMTP_USER", "SMTP_PASSWORD") if not secrets.get(k)]
    if missing:
        raise SystemExit(f"Missing in .streamlit/secrets.toml: {', '.join(missing)}")
    return {
        "host": secrets.get("SMTP_HOST", "smtp.ionos.co.uk"),
        "port": int(secrets.get("SMTP_PORT", 587)),
        "user": secrets["SMTP_USER"],
        "password": secrets["SMTP_PASSWORD"],
        "name": secrets.get("SENDER_NAME", "Pius Ajamma"),
        "imap_host": secrets.get("IMAP_HOST", "imap.ionos.co.uk"),
    }


def take_lock():
    """Only one sender at a time. Returns False if another is still running."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        try:
            os.kill(int(LOCK.read_text().strip()), 0)  # still running?
            return False
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # left over from a run that crashed
    LOCK.write_text(str(os.getpid()))
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", type=int, help="send this list straight away (default: lists whose chosen time has come)")
    parser.add_argument("--limit", type=int, help="most first emails for this run (default: the list's daily number)")
    parser.add_argument("--gap", type=float, default=3.0, help="minutes between emails (default 3)")
    args = parser.parse_args(argv)

    con = store.connect()
    now = datetime.now()
    if args.list:
        due = [(args.list, "now")]
    else:
        # the timer: which lists' chosen time has come?
        due = []
        for list_id, why in store.due_lists(con, now):
            if why.startswith("missed"):
                store.mark_ran(con, list_id, why, now)
                log(f"List {list_name(con, list_id)}: missed its send time (computer was off or asleep), "
                    "so nothing was sent. It'll go at the next chosen time.")
            else:
                due.append((list_id, why))
        if not due:
            return 0  # nothing due: stay quiet, this runs every few minutes

    secrets = tomllib.loads(SECRETS.read_text()) if SECRETS.exists() else {}
    box = mailbox_settings(secrets)
    send = smtp_sender(box["host"], box["port"], box["user"], box["password"])
    sender = {"email": box["user"], "name": box["name"]}

    if not take_lock():
        if args.list:
            log("Another send is still running, so this one stopped.")
        return 0  # the timer tries again in a few minutes
    try:
        # mark the scheduled runs as done first, so a crash can't make them repeat
        for list_id, why in due:
            if why != "now":
                store.mark_ran(con, list_id, why, now)
        return send_lists(con, [list_id for list_id, _ in due], args, box, send, sender)
    finally:
        LOCK.unlink(missing_ok=True)


def list_name(con, list_id):
    row = con.execute("SELECT name FROM lists WHERE id = ?", (list_id,)).fetchone()
    return f"'{row['name']}'" if row else str(list_id)


def send_lists(con, lists, args, box, send, sender):
    # replies first: someone who said "unsubscribe" yesterday must not get a follow-up today
    try:
        counts = check_replies(con, box["imap_host"], box["user"], box["password"], on_event=log)
        log(f"Inbox checked: {counts['opted_out']} opted out, {counts['interested']} said yes, "
            f"{counts['replied']} replied, {counts['bounced']} bounced.")
    except Exception as e:
        log(f"Couldn't read your inbox ({e}), so nothing was sent today, to be safe.")
        return 1

    for list_id in lists:
        template = store.get_template(con, list_id, sendqueue.DEFAULT_TEMPLATES)
        limit = args.limit if args.limit is not None else template["daily_limit"]
        name = con.execute("SELECT name FROM lists WHERE id = ?", (list_id,)).fetchone()
        if name is None:
            continue
        log(f"List '{name['name']}': sending up to {limit} new emails plus any follow-ups due.")
        sent, failed = sendqueue.run(con, list_id, sender, send, limit, gap_minutes=args.gap, on_event=log)
        log(f"List '{name['name']}': {sent} sent, {failed} failed. {store.sent_today(con)} sent today in total.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
