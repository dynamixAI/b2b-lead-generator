import html
import io
import re

import pandas as pd
import streamlit as st

from core.emails import AbstractChecker
from core.leads import COLUMNS, build_leads, is_closed
from core.places import PlacesError, search_places
from core.website import BROKEN, HAS_SITE, HOLDING, NO_SITE, SOCIAL

st.set_page_config(
    page_title="Lead Finder",
    page_icon="◎",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# =========================================================
# STYLING
# =========================================================
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=DM+Sans:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');

:root {
    --bg: #0D0F12;
    --surface: #16191E;
    --line: #262A31;
    --text: #E8EAED;
    --muted: #8A919C;
    --accent: #C8F169;
}

html, body, [class*="css"], .stMarkdown, input, button, textarea {
    font-family: 'DM Sans', sans-serif !important;
}

[data-testid="stHeader"], [data-testid="stToolbar"], [data-testid="stDecoration"],
[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"], footer {
    display: none !important;
}

.block-container, [data-testid="stMainBlockContainer"] {
    max-width: 880px;
    padding-top: 4.5rem;
    padding-bottom: 5rem;
}

.eyebrow {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.72rem;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--accent);
    margin-bottom: 1.1rem;
}
.eyebrow span { color: var(--muted); }

.hero {
    font-family: 'Instrument Serif', Georgia, serif !important;
    font-weight: 400;
    font-size: clamp(2.6rem, 6vw, 3.9rem);
    line-height: 1.02;
    letter-spacing: -0.01em;
    color: var(--text);
    margin: 0 0 1.1rem 0;
}
.hero em { color: var(--accent); }

.lede {
    color: var(--muted);
    font-size: 1.02rem;
    line-height: 1.6;
    max-width: 560px;
    margin-bottom: 2.4rem;
}

[data-testid="stForm"] {
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 1.4rem 1.4rem 1.1rem;
}

[data-testid="stTextInput"] label p {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.7rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--muted);
}
[data-testid="stTextInput"] input {
    background: var(--bg);
    border-radius: 8px;
    font-size: 1rem;
    padding: 0.75rem 0.9rem;
}

[data-testid="stFormSubmitButton"] button,
[data-testid="stDownloadButton"] button {
    border-radius: 8px;
    font-weight: 600;
    padding: 0.7rem 1rem;
    transition: transform .12s ease, opacity .12s ease, border-color .12s ease;
}
[data-testid="stBaseButton-primaryFormSubmit"] {
    background: var(--accent) !important;
    color: #0D0F12 !important;
    border: none !important;
}
[data-testid="stBaseButton-primaryFormSubmit"]:hover {
    opacity: .9;
    transform: translateY(-1px);
}
[data-testid="stBaseButton-secondaryFormSubmit"],
[data-testid="stDownloadButton"] button {
    background: transparent !important;
    color: var(--text) !important;
    border: 1px solid var(--line) !important;
}
[data-testid="stBaseButton-secondaryFormSubmit"]:hover,
[data-testid="stDownloadButton"] button:hover {
    border-color: var(--accent) !important;
    color: var(--accent) !important;
}

.stats {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 0.75rem;
    margin: 2.6rem 0 1.8rem;
}
.stat {
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 1rem 1.1rem;
}
.stat .num {
    font-family: 'Instrument Serif', Georgia, serif !important;
    font-size: 2.4rem;
    line-height: 1;
    color: var(--text);
}
.stat.hot .num { color: var(--accent); }
.stat .lbl {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.66rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--muted);
    margin-top: 0.5rem;
}
@media (max-width: 640px) {
    .stats { grid-template-columns: repeat(2, 1fr); }
}

.section-title {
    font-family: 'Instrument Serif', Georgia, serif !important;
    font-size: 1.7rem;
    color: var(--text);
    margin: 0.4rem 0 0.2rem;
}
.section-sub { color: var(--muted); font-size: 0.92rem; margin-bottom: 1rem; }

[data-testid="stTabs"] button p { font-weight: 500; }

.note-line {
    color: var(--muted);
    font-size: 0.85rem;
    margin: -0.8rem 0 1.2rem;
}

.status-line {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.78rem;
    color: var(--muted);
}
</style>
""", unsafe_allow_html=True)


def get_secret(name):
    try:
        return st.secrets.get(name, "")
    except Exception:
        return ""


google_api_key = get_secret("GOOGLE_API_KEY")
abstract_api_key = get_secret("ABSTRACT_API_KEY")

for key, default in {"results_df": None, "last_search": ("", ""), "closed_removed": 0}.items():
    if key not in st.session_state:
        st.session_state[key] = default

GROUPS = [
    ("No website", [NO_SITE]),
    ("Social or directory", [SOCIAL]),
    ("Broken or holding", [BROKEN, HOLDING]),
    ("Has website", [HAS_SITE]),
]

TABLE_COLUMNS = [
    "name", "phone", "website_status", "website", "email", "email_source",
    "email_check", "other_emails", "rating", "reviews", "address", "google_maps", "note",
]


def to_excel(df):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.drop(columns=["lat", "lng", "place_id"]).to_excel(writer, sheet_name="All leads", index=False)
        for label, statuses in GROUPS:
            part = df[df["website_status"].isin(statuses)]
            part.drop(columns=["lat", "lng", "place_id"]).to_excel(writer, sheet_name=label, index=False)
    return output.getvalue()


def show_table(df):
    df = df.replace({"website": {"": None}, "google_maps": {"": None}})
    # hide columns that are empty for every row in this group
    columns = [
        c for c in TABLE_COLUMNS
        if c in ("name", "phone", "website_status") or df[c].replace("", None).notna().any()
    ]
    st.dataframe(
        df,
        hide_index=True,
        width="stretch",
        column_order=columns,
        column_config={
            "name": "Business",
            "phone": "Phone",
            "website_status": "Website status",
            "website": st.column_config.LinkColumn("Website"),
            "email": "Email",
            "email_source": "Email source",
            "email_check": "Email check",
            "other_emails": "Other emails",
            "rating": st.column_config.NumberColumn("Rating", format="%.1f ★"),
            "reviews": "Reviews",
            "address": "Address",
            "google_maps": st.column_config.LinkColumn("Maps", display_text="Open"),
            "note": "Note",
        },
    )


def run_search(trade, location):
    query = f"{trade} in {location}, UK"
    status = st.empty()
    status.markdown(
        f'<div class="status-line">Searching Google Places for "{html.escape(query)}"…</div>',
        unsafe_allow_html=True,
    )

    try:
        places = search_places(query, google_api_key)
    except PlacesError as e:
        status.empty()
        st.error(f"Google Places: {e}")
        return None, 0

    open_places = [p for p in places if not is_closed(p)]
    closed = len(places) - len(open_places)
    if not open_places:
        status.empty()
        return pd.DataFrame(columns=COLUMNS), closed

    progress = st.progress(0.0)

    def on_progress(done, total, name):
        progress.progress(done / total)
        status.markdown(
            f'<div class="status-line">Checked {done} of {total} · {html.escape(name)}</div>',
            unsafe_allow_html=True,
        )

    abstract = AbstractChecker(abstract_api_key) if abstract_api_key else None
    rows = build_leads(open_places, trade, location, abstract=abstract, on_progress=on_progress)

    status.empty()
    progress.empty()
    return pd.DataFrame(rows, columns=COLUMNS), closed


# =========================================================
# PAGE
# =========================================================
st.markdown('<div class="eyebrow">DynamixAI <span>/ Lead Finder</span></div>', unsafe_allow_html=True)
st.markdown('<h1 class="hero">Find local trades<br>that <em>need a website.</em></h1>', unsafe_allow_html=True)
st.markdown(
    '<p class="lede">Search any UK town for a trade. Each business is checked for a working '
    'website and a contact email, then split into lists you can download.</p>',
    unsafe_allow_html=True,
)

if not google_api_key:
    st.error("No Google API key found. Add GOOGLE_API_KEY to .streamlit/secrets.toml and restart the app.")
    st.stop()

with st.form("search", border=False):
    c1, c2 = st.columns(2)
    with c1:
        trade = st.text_input("Trade", placeholder="Roofers, electricians, plumbers…")
    with c2:
        location = st.text_input("Town or postcode", placeholder="Wigan, Stockport, WN1…")
    b1, b2 = st.columns([3, 1])
    with b1:
        submitted = st.form_submit_button("Find leads", type="primary", width="stretch")
    with b2:
        cleared = st.form_submit_button("Clear", type="secondary", width="stretch")

if cleared:
    st.session_state.results_df = None

if submitted:
    if not trade.strip() or not location.strip():
        st.warning("Enter both a trade and a town.")
    else:
        result, closed = run_search(trade.strip(), location.strip())
        if result is not None:
            st.session_state.results_df = result
            st.session_state.last_search = (trade.strip(), location.strip())
            st.session_state.closed_removed = closed

df = st.session_state.results_df

if df is not None:
    if df.empty:
        st.info("No open businesses found for that search. Try a nearby town or a broader trade.")
    else:
        last_trade, last_loc = st.session_state.last_search
        counts = {label: int(df["website_status"].isin(s).sum()) for label, s in GROUPS}
        weak = counts["Social or directory"] + counts["Broken or holding"]
        no_site_count = counts["No website"]
        real_emails = int(((df["email"] != "") & (df["email_source"] != "Guessed")).sum())

        st.markdown(f"""
        <div class="stats">
            <div class="stat"><div class="num">{len(df)}</div><div class="lbl">Businesses</div></div>
            <div class="stat hot"><div class="num">{no_site_count}</div><div class="lbl">No website</div></div>
            <div class="stat"><div class="num">{weak}</div><div class="lbl">Weak web presence</div></div>
            <div class="stat"><div class="num">{real_emails}</div><div class="lbl">Emails found</div></div>
        </div>
        """, unsafe_allow_html=True)

        st.markdown(
            f'<div class="section-title">{html.escape(last_trade.title())} in {html.escape(last_loc.title())}</div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="section-sub">Sorted with the strongest prospects first. '
            'Guessed emails are marked, so check them before sending.</div>',
            unsafe_allow_html=True,
        )
        closed = st.session_state.closed_removed
        if closed:
            st.markdown(
                f'<div class="note-line">{closed} permanently closed business{"es" if closed != 1 else ""} left out.</div>',
                unsafe_allow_html=True,
            )

        tabs = st.tabs([f"{label} · {counts[label]}" for label, _ in GROUPS])
        for tab, (label, statuses) in zip(tabs, GROUPS):
            with tab:
                part = df[df["website_status"].isin(statuses)]
                if part.empty:
                    st.caption("Nothing in this group.")
                else:
                    show_table(part)

        slug = f"{re.sub(r'[^a-z0-9]+', '_', last_trade.lower())}_{re.sub(r'[^a-z0-9]+', '_', last_loc.lower())}"
        st.download_button(
            "Download all leads (.xlsx, one sheet per group)",
            data=to_excel(df),
            file_name=f"{slug}_leads.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )
