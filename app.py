import re
import socket
import time
import urllib.parse
import requests
import pandas as pd
import streamlit as st

# =========================================================
# PAGE CONFIG & UI STYLING
# =========================================================
st.set_page_config(page_title="UK B2B Lead Generator", page_icon="🎯", layout="centered")

st.title("🎯 UK B2B Lead Generator & Verifier")
st.write("Extract local trade listings, crawl custom domains, and verify email deliverability.")

# Sidebar Configuration
st.sidebar.header("API Configurations")
google_api_key = st.sidebar.text_input(
    "Google Cloud API Key", 
    value="YOUR_GOOGLE_PLACES_API_KEY_HERE", 
    type="password"
)
abstract_api_key = st.sidebar.text_input(
    "Abstract Email Verification API Key", 
    value="", 
    type="password", 
    help="Enter your Abstract API key to verify email deliverability."
)

# Main Inputs
trade = st.text_input("Trade / Service", placeholder="e.g. Roofers, Electricians, Plumbers")
location = st.text_input("Town / Postcode", placeholder="e.g. Wigan, Stockport, Oxford")

# =========================================================
# BROWSER EMULATION HEADERS & REGEX
# =========================================================
BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8',
    'Sec-Ch-Ua': '"Not-A.Brand";v="99", "Chromium";v="124", "Google Chrome";v="124"',
    'Sec-Ch-Ua-Mobile': '?0',
    'Sec-Ch-Ua-Platform': '"Windows"',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'Upgrade-Insecure-Requests': '1'
}

EMAIL_REGEX = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
INVALID_EXTS = ('.png', '.jpg', '.jpeg', '.svg', '.gif', '.webp', '.css', '.js')

# =========================================================
# ADVANCED SCRAPING & VERIFICATION FUNCTIONS
# =========================================================
def extract_valid_emails(html_text):
    """Filters out web assets and extracts clean email addresses from raw HTML."""
    if not html_text:
        return set()
    matches = set(re.findall(EMAIL_REGEX, html_text))
    return {e for e in matches if not e.lower().endswith(INVALID_EXTS)}

def is_holding_page(html_text):
    """Detects standard blank or under-construction landing pages."""
    if not html_text:
        return True
    holding_phrases = [
        "under construction", 
        "coming soon", 
        "check back soon", 
        "domain for sale", 
        "website under maintenance",
        "parked domain"
    ]
    text_lower = html_text.lower()
    return any(phrase in text_lower for phrase in holding_phrases)

def scrape_website_deep(url):
    """Crawls website home page, subpages, script blocks, and tests custom domain emails."""
    if not url or 'facebook.com' in url or 'instagram.com' in url:
        return None, False
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url

    found_emails = set()
    is_blank = False
    
    urls_to_check = [url, f"{url.rstrip('/')}/contact", f"{url.rstrip('/')}/about"]

    for link in urls_to_check:
        try:
            resp = requests.get(link, headers=BROWSER_HEADERS, timeout=4)
            if resp.status_code == 200:
                if is_holding_page(resp.text):
                    is_blank = True
                found_emails.update(extract_valid_emails(resp.text))
        except Exception:
            continue

    # Custom Domain Fallback: If site is online but blank, construct common domain emails
    clean_domain = url.replace('https://', '').replace('http://', '').replace('www.', '').split('/')[0]
    if not found_emails and '.' in clean_domain:
        found_emails.add(f"info@{clean_domain}")

    return (", ".join(found_emails) if found_emails else None), is_blank

def search_bing_for_emails(business_name, loc, domain_name=None):
    """Searches Bing index for business email mentions across web classifieds & domain queries."""
    found_emails = set()

    query1 = f'"{business_name}" "{loc}" email OR contact'
    bing_url1 = f"https://www.bing.com/search?q={urllib.parse.quote(query1)}"

    try:
        resp = requests.get(bing_url1, headers=BROWSER_HEADERS, timeout=5)
        if resp.status_code == 200:
            found_emails.update(extract_valid_emails(resp.text))
    except Exception:
        pass

    if domain_name and 'facebook.com' not in domain_name and 'instagram.com' not in domain_name:
        clean_domain = domain_name.replace('https://', '').replace('http://', '').replace('www.', '').split('/')[0]
        query2 = f'"{clean_domain}" email OR "info@{clean_domain}" OR "contact@{clean_domain}"'
        bing_url2 = f"https://www.bing.com/search?q={urllib.parse.quote(query2)}"
        
        try:
            resp2 = requests.get(bing_url2, headers=BROWSER_HEADERS, timeout=5)
            if resp2.status_code == 200:
                dom_emails = {e for e in extract_valid_emails(resp2.text) if clean_domain in e.lower()}
                found_emails.update(dom_emails)
        except Exception:
            pass

    return ", ".join(found_emails) if found_emails else "None Found"

def verify_email_abstract(email, api_key):
    """Safely verifies emails across Abstract API endpoints with DNS fallback."""
    if not email or email == "None Found":
        return "N/A - No Email"
    
    primary_email = email.split(',')[0].strip()
    
    # Check basic regex syntax format
    if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', primary_email):
        return "Invalid Format"
    
    if not api_key or api_key == "" or "YOUR_ABSTRACT" in api_key:
        # Direct DNS MX Mail Server check if no API key is set
        try:
            domain = primary_email.split('@')[1]
            socket.gethostbyname(domain)
            return "Valid Domain (DNS Checked)"
        except Exception:
            return "Invalid Domain"

    # Multi-endpoint check: Try Email Validation and Email Reputation APIs
    endpoints = [
        f"https://emailvalidation.abstractapi.com/v1/?api_key={api_key}&email={primary_email}",
        f"https://email-reputation.abstractapi.com/v1/?api_key={api_key}&email={primary_email}"
    ]

    for url in endpoints:
        try:
            resp = requests.get(url, timeout=4)
            if resp.status_code == 200:
                data = resp.json()
                
                # Check deliverability
                deliv = data.get("deliverability")
                if not deliv and isinstance(data.get("email_deliverability"), dict):
                    deliv = data.get("email_deliverability", {}).get("status")
                
                score = data.get("quality_score")
                is_disposable = data.get("is_disposable_email", {}).get("value", False) if isinstance(data.get("is_disposable_email"), dict) else data.get("is_disposable_email", False)
                
                if is_disposable:
                    return "Disposable Email"
                elif deliv and str(deliv).upper() == "DELIVERABLE":
                    return "Valid / Deliverable"
                elif deliv and str(deliv).upper() == "UNDELIVERABLE":
                    return "Invalid Inbox"
                elif score is not None and float(score) >= 0.5:
                    return "Valid / High Reputation"
                else:
                    return "Valid Format"
            elif resp.status_code == 401:
                continue
            elif resp.status_code == 429:
                return "Abstract Limit Reached"
        except Exception:
            continue

    # Fallback to local DNS check if API call fails
    try:
        domain = primary_email.split('@')[1]
        socket.gethostbyname(domain)
        return "Valid Domain (DNS Checked)"
    except Exception:
        return "Invalid Domain"

def to_excel(df):
    """Converts a DataFrame into an Excel file buffer."""
    import io
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False)
    return output.getvalue()

# =========================================================
# RUN SCRAPER ACTION (PAGINATED UP TO 60 LEADS)
# =========================================================
if st.button("🚀 Generate Leads", type="primary"):
    if not trade or not location:
        st.error("Please enter both a Trade and Location.")
    elif not google_api_key or google_api_key == "YOUR_GOOGLE_PLACES_API_KEY_HERE":
        st.error("Please enter a valid Google Places API Key in the sidebar.")
    else:
        search_query = f"{trade} in {location}, UK"
        
        status_box = st.empty()
        status_box.info(f"Searching Google Places for: **'{search_query}'**...")

        new_places_url = "https://places.googleapis.com/v1/places:searchText"
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": google_api_key,
            "X-Goog-FieldMask": "places.displayName,places.nationalPhoneNumber,places.websiteUri,places.rating,places.userRatingCount,places.formattedAddress,nextPageToken"
        }
        
        places = []
        next_page_token = None
        
        # Paginate to fetch up to 3 pages (maximum 60 leads per query)
        for page in range(3):
            payload = {"textQuery": search_query, "pageSize": 20}
            if next_page_token:
                payload["pageToken"] = next_page_token
                
            try:
                response = requests.post(new_places_url, headers=headers, json=payload)
                data = response.json()
                page_places = data.get('places', [])
                places.extend(page_places)
                
                next_page_token = data.get('nextPageToken')
                if not next_page_token:
                    break # Stop early if no more results exist
                    
                time.sleep(1.5) # Required delay for Google page token readiness
            except Exception as e:
                st.error(f"API Request failed: {e}")
                break

        raw_data = []
        progress_bar = st.progress(0)
        
        for idx, place in enumerate(places):
            b_name = place.get('displayName', {}).get('text', 'N/A')
            b_phone = place.get('nationalPhoneNumber', 'N/A')
            b_site = place.get('websiteUri', None)
            b_rating = place.get('rating', 'N/A')
            b_reviews = place.get('userRatingCount', 0)
            b_address = place.get('formattedAddress', 'N/A')
            
            # Step 1: Deep Crawl Website & check for under construction pages
            found_email = None
            is_blank_site = False
            if b_site:
                found_email, is_blank_site = scrape_website_deep(b_site)
            
            # Step 2: Query Bing Search Engine if missing
            if not found_email or found_email == "None Found":
                found_email = search_bing_for_emails(b_name, location, domain_name=b_site)
            
            # Step 3: Abstract API Email Verification (or local DNS check)
            email_status = "None Found"
            if found_email and found_email != "None Found":
                email_status = verify_email_abstract(found_email, abstract_api_key)
            
            # Reclassify under-construction sites as "None" so they go to NO_WEBSITE (Prime Targets)
            final_website = "None" if (not b_site or is_blank_site) else b_site
                
            raw_data.append({
                'name': b_name,
                'phone': b_phone,
                'email': found_email if found_email else 'None Found',
                'verification_status': email_status,
                'rating': b_rating,
                'reviews': b_reviews,
                'website': final_website,
                'address': b_address
            })
            
            if len(places) > 0:
                progress_bar.progress((idx + 1) / len(places))

        status_box.empty()
        progress_bar.empty()

        df = pd.DataFrame(raw_data)

        if df.empty:
            st.warning("No results returned. Please verify your Google API key or search query.")
        else:
            no_website_df = df[df['website'] == 'None'].copy()
            has_website_df = df[df['website'] != 'None'].copy()

            st.success(f"Scraping Complete! Found {len(df)} total businesses.")

            m1, m2 = st.columns(2)
            m1.metric("🔴 No Website (Prime Targets)", len(no_website_df))
            m2.metric("🟢 Has Website / Socials", len(has_website_df))

            clean_trade = re.sub(r'\W+', '_', trade.lower())
            clean_loc = re.sub(r'\W+', '_', location.lower())

            st.write("---")
            st.subheader("📥 Download Your Files")

            d_col1, d_col2 = st.columns(2)

            with d_col1:
                excel_no_site = to_excel(no_website_df)
                st.download_button(
                    label="Download No_Website_Leads.xlsx",
                    data=excel_no_site,
                    file_name=f"{clean_trade}_{clean_loc}_NO_WEBSITE.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True
                )

            with d_col2:
                excel_has_site = to_excel(has_website_df)
                st.download_button(
                    label="Download Has_Website_Leads.xlsx",
                    data=excel_has_site,
                    file_name=f"{clean_trade}_{clean_loc}_HAS_WEBSITE.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True
                )
