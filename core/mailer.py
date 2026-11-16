"""Writing and sending outreach emails, one person per email, through your own mailbox.

Every email is addressed to one person only (no CC or BCC), personalised with their
first name and company, and goes out as plain text plus a light HTML version that
carries the buttons and the branded signature.
"""
import html
import re
import smtplib
import ssl
import urllib.parse
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

try:
    # a free library of about 40,000 first names from many countries, used offline
    import gender_guesser.detector as _names
    _detector = _names.Detector(case_sensitive=False)
except ImportError:  # still works without it, using director names only
    _detector = None


def is_first_name(word):
    """True if the name library knows this word as someone's first name."""
    if not _detector or len(word) < 2:
        return False
    return _detector.get_gender(word) != "unknown"


# shared inboxes, never a person's name
GENERIC_BOXES = {
    "info", "office", "hello", "hi", "contact", "admin", "sales", "enquiries", "enquiry", "accounts", "mail",
    "support", "team", "help", "service", "services", "bookings", "booking", "jobs", "hr", "reception", "quotes",
    "general", "post", "email", "web", "website", "design", "studio",
}

UNSUBSCRIBE_LINE = "Not for you? Reply \"unsubscribe\" and I won't contact you again."
NO_THANKS = "No thanks, don't email me again"

LEGAL_SUFFIX = re.compile(r"[\s,.]*\b(limited|ltd|llp|plc)\.?\s*$", re.I)

# starting templates for each list purpose; you can edit them in the app
DEFAULT_TEMPLATES = {
    "Web design": {
        "subject": "A website for {company}?",
        "body": (
            "Hi {first_name},\n\n"
            "I'm Pius, a web designer based in Wigan. I was looking at local {trade} and noticed "
            "{company} doesn't seem to have a website yet.\n\n"
            "Most people now check online before they ring a tradesperson, so a simple site with your "
            "services, photos of your work and a quote form can bring in jobs you're currently missing.\n\n"
            "I build clean, mobile-friendly sites for local trades at a fixed price. Would you be open to "
            "a quick chat, or a free mock-up of what yours could look like?\n\n"
            "Thanks,\nPius"
        ),
        "follow_subject": "Re: A website for {company}?",
        "follow_body": (
            "Hi {first_name},\n\n"
            "Just checking my last email reached you. If a website isn't on your list right now, "
            "no problem at all, I'll leave it there.\n\n"
            "Thanks,\nPius"
        ),
    },
    "TradeReports": {
        "subject": "Quick question about {company}'s invoices",
        "body": (
            "Hi {first_name},\n\n"
            "I'm Pius, a data analyst in Wigan.\n\n"
            "If you keep your invoices in a spreadsheet, you'll know the evening job: filtering out who "
            "hasn't paid, working out how late they are, checking nothing was missed or typed twice, "
            "and adding it all up for your accountant at the end of the year.\n\n"
            "I built TradeReports to do that part for you. Drop in your spreadsheet (or the export from "
            "your job software) and in seconds it shows:\n"
            "- who still owes you, and how late\n"
            "- work that's done but never invoiced\n"
            "- your income by month and VAT quarter\n"
            "- mistakes in the sheet, like the same invoice entered twice\n\n"
            "It's free, there's no sign-up, and your files never leave your computer.\n\n"
            "Would it save {company} some time? An honest opinion would really help.\n\n"
            "Thanks,\nPius"
        ),
        "follow_subject": "Re: Quick question about {company}'s invoices",
        "follow_body": (
            "Hi {first_name},\n\n"
            "Just following up on my note about TradeReports. If it's not for you, no worries, "
            "I won't chase again.\n\n"
            "Thanks,\nPius"
        ),
    },
    "Data service": {
        "subject": "Clear numbers for {company}",
        "body": (
            "Hi {first_name},\n\n"
            "I'm Pius, a data analyst in Wigan. I help small businesses turn their spreadsheets and software "
            "exports into a clear report: what's coming in, who owes them and what's actually making money.\n\n"
            "Would a short business health check be useful for {company}?\n\n"
            "Thanks,\nPius"
        ),
        "follow_subject": "Re: Clear numbers for {company}",
        "follow_body": (
            "Hi {first_name},\n\n"
            "Just checking my last email reached you. Happy to leave it if now isn't a good time.\n\n"
            "Thanks,\nPius"
        ),
    },
}
DEFAULT_TEMPLATES["Other"] = DEFAULT_TEMPLATES["Data service"]

SIGNATURE_HTML = """
<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="font-family:Arial,Helvetica,sans-serif;color:#1f2933;margin-top:18px;">
  <tr>
    <td style="border-left:3px solid #e2784a;padding:2px 0 2px 12px;">
      <div style="font-size:15px;font-weight:bold;color:#1f2933;line-height:1.4;">{name}</div>
      <div style="font-size:13px;color:#52606d;line-height:1.4;">Founder, TradeReports &middot; Wigan</div>
      <div style="font-size:16px;font-weight:bold;line-height:1.6;padding-top:4px;">
        <span style="color:#1f2933;">Trade</span><span style="color:#e2784a;">Reports</span>
      </div>
      <div style="font-size:12px;color:#52606d;line-height:1.4;">Focus on your trade, not spreadsheets.</div>
      <div style="font-size:13px;line-height:1.6;padding-top:4px;">
        <a href="https://tradereports.co.uk/?utm_campaign=email" style="color:#e2784a;text-decoration:none;font-weight:bold;">tradereports.co.uk</a>
        <span style="color:#9aa5b1;">&nbsp;|&nbsp;</span>
        <a href="mailto:{email}" style="color:#1f2933;text-decoration:none;">{email}</a>
      </div>
    </td>
  </tr>
</table>
"""

SIGNATURE_TEXT = "--\n{name}\nFounder, TradeReports · Wigan\ntradereports.co.uk"


def first_name_for(email, director=""):
    """The name to greet someone by, or "" if there's no safe guess.

    Uses the email address when it starts with the director's first name or a name the
    name library knows (adrian.barnett@), otherwise the director's first name from Companies House.
    """
    local = (email or "").split("@")[0].lower()
    token = re.split(r"[._\-+0-9]", local)[0] if local else ""
    director_first = (director or "").split()[0].title() if (director or "").split() else ""
    if token and token not in GENERIC_BOXES:
        if director_first and token == director_first.lower():
            return director_first
        if is_first_name(token):
            return token.title()
    return director_first


def company_short(name):
    # "Jon Wright Electrical Ltd" -> "Jon Wright Electrical"
    return LEGAL_SUFFIX.sub("", (name or "").strip()).strip() or (name or "")


class _Blank(dict):
    # a placeholder the template uses but this lead doesn't have becomes empty, not an error
    def __missing__(self, key):
        return ""


def fill(template, lead):
    first = first_name_for(lead.get("email", ""), lead.get("director", ""))
    values = _Blank(
        first_name=first or "there",
        company=company_short(lead.get("name", "")),
        town=(lead.get("town") or "").title(),
        trade=(lead.get("trade") or "trades").lower(),
    )
    text = (template or "").format_map(values)
    # "Hi there," reads fine; tidy any double spaces left by an empty placeholder
    return re.sub(r"[ \t]{2,}", " ", text)


def body_to_html(text):
    # plain paragraphs to simple HTML: blank line = new paragraph, single line break kept
    paras = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return "".join(
        f'<p style="margin:0 0 14px;">{html.escape(p).replace(chr(10), "<br>")}</p>' for p in paras
    )


SITE = "https://tradereports.co.uk"

# the buttons under each kind of email. A link opens a page; "reply" opens their email app with
# a short reply already written, so they only press Send. {company} is filled in per business.
BUTTONS = {
    "TradeReports": [
        {"label": "Try it free", "link": SITE + "/?utm_campaign=email-try"},
        {"label": "See an example report", "link": SITE + "/example?utm_campaign=email-example"},
    ],
    "Web design": [
        {"label": "Yes, show me a free mock-up",
         "reply": ("Yes please: a free mock-up for {company}",
                   "Yes please, send me a free website mock-up for {company}.")},
    ],
    "Data service": [
        {"label": "Yes, tell me more",
         "reply": ("Yes please: a health check for {company}",
                   "Yes please, tell me more about a business health check for {company}.")},
    ],
}

# a reply whose subject starts with this means they said yes to a button
YES_PREFIX = "Yes please"
UNSUBSCRIBE_SUBJECT = "Unsubscribe"


def mailto(address, subject, body=""):
    query = {"subject": subject}
    if body:
        query["body"] = body
    return f"mailto:{address}?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)


def buttons_for(purpose, lead, sender_email):
    """The buttons for one email: [(label, url)], the main ones first, "No thanks" last."""
    company = company_short(lead.get("name", ""))
    out = []
    for b in BUTTONS.get(purpose, []):
        if "link" in b:
            out.append((b["label"], b["link"]))
        else:
            subject, body = b["reply"]
            out.append((b["label"], mailto(sender_email, subject.format(company=company),
                                           body.format(company=company))))
    out.append((NO_THANKS, mailto(sender_email, UNSUBSCRIBE_SUBJECT,
                                  f"Please don't email {company} again.")))
    return out


def _button_html(label, url, style):
    # table buttons show the same in Outlook, Gmail and phone mail apps
    looks = {
        "main": "background:#e2784a;color:#ffffff;border:1px solid #e2784a;font-size:15px;padding:11px 20px;",
        "second": "background:#ffffff;color:#e2784a;border:1px solid #e2784a;font-size:15px;padding:11px 20px;",
        "quiet": "background:#ffffff;color:#52606d;border:1px solid #cbd2d9;font-size:13px;padding:8px 14px;",
    }[style]
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="display:inline-table;margin:0 8px 8px 0;"><tr><td style="border-radius:6px;">'
        f'<a href="{html.escape(url)}" style="{looks}display:inline-block;border-radius:6px;'
        f'font-family:Arial,Helvetica,sans-serif;font-weight:bold;text-decoration:none;">{html.escape(label)}</a>'
        "</td></tr></table>"
    )


def _buttons_text(buttons):
    # the plain-text version: links written out, reply buttons explained
    lines = []
    for label, url in buttons:
        if label == NO_THANKS:
            continue
        lines.append(f"{label}: {url}" if url.startswith("http") else f"{label}? Just reply \"yes please\".")
    return "\n".join(lines)


SIGN_OFF = re.compile(r"^(thanks|many thanks|thank you|cheers|best|kind regards|regards|all the best)\b", re.I)


def split_sign_off(body):
    # "Thanks,\nPius" at the end: the buttons go just above it
    paras = re.split(r"\n\s*\n", body.strip())
    if len(paras) > 1 and SIGN_OFF.match(paras[-1].strip()):
        return "\n\n".join(paras[:-1]), paras[-1].strip()
    return body.strip(), ""


def build_message(sender_email, sender_name, to_email, subject, body, buttons=None):
    """One email to one person: plain text plus an HTML version with buttons and the signature.

    buttons: [(label, url)] from buttons_for(). Without them, only "No thanks" is shown.
    """
    if buttons is None:
        buttons = buttons_for("", {}, sender_email)
    main = [b for b in buttons if b[0] != NO_THANKS]
    extra = _buttons_text(buttons)
    message, sign_off = split_sign_off(body)
    text = (f"{message}\n\n" + (f"{extra}\n\n" if extra else "") + (f"{sign_off}\n\n" if sign_off else "")
            + f"{UNSUBSCRIBE_LINE}\n\n{SIGNATURE_TEXT.format(name=sender_name)}\n")
    button_row = "".join(_button_html(label, url, "main" if i == 0 else "second")
                         for i, (label, url) in enumerate(main))
    no_thanks = next((url for label, url in buttons if label == NO_THANKS), "")
    html_body = (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.55;color:#1f2933;">'
        + body_to_html(message)
        + (f'<div style="margin:6px 0 16px;">{button_row}</div>' if button_row else "")
        + (body_to_html(sign_off) if sign_off else "")
        + SIGNATURE_HTML.format(name=html.escape(sender_name), email=html.escape(sender_email))
        + (f'<div style="margin:22px 0 0;">{_button_html(NO_THANKS, no_thanks, "quiet")}</div>' if no_thanks else "")
        + "</div>"
    )
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender_email))
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Reply-To"] = sender_email
    msg["Message-ID"] = make_msgid(domain=sender_email.split("@")[-1])
    # lets Gmail and Outlook show their own "Unsubscribe" link at the top
    msg["List-Unsubscribe"] = f"<{mailto(sender_email, UNSUBSCRIBE_SUBJECT)}>"
    msg.set_content(text)
    msg.add_alternative(f"<!DOCTYPE html><html><body>{html_body}</body></html>", subtype="html")
    return msg


def smtp_sender(host, port, user, password):
    """Returns a function that sends one message through your mailbox."""
    def send(msg):
        context = ssl.create_default_context()
        if int(port) == 465:
            with smtplib.SMTP_SSL(host, int(port), context=context, timeout=30) as s:
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, int(port), timeout=30) as s:
                s.starttls(context=context)
                s.login(user, password)
                s.send_message(msg)
    return send
