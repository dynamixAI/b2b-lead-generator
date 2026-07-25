import re
import requests
import pandas as pd
import streamlit as st

# =========================================================
# PAGE CONFIG & UI STYLING
# =========================================================
st.set_page_config(page_title="UK B2B Lead Generator", page_icon="🎯", layout="centered")

st.title("🎯 UK B2B Lead Generator")
st.write("Generate local business leads, split automatically by web presence.")

# Sidebar Configuration for API Key
st.sidebar.header("Settings")
api_key_input = st.sidebar.text_input("Google Cloud API Key", value="AIzaSyBlB0xgNEmdWnY29ZoZWWFJ7rrZsvjrny4", type="password")

# Main Form Inputs
trade = st.text_input("Trade / Service", placeholder="e.g. Roofers, Electricians, Plumbers")
location = st.text_input("Town / Postcode", placeholder="e.g. Wigan, Stockport, WN1")

# =========================================================
# HELPER SCRAPING FUNCTIONS (SILENT EXECUTION)
# =========================================================
def scrape_email_from_website(url):
    """Scrapes direct emails from custom business websites without printing logs."""
    if not url or 'facebook.com' in url or 'instagram.com' in url:
        return None
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url

    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    try:
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
            emails = set(re.findall(email_pattern, response.text))
            valid = [e for e in emails if not e.endswith(('.png', '.jpg', '.jpeg', '.svg', '.gif', '.webp'))]
            return ", ".join(valid) if valid else None
    except Exception:
        return None
    return None

def search_directory_email(business_name, loc):
    """Searches UK directory listings (Yell, Checkatrade, Thomson, Trustpilot) silently."""
    query = f'"{business_name}" "{loc}" site:yell.com OR site:checkatrade.com OR site:thomsonlocal.com OR site:trustpilot.com email'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    url = f"https://html.duckduckgo.com/html/?q={query}"
    
    try:
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
            emails = set(re.findall(email_pattern, resp.text))
            valid = [e for e in emails if not e.endswith(('.png', '.jpg', '.jpeg', '.svg'))]
            return ", ".join(valid) if valid else "None Found"
    except Exception:
        return "None Found"
    return "None Found"

def to_excel(df):
    """Converts a DataFrame into an Excel file buffer for instant download."""
    import io
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False)
    return output.getvalue()

# =========================================================
# RUN SCRAPER ACTION
# =========================================================
if st.button("🚀 Generate Leads", type="primary"):
    if not trade or not location:
        st.error("Please enter both a Trade and Location.")
    elif not api_key_input or api_key_input == "YOUR_GOOGLE_PLACES_API_KEY_HERE":
        st.error("Please enter a valid Google Places API Key in the sidebar.")
    else:
        search_query = f"{trade} in {location}, UK"
        
        # UI Status container right at the top
        status_box = st.empty()
        status_box.info(f"Searching Google Places for: **'{search_query}'**...")

        new_places_url = "https://places.googleapis.com/v1/places:searchText"
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key_input,
            "X-Goog-FieldMask": "places.displayName,places.nationalPhoneNumber,places.websiteUri,places.rating,places.userRatingCount,places.formattedAddress"
        }
        payload = {"textQuery": search_query}

        try:
            response = requests.post(new_places_url, headers=headers, json=payload)
            data = response.json()
            places = data.get('places', [])
        except Exception as e:
            st.error(f"API Request failed: {e}")
            places = []

        raw_data = []
        progress_bar = st.progress(0)
        
        for idx, place in enumerate(places):
            b_name = place.get('displayName', {}).get('text', 'N/A')
            b_phone = place.get('nationalPhoneNumber', 'N/A')
            b_site = place.get('websiteUri', None)
            b_rating = place.get('rating', 'N/A')
            b_reviews = place.get('userRatingCount', 0)
            b_address = place.get('formattedAddress', 'N/A')
            
            # Step A: Check Website
            found_email = None
            if b_site:
                found_email = scrape_email_from_website(b_site)
            
            # Step B: Check UK Directories if no website email found
            if not found_email or found_email == "None Found":
                found_email = search_directory_email(b_name, location)
                
            raw_data.append({
                'name': b_name,
                'phone': b_phone,
                'email': found_email if found_email else 'None Found',
                'rating': b_rating,
                'reviews': b_reviews,
                'website': b_site if b_site else 'None',
                'address': b_address
            })
            
            if len(places) > 0:
                progress_bar.progress((idx + 1) / len(places))

        # Clean up progress bar & status message after run
        status_box.empty()
        progress_bar.empty()

        df = pd.DataFrame(raw_data)

        if df.empty:
            st.warning("No results returned. Please verify your Google API key or search query.")
        else:
            # Separate into 2 clean DataFrames
            no_website_df = df[df['website'] == 'None'].copy()
            has_website_df = df[df['website'] != 'None'].copy()

            st.success(f"Scraping Complete! Found {len(df)} total businesses.")

            # Summary Metrics Card
            m1, m2 = st.columns(2)
            m1.metric("🔴 No Website (Prime Targets)", len(no_website_df))
            m2.metric("🟢 Has Website / Socials", len(has_website_df))

            clean_trade = re.sub(r'\W+', '_', trade.lower())
            clean_loc = re.sub(r'\W+', '_', location.lower())

            st.write("---")
            st.subheader("📥 Download Your Files")

            # Download Buttons placed right under summary
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
