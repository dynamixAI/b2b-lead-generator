"""The outreach list screens: saving search results to a list, and the My lists page."""
import io
import re
import subprocess
import sys
import urllib.parse
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from core import sendqueue, store
from core.companies import get_director
from core.inbox import check_replies
from core.mailer import DEFAULT_TEMPLATES, buttons_for, smtp_sender

STATUS_HELP = "Opted out is remembered for this business in every list, so it never gets contacted again."


def _list_label(lists, list_id):
    for l in lists:
        if l["id"] == list_id:
            return f'{l["name"]} · {l["purpose"]} · {l["leads"]} leads'
    return str(list_id)


def pick_rows(df, groups, only_email, only_companies):
    # which search results go into the list
    rows = df[df["website_status"].map(store.group_of).isin(groups)]
    if only_email:
        rows = rows[rows["email"].fillna("") != ""]
    if only_companies:
        rows = rows[rows["ok_to_email"] == "Yes"]
    return rows


def look_up_directors(rows, ch_api_key, progress=None):
    # {lead key: director name}, one Companies House call per company
    found = {}
    todo = [r for r in rows if (r.get("company_number") or "").strip()]
    for i, row in enumerate(todo, start=1):
        name = get_director(row["company_number"], ch_api_key)
        if name:
            found[store.lead_key(row)] = name
        if progress:
            progress(i, len(todo))
    return found


# ---------------------------------------------------------
# under the search results
# ---------------------------------------------------------

def save_panel(con, df, trade, town, ch_api_key):
    st.markdown('<div class="section-title">Save to an outreach list</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="section-sub">Pick who to keep, then start a new list or add to one you have. '
        "Businesses already on that list are skipped.</div>",
        unsafe_allow_html=True,
    )
    lists = store.get_lists(con)

    with st.form("save_list", border=False):
        groups = st.multiselect(
            "Which businesses", store.GROUPS, default=[store.GROUP_NO_SITE, store.GROUP_WEAK],
            help="No website and Weak website are for a website pitch. Has website suits TradeReports or the data service.",
        )
        c1, c2 = st.columns(2)
        with c1:
            only_email = st.checkbox("Only those with an email")
        with c2:
            only_companies = st.checkbox("Only limited companies", disabled=not ch_api_key,
                                         help="Only these can be cold emailed without asking first.")
        where = st.radio("Save to", ["Start a new list", "Add to a list I have"],
                         index=1 if lists else 0, horizontal=True)
        c3, c4 = st.columns([2, 1])
        with c3:
            new_name = st.text_input("New list name", value=f"{trade.title()} in {town.title()}")
        with c4:
            purpose = st.selectbox("Purpose", store.PURPOSES)
        existing = st.selectbox(
            "List I have", [l["id"] for l in lists], format_func=lambda i: _list_label(lists, i),
            index=0 if lists else None, placeholder="No lists yet",
        )
        directors = st.checkbox("Look up each company's director, for letters", value=bool(ch_api_key),
                                disabled=not ch_api_key)
        go = st.form_submit_button("Save to list", type="primary", width="stretch")

    if not go:
        return
    if not groups:
        st.warning("Pick at least one group.")
        return
    rows = pick_rows(df, groups, only_email, only_companies)
    if rows.empty:
        st.warning("None of these results match what you picked.")
        return

    try:
        if where == "Start a new list":
            list_id = store.create_list(con, new_name, purpose)
        elif existing is None:
            st.warning("Choose which list to add to, or start a new one.")
            return
        else:
            list_id = existing
    except ValueError as e:
        st.error(str(e))
        return

    records = rows.to_dict("records")
    found = {}
    if directors:
        bar = st.progress(0.0, text="Looking up directors…")
        found = look_up_directors(records, ch_api_key,
                                  lambda i, n: bar.progress(i / n, text=f"Looking up directors… {i} of {n}"))
        bar.empty()

    added, already, opted = store.add_leads(con, list_id, records, trade=trade, town=town, directors=found)
    msg = f"Added {added} to the list."
    if already:
        msg += f" {already} were already on it."
    if opted:
        msg += f" {opted} opted out before, so they're marked Opted out."
    st.success(msg + " Open **My lists** at the top to work through them.")


# ---------------------------------------------------------
# My lists page
# ---------------------------------------------------------

def _search_link(site, name, town):
    q = urllib.parse.quote(f"{name} {town}".strip())
    if site == "facebook":
        return f"https://www.facebook.com/search/pages/?q={q}"
    return f"https://www.google.com/search?q={q}"


def _to_date(value):
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _save_edits(con, editor_key, ids, version_key):
    # runs when you change a cell: save it, then redraw the table from the database
    edits = st.session_state.get(editor_key, {}).get("edited_rows", {})
    for row_index, changes in edits.items():
        lead_id = int(ids[int(row_index)])
        when = changes.get("date_contacted", ...)
        store.update_lead(
            con, lead_id,
            status=changes.get("status"),
            date_contacted=None if when is ... else (str(when)[:10] if when else ""),
            notes=changes.get("notes"),
        )
        # marking someone Contacted with no date: assume today
        if changes.get("status") == "Contacted" and when is ...:
            row = con.execute("SELECT date_contacted FROM leads WHERE id = ?", (lead_id,)).fetchone()
            if not row["date_contacted"]:
                store.update_lead(con, lead_id, date_contacted=date.today().isoformat())
    st.session_state[version_key] = st.session_state.get(version_key, 0) + 1


def _address_key(address):
    return re.sub(r"[^a-z0-9]", "", (address or "").lower())


def flags_for(view, whole_list, today=None):
    """Short warnings for each lead: an address several companies share is usually their
    accountant's (a letter may not reach them), and a company under a year old is new."""
    today = today or date.today()
    counts = whole_list["address"].map(_address_key).value_counts()
    year_ago = (today - timedelta(days=365)).isoformat()
    flags = []
    for address, founded in zip(view["address"], view["founded"]):
        f = []
        key = _address_key(address)
        if key and counts.get(key, 0) > 1:
            f.append("Shared address (likely accountant)")
        if founded and str(founded)[:10] >= year_ago:
            f.append("New company")
        flags.append("; ".join(f))
    return flags


def list_to_excel(leads):
    output = io.BytesIO()
    df = pd.DataFrame(leads).drop(columns=["id"], errors="ignore")
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="All", index=False)
        for grp in store.GROUPS:
            part = df[df["grp"] == grp] if "grp" in df else df.iloc[0:0]
            if not part.empty:
                part.to_excel(writer, sheet_name=grp, index=False)
    return output.getvalue()


def lists_page(con, ch_api_key, mailbox=None):
    lists = store.get_lists(con)
    if not lists:
        st.info("No lists yet. Run a search on **Find leads**, then use **Save to an outreach list** under the results.")
        return

    list_id = st.selectbox("List", [l["id"] for l in lists], format_func=lambda i: _list_label(lists, i))
    info = next(l for l in lists if l["id"] == list_id)
    leads = store.load_leads(con, list_id)
    if not leads:
        st.caption("This list is empty.")
    df = pd.DataFrame(leads, columns=store.LEAD_COLUMNS + ["follow_up"])

    today = date.today().isoformat()
    due = int(((df["follow_up"] != "") & (df["follow_up"] <= today)).sum())
    contacted = int(df["status"].isin(["Contacted", "Followed up", "Replied", "Interested", "Not interested"]).sum())
    keen = int((df["status"] == "Interested").sum())
    st.markdown(f"""
    <div class="stats">
        <div class="stat"><div class="num">{len(df)}</div><div class="lbl">Leads · {info["purpose"]}</div></div>
        <div class="stat"><div class="num">{contacted}</div><div class="lbl">Contacted</div></div>
        <div class="stat hot"><div class="num">{due}</div><div class="lbl">Follow-ups due</div></div>
        <div class="stat"><div class="num">{keen}</div><div class="lbl">Interested</div></div>
    </div>
    """, unsafe_allow_html=True)

    counts = {g: int((df["grp"] == g).sum()) for g in store.GROUPS}
    show = st.radio("Show", ["All"] + store.GROUPS, horizontal=True,
                    format_func=lambda g: f"{g} · {len(df) if g == 'All' else counts[g]}")
    view = df if show == "All" else df[df["grp"] == show]
    if st.toggle("Only follow-ups due"):
        view = view[(view["follow_up"] != "") & (view["follow_up"] <= today)]
    view = view.reset_index(drop=True).copy()

    view["flags"] = flags_for(view, df)
    view["date_contacted"] = view["date_contacted"].map(_to_date)
    view["follow_up"] = view["follow_up"].map(_to_date)
    view["facebook"] = [_search_link("facebook", n, t or "") for n, t in zip(view["name"], view["town"])]
    view["google"] = [_search_link("google", n, t or "") for n, t in zip(view["name"], view["town"])]

    version_key = f"lists_version_{list_id}"
    editor_key = f"lists_editor_{list_id}_{show}_{st.session_state.get(version_key, 0)}"
    st.data_editor(
        view,
        key=editor_key,
        hide_index=True,
        width="stretch",
        column_order=["name", "contact_by", "status", "date_contacted", "follow_up", "notes", "email",
                      "email_source", "phone",
                      "director", "address", "flags", "facebook", "google", "website", "grp", "founded",
                      "job_software", "vat_registered", "other_emails", "company_number"],
        disabled=[c for c in view.columns if c not in ("status", "date_contacted", "notes")],
        column_config={
            "name": "Business",
            "contact_by": "Contact by",
            "status": st.column_config.SelectboxColumn("Status", options=store.STATUSES, required=True,
                                                       help=STATUS_HELP),
            "date_contacted": st.column_config.DateColumn("Contacted on", format="D MMM YYYY"),
            "follow_up": st.column_config.DateColumn("Follow up on", format="D MMM YYYY"),
            "notes": st.column_config.TextColumn("Notes", width="medium"),
            "email": "Email",
            "email_source": st.column_config.TextColumn(
                "Email from", help="Guessed addresses (info@ + their website) are never emailed automatically"),
            "phone": "Phone",
            "director": "Director",
            "address": "Address",
            "facebook": st.column_config.LinkColumn("Facebook", display_text="Search"),
            "google": st.column_config.LinkColumn("Google", display_text="Search"),
            "website": st.column_config.LinkColumn("Website"),
            "grp": "Group",
            "other_emails": "Other emails",
            "company_number": "Company no.",
            "flags": "Flags",
            "job_software": "Job software",
            "vat_registered": "VAT registered",
            "founded": "Founded",
        },
        on_change=_save_edits,
        args=(con, editor_key, view["id"].tolist(), version_key),
    )
    st.caption("Changes save as you make them. Setting Contacted fills in today's date if it's empty; "
               "the follow-up date is a week later.")

    c1, c2 = st.columns(2)
    with c1:
        st.download_button("Download this list (.xlsx)", data=list_to_excel(leads),
                           file_name=f"{info['name'].lower().replace(' ', '_')}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           width="stretch", disabled=not leads)
    with c2:
        missing = store.missing_directors(con, list_id)
        if st.button(f"Look up directors ({len(missing)} missing)", width="stretch",
                     disabled=not (missing and ch_api_key)):
            bar = st.progress(0.0, text="Looking up directors…")
            for i, (lead_id, number) in enumerate(missing, start=1):
                name = get_director(number, ch_api_key)
                if name:
                    store.set_director(con, lead_id, name)
                bar.progress(i / len(missing), text=f"Looking up directors… {i} of {len(missing)}")
            st.session_state[version_key] = st.session_state.get(version_key, 0) + 1
            st.rerun()

    email_section(con, list_id, info, mailbox)

    with st.expander("Delete this list"):
        st.caption("Removes the list and its notes. Opt-outs are kept, so they still apply to future lists.")
        sure = st.checkbox(f'Yes, delete "{info["name"]}"')
        if st.button("Delete list", disabled=not sure):
            store.delete_list(con, list_id)
            st.rerun()


# ---------------------------------------------------------
# emailing a list
# ---------------------------------------------------------

APP_DIR = Path(__file__).resolve().parent
PLACEHOLDER_HELP = ("Use {first_name}, {company}, {trade} and {town}: each email gets that person's details. "
                    "The opt-out line and your branded signature are added to every email automatically.")


def _sending_now():
    # the background sender holds this file while it runs
    return (APP_DIR / "data" / "send.lock").exists()


def email_section(con, list_id, info, mailbox):
    st.markdown('<div class="section-title">Email this list</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="section-sub">One email per person, sent from your own mailbox a few minutes apart. '
        "Only limited companies with an email who haven't been contacted or opted out.</div>",
        unsafe_allow_html=True,
    )
    tpl = store.get_template(con, list_id, DEFAULT_TEMPLATES)

    with st.form(f"template_{list_id}", border=False):
        subject = st.text_input("Subject", tpl["subject"])
        body = st.text_area("Email", tpl["body"], height=260, help=PLACEHOLDER_HELP)
        with st.expander("Follow-up (sent once, 7 days later, if they haven't replied)"):
            follow_subject = st.text_input("Follow-up subject", tpl["follow_subject"])
            follow_body = st.text_area("Follow-up email", tpl["follow_body"], height=140)
        daily = st.number_input("New emails per day", 1, 50, int(tpl["daily_limit"]),
                                help=f"Keep it low while your domain is new. At most {store.DAILY_CAP} a day go out across all lists.")
        st.caption(PLACEHOLDER_HELP)
        if st.form_submit_button("Save email", width="stretch"):
            store.save_template(con, list_id, subject, body, follow_subject, follow_body, daily,
                                bool(tpl["auto_send"]))
            st.success("Saved.")
            tpl = store.get_template(con, list_id, DEFAULT_TEMPLATES)

    follow, first = sendqueue.plan(con, list_id, int(tpl["daily_limit"]))
    today_count = store.sent_today(con)
    st.markdown(
        f"**Ready now:** {len(first)} new email{'s' if len(first) != 1 else ''} and "
        f"{len(follow)} follow-up{'s' if len(follow) != 1 else ''}. "
        f"Sent today across all lists: {today_count} of {store.DAILY_CAP}."
    )

    sample = (follow + first)[:3]
    if sample:
        with st.expander("Preview what they'll get", expanded=False):
            for lead in sample:
                kind = "follow_up" if lead in follow else "first"
                subj, text = sendqueue.render(tpl, lead, kind)
                st.markdown(f"**To:** {lead['email']}  \n**Subject:** {subj}")
                st.text(text)
                labels = [label for label, _ in buttons_for(info["purpose"], lead, mailbox["user"] if mailbox else "")]
                st.caption("Buttons: " + " · ".join(labels))
                st.divider()

    if not mailbox:
        st.info("To send from the app, add SMTP_USER and SMTP_PASSWORD (your IONOS email login) "
                "to .streamlit/secrets.toml.")
        return

    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("Send a test to myself", width="stretch", disabled=not sample):
            lead = sample[0]
            sender = {"email": mailbox["user"], "name": mailbox["name"]}
            msg = sendqueue.make_message(info["purpose"], tpl, lead, "first", sender)
            # the real email, buttons and all, but addressed to you
            subj = msg["Subject"]
            del msg["To"], msg["Subject"]
            msg["To"], msg["Subject"] = mailbox["user"], f"[Test] {subj}"
            try:
                smtp_sender(mailbox["host"], mailbox["port"], mailbox["user"], mailbox["password"])(msg)
                st.success(f"Test sent to {mailbox['user']}, written as if to {lead['name']}.")
            except Exception as e:
                st.error(f"Couldn't send: {e}")
    with c2:
        if st.button("Check replies now", width="stretch",
                     help="Reads your inbox: unsubscribes become Opted out, other replies become Replied. "
                          "This also runs before every send."):
            events = []
            try:
                counts = check_replies(con, mailbox.get("imap_host", "imap.ionos.co.uk"), mailbox["user"],
                                       mailbox["password"], on_event=events.append)
                st.success(f"{counts['opted_out']} opted out, {counts['interested']} said yes, "
                           f"{counts['replied']} replied, {counts['bounced']} bounced." + ("  \n" + "  \n".join(events) if events else ""))
                st.session_state[f"lists_version_{list_id}"] = st.session_state.get(f"lists_version_{list_id}", 0) + 1
            except Exception as e:
                st.error(f"Couldn't read your inbox: {e}")
    with c3:
        busy = _sending_now()
        if st.button("Sending…" if busy else f"Send now ({len(first) + len(follow)})", type="primary",
                     width="stretch", disabled=busy or not (first or follow)):
            # runs in the background, so it carries on if you close this page
            log = (APP_DIR / "data" / "send.log").open("a")
            subprocess.Popen(
                [sys.executable, str(APP_DIR / "send_daily.py"), "--list", str(list_id),
                 "--limit", str(int(tpl["daily_limit"]))],
                cwd=APP_DIR, stdout=log, stderr=log, start_new_session=True,
            )
            st.success("Sending in the background, about 3 minutes apart. Refresh this page to see progress below.")

    schedule_section(con, list_id, tpl)

    log_rows = store.send_log(con, list_id, 30)
    if log_rows:
        with st.expander(f"Sent from this list ({len(log_rows)} most recent)"):
            st.dataframe(pd.DataFrame(log_rows).rename(columns={
                "sent_at": "When", "name": "Business", "email": "Email", "kind": "Type", "ok": "Sent", "error": "Problem"}),
                hide_index=True, width="stretch")


# ---------------------------------------------------------
# sending at a time you choose
# ---------------------------------------------------------

TIMER_LINE = "*/5 * * * * cd {dir} && .venv/bin/python send_daily.py >> data/cron.log 2>&1"
MODES = {"manual": "Only when I click Send now", "weekdays": "Every weekday at a time I choose",
         "once": "Once, on a day and time I choose"}


def timer_installed():
    # the computer's timer (crontab) that checks every 5 minutes whether a list is due
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "send_daily.py" in out


def install_timer():
    try:
        current = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        current = ""
    keep = [l for l in current.splitlines() if "send_daily.py" not in l]
    new = "\n".join(keep + [TIMER_LINE.format(dir=APP_DIR)]) + "\n"
    subprocess.run(["crontab", "-"], input=new, text=True, check=True, timeout=5)


def _hhmm(value):
    try:
        h, m = (int(x) for x in (value or store.DEFAULT_SEND_TIME).split(":")[:2])
        return time(h, m)
    except ValueError:
        return time(8, 45)


def schedule_text(tpl):
    if tpl.get("once_at"):
        when = datetime.fromisoformat(tpl["once_at"])
        return f"Scheduled once: {when:%a %d %b} at {when:%H:%M}."
    if tpl.get("auto_send"):
        return f"Scheduled: every weekday (Mon to Fri) at {_hhmm(tpl.get('send_time')):%H:%M}."
    return "Not scheduled: emails only go when you click Send now."


def schedule_section(con, list_id, tpl):
    st.markdown("**When to send**")
    current = "once" if tpl.get("once_at") else ("weekdays" if tpl.get("auto_send") else "manual")
    mode = st.radio("When to send", list(MODES), index=list(MODES).index(current), format_func=MODES.get,
                    key=f"mode_{list_id}", label_visibility="collapsed")
    send_time, once_at = tpl.get("send_time") or store.DEFAULT_SEND_TIME, ""
    if mode == "weekdays":
        t = st.time_input("Time", _hhmm(send_time), step=timedelta(minutes=5), key=f"time_{list_id}")
        send_time = f"{t:%H:%M}"
    elif mode == "once":
        old = datetime.fromisoformat(tpl["once_at"]) if tpl.get("once_at") else None
        tomorrow = date.today() + timedelta(days=1)
        c1, c2 = st.columns(2)
        with c1:
            d = st.date_input("Day", old.date() if old else tomorrow, min_value=date.today(),
                              format="DD/MM/YYYY", key=f"day_{list_id}")
        with c2:
            t = st.time_input("Time", old.time() if old else _hhmm(send_time), step=timedelta(minutes=5),
                              key=f"once_time_{list_id}")
        once_at = datetime.combine(d, t).isoformat(timespec="minutes")

    if st.button("Save when to send", width="stretch", key=f"save_schedule_{list_id}"):
        if mode == "once" and datetime.fromisoformat(once_at) <= datetime.now():
            st.error("That time has already passed. Pick a later time, or use Send now.")
        else:
            store.save_schedule(con, list_id, DEFAULT_TEMPLATES, mode, send_time, once_at)
            tpl = store.get_template(con, list_id, DEFAULT_TEMPLATES)
            st.success(schedule_text(tpl))
    st.caption(schedule_text(tpl) + " The computer must be on and awake then. If it was asleep, the "
               f"emails go when it wakes, up to {store.LATE_HOURS} hours late; after that it waits for the next time.")

    if mode != "manual" and not timer_installed():
        st.warning("This computer's timer isn't set up yet, so scheduled emails won't go. "
                   "It's a one-off: it checks every 5 minutes whether a list is due.")
        if st.button("Set up the timer", key=f"timer_{list_id}"):
            try:
                install_timer()
                st.success("Timer set up. Scheduled emails will now go at their times.")
            except Exception as e:
                st.error(f"Couldn't set up the timer: {e}")
