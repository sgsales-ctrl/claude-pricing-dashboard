# app.py — Heritage Collection dashboard with room-name filtering for Seah & Clarke Quay
import datetime as dt
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Claude Pricing Dashboard", layout="wide")

TOKEN_URL = "https://hotels.cloudbeds.com/api/v1.1/access_token"
API_BASE  = "https://hotels.cloudbeds.com/api/v1.1"

# Properties that need room-name filtering: {propertyID substring match: keyword}
# The keyword must appear in the ROOM TYPE name to be counted.
ROOM_NAME_FILTERS = {
    "Heritage Collection on Seah": "seah",
    "Heritage Collection on Clarke Quay": "clarke quay",
}

@st.cache_data(ttl=3000)
def get_access_token():
    return st.secrets["CB_API_KEY"]
def api_get(path, token, params=None):
    r = requests.get(f"{API_BASE}/{path}", headers={"Authorization": f"Bearer {token}"}, params=params or {})
    r.raise_for_status()
    return r.json()

@st.cache_data(ttl=1800)
def get_properties(token):
    data = api_get("getHotels", token)
    return {h["propertyID"]: h["propertyName"] for h in data.get("data", [])}

@st.cache_data(ttl=1800)
def get_property_occupancy(token, property_id, start, end):
    """Property-level daily occupancy % (used for the 10 unfiltered properties)."""
    data = api_get("getDashboard", token, {"propertyID": property_id, "startDate": start, "endDate": end})
    return pd.DataFrame([
        {"date": d["date"], "occupancy": float(d["occupancyPercentage"])}
        for d in data.get("data", {}).get("occupancy", [])
    ])

@st.cache_data(ttl=1800)
def get_filtered_occupancy(token, property_id, keyword, start, end):
    """
    Room-name-filtered daily occupancy.
    Counts only rooms whose ROOM TYPE name contains `keyword`.
    occupancy% = occupied matching rooms / total matching rooms.
    Blocked/out-of-order rooms are NOT counted as occupied (matches Cloudbeds' own logic).
    """
    # 1) Which rooms belong to matching room types?
    rooms_data = api_get("getRooms", token, {"propertyID": property_id})
    matching_room_ids = set()
    total_matching = 0
    for rt in rooms_data.get("data", []):
        rt_name = str(rt.get("roomTypeName", "")).lower()
        if keyword in rt_name:
            for room in rt.get("rooms", []):
                matching_room_ids.add(str(room["roomID"]))
                total_matching += 1
    if total_matching == 0:
        return pd.DataFrame()

    # 2) Pull reservations in the window and count, per date, how many matching rooms are occupied.
    res = api_get("getReservations", token, {
        "propertyID": property_id, "checkInFrom": start, "checkOutTo": end,
        "status": "confirmed,checked_in,checked_out", "includeRooms": "true",
    })
    dates = pd.date_range(start, end, inclusive="left").date
    occupied = {str(d): set() for d in dates}
    for r in res.get("data", []):
        for assign in r.get("assigned", r.get("rooms", [])):
            rid = str(assign.get("roomID"))
            if rid not in matching_room_ids:
                continue
            ci = pd.to_datetime(assign.get("startDate", r.get("startDate"))).date()
            co = pd.to_datetime(assign.get("endDate", r.get("endDate"))).date()
            for d in pd.date_range(ci, co, inclusive="left").date:
                if str(d) in occupied:
                    occupied[str(d)].add(rid)

    return pd.DataFrame([
        {"date": str(d), "occupancy": len(occupied[str(d)]) / total_matching * 100}
        for d in dates
    ])

# ---------------- UI ----------------
st.title("Claude Pricing Dashboard")
tab_occ, tab_comp, tab_events, tab_price = st.tabs(
    ["📊 Occupancy", "🏨 Competitor Rates", "📅 Events", "💡 Pricing Suggestions"]
)

today = dt.date.today()
horizon = st.sidebar.slider("Days ahead", 7, 30, 14)
start = today.isoformat()
end = (today + dt.timedelta(days=horizon)).isoformat()

try:
    token = get_access_token()
    props = get_properties(token)
except Exception as e:
    st.error(f"Could not connect to Cloudbeds: {e}")
    st.stop()

with tab_occ:
    st.subheader(f"Daily occupancy — {len(props)} properties")
    st.caption("Seah and Clarke Quay are filtered to matching room names only; others are property-level.")
    frames = []
    for pid, name in props.items():
        keyword = ROOM_NAME_FILTERS.get(name)
        if keyword:
            df = get_filtered_occupancy(token, pid, keyword, start, end)
            label = f"{name} ({keyword} rooms only)"
        else:
            df = get_property_occupancy(token, pid, start, end)
            label = name
        if not df.empty:
            df["property"] = label
            frames.append(df)
    if frames:
        allocc = pd.concat(frames)
        pivot = allocc.pivot(index="property", columns="date", values="occupancy")
        st.dataframe(pivot.style.format("{:.1f}%").background_gradient(cmap="RdYlGn", axis=None),
                     use_container_width=True)
        st.line_chart(allocc.pivot(index="date", columns="property", values="occupancy"))

with tab_comp:
    st.subheader("Competitor rates (manual entry)")
    st.caption("No rate-shopping feed connected. Enter competitor rates below.")
    default = pd.DataFrame({
        "date": pd.date_range(today, periods=horizon).date,
        "our_rate": [None]*horizon, "competitor_A": [None]*horizon, "competitor_B": [None]*horizon,
    })
    st.session_state["comp_df"] = st.data_editor(default, num_rows="dynamic", use_container_width=True, key="comp")

with tab_events:
    st.subheader("Events (manual entry)")
    ev_default = pd.DataFrame({"date": pd.date_range(today, periods=5).date, "event": [""]*5, "impact (1-5)": [1]*5})
    st.session_state["events_df"] = st.data_editor(ev_default, num_rows="dynamic", use_container_width=True, key="events")

with tab_price:
    st.subheader("Pricing suggestions")
    st.caption("Rules-based operational heuristics. Review and adjust — final pricing is your decision.")
    lift_occ = st.slider("Raise rate when occupancy exceeds (%)", 70, 100, 90)
    event_boost = st.slider("Extra suggested lift on high-impact event days (%)", 0, 50, 15)
    if frames:
        ev_df = st.session_state.get("events_df")
        ev_dates = set()
        if ev_df is not None:
            ev_dates = {str(r["date"]) for _, r in ev_df.iterrows()
                        if r.get("impact (1-5)", 0) and r["impact (1-5)"] >= 4}
        recs = []
        for _, row in allocc.iterrows():
            note = []
            if row["occupancy"] >= lift_occ:
                note.append(f"High demand ({row['occupancy']:.0f}%): consider raising rate")
            if str(row["date"]) in ev_dates:
                note.append(f"High-impact event: consider +{event_boost}%")
            if note:
                recs.append({"property": row["property"], "date": row["date"], "suggestion": "; ".join(note)})
        st.dataframe(pd.DataFrame(recs), use_container_width=True) if recs else st.info("No pricing flags.")
