# Heritage Collection — Pricing Strategy (dashboard logic)

This documents how the dashboard recommends a rate. Update this file when the rules change.

## Inputs to every recommendation
1. **IA rate ladder** (own pricing guide, `data/pricing.json`): ideal base rate >10 days out,
   stepping down to the 7–10 day and 4–7 day rates as arrival approaches. Breakeven floor always applies.
2. **Own occupancy** for the night (room-level, from Cloudbeds reservations).
3. **Events** (`data/events.json`) — demand and suggested markup.
4. **Competitor rate for the EQUIVALENT room category** (`data/comp_rates.json`, Booking.com).

## Room categories (for competitor comparison)
Our room → category: any name with "Single" → **Single**; "Bedroom"/Raffles/Stamford → **Suite**;
"Loft" → **Loft**; else "Studio" → **Studio**. Example: Chinatown "Studio Single (Skylight)" → **Single**,
so it is compared against competitors' Single rooms — not their studios.

## The rule (in order)
1. **Own occupancy / event rate** first:
   - High / Very High event → price **above** IA using the event's suggested markup (default +10% / +20%).
   - Moderate event → hold IA (small 5% cut only if occupancy <70%).
   - Occupancy ≥95% → small premium; ≥80% → hold; <80% → 10% cut. Never below floor.
2. **Competitor blend** (equivalent category, sector competitors, latest scrape):
   - Target **~5% below** the competitor median for that category (`COMP_UNDERCUT = 0.95`).
   - If our occupancy **≥85%** → **take the higher** of (own-occupancy rate, competitor-anchored rate) — capture ADR when both signals are strong.
   - If occupancy **<80%** → take the **lower** of the two (≈5% below competitor) to win share.
   - In between → sit ~5% below competitor.
   - High/Very High event uplift is never undercut by the competitor step.
   - Never below the breakeven floor.

## Data note
Competitor rates come from the daily Booking.com scrape. The scrape captures each competitor's rate
**by room category** (Single / Studio / Loft / Suite) when visible, plus the cheapest available rate as
a fallback proxy. Competitor rates for future dates use the latest scraped snapshot as the reference.

## Parameters (in app.py)
- `OCC_TARGET = 0.80` — discount threshold.
- `COMP_UNDERCUT = 0.95` — target position vs competitor equivalent-category median.
- `PEAK_MONTHS = {7, 8}` — Jul/Aug peak; the 5%-below-competitor step applies only when occupancy <80%; at ≥80% take the higher of own rate vs competitor (never undercut).
- `GAP_MIN_NIGHTS = 6` — long-stay gap threshold (Ann Siang, BQ South Bridge, Smith, Boon Tat).

## Peak season (July & August)
Singapore peak months. In **July and August** the competitor step is gated by our own occupancy:
- **Occupancy < 80%** → price **~5% below** the competitor median for that category (undercut to win share while we still have rooms to fill).
- **Occupancy ≥ 80%** → **take the higher of our own rate and the competitor rate** — never price below the competitor when we are already busy.
- The breakeven floor still applies, and a High / Very High event uplift is never undercut.
- Outside July / August, the general competitor rule above applies unchanged.

Controlled by `PEAK_MONTHS = {7, 8}` in `app.py`; the Competitor Analysis Rationale column shows which branch applied on each date.
