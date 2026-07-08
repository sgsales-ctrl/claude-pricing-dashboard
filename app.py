import streamlit as st
import requests
import pandas as pd
from datetime import date, timedelta

# ---------- 1. Read the API key from .streamlit/secrets.toml ----------
API_KEY = st.secrets["CLOUDBEDS_API_KEY"]

# ---------- 2. Base setup for every Cloudbeds request ----------
BASE_URL = "https://api.cloudbeds.com/api/v1.2"
HEADERS = {"x-api-key": API_KEY}


def cloudbeds_get(endpoint: str, params: dict | None = None) -> list | dict:
    """Call a Cloudbeds endpoint and return its 'data' payload."""
    r = requests.get(f"{BASE_URL}/{endpoint}", headers=HEADERS, params=params)
    r.raise_for_status()  # stops with an error if the key/scopes are wrong
    body = r.json()
    if not body.get("success", True):
        st.error(f"Cloudbeds error: {body.get('message')}")
        st.stop()
    return body.get("data", [])


# ---------- 3. Fetch data (cached 5 min to respect rate limits) ----------
@st.cache_data(ttl=300)
def get_property_id() -> str:
    """Fetch the property ID linked to this API key."""
    hotels = cloudbeds_get("getHotels")
    if isinstance(hotels, list) and hotels:
        return str(hotels[0].get("propertyID", ""))
    if isinstance(hotels, dict):
        return str(hotels.get("propertyID", ""))
    return ""


PROPERTY_ID = get_property_id()
if not PROPERTY_ID:
    st.error("Couldn't find a property for this API key — check the key's permissions in Cloudbeds.")
    st.stop()


@st.cache_data(ttl=300)
def get_reservations(start: str, end: str):
    return cloudbeds_get(
        "getReservations",
        {"propertyID": PROPERTY_ID, "checkInFrom": start, "checkInTo": end},
    )


@st.cache_data(ttl=300)
def get_rooms():
    return cloudbeds_get("getRooms", {"propertyID": PROPERTY_ID})


@st.cache_data(ttl=300)
def get_today_dashboard():
    """Today's stats straight from Cloudbeds (arrivals, departures, occupancy)."""
    return cloudbeds_get("getDashboard", {"propertyID": PROPERTY_ID})


# ---------- 4. Build the dashboard page ----------
st.set_page_config(page_title="Claude Pricing Dashboard", layout="wide")
st.title("Claude Pricing Dashboard")

# Date filter in the sidebar
st.sidebar.header("Filters")
start_date = st.sidebar.date_input("Check-in from", date.today())
end_date = st.sidebar.date_input("Check-in to", date.today() + timedelta(days=30))

# ----- Today's occupancy snapshot (from Cloudbeds getDashboard) -----
st.subheader("Today at a glance")
dash = get_today_dashboard()
if isinstance(dash, dict) and dash:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Occupancy", dash.get("percentageOccupancy", dash.get("occupancy", "n/a")))
    c2.metric("Arrivals", dash.get("arrivals", "n/a"))
    c3.metric("Departures", dash.get("departures", "n/a"))
    c4.metric("In house", dash.get("inHouse", dash.get("stayovers", "n/a")))

# ----- Daily occupancy chart over the selected date range -----
st.subheader("Occupancy by night")


@st.cache_data(ttl=300)
def occupancy_by_night(start: str, end: str, total_rooms: int):
    """Count occupied rooms per night from reservations."""
    res = cloudbeds_get(
        "getReservations",
        {"propertyID": PROPERTY_ID, "checkInFrom": "2000-01-01", "checkInTo": end, "status": "checked_in,checked_out,confirmed,not_confirmed"},
    )
    nights = pd.date_range(start, end)
    counts = {n.date(): 0 for n in nights}
    for r in res:
        try:
            ci = pd.to_datetime(r["startDate"]).date()
            co = pd.to_datetime(r["endDate"]).date()
        except (KeyError, ValueError):
            continue
        for n in counts:
            if ci <= n < co:  # guest occupies the night if checked in on/before, out after
                counts[n] += 1
    df = pd.DataFrame({"night": list(counts.keys()), "occupied": list(counts.values())})
    if total_rooms:
        df["occupancy %"] = (df["occupied"] / total_rooms * 100).round(1)
    return df


rooms_data_for_count = get_rooms()
total_rooms = sum(len(p.get("rooms", [])) for p in rooms_data_for_count) if rooms_data_for_count else 0

occ_df = occupancy_by_night(str(start_date), str(end_date), total_rooms)
if not occ_df.empty and total_rooms:
    st.bar_chart(occ_df.set_index("night")["occupancy %"])
    st.caption(f"Based on {total_rooms} total rooms.")
elif not total_rooms:
    st.info("Couldn't count total rooms — check the getRooms permission on your API key.")

# Reservations table
st.subheader("Reservations")
reservations = get_reservations(str(start_date), str(end_date))
if reservations:
    df = pd.DataFrame(reservations)
    cols = [c for c in ["guestName", "startDate", "endDate", "status", "balance"] if c in df.columns]
    st.dataframe(df[cols] if cols else df, use_container_width=True)
    st.metric("Total reservations", len(df))
else:
    st.info("No reservations found for this date range.")

# Rooms table
st.subheader("Rooms")
rooms_data = get_rooms()
if rooms_data:
    # getRooms returns one entry per property, each with a 'rooms' list
    all_rooms = []
    for prop in rooms_data:
        all_rooms.extend(prop.get("rooms", []))
    if all_rooms:
        st.dataframe(pd.DataFrame(all_rooms), use_container_width=True)
