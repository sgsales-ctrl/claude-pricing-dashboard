import json
from pathlib import Path
from datetime import date, timedelta

import streamlit as st
import requests
import pandas as pd

st.set_page_config(page_title="Claude Pricing Dashboard", layout="wide")

# ---------- Cloudbeds setup ----------
API_KEY = st.secrets["CLOUDBEDS_API_KEY"]
BASE_URL = "https://api.cloudbeds.com/api/v1.2"
HEADERS = {"x-api-key": API_KEY}
DATA_DIR = Path(__file__).parent / "data"

OCC_TARGET = 0.85          # below this, recommend discounting
SOFT_DISCOUNT = 0.90       # 10% cut when occupancy < 85%
HIGH_OCC_PREMIUM = 1.05    # small lift when nearly full

# Some Cloudbeds properties contain rooms that belong to other entities.
# Map: property name -> substring that must appear in the ROOM NAME to count.
ROOM_NAME_FILTERS = {
    "Heritage Collection on Seah": "seah",
    "Heritage Collection on Clarke Quay": "clarke quay",
}


def cloudbeds_get(endpoint: str, params: dict | None = None) -> list | dict:
    r = requests.get(f"{BASE_URL}/{endpoint}", headers=HEADERS, params=params)
    r.raise_for_status()
    body = r.json()
    if not body.get("success", True):
        st.error(f"Cloudbeds error: {body.get('message')}")
        st.stop()
    return body.get("data", [])


@st.cache_data
def load_json(name: str):
    p = DATA_DIR / name
    if not p.exists():
        return {}
    return json.loads(p.read_text())


PRICING = load_json("pricing.json")
COMPETITORS = load_json("competitors.json")
COMP_RATES = load_json("comp_rates.json")
EVENTS = load_json("events.json").get("events", [])


# ---------- Cloudbeds data (cached 5 min) ----------
@st.cache_data(ttl=300)
def get_properties() -> dict:
    """All properties on this API key — fully paginated."""
    hotels, page = [], 1
    while True:
        batch = cloudbeds_get("getHotels", {"pageNumber": page, "pageSize": 100})
        if isinstance(batch, dict):
            batch = [batch]
        hotels.extend(batch or [])
        if not batch or len(batch) < 100:
            break
        page += 1
    return {h.get("propertyName", f"Property {h.get('propertyID')}"): str(h.get("propertyID"))
            for h in hotels if h.get("propertyID")}


@st.cache_data(ttl=300)
def get_rooms(property_id: str) -> list:
    """Flat list of all rooms for the property — fully paginated."""
    rooms, page = [], 1
    while True:
        batch = cloudbeds_get("getRooms", {"propertyID": property_id,
                                           "pageNumber": page, "pageSize": 100})
        if isinstance(batch, dict):
            batch = [batch]
        got = 0
        for prop in batch or []:
            prop_rooms = prop.get("rooms", [])
            rooms.extend(prop_rooms)
            got += len(prop_rooms)
        if got < 100:
            break
        page += 1
    return rooms


@st.cache_data(ttl=300)
def availability_by_type(property_id: str, day: str):
    """Rooms available per room type for one night — fully paginated."""
    d = date.fromisoformat(day)
    out, page = {}, 1
    while True:
        data = cloudbeds_get("getAvailableRoomTypes", {
            "propertyIDs": property_id,
            "startDate": day,
            "endDate": str(d + timedelta(days=1)),
            "adults": 1, "rooms": 1,
            "pageNumber": page, "pageSize": 100,
        })
        got = 0
        for prop in data if isinstance(data, list) else [data]:
            for rt in prop.get("propertyRooms", []):
                out[rt.get("roomTypeName", "?")] = int(rt.get("roomsAvailable", 0))
                got += 1
        if got < 100:
            break
        page += 1
    return out


@st.cache_data(ttl=300)
def reservations_overlapping(property_id: str, start: str, end: str) -> list:
    """All reservations that could cover a night in [start, end] — fully paginated."""
    out, page = [], 1
    while True:
        batch = cloudbeds_get("getReservations", {
            "propertyID": property_id,
            "checkOutFrom": start,   # still in-house on/after window start
            "checkInTo": end,        # arrives before window ends
            "pageNumber": page, "pageSize": 100,
        })
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    # physical occupancy: exclude cancellations and no-shows client-side
    return [r for r in out if str(r.get("status", "")).lower() not in ("canceled", "cancelled", "no_show")]


def occupancy_for_dates(property_id: str, days: list[date], total_rooms: int,
                        allowed_types: frozenset | None = None) -> dict:
    """Occupied-room count per night from actual reservations (physical occupancy).
    If allowed_types is given, only reservations for those room types count
    (used when a Cloudbeds property contains rooms belonging to other entities)."""
    if not days:
        return {}
    res = reservations_overlapping(property_id, str(min(days)), str(max(days)))
    counts = {d: 0 for d in days}
    saw_type_field = False
    for r in res:
        rt = r.get("roomTypeName") or r.get("roomType") or ""
        if rt:
            saw_type_field = True
        if allowed_types is not None and rt and rt not in allowed_types:
            continue
        try:
            ci = pd.to_datetime(r["startDate"]).date()
            co = pd.to_datetime(r["endDate"]).date()
        except (KeyError, ValueError):
            continue
        for d in counts:
            if ci <= d < co:
                counts[d] += 1
    # If we needed type filtering but reservations carry no type info,
    # fall back to availability: occupied = total - available (allowed types only).
    if allowed_types is not None and not saw_type_field:
        for d in days:
            try:
                avail = availability_by_type(property_id, str(d))
                open_rooms = sum(v for k, v in avail.items() if k in allowed_types)
                counts[d] = max(total_rooms - open_rooms, 0)
            except Exception:
                counts[d] = None
    if total_rooms:  # a room can't be occupied twice; cap at total
        counts = {d: (min(c, total_rooms) if c is not None else None) for d, c in counts.items()}
    return counts


# ---------- Pricing logic ----------
def ladder_rate(rates: dict, days_out: int) -> float:
    """Rate from the booking-window ladder. IA = ideal base rate."""
    if days_out > 10:
        return rates["ia"]
    if days_out >= 7:
        return rates["d7_10"]
    if days_out >= 4:
        return rates["d4_7"]
    return rates["d4_7"]  # 0-3 days: start from the 4-7 rate; floor protects downside


def event_for(d: date):
    for ev in EVENTS:
        ds = ev.get("dates", "")
        try:
            if "to" in ds:
                a, b = [x.strip() for x in ds.split("to")]
                if date.fromisoformat(a) <= d <= date.fromisoformat(b):
                    return ev
            elif ds and ds != "":
                if ds.startswith(str(d.year)) and str(d) in ds:
                    return ev
        except ValueError:
            continue
    return None


def recommend(rates: dict, days_out: int, occ: float | None, ev) -> tuple[float, str]:
    base = ladder_rate(rates, days_out)
    floor = rates["floor"]
    demand = (ev or {}).get("demand", "")
    # High-demand event: hold or lift, ignore discounting
    if demand in ("High", "Very High"):
        rec = max(base, rates["ia"]) * (1.10 if demand == "Very High" else 1.0)
        return round(max(rec, floor)), f"{demand} demand event — hold/lift, no discounting"
    if occ is None:
        return round(max(base, floor)), "No occupancy data — ladder rate"
    if occ >= 0.95:
        return round(max(base * HIGH_OCC_PREMIUM, floor)), "Occupancy ≥95% — small premium"
    if occ >= OCC_TARGET:
        return round(max(base, floor)), "Occupancy ≥85% — hold ladder rate"
    rec = max(base * SOFT_DISCOUNT, floor)
    note = "Occupancy <85% — 10% cut"
    if days_out <= 3 and rec <= floor + 1:
        note = "Occupancy <85%, 0-3 days — at breakeven floor"
    return round(rec), note


# ---------- Page ----------
st.title("Claude Pricing Dashboard")

properties = get_properties()
if not properties:
    st.error("No properties found for this API key.")
    st.stop()

st.sidebar.header("Filters")
property_name = st.sidebar.selectbox("Property", list(properties.keys()))
property_id = properties[property_name]
start_date = st.sidebar.date_input("Window start", date.today())
end_date = st.sidebar.date_input("Window end", date.today() + timedelta(days=14))

st.caption(f"Showing: {property_name}")

# Match pricing guide entry (exact then fuzzy)
prop_pricing = PRICING.get(property_name)
if prop_pricing is None:
    key = next((k for k in PRICING if k.casefold().replace(" ", "") == property_name.casefold().replace(" ", "")), None)
    prop_pricing = PRICING.get(key, {})

# Sector + competitor set
sector = next((s for s, v in COMPETITORS.items()
               if isinstance(v, dict) and property_name in v.get("hc_properties", [])), None)
sector_comps = COMPETITORS.get(sector, {}).get("competitors", []) if sector else []

# Total rooms (apply room-name filter for shared Cloudbeds properties, e.g. Seah)
name_filter = next((v for k, v in ROOM_NAME_FILTERS.items()
                    if k.casefold() == property_name.casefold()), None)
rooms_data = get_rooms(property_id)
if name_filter:
    rooms_data = [r for r in rooms_data
                  if name_filter in str(r.get("roomName", "")).casefold()]
total_rooms = len(rooms_data)
allowed_types = (frozenset(str(r.get("roomTypeName")) for r in rooms_data if r.get("roomTypeName"))
                 if name_filter else None)

# Occupancy for the window
window_days = [start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1)]
occ_counts = occupancy_for_dates(property_id, window_days, total_rooms, allowed_types)
if name_filter:
    st.caption(f"Room filter active: only rooms named with “{name_filter}” are counted ({total_rooms} rooms).")

# ===== Tonight at a glance =====
st.subheader("Tonight at a glance")
tonight = date.today()
occ_tonight = occ_counts.get(tonight)
c1, c2, c3 = st.columns(3)
if total_rooms and occ_tonight is not None:
    pct = occ_tonight / total_rooms
    c1.metric("Tonight's occupancy", f"{pct:.0%}", f"{occ_tonight}/{total_rooms} sold")
    below = pct < OCC_TARGET
    c2.metric("Pricing posture", "Discount" if below else "Hold/Lift",
              "occupancy under 85%" if below else "occupancy at/above 85%")
else:
    c1.metric("Tonight's occupancy", "n/a")
ev_today = event_for(tonight)
c3.metric("Tonight's demand driver", (ev_today or {}).get("demand", "None"),
          (ev_today or {}).get("name", "no dominant driver"))

# ===== Our rates vs competitors (sector) =====
st.subheader(f"Rates vs competitors — {sector or 'sector unknown'}")
scraped = COMP_RATES.get("rates", {})
latest_day = max(scraped.keys()) if scraped else None
if latest_day and sector_comps:
    rows = []
    for comp in sector_comps:
        info = scraped.get(latest_day, {}).get(comp, {})
        if info.get("status") == "ok":
            rows.append({"Competitor": comp, "Status": "Available",
                         "Rate incl. taxes (S$)": info.get("est_incl_taxes"),
                         "Room": info.get("room", "")})
        else:
            rows.append({"Competitor": comp, "Status": "SOLD OUT", "Rate incl. taxes (S$)": None, "Room": ""})
    comp_df = pd.DataFrame(rows)
    st.dataframe(comp_df, use_container_width=True, hide_index=True)

    avail = comp_df["Rate incl. taxes (S$)"].dropna()
    our_short = pd.Series([v["d4_7"] for v in prop_pricing.values()]).mean() if prop_pricing else None
    m1, m2, m3 = st.columns(3)
    m1.metric("Comp median (avail.)", f"S$ {avail.median():.0f}" if not avail.empty else "all sold out")
    m2.metric("Our avg short-window rate", f"S$ {our_short:.0f}" if our_short else "n/a")
    sold_out_n = (comp_df["Status"] == "SOLD OUT").sum()
    m3.metric("Comps sold out", f"{sold_out_n}/{len(comp_df)}",
              "compression — hold rates" if sold_out_n > len(comp_df) / 2 else None)
    st.caption(f"Booking.com rates for {latest_day} (cheapest room, 2 adults, est. incl. taxes/fees). "
               "Refresh data/comp_rates.json regularly.")
else:
    st.info("No competitor rates on file — update data/comp_rates.json.")

# ===== Vacant rooms, next 14 days =====
st.subheader("Vacant rooms — next 14 days")
next14 = [tonight + timedelta(days=i) for i in range(14)]
vac_rows = []
for d in next14:
    try:
        avail_types = availability_by_type(property_id, str(d))
    except Exception:
        avail_types = {}
    if allowed_types is not None:
        avail_types = {k: v for k, v in avail_types.items() if k in allowed_types}
    vacant = {k: v for k, v in avail_types.items() if v > 0}  # occupied types removed
    occ_n = occ_counts.get(d)
    vac_rows.append({
        "Date": str(d),
        "Day": d.strftime("%a"),
        "Occ %": f"{occ_n / total_rooms:.0%}" if (total_rooms and occ_n is not None) else "n/a",
        "Vacant rooms": ", ".join(f"{k} ({v})" for k, v in sorted(vacant.items())) or "FULLY BOOKED",
    })
st.dataframe(pd.DataFrame(vac_rows), use_container_width=True, hide_index=True)

# ===== Events =====
st.subheader("Events — Heritage Collection relevance")
if EVENTS:
    ev_df = pd.DataFrame([{
        "Dates": e.get("dates"), "Event": e.get("name"), "Venue": e.get("venue"),
        "Attendees": e.get("attendees"), "Demand": e.get("demand"), "Why": e.get("rationale"),
    } for e in EVENTS])
    st.dataframe(ev_df, use_container_width=True, hide_index=True)
    st.caption("Demand scored on past materialization + attendee count/type. "
               "We are 3.5-star, adults-only, shophouse CBD — day-attendee and family events score low.")

# ===== Price recommendations =====
st.subheader("Price recommendations")
if prop_pricing:
    rec_rows = []
    for d in window_days:
        days_out = (d - tonight).days
        occ_n = occ_counts.get(d)
        occ_pct = (occ_n / total_rooms) if (total_rooms and occ_n is not None) else None
        ev = event_for(d)
        for room, rates in prop_pricing.items():
            rec, why = recommend(rates, days_out, occ_pct, ev)
            rec_rows.append({
                "Date": str(d), "Day": d.strftime("%a"),
                "Occ %": f"{occ_pct:.0%}" if occ_pct is not None else "n/a",
                "Event": (ev or {}).get("name", "—"),
                "Demand": (ev or {}).get("demand", "—"),
                "Room": room,
                "Ladder (S$)": ladder_rate(rates, days_out),
                "Floor (S$)": rates["floor"],
                "Recommended (S$)": rec,
                "Reason": why,
            })
    st.dataframe(pd.DataFrame(rec_rows), use_container_width=True, hide_index=True, height=500)
    st.caption("Ladder: IA rate >10 days out → 7-10 → 4-7 day rates. Below 85% occupancy: 10% cut, "
               "never below breakeven floor. High/Very High demand events: hold or lift, no discounting.")
else:
    st.info(f"No pricing guide entry found for {property_name} — check data/pricing.json names.")
