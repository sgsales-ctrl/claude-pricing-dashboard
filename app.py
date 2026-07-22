import difflib
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

OCC_TARGET = 0.80          # below this, recommend discounting; at/above: hold or lift
SOFT_DISCOUNT = 0.90       # 10% cut when occupancy < 85%
HIGH_OCC_PREMIUM = 1.05    # small lift when nearly full

# Some Cloudbeds properties contain rooms that belong to other entities.
# Map: property name -> substring that must appear in the ROOM NAME to count.
ROOM_NAME_FILTERS = {
    "Heritage Collection on Seah": "seah",
    "Heritage Collection on Clarke Quay": "clarke quay",
}

# Properties temporarily excluded from the portfolio overview (e.g. closed for works)
CLOSED_PROPERTIES = {
    "Heritage Collection on Smith": "closed for refurbishment",
}

# Display order for the property dropdown
PROPERTY_ORDER = [
    "Heritage Collection on Boat Quay (Quayside Wing)",
    "Heritage Collection on Boat Quay (South Bridge Wing)",
    "Heritage Collection on Clarke Quay",
    "Heritage Collection on Seah",
    "Heritage Collection on Victoria",
    "Heritage Collection on KG",
    "Heritage Collection on Arab",
    "Heritage Collection on Chinatown",
    "Heritage Collection on Pagoda",
    "Heritage Collection on Ann Siang",
    "Heritage Collection on Boon Tat",
    "Heritage Collection on Smith",
]


def property_sort_key(name: str):
    for i, p in enumerate(PROPERTY_ORDER):
        if p.casefold() == name.casefold():
            return (i, "")
    return (len(PROPERTY_ORDER), name)  # unknown names go last, alphabetically


# Long-stay properties (min stay): vacancy shorter than GAP_MIN_NIGHTS consecutive
# nights is a 'gap' between bookings, not sellable vacancy
GAP_MIN_NIGHTS = 6
GAP_PROPERTIES = [
    "Heritage Collection on Ann Siang",
    "Heritage Collection on Boat Quay (South Bridge Wing)",
    "Heritage Collection on Smith",
    "Heritage Collection on Boon Tat",
]

# Individual rooms excluded from all calculations (e.g. out of service)
EXCLUDED_ROOMS = {
    "Heritage Collection on Arab": ["AR204"],
    "Heritage Collection on Chinatown": ["CN106"],
    "Heritage Collection on Boat Quay (Quayside Wing)": ["BQQ218"],
}


def _norm(s) -> str:
    """Normalize names for matching: lowercase, alphanumerics only."""
    return re.sub(r"[^a-z0-9]", "", str(s).casefold())


def apply_room_exclusions(pname: str, rooms: list) -> list:
    """Drop individually excluded rooms (e.g. out of service) for a property."""
    exc = next((v for k, v in EXCLUDED_ROOMS.items() if k.casefold() == pname.casefold()), None)
    if not exc:
        return rooms
    keys = [_norm(x) for x in exc if _norm(x)]
    return [r for r in rooms
            if not any(k in _norm(r.get("roomName", "")) for k in keys)]


def build_type_mapping(guide_names: list, cb_names: list) -> dict:
    """One-to-one best match: pricing-guide room name -> Cloudbeds room type name.
    Prevents overlapping names (e.g. 'Studio (No Window)' vs
    'Next-to-the-Bar Studio (No Window)') from stealing each other's match."""
    pairs = []
    for g in guide_names:
        gn = _norm(g)
        for c in cb_names:
            cn = _norm(c)
            if not gn or not cn:
                continue
            score = difflib.SequenceMatcher(None, gn, cn).ratio()
            if gn == cn:
                score = 3.0
            elif gn in cn or cn in gn:
                score = max(score, 1.0 + min(len(gn), len(cn)) / max(len(gn), len(cn)))
            pairs.append((score, g, c))
    pairs.sort(key=lambda x: -x[0])
    used_g, used_c, out = set(), set(), {}
    for s, g, c in pairs:
        if s < 0.6:
            break
        if g in used_g or c in used_c:
            continue
        out[g] = c
        used_g.add(g)
        used_c.add(c)
    return out


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
            "adults": 1, "children": 0, "rooms": 1,
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
def rates_for_type(property_id: str, room_type_id: str, start: str, end: str) -> dict:
    """Listed rate per date for one room type, from getRate. Quiet: returns {}
    on any failure (e.g. 'no rate found') instead of stopping the app."""
    try:
        r = requests.get(f"{BASE_URL}/getRate", headers=HEADERS, params={
            "propertyID": property_id, "roomTypeID": room_type_id,
            "startDate": start, "endDate": end,
            "adults": 1, "detailedRates": "true",
        }, timeout=15)
        body = r.json()
        if not body.get("success", False):
            return {}
        data = body.get("data") or {}
    except Exception:
        return {}
    det = data.get("roomRateDetailed") or [] if isinstance(data, dict) else []
    out = {}
    for x in det:
        if isinstance(x, dict) and x.get("date") is not None and x.get("rate") is not None:
            try:
                out[str(x["date"])] = round(float(x["rate"]))
            except (TypeError, ValueError):
                pass
    return out


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
def reservation_rooms_overlapping(property_id: str, start: str, end: str) -> list:
    """Room-level bookings (with room TYPE) for reservations that could cover
    [start, end] — from getReservationsWithRateDetails, fully paginated.
    Includes bookings not yet assigned to a physical room."""
    out, page, prev_first = [], 1, None
    while page <= MAX_PAGES:
        batch = cloudbeds_get("getReservationsWithRateDetails", {
            "propertyID": property_id,
            "reservationCheckOutFrom": start,
            "excludeStatuses": "canceled,no_show",
            "pageNumber": page, "pageSize": 100,
        })
        if not batch:
            break
        first = str(batch[0].get("reservationID", "")) if isinstance(batch[0], dict) else str(batch[0])
        if first and first == prev_first:
            break
        prev_first = first
        for r in batch:
            if not isinstance(r, dict):
                continue
            if str(r.get("status", "")).lower() in ("canceled", "cancelled", "no_show"):
                continue
            for room in r.get("rooms") or []:
                if not isinstance(room, dict):
                    continue
                if str(room.get("roomStatus", "")).lower() == "cancelled":
                    continue
                try:
                    ci = pd.to_datetime(room.get("roomCheckIn")).date()
                    co = pd.to_datetime(room.get("roomCheckOut")).date()
                except (TypeError, ValueError):
                    continue
                if str(ci) <= end:  # arrives before window ends
                    out.append({"type": str(room.get("roomTypeName", "")), "ci": ci, "co": co})
        if len(batch) < 100:
            break
        page += 1
    return out


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


def split_vacant_for_date(rooms_list: list, booked_rooms: list,
                          allowed_types: frozenset | None, target: date) -> tuple[int, int]:
    """For a long-stay property, split vacant rooms on `target` into
    (sellable_vacant, gap_rooms): a vacant room-type is sellable if it stays
    open for GAP_MIN_NIGHTS consecutive nights, otherwise it's a gap."""
    totals = {}
    for r in rooms_list:
        t = str(r.get("roomTypeName", ""))
        totals[t] = totals.get(t, 0) + 1
    an = {_norm(t) for t in allowed_types} if allowed_types is not None else None

    def vac_on(day: date) -> dict:
        occ = {}
        for br in booked_rooms:
            if an is not None and _norm(br["type"]) not in an:
                continue
            if br["ci"] <= day < br["co"]:
                key = br["type"] if br["type"] in totals else next(
                    (t for t in totals if _norm(t) == _norm(br["type"])), None)
                if key:
                    occ[key] = occ.get(key, 0) + 1
        return {t: max(totals[t] - occ.get(t, 0), 0) for t in totals}

    vt = vac_on(target)
    sellable = gap = 0
    gap_bits = []
    for t, cnt in vt.items():
        if cnt <= 0:
            continue
        run, dd = 1, target - timedelta(days=1)
        lo = target - timedelta(days=GAP_MIN_NIGHTS)
        while dd >= lo and run < 2 * GAP_MIN_NIGHTS and vac_on(dd).get(t, 0) > 0:
            run += 1; dd -= timedelta(days=1)
        dd, hi = target + timedelta(days=1), target + timedelta(days=GAP_MIN_NIGHTS)
        while dd <= hi and run < 2 * GAP_MIN_NIGHTS and vac_on(dd).get(t, 0) > 0:
            run += 1; dd += timedelta(days=1)
        if run >= GAP_MIN_NIGHTS:
            sellable += cnt
        else:
            gap += cnt
            gap_bits.append(f"{run}n ({cnt} rm)")
    gap_detail = "; ".join(gap_bits) if gap_bits else "—"
    return sellable, gap, gap_detail


def occupancy_for_dates(property_id: str, days: list[date], total_rooms: int,
                        allowed_types: frozenset | None = None,
                        room_keys: frozenset | None = None) -> dict:
    """Occupied-ROOM count per night, from room-level booking data
    (getReservationsWithRateDetails): a booking holding 2 rooms counts as 2.
    Includes bookings not yet assigned to a physical room.
    allowed_types (room-name-filtered properties): only rooms of those types
    count — other entities sharing the same Cloudbeds property are excluded."""
    if not days:
        return {}
    try:
        booked = reservation_rooms_overlapping(property_id, str(min(days)), str(max(days)))
    except Exception:
        booked = None
    counts = {d: 0 for d in days}
    if booked is not None:
        allowed_norm = ({_norm(t) for t in allowed_types} if allowed_types is not None else None)
        for br in booked:
            if allowed_norm is not None and _norm(br["type"]) not in allowed_norm:
                continue
            for d in counts:
                if br["ci"] <= d < br["co"]:
                    counts[d] += 1
    else:
        # Fallback: reservation-level counting (one booking = one room)
        try:
            res = reservations_overlapping(property_id, str(min(days)), str(max(days)))
        except Exception:
            return {d: None for d in days}
        for r in res:
            rt = r.get("roomTypeName") or r.get("roomType") or ""
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
    if total_rooms:  # a room can't be occupied twice; cap at total
        counts = {d: min(c, total_rooms) for d, c in counts.items()}
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


COMP_UNDERCUT = 0.95  # target ~5% below competitor equivalent-category rate


# Display buckets for our rooms (tabs) and the competitor category each maps to
CATEGORY_TABS = ["Single", "Studio", "Loft", "Premium", "Top"]
COMP_CAT_FOR = {"Single": "Single", "Studio": "Studio", "Premium": "Studio",
                "Loft": "Loft", "Top": "Suite"}


def room_category(name: str) -> str:
    """Bucket our room name into a display category (Single/Studio/Loft/Premium/Top)."""
    n = name.casefold()
    if "single" in n:
        return "Single"
    if "bedroom" in n or "raffles" in n or "stamford" in n or "suite" in n:
        return "Top"
    if "premium" in n or "deluxe" in n:
        return "Premium"
    if "loft" in n:
        return "Loft"
    if "studio" in n:
        return "Studio"
    return "Studio"


def comp_rate_on(day_snapshot: dict, competitor: str, display_category: str):
    """One competitor's rate (incl. taxes) for the equivalent category on a date.
    Returns None if sold out for that category (excluded from benchmark)."""
    info = day_snapshot.get(competitor, {})
    if info.get("status") != "ok":
        return None
    comp_cat = COMP_CAT_FOR.get(display_category, "Studio")
    bycat = info.get("by_category")
    if isinstance(bycat, dict):
        r = bycat.get(comp_cat)          # sold out for this category -> None (excluded)
    else:
        r = info.get("est_incl_taxes")   # old flat data: cheapest-room proxy
    try:
        return float(r) if r is not None else None
    except (TypeError, ValueError):
        return None


def comp_category_median(sector_comps: list, scraped_day: dict, category: str):
    """Median competitor rate (incl. taxes) for the equivalent room category,
    excluding competitors whose equivalent category is sold out."""
    vals = [v for c in sector_comps if (v := comp_rate_on(scraped_day, c, category)) is not None]
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    return (vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2)


def recommend(rates: dict, days_out: int, occ: float | None, ev, comp_median: float | None = None) -> tuple[float, str]:
    base = ladder_rate(rates, days_out)
    floor = rates["floor"]
    demand = (ev or {}).get("demand", "")
    # ---- Step 1: own occupancy / event-based rate ----
    if demand in ("High", "Very High"):
        m = re.search(r"(\d+)\s*-\s*(\d+)\s*%", str((ev or {}).get("rationale", "")))
        up = (int(m.group(1)) + int(m.group(2))) / 200 if m else (0.20 if demand == "Very High" else 0.10)
        occ_rate = max(base, rates["ia"]) * (1 + up)
        occ_reason = f"{demand} event +{up:.0%} vs IA"
    elif demand == "Moderate":
        if occ is None or occ >= 0.70:
            occ_rate, occ_reason = base, "Moderate event — hold"
        else:
            occ_rate, occ_reason = base * 0.95, "Moderate event, occ<70% — 5% cut"
    elif occ is None:
        occ_rate, occ_reason = base, "No occ data — IA/window rate"
    elif occ >= 0.95:
        occ_rate, occ_reason = base * HIGH_OCC_PREMIUM, "occ ≥95% — premium"
    elif occ >= OCC_TARGET:
        occ_rate, occ_reason = base, f"occ ≥{OCC_TARGET:.0%} — hold"
    else:
        occ_rate, occ_reason = base * SOFT_DISCOUNT, f"occ <{OCC_TARGET:.0%} — 10% cut"
    occ_rate = max(occ_rate, floor)
    # ---- Step 2: blend with competitor equivalent-category rate ----
    if not comp_median:
        return round(occ_rate), occ_reason + " · no comp data"
    anchor = comp_median * COMP_UNDERCUT  # ~5% below competitor
    if occ is not None and occ >= 0.85:
        rec = max(occ_rate, anchor)
        note = f"≥85% occ — take higher of own vs comp (comp S${comp_median:.0f})"
    elif occ is not None and occ < OCC_TARGET:
        rec = min(occ_rate, anchor)
        note = f"soft occ — ~5% below comp S${comp_median:.0f} to win share"
    else:
        rec = anchor
        note = f"~5% below comp median S${comp_median:.0f}"
    if demand in ("High", "Very High"):
        rec = max(rec, occ_rate)  # never undercut an event uplift
    rec = max(rec, floor)
    return round(rec), occ_reason + " · " + note


def overview_pricing(pname: str, rd: list, booked: list, allowed_types: frozenset | None,
                     target: date, occ_pct: float | None, is_gap: bool):
    """For the overview: available (sellable) room types with recommended price,
    the event, and the rate reason for `target`. Returns (available_str, event_str, reason_str)."""
    pp = PRICING.get(pname)
    if pp is None:
        k = next((k for k in PRICING if k.casefold().replace(" ", "") == pname.casefold().replace(" ", "")), None)
        pp = PRICING.get(k, {})
    if not pp:
        return "no pricing guide", "—", "—"
    cb_types = {}
    for r in rd:
        t = str(r.get("roomTypeName", ""))
        if t and t not in cb_types:
            cb_types[t] = True
    tmap = build_type_mapping(list(pp.keys()), list(cb_types.keys()))
    an = {_norm(t) for t in allowed_types} if allowed_types is not None else None

    def vac_on(day: date) -> dict:
        totals = {}
        for r in rd:
            t = str(r.get("roomTypeName", ""))
            totals[t] = totals.get(t, 0) + 1
        occ = {}
        for br in booked:
            if an is not None and _norm(br["type"]) not in an:
                continue
            if br["ci"] <= day < br["co"]:
                key = br["type"] if br["type"] in totals else next(
                    (t for t in totals if _norm(t) == _norm(br["type"])), None)
                if key:
                    occ[key] = occ.get(key, 0) + 1
        return {t: max(totals[t] - occ.get(t, 0), 0) for t in totals}

    vac = vac_on(target)
    ev = event_for(target)
    ev_demand = str((ev or {}).get("demand", ""))
    show_event = ev is not None and not ev_demand.casefold().startswith("low")
    days_out = (target - date.today()).days
    rooms_out, prices_out, reason = [], [], "—"
    for guide_room, rates in pp.items():
        cb = tmap.get(guide_room)
        if not cb or vac.get(cb, 0) <= 0:
            continue
        if is_gap:  # skip gap-only vacancy (not sellable under min stay)
            run, dd = 1, target - timedelta(days=1)
            while run < GAP_MIN_NIGHTS and vac_on(dd).get(cb, 0) > 0:
                run += 1; dd -= timedelta(days=1)
            dd = target + timedelta(days=1)
            while run < GAP_MIN_NIGHTS and vac_on(dd).get(cb, 0) > 0:
                run += 1; dd += timedelta(days=1)
            if run < GAP_MIN_NIGHTS:
                continue
        rec, why = recommend(rates, days_out, occ_pct, ev)
        rooms_out.append(guide_room)
        prices_out.append(f"S${rec}")
        reason = why
    if rooms_out:
        available_str = "; ".join(rooms_out)
        price_str = "; ".join(prices_out)
    else:  # fully booked / nothing sellable
        available_str = price_str = reason = "—"
    event_str = (ev or {}).get("name", "No event") if show_event else "No event"
    return available_str, price_str, event_str, reason


# Location keywords per sector, for deciding which properties an event impacts
SECTOR_KEYWORDS = {
    "QUAYS": ["quay", "quays", "river valley", "clarke", "boat quay", "robertson"],
    "CHINATOWN": ["chinatown", "tanjong pagar", "mandopop", "chinese", "maxwell"],
    "CITY HALL": ["city hall", "seah", "bras basah", "capitol", "esplanade"],
    "KAMPONG GLAM": ["kampong glam", "kallang", "arab", "sultan", "bugis", "malay", "victoria"],
}
_GENERIC_TOKENS = {"boat", "quay", "quays", "wing", "south", "bridge", "north",
                   "quayside", "heritage", "collection", "the", "on", "hc"}


def event_impact(pname_short: str, sector: str, ev) -> str:
    """Does this property catch the event's demand, by location? Direct / Overflow / —."""
    if not ev or str(ev.get("demand", "")).casefold() in ("low", "low-moderate", ""):
        return "—"
    text = " ".join(str(ev.get(k, "")) for k in
                    ("best_fit", "rationale", "attendees", "venue", "name")).lower()
    toks = [t for t in re.split(r"[^a-z0-9]+", pname_short.lower())
            if t and len(t) >= 3 and t not in _GENERIC_TOKENS]
    if any(t in text for t in toks):
        return "Direct"
    if any(kw in text for kw in SECTOR_KEYWORDS.get(sector, [])):
        return "Overflow"
    return "—"


# ---------- Page ----------
st.title("Claude Pricing Dashboard")

properties = get_properties()
if not properties:
    st.error("No properties found for this API key.")
    st.stop()

st.sidebar.header("Filters")
view = st.sidebar.radio("View", ["Portfolio overview", "Property detail", "Competitor analysis"])

if view == "Portfolio overview":
    st.subheader("Portfolio overview")
    ov_date = st.sidebar.date_input("Overview date", date.today())
    is_today = ov_date == date.today()
    day_label = "tonight" if is_today else ov_date.strftime("%a %d %b %Y")
    horizon = [ov_date + timedelta(days=i) for i in range(8)]
    if not is_today:
        st.caption(f"Showing data for **{day_label}** — change it via 'Overview date' in the sidebar.")

    # Upcoming high-demand events (3 weeks from the selected date)
    upcoming = []
    for ev in EVENTS:
        if str(ev.get("demand")) not in ("High", "Very High"):
            continue
        m = re.match(r"(\d{4}-\d{2}-\d{2})", str(ev.get("dates", "")))
        if m:
            start = date.fromisoformat(m.group(1))
            if ov_date <= start <= ov_date + timedelta(days=21):
                upcoming.append(f"{ev['name']} ({ev['dates']}, {ev['demand']})")
    ev_on_date = event_for(ov_date)
    if ev_on_date and str(ev_on_date.get("demand", "")).casefold() not in ("low", "low-moderate"):
        st.info(f"Event on {day_label}: {ev_on_date['name']} ({ev_on_date.get('demand')})")
    if upcoming:
        st.info("Upcoming high-demand events (3 weeks): " + " • ".join(upcoming[:4]))

    ov_rows, port_sold, port_total, port_gap = [], 0, 0, 0
    progress = st.progress(0.0, text="Loading properties…")
    plist = [(p, i) for p, i in properties.items()
             if not any(c.casefold() == p.casefold() for c in CLOSED_PROPERTIES)]
    for i, (pname, pid) in enumerate(plist):
        nf = next((v for k, v in ROOM_NAME_FILTERS.items()
                   if k.casefold() == pname.casefold()), None)
        try:
            rd = get_rooms(pid)
        except Exception:
            rd = []
        if nf:
            key = _norm(nf)
            rd = [r for r in rd
                  if key in _norm(r.get("roomName", "")) or key in _norm(r.get("roomTypeName", ""))]
        rd = apply_room_exclusions(pname, rd)
        tot = len(rd)
        at = (frozenset(str(r.get("roomTypeName")) for r in rd if r.get("roomTypeName"))
              if nf else None)
        rk = (frozenset(list({str(r.get("roomID") or "") for r in rd} - {""}) +
                        [_norm(r.get("roomName", "")) for r in rd if r.get("roomName")])
              if nf else None)
        try:
            occ = occupancy_for_dates(pid, horizon, tot, at, rk)
        except Exception:
            occ = {}
        o0 = occ.get(ov_date)
        pct = (o0 / tot) if (tot and o0 is not None) else None
        week_counts = [occ[d] for d in horizon if occ.get(d) is not None]
        avg7 = (sum(week_counts) / (len(week_counts) * tot)) if (tot and week_counts) else None
        total_vac = (tot - o0) if (o0 is not None and tot) else None
        # Room-level bookings for this property/date window (cached)
        try:
            booked = reservation_rooms_overlapping(
                pid, str(ov_date - timedelta(days=GAP_MIN_NIGHTS)),
                str(ov_date + timedelta(days=7 + GAP_MIN_NIGHTS)))
        except Exception:
            booked = []
        # Long-stay properties: split vacant into sellable vs gap
        sellable_vac, gap_rooms, gap_detail = total_vac, 0, "—"
        is_gap = any(g.casefold() == pname.casefold() for g in GAP_PROPERTIES)
        if is_gap and total_vac:
            try:
                sellable_vac, gap_rooms, gap_detail = split_vacant_for_date(rd, booked, at, ov_date)
            except Exception:
                pass
        if o0 is not None and tot:
            port_sold += o0
            port_total += tot
            port_gap += gap_rooms
        p_sector = next((s for s, v in COMPETITORS.items()
                         if isinstance(v, dict) and pname in v.get("hc_properties", [])), "—")
        impact_str = event_impact(pname.replace("Heritage Collection on ", ""),
                                  p_sector, event_for(ov_date))
        # Available room types + recommended prices, event, and reason for this date
        try:
            avail_str, price_str, event_str, reason_str = overview_pricing(pname, rd, booked, at, ov_date, pct, is_gap)
        except Exception:
            avail_str, price_str, event_str, reason_str = "—", "—", "—", "—"
        ov_rows.append({
            "Property": pname.replace("Heritage Collection on ", ""),
            "Occ %": f"{pct:.0%}" if pct is not None else "n/a",
            "Sold": f"{o0}/{tot}" if (o0 is not None and tot) else "n/a",
            "Vacant": sellable_vac if sellable_vac is not None else None,
            "Gap": (gap_detail if gap_rooms else "0") if is_gap else "—",
            "Next 7d avg": f"{avg7:.0%}" if avg7 is not None else "n/a",
            "Posture": ("Discount" if pct < OCC_TARGET else "Hold/Lift") if pct is not None else "n/a",
            "Available": avail_str,
            "Rec. price": price_str,
            "Event": event_str,
            "Event fit": impact_str,
            "Reason": reason_str,
            "Sector": p_sector,
            "_sort": pct if pct is not None else 2,
        })
        progress.progress((i + 1) / len(plist), text=f"Loading properties… {pname}")
    progress.empty()

    m1, m2, m3 = st.columns(3)
    m1.metric(f"Portfolio occupancy — {day_label}",
              f"{port_sold / port_total:.0%}" if port_total else "n/a",
              f"{port_sold}/{port_total} sold")
    m2.metric(f"Vacant rooms — {day_label}",
              (port_total - port_sold - port_gap) if port_total else "n/a",
              f"+{port_gap} gap" if port_gap else None, delta_color="off")
    below_n = sum(1 for r in ov_rows if r["Posture"] == "Discount")
    m3.metric(f"Properties under 80% — {day_label}", f"{below_n}/{len(ov_rows)}")

    ov_rows.sort(key=lambda r: r["_sort"])  # weakest occupancy first
    ov_df = pd.DataFrame(ov_rows).drop(columns=["_sort"])
    st.dataframe(ov_df, use_container_width=True, hide_index=True)
    if CLOSED_PROPERTIES:
        st.caption("Excluded: " + "; ".join(
            f"{k.replace('Heritage Collection on ', '')} ({v})" for k, v in CLOSED_PROPERTIES.items()))
    st.caption(f"All figures are for {day_label}; 'Next 7d avg' covers the 7 days from that date. "
               "Sorted weakest-occupancy first. Posture: Discount below 80%, Hold/Lift at/above. "
               f"Vacant excludes gaps for long-stay properties (Ann Siang, BQ South Bridge, Smith, Boon Tat): "
               f"a vacant stretch under {GAP_MIN_NIGHTS} nights is counted under Gap, not Vacant. "
               "Event fit: Direct = event names this property/area; Overflow = its sector catches spillover. "
               "Switch to Property detail (sidebar) for room-level price recommendations.")
    st.stop()

property_name = st.sidebar.selectbox("Property", sorted(properties.keys(), key=property_sort_key))
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
rooms_data = apply_room_exclusions(property_name, rooms_data)
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

# Per-room-type listed rates + type mapping (shared by detail & competitor views)
cb_types = {}
for r in rooms_data:
    tname, tid = str(r.get("roomTypeName", "")), str(r.get("roomTypeID") or "")
    if tname and tname not in cb_types:
        cb_types[tname] = tid
type_map = build_type_mapping(list(prop_pricing.keys()), list(cb_types.keys())) if prop_pricing else {}
listed_maps = {}
if prop_pricing:
    for cb_name, tid in cb_types.items():
        if not tid:
            listed_maps[cb_name] = {}
            continue
        try:
            listed_maps[cb_name] = rates_for_type(property_id, tid, str(window_days[0]),
                                                  str(window_days[-1] + timedelta(days=1)))
        except Exception:
            listed_maps[cb_name] = {}

# ===== Competitor analysis — per room type =====
if view == "Competitor analysis":
    st.subheader("Competitor Analysis — per room type")
    st.caption("For each of our room types, current rate + recommended next to the comparable "
               "competitor properties. Rates inclusive of taxes & fees.")
    if not prop_pricing:
        st.info(f"No pricing guide entry found for {property_name}.")
        st.stop()
    scraped = COMP_RATES.get("rates", {})
    tonight = date.today()
    # group our rooms by display category
    by_cat = {c: [] for c in CATEGORY_TABS}
    for room in prop_pricing:
        by_cat.setdefault(room_category(room), []).append(room)
    tabs = st.tabs(CATEGORY_TABS)
    for tab, cat in zip(tabs, CATEGORY_TABS):
        with tab:
            rooms_here = by_cat.get(cat, [])
            if not rooms_here:
                st.caption(f"No {cat} room types at {property_name.replace('Heritage Collection on ','')}.")
                continue
            for room in rooms_here:
                rates = prop_pricing[room]
                cb_name = type_map.get(room)
                st.markdown(f"**{room}**")
                rows = []
                for d in window_days:
                    ds = str(d)
                    days_out = (d - tonight).days
                    occ_n = occ_counts.get(d)
                    occ_pct = (occ_n / total_rooms) if (total_rooms and occ_n is not None) else None
                    ev = event_for(d)
                    listed = (listed_maps.get(cb_name) or {}).get(ds) if cb_name else None
                    day_snap = scraped.get(ds, {})
                    comp_vals = {}
                    for comp in sector_comps:
                        comp_vals[comp] = comp_rate_on(day_snap, comp, cat)
                    present = [v for v in comp_vals.values() if v is not None]
                    cheapest = min(present) if present else None
                    comp_med = comp_category_median(sector_comps, day_snap, cat)
                    rec, _ = recommend(rates, days_out, occ_pct, ev, comp_med)
                    row = {
                        "Date": d.strftime("%m-%d"), "DOW": d.strftime("%a"),
                        "Our rate": f"${listed:.0f}" if listed is not None else "None",
                        "Our rec": f"${rec:.0f}",
                    }
                    for comp in sector_comps:
                        v = comp_vals[comp]
                        row[f"{comp} ({COMP_CAT_FOR.get(cat, 'Studio')})"] = f"${v:.0f}" if v is not None else "None"
                    row["Cheapest comp"] = f"${cheapest:.0f}" if cheapest is not None else "None"
                    if cheapest is not None:
                        diff = rec - cheapest
                        row["Our rec vs cheapest"] = f"${'+' if diff >= 0 else '-'}{abs(diff):.0f}"
                    else:
                        row["Our rec vs cheapest"] = "None"
                    rows.append(row)
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    st.caption("Competitor rates come from the daily Booking.com scrape (by room category, incl. taxes & fees); "
               "dates not yet scraped show None. A competitor whose equivalent category is sold out is excluded. "
               "Our rec targets ~5% below the competitor median, taking the higher of own-rate vs competitor when occupancy ≥85%.")
    st.stop()

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
              f"occupancy under {OCC_TARGET:.0%}" if below else f"occupancy at/above {OCC_TARGET:.0%}")
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
    """Vacancy per room type on a date, from room-level booking data
    (includes bookings not yet assigned to a physical room)."""
    totals = {}
    for r in rooms_data:
        t = str(r.get("roomTypeName", ""))
        totals[t] = totals.get(t, 0) + 1
    try:
        booked_rooms = reservation_rooms_overlapping(
            property_id,
            str(min(window_days) - timedelta(days=GAP_MIN_NIGHTS)),
            str(max(window_days) + timedelta(days=GAP_MIN_NIGHTS)))
    except Exception:
        booked_rooms = []
    occupied = {}
    for br in booked_rooms:
        if not (br["ci"] <= d < br["co"]):
            continue
        rt = br["type"]
        key = rt if rt in totals else next((t for t in totals if _norm(t) == _norm(rt)), None)
        if key:
            occupied[key] = occupied.get(key, 0) + 1
    return {t: max(totals[t] - occupied.get(t, 0), 0) for t in totals}


# ===== Price recommendations =====
st.subheader("Price recommendations")
if prop_pricing:
    # Competitor rates (latest scraped snapshot) for this property's sector, by category
    _scraped = COMP_RATES.get("rates", {})
    _comp_day = _scraped.get(max(_scraped.keys())) if _scraped else {}
    comp_med_cache = {}

    def comp_med_for(room_name: str):
        cat = room_category(room_name)
        if cat not in comp_med_cache:
            comp_med_cache[cat] = comp_category_median(sector_comps, _comp_day, cat) if sector_comps else None
        return cat, comp_med_cache[cat]

    # type_map and listed_maps are computed above (shared with the competitor view)
    is_gap_prop = any(g.casefold() == property_name.casefold() for g in GAP_PROPERTIES)

    def vacancy_run(cb_type: str, d0: date) -> int:
        """Consecutive nights (containing d0) on which this room type has ≥1 vacant room."""
        run = 1
        lo = min(window_days) - timedelta(days=GAP_MIN_NIGHTS)
        hi = max(window_days) + timedelta(days=GAP_MIN_NIGHTS)
        dd = d0 - timedelta(days=1)
        while dd >= lo and run < 2 * GAP_MIN_NIGHTS and vacant_count_by_type(dd).get(cb_type, 0) > 0:
            run += 1
            dd -= timedelta(days=1)
        dd = d0 + timedelta(days=1)
        while dd <= hi and run < 2 * GAP_MIN_NIGHTS and vacant_count_by_type(dd).get(cb_type, 0) > 0:
            run += 1
            dd += timedelta(days=1)
        return run

    rec_rows = []
    for d in window_days:
        days_out = (d - tonight).days
        occ_n = occ_counts.get(d)
        occ_pct = (occ_n / total_rooms) if (total_rooms and occ_n is not None) else None
        if occ_pct is not None and occ_pct >= 1.0:
            continue  # fully booked — nothing to price
        vac_types = vacant_count_by_type(d)
        ev = event_for(d)
        ev_demand = str((ev or {}).get("demand", ""))
        show_event = ev is not None and not ev_demand.casefold().startswith("low")
        for room, rates in prop_pricing.items():
            cb_name = type_map.get(room)
            room_vacancy = vac_types.get(cb_name) if cb_name else None
            if room_vacancy == 0:
                continue  # this room type is fully occupied that night
            vac_label = "Vacant"
            if is_gap_prop and cb_name and room_vacancy:
                run = vacancy_run(cb_name, d)
                if run < GAP_MIN_NIGHTS:
                    vac_label = f"Gap ({run} night{'s' if run != 1 else ''})"
            cat, comp_med = comp_med_for(room)
            rec, why = recommend(rates, days_out, occ_pct, ev, comp_med)
            listed = (listed_maps.get(cb_name) or {}).get(str(d)) if cb_name else None
            rec_rows.append({
                "Date": str(d), "Day": d.strftime("%a"),
                "Occ %": f"{occ_pct:.0%}" if occ_pct is not None else "n/a",
                "Room": room,
                "Vacancy": vac_label,
                "Event": (ev or {}).get("name", "No event") if show_event else "No event",
                "Demand": ev_demand if show_event else "None",
                "IA Rate (S$)": ladder_rate(rates, days_out),
                "Floor (S$)": rates["floor"],
                "Current (S$)": listed if listed is not None else "—",
                "Comp cat": cat,
                "Comp median (S$)": round(comp_med) if comp_med else "—",
                "Recommended (S$)": rec,
                "Reason": why,
            })
    if rec_rows:
        st.dataframe(pd.DataFrame(rec_rows), use_container_width=True, hide_index=True, height=500)
    else:
        st.success("All room types fully booked across the selected window — nothing to price.")
    unmatched = [room for room in prop_pricing if room not in type_map]
    if unmatched:
        st.warning("These pricing-guide room names don't match any Cloudbeds room type "
                   f"(vacancy can't be verified for them): {', '.join(unmatched)}. "
                   "Align the names in data/pricing.json with Cloudbeds.")
    with st.expander("Room-type name mapping (pricing guide → Cloudbeds)"):
        st.dataframe(pd.DataFrame([{"Pricing guide": g, "Cloudbeds type": c} for g, c in type_map.items()]),
                     use_container_width=True, hide_index=True)
    st.caption("IA Rate: your ideal base rate >10 days out, stepping to the 7-10 then 4-7 day rates. "
               "Current: today's listed rate in Cloudbeds for that night. "
               f"Below {OCC_TARGET:.0%} occupancy: 10% cut, never below breakeven floor; at/above: hold or lift. "
               "Moderate demand events: hold rate (small 5% cut only if occupancy <70%). "
               "High/Very High demand events: priced above IA rate using the event's suggested markup "
               "(from the events tracker), default +10%/+20%. "
               "Comp median = competitor rate for the equivalent room category (latest scrape); we target "
               "~5% below it, but when our occupancy ≥85% we take the higher of own-rate vs competitor. "
               f"Long-stay properties (Ann Siang, BQ South Bridge, Smith, Boon Tat): vacancy shorter than "
               f"{GAP_MIN_NIGHTS} consecutive nights shows as a Gap, not sellable vacancy.")
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


def impacted_properties(ev) -> str:
    """List HC properties that catch this event's demand, grouped Direct / Overflow."""
    direct, overflow = [], []
    for pn in properties:
        short = pn.replace("Heritage Collection on ", "")
        sec = next((s for s, v in COMPETITORS.items()
                    if isinstance(v, dict) and pn in v.get("hc_properties", [])), "—")
        imp = event_impact(short, sec, ev)
        if imp == "Direct":
            direct.append(short)
        elif imp == "Overflow":
            overflow.append(short)
    parts = []
    if direct:
        parts.append("Direct: " + ", ".join(direct))
    if overflow:
        parts.append("Overflow: " + ", ".join(overflow))
    return " · ".join(parts) if parts else "—"


if shown_events:
    ev_df = pd.DataFrame([{
        "Dates": e.get("dates"), "Event": e.get("name"), "Demand": e.get("demand"),
        "Impacted properties (by location)": impacted_properties(e),
        "Venue": e.get("venue"), "Attendees": e.get("attendees"), "Why": e.get("rationale"),
    } for e in shown_events])
    st.dataframe(ev_df, use_container_width=True, hide_index=True)
    st.caption("Demand scored on past materialization + attendee count/type. "
               "We are 3.5-star, adults-only, shophouse CBD — day-attendee and family events score low. "
               "Impacted properties: Direct = event names the property/area; Overflow = its sector catches spillover.")

