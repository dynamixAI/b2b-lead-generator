import time

import requests

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
FIELDS = ",".join([
    "places.id",
    "places.displayName",
    "places.nationalPhoneNumber",
    "places.websiteUri",
    "places.rating",
    "places.userRatingCount",
    "places.formattedAddress",
    "places.businessStatus",
    "places.location",
    "places.googleMapsUri",
    "nextPageToken",
])


class PlacesError(Exception):
    pass


def search_places(query, api_key, max_pages=3):
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELDS,
    }
    places, seen, token = [], set(), None

    for _ in range(max_pages):
        payload = {"textQuery": query, "pageSize": 20, "regionCode": "GB", "languageCode": "en-GB"}
        if token:
            payload["pageToken"] = token
        try:
            r = requests.post(PLACES_URL, headers=headers, json=payload, timeout=20)
            data = r.json()
        except requests.RequestException as e:
            if places:
                break
            raise PlacesError(f"Couldn't reach Google Places: {e}")
        except ValueError:
            if places:
                break
            raise PlacesError(f"Google Places returned an unexpected response (HTTP {r.status_code})")

        if "error" in data:
            if places:
                break
            raise PlacesError(data["error"].get("message", "Unknown Google Places error"))

        for p in data.get("places", []):
            pid = p.get("id")
            if pid and pid in seen:
                continue
            seen.add(pid)
            places.append(p)

        token = data.get("nextPageToken")
        if not token:
            break
        time.sleep(1)

    return places
