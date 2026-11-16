"""Send outreach emails without the app open.

Run by a schedule each weekday morning (crontab), or by the app's "Send now" button:
    .venv/bin/python send_daily.py                 # every list with daily sending switched on
    .venv/bin/python send_daily.py --list 3 --limit 10
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
    parser.add_argument("--list", type=int, help="only this list (default: lists with daily sending on)")
    parser.add_argument("--limit", type=int, help="most first emails for this run (default: the list's daily number)")
    parser.add_argument("--gap", type=float, default=3.0, help="minutes between emails (default 3)")
    parser.add_argument("--any-day", action="store_true", help="send at weekends too")
    args = parser.parse_args(argv)

    if not args.list and date.today().weekday() >= 5 and not args.any_day:
        log("Weekend, nothing sent.")
        return 0

    secrets = tomllib.loads(SECRETS.read_text()) if SECRETS.exists() else {}
    box = mailbox_settings(secrets)
    send = smtp_sender(box["host"], box["port"], box["user"], box["password"])
    sender = {"email": box["user"], "name": box["name"]}

    if not take_lock():
        log("Another send is still running, so this one stopped.")
        return 0
    try:
        return send_lists(args, box, send, sender)
    finally:
        LOCK.unlink(missing_ok=True)


def send_lists(args, box, send, sender):
    con = store.connect()
    lists = [args.list] if args.list else store.auto_send_lists(con)
    if not lists:
        log("No lists have daily sending switched on.")
        return 0

    # replies first: someone who said "unsubscribe" yesterday must not get a follow-up today
    try:
        counts = check_replies(con, box["imap_host"], box["user"], box["password"], on_event=log)
        log(f"Inbox checked: {counts['opted_out']} opted out, {counts['interested']} said yes, "
            f"{counts['replied']} replied.")
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
