import streamlit as st
import requests
import pandas as pd
from datetime import date, timedelta

st.set_page_config(page_title="Claude Pricing Dashboard", layout="wide")

# ---------- 1. Read the API key from Streamlit secrets ----------
API_KEY = st.secrets["CLOUDBEDS_API_KEY"]

# ---------- 2. Base setup for every Cloudbeds request ----------
BASE_URL = "https://api.cloudbeds.com/api/v1.2"
HEADERS = {"x-api-key": API_KEY}


def cloudbeds_get(endpoint: str, params: dict | None = None) -> list | dict:
    """Call a Cloudbeds endpoint and return its 'data' payload."""
    r = requests.get(f"{BASE_URL}/{endpoint}", headers=HEADERS, params=params)
    r.raise_for_status()
    body = r.json()
    if not body.get("success", True):
        st.error(f"Cloudbeds error: {body.get('message')}")
        st.stop()
    return body.get("data", [])


# ---------- 3. Fetch data (cached 5 min to respect rate limits) ----------
@st.cache_data(ttl=300)
def get_properties() -> dict:
    """Return {property name: property ID} for all properties on this API key."""
    hotels = cloudbeds_get("getHotels")
    if isinstance(hotels, dict):
        hotels = [hotels]
    props = {}
    for h in hotels or []:
        pid = str(h.get("propertyID", ""))
        name = h.get("propertyName") or h.get("hotelName") or f"Property {pid}"
        if pid:
            props[name] = pid
    return props


@st.cache_data(ttl=300)
def get_reservations(property_id: str, start: str, end: str):
    return cloudbeds_get(
        "getReservations",
        {"propertyID": property_id, "checkInFrom": start, "checkInTo": end},
    )


@st.cache_data(ttl=300)
def get_rooms(property_id: str):
    return cloudbeds_get("getRooms", {"propertyID": property_id})


@st.cache_data(ttl=300)
def get_today_dashboard(property_id: str):
    return cloudbeds_get("getDashboard", {"propertyID": property_id})


@st.cache_data(ttl=300)
def occupancy_by_night(property_id: str, start: str, end: str, total_rooms: int):
    """Count occupied rooms per night from reservations."""
    res = cloudbeds_get(
        "getReservations",
        {"propertyID": property_id, "checkInFrom": "2000-01-01", "checkInTo": end,
         "status": "checked_in,checked_out,confirmed,not_confirmed"},
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


# ---------- 4. Build the dashboard page ----------
st.title("Claude Pricing Dashboard")

properties = get_properties()
if not properties:
    st.error("No properties found for this API key — check its permissions in Cloudbeds.")
    st.stop()

# Sidebar: property selector + date filters
st.sidebar.header("Filters")
property_name = st.sidebar.selectbox("Property", list(properties.keys()))
property_id = properties[property_name]
start_date = st.sidebar.date_input("Check-in from", date.today())
end_date = st.sidebar.date_input("Check-in to", date.today() + timedelta(days=30))

st.caption(f"Showing: {property_name}")

# Occupancy data
rooms_data = get_rooms(property_id)
total_rooms = sum(len(p.get("rooms", [])) for p in rooms_data) if rooms_data else 0
occ_df = occupancy_by_night(property_id, str(start_date), str(end_date), total_rooms)

# ----- Today's snapshot -----
st.subheader("Today at a glance")
today_occ = "n/a"
if total_rooms and not occ_df.empty:
    today_row = occ_df[occ_df["night"] == date.today()]
    if not today_row.empty:
        today_occ = f"{today_row.iloc[0]['occupancy %']:.0f}%"

dash = get_today_dashboard(property_id)
dash = dash if isinstance(dash, dict) else {}
c1, c2, c3, c4 = st.columns(4)
c1.metric("Occupancy", today_occ)
c2.metric("Arrivals", dash.get("arrivals", "n/a"))
c3.metric("Departures", dash.get("departures", "n/a"))
c4.metric("In house", dash.get("inHouse", dash.get("stayovers", "n/a")))

# ----- Occupancy chart -----
st.subheader("Occupancy by night")
if not occ_df.empty and total_rooms:
    st.bar_chart(occ_df.set_index("night")["occupancy %"])
    st.caption(f"Based on {total_rooms} total rooms.")
elif not total_rooms:
    st.info("Couldn't count total rooms — check the getRooms permission on your API key.")

# ----- Reservations table -----
st.subheader("Reservations")
reservations = get_reservations(property_id, str(start_date), str(end_date))
if reservations:
    df = pd.DataFrame(reservations)
    cols = [c for c in ["guestName", "startDate", "endDate", "status", "balance"] if c in df.columns]
    st.dataframe(df[cols] if cols else df, use_container_width=True)
    st.metric("Total reservations", len(df))
else:
    st.info("No reservations found for this date range.")

# ----- Rooms table -----
st.subheader("Rooms")
if rooms_data:
    all_rooms = []
    for p in rooms_data:
        all_rooms.extend(p.get("rooms", []))
    if all_rooms:
        st.dataframe(pd.DataFrame(all_rooms), use_container_width=True)
