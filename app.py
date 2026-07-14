import json
import re
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


def _norm(s) -> str:
    """Normalize names for matching: lowercase, alphanumerics only."""
    return re.sub(r"[^a-z0-9]", "", str(s).casefold())


MAX_PAGES = 30  # hard cap on any pagination loop (safety against endpoints ignoring pageNumber)


def cloudbeds_get(endpoint: str, params: dict | None = None) -> list | dict:
    r = requests.get(f"{BASE_URL}/{endpoint}", headers=HEADERS, params=params, timeout=15)
    r.raise_for_status()
    body = r.json()
    if not body.get("success", True):
        st.error(f"Cloudbeds error: {body.get('message')}")
        st.stop()
    return body.get("data", [])


@st.cache_data(ttl=300)  # re-read data files every 5 min so daily commits show up automatically
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
    hotels, page, prev_first = [], 1, None
    while page <= MAX_PAGES:
        batch = cloudbeds_get("getHotels", {"pageNumber": page, "pageSize": 100})
        if isinstance(batch, dict):
            batch = [batch]
        if not batch:
            break
        first = str(batch[0].get("propertyID", ""))
        if first and first == prev_first:
            break
        prev_first = first
        hotels.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return {h.get("propertyName", f"Property {h.get('propertyID')}"): str(h.get("propertyID"))
            for h in hotels if h.get("propertyID")}


@st.cache_data(ttl=300)
def get_rooms(property_id: str) -> list:
    """Flat list of all rooms for the property — fully paginated."""
    rooms, page, prev_first = [], 1, None
    while page <= MAX_PAGES:
        batch = cloudbeds_get("getRooms", {"propertyID": property_id,
                                           "pageNumber": page, "pageSize": 100})
        if isinstance(batch, dict):
            batch = [batch]
        got, first = 0, None
        for prop in batch or []:
            prop_rooms = prop.get("rooms", [])
            if prop_rooms and first is None:
                first = str(prop_rooms[0].get("roomID", "")) or str(prop_rooms[0].get("roomName", ""))
            rooms.extend(prop_rooms)
            got += len(prop_rooms)
        if first and first == prev_first:
            rooms = rooms[:-got]  # endpoint ignored pageNumber — drop the duplicate page
            break
        prev_first = first
        if got < 100:
            break
        page += 1
    return rooms


@st.cache_data(ttl=300)
def room_type_snapshot(property_id: str, day: str) -> dict:
    """Per room type for one night: rooms available + current listed rate — fully paginated."""
    d = date.fromisoformat(day)
    out, page = {}, 1
    while page <= MAX_PAGES:
        data = cloudbeds_get("getAvailableRoomTypes", {
            "propertyIDs": property_id,
            "startDate": day,
            "endDate": str(d + timedelta(days=1)),
            "adults": 1, "rooms": 1,
            "pageNumber": page, "pageSize": 100,
        })
        got, before = 0, len(out)
        for prop in data if isinstance(data, list) else [data]:
            for rt in prop.get("propertyRooms", []):
                rate = rt.get("roomRate") or rt.get("totalRate") or rt.get("rate")
                try:
                    rate = round(float(rate)) if rate is not None else None
                except (TypeError, ValueError):
                    rate = None
                out[rt.get("roomTypeName", "?")] = {
                    "avail": int(rt.get("roomsAvailable", 0)),
                    "rate": rate,
                }
                got += 1
        if got < 100 or len(out) == before:  # short page, or nothing new (pageNumber ignored)
            break
        page += 1
    return out


def availability_by_type(property_id: str, day: str) -> dict:
    return {k: v["avail"] for k, v in room_type_snapshot(property_id, day).items()}


@st.cache_data(ttl=300)
def assignments_for_date(property_id: str, day: str) -> list:
    """Room-level reservation assignments for one date (which physical rooms are taken)."""
    data = cloudbeds_get("getReservationAssignments", {"propertyID": property_id, "date": day})
    if isinstance(data, dict):
        for k in ("assignments", "reservationAssignments", "rooms"):
            if isinstance(data.get(k), list):
                return data[k]
        return []
    return data or []


@st.cache_data(ttl=300)
def reservations_overlapping(property_id: str, start: str, end: str) -> list:
    """All reservations that could cover a night in [start, end] — fully paginated."""
    out, page, prev_first = [], 1, None
    while page <= MAX_PAGES:
        batch = cloudbeds_get("getReservations", {
            "propertyID": property_id,
            "checkOutFrom": start,   # still in-house on/after window start
            "checkInTo": end,        # arrives before window ends
            "pageNumber": page, "pageSize": 100,
        })
        if not batch:
            break
        first = str(batch[0].get("reservationID", "")) if isinstance(batch[0], dict) else str(batch[0])
        if first and first == prev_first:
            break  # endpoint ignored pageNumber — same page again, stop
        prev_first = first
        out.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    # physical occupancy: exclude cancellations and no-shows client-side
    return [r for r in out if str(r.get("status", "")).lower() not in ("canceled", "cancelled", "no_show")]


def occupancy_for_dates(property_id: str, days: list[date], total_rooms: int,
                        allowed_types: frozenset | None = None,
                        room_keys: frozenset | None = None) -> dict:
    """Occupied-room count per night from actual reservations (physical occupancy).
    If room_keys is given (room-name-filtered property), count room-level
    assignments matching those rooms — reservations from other entities sharing
    the same Cloudbeds property are excluded exactly."""
    if not days:
        return {}
    if room_keys:
        def _room_id_and_name(e: dict) -> tuple[str, str]:
            room = e.get("room") if isinstance(e.get("room"), dict) else {}
            rid = next((str(e.get(k) or room.get(k) or "") or "" for k in
                        ("roomID", "roomId", "room_id", "id") if (e.get(k) or room.get(k))), "")
            rname = next((str(e.get(k) or room.get(k) or "") or "" for k in
                          ("roomName", "room_name", "name", "roomNumber") if (e.get(k) or room.get(k))), "")
            return rid, _norm(rname)

        def _room_units(e: dict) -> list[dict]:
            """An assignment entry nests its room(s) under 'assigned'."""
            a = e.get("assigned")
            if isinstance(a, list):
                return [x for x in a if isinstance(x, dict)]
            if isinstance(a, dict):
                return [a]
            return [e]

        counts, diagnosed = {}, False
        for d in days:
            try:
                entries = assignments_for_date(property_id, str(d))
            except Exception:
                entries = []
            if entries:
                seen, occ = set(), 0
                for e in entries:
                    if not isinstance(e, dict):
                        continue
                    for unit in _room_units(e):
                        rid, rname = _room_id_and_name(unit)
                        dedupe = rid or rname
                        if not dedupe or dedupe in seen:
                            continue
                        seen.add(dedupe)
                        if rid in room_keys or (rname and rname in room_keys):
                            occ += 1
                if occ == 0 and not diagnosed:
                    # Field-shape mismatch? Surface field NAMES only (no guest data) to aid setup.
                    first = next((e for e in entries if isinstance(e, dict)), {})
                    units = _room_units(first)
                    st.caption(f"⚠ assignment matching found 0 rooms — entry fields: {sorted(first.keys())}, "
                               f"room-unit fields: {sorted(units[0].keys()) if units else '[]'}")
                    diagnosed = True
                counts[d] = min(occ, total_rooms) if total_rooms else occ
            else:
                counts[d] = None
        # If assignments matched nothing anywhere, fall back to type-filtered reservation count.
        if not any(counts.values()):
            counts = None
        else:
            return counts
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


DEMAND_RANK = {"Very High": 4, "High": 3, "Moderate": 2, "Low-Moderate": 1, "Low": 0}


def event_for(d: date):
    """Highest-demand event covering date d (events can overlap)."""
    best = None
    for ev in EVENTS:
        ds = ev.get("dates", "")
        hit = False
        try:
            if " to " in ds:
                a, b = [x.strip() for x in ds.split(" to ")]
                hit = date.fromisoformat(a) <= d <= date.fromisoformat(b)
            elif ds:
                hit = ds.startswith(str(d.year)) and str(d) in ds
        except ValueError:
            continue
        if hit and (best is None or
                    DEMAND_RANK.get(str(ev.get("demand")), 0) > DEMAND_RANK.get(str(best.get("demand")), 0)):
            best = ev
    return best


def recommend(rates: dict, days_out: int, occ: float | None, ev) -> tuple[float, str]:
    base = ladder_rate(rates, days_out)
    floor = rates["floor"]
    demand = (ev or {}).get("demand", "")
    # High-demand event: price ABOVE the IA rate, using the event's suggested markup when known
    if demand in ("High", "Very High"):
        m = re.search(r"(\d+)\s*-\s*(\d+)\s*%", str((ev or {}).get("rationale", "")))
        up = (int(m.group(1)) + int(m.group(2))) / 200 if m else (0.20 if demand == "Very High" else 0.10)
        rec = max(base, rates["ia"]) * (1 + up)
        return round(max(rec, floor)), f"{demand} demand event — +{up:.0%} above IA rate"
    # Moderate event: expected demand — don't undercut ahead of it.
    if demand == "Moderate":
        if occ is None or occ >= 0.70:
            return round(max(base, floor)), "Moderate demand event — hold rate, no discounting"
        return round(max(base * 0.95, floor)), "Moderate event but occupancy <70% — small 5% cut only"
    if occ is None:
        return round(max(base, floor)), "No occupancy data — IA/window rate"
    if occ >= 0.95:
        return round(max(base * HIGH_OCC_PREMIUM, floor)), "Occupancy ≥95% — small premium"
    if occ >= OCC_TARGET:
        return round(max(base, floor)), "Occupancy ≥85% — hold IA/window rate"
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
all_rooms_unfiltered = rooms_data
if name_filter:
    key = _norm(name_filter)
    rooms_data = [r for r in rooms_data
                  if key in _norm(r.get("roomName", "")) or key in _norm(r.get("roomTypeName", ""))]
total_rooms = len(rooms_data)
allowed_types = (frozenset(str(r.get("roomTypeName")) for r in rooms_data if r.get("roomTypeName"))
                 if name_filter else None)
room_keys = (frozenset(list({str(r.get("roomID") or "") for r in rooms_data} - {""}) +
                       [_norm(r.get("roomName", "")) for r in rooms_data if r.get("roomName")])
             if name_filter else None)

# Occupancy for the window
window_days = [start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1)]
occ_counts = occupancy_for_dates(property_id, window_days, total_rooms, allowed_types, room_keys)
if name_filter:
    st.caption(f"Room filter active: only rooms named with “{name_filter}” are counted ({total_rooms} rooms).")
    with st.expander("Room inventory (check filter)"):
        inv = pd.DataFrame([{
            "Room name": r.get("roomName"), "Room type": r.get("roomTypeName"),
            "Counted": "✓" if r in rooms_data else "✗ excluded",
        } for r in all_rooms_unfiltered])
        st.dataframe(inv, use_container_width=True, hide_index=True)

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

# ----- Room-assignment helper (physical occupancy per room) -----
def assigned_room_keys(day: str) -> set:
    keys = set()
    try:
        for e in assignments_for_date(property_id, day):
            if not isinstance(e, dict):
                continue
            units = e.get("assigned")
            units = units if isinstance(units, list) else ([units] if isinstance(units, dict) else [e])
            for u in units:
                if isinstance(u, dict):
                    rid = str(u.get("roomID") or "")
                    if rid:
                        keys.add(rid)
                    rn = _norm(u.get("roomName", ""))
                    if rn:
                        keys.add(rn)
    except Exception:
        pass
    return keys


def vacant_count_by_type(d: date) -> dict:
    """Physical vacancy per room type on a date (rooms with no assigned reservation)."""
    assigned = assigned_room_keys(str(d))
    counts = {}
    for r in rooms_data:
        rt = str(r.get("roomTypeName", ""))
        occupied = (str(r.get("roomID") or "") in assigned
                    or _norm(r.get("roomName", "")) in assigned)
        counts[rt] = counts.get(rt, 0) + (0 if occupied else 1)
    return counts


# ===== Price recommendations =====
st.subheader("Price recommendations")
if prop_pricing:
    rec_rows = []
    for d in window_days:
        days_out = (d - tonight).days
        occ_n = occ_counts.get(d)
        occ_pct = (occ_n / total_rooms) if (total_rooms and occ_n is not None) else None
        if occ_pct is not None and occ_pct >= 1.0:
            continue  # fully booked — nothing to price
        vac_types = vacant_count_by_type(d)
        vac_norm = {_norm(k): v for k, v in vac_types.items()}
        try:
            snap = room_type_snapshot(property_id, str(d))
        except Exception:
            snap = {}
        listed_norm = {_norm(k): v.get("rate") for k, v in snap.items()}
        ev = event_for(d)
        ev_demand = str((ev or {}).get("demand", ""))
        show_event = ev is not None and not ev_demand.casefold().startswith("low")
        for room, rates in prop_pricing.items():
            room_vacancy = vac_norm.get(_norm(room))
            if room_vacancy == 0:
                continue  # this room type is fully occupied that night
            rec, why = recommend(rates, days_out, occ_pct, ev)
            listed = listed_norm.get(_norm(room))
            rec_rows.append({
                "Date": str(d), "Day": d.strftime("%a"),
                "Occ %": f"{occ_pct:.0%}" if occ_pct is not None else "n/a",
                "Room": room,
                "Event": (ev or {}).get("name", "—") if show_event else "—",
                "Demand": ev_demand if show_event else "—",
                "IA Rate (S$)": ladder_rate(rates, days_out),
                "Floor (S$)": rates["floor"],
                "Current (S$)": listed if listed is not None else "—",
                "Recommended (S$)": rec,
                "Reason": why,
            })
    if rec_rows:
        st.dataframe(pd.DataFrame(rec_rows), use_container_width=True, hide_index=True, height=500)
    else:
        st.success("All room types fully booked across the selected window — nothing to price.")
    st.caption("IA Rate: your ideal base rate >10 days out, stepping to the 7-10 then 4-7 day rates. "
               "Current: today's listed rate in Cloudbeds for that night. "
               "Below 85% occupancy: 10% cut, never below breakeven floor. "
               "Moderate demand events: hold rate (small 5% cut only if occupancy <70%). "
               "High/Very High demand events: priced above IA rate using the event's suggested markup "
               "(from the events tracker), default +10%/+20%.")
else:
    st.info(f"No pricing guide entry found for {property_name} — check data/pricing.json names.")

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

# ===== Events =====
st.subheader("Events — Heritage Collection relevance")
shown_events = [e for e in EVENTS
                if not str(e.get("demand", "")).casefold().startswith("low")]
if shown_events:
    ev_df = pd.DataFrame([{
        "Dates": e.get("dates"), "Event": e.get("name"), "Demand": e.get("demand"),
        "Venue": e.get("venue"), "Attendees": e.get("attendees"), "Why": e.get("rationale"),
    } for e in shown_events])
    st.dataframe(ev_df, use_container_width=True, hide_index=True)
    st.caption("Demand scored on past materialization + attendee count/type. "
               "We are 3.5-star, adults-only, shophouse CBD — day-attendee and family events score low.")

