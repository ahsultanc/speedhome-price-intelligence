"""
app.py — Property Price Intelligence (SPEEDHOME.com) Streamlit UI.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import time

import pandas as pd
import plotly.express as px
import streamlit as st

from scraper import (
    AREAS,
    filter_by_area,
    filter_by_rental_type,
    is_speedhome_url,
    scrape_area,
)
from utils import (
    build_summary,
    excel_filename,
    generate_insights,
    humanize_for_display,
    make_excel,
)

# --------------------------------------------------------------------------- #
# Page config (must be the first Streamlit call)
# --------------------------------------------------------------------------- #
st.set_page_config(
    page_title="Property Price Intelligence — SPEEDHOME",
    page_icon="🏠",
    layout="wide",
)

# Seed list for the autocomplete dropdown (single source of truth in scraper.py).
SEED_AREAS = AREAS

RENTAL_TABS = [("Daily", "daily"), ("Monthly", "monthly"), ("Yearly", "yearly")]


# --------------------------------------------------------------------------- #
# Cached scraping (session-level caching)
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner=False, ttl=3600)
def cached_scrape(query: str):
    """Scrape once per area/URL and cache for the session/hour.

    The Daily / Monthly / Yearly tabs all reuse this single result and filter it
    locally, so we never hit SPEEDHOME more than necessary.
    """
    return scrape_area(query)


# --------------------------------------------------------------------------- #
# Sidebar — search controls
# --------------------------------------------------------------------------- #
st.sidebar.header("🔎 Search")

mode = st.sidebar.radio(
    "Search by",
    ["Area name", "Direct URL"],
    help="Pick an area from the seed list, or paste a SPEEDHOME URL.",
)

if mode == "Area name":
    query_input = st.sidebar.selectbox(
        "Area (autocomplete)",
        options=SEED_AREAS,
        index=0,
        help="Type to filter the list.",
    )
else:
    query_input = st.sidebar.text_input(
        "SPEEDHOME URL",
        value="https://speedhome.com/rent/mont-kiara",
        help="e.g. https://speedhome.com/rent/mont-kiara",
    )

strict_area = st.sidebar.checkbox(
    "Only show listings in this area",
    value=True,
    help="SPEEDHOME returns nearby areas too (e.g. Mont Kiara also pulls in "
    "Segambut, Sentul…). When ticked, only listings whose name/address actually "
    "mentions the searched area are kept.",
)

st.sidebar.caption(
    "Polite scraping: 1.5s delay between requests · only `/rent/` paths · "
    "all pages fetched."
)
search = st.sidebar.button("Search", type="primary", use_container_width=True)

# Persist the active query across reruns (tab clicks, downloads).
if search:
    st.session_state["active_query"] = (query_input or "").strip()

active_query = st.session_state.get("active_query")


# --------------------------------------------------------------------------- #
# Header
# --------------------------------------------------------------------------- #
st.title("🏠 Property Price Intelligence")
st.caption("Rental market insights scraped live from **SPEEDHOME.com**")

if not active_query:
    st.info(
        "👈 Choose an **area** or paste a **SPEEDHOME URL** in the sidebar, "
        "then click **Search** to begin."
    )
    st.stop()

if mode == "Direct URL" and not is_speedhome_url(active_query):
    st.error("That doesn't look like a SPEEDHOME URL. It must contain `speedhome.com`.")
    st.stop()


# --------------------------------------------------------------------------- #
# Scrape ONCE per area (cached), then split across rental-type tabs.
# --------------------------------------------------------------------------- #
# Number of FULL scrape attempts before surfacing a Cloudflare-block error.
MAX_SCRAPE_ATTEMPTS = 3


def _scrape_blocked(listings, meta_) -> bool:
    """True when a scrape came back empty AND looks Cloudflare-blocked."""
    dbg = (meta_ or {}).get("debug", {})
    return (not listings) and (
        dbg.get("challenge_detected")
        or dbg.get("http_status") not in (200, None)
        or not dbg.get("has_next_data")
    )


# --------------------------------------------------------------------------- #
# Scrape with AUTOMATIC retries.
# SPEEDHOME sits behind Cloudflare, which blocks datacenter IPs (e.g. Streamlit
# Cloud) more aggressively than home connections — and the blocks are often
# transient. `st.cache_data` would otherwise cache a blocked (empty) result, so
# between attempts we clear the cache and re-scrape from scratch. The user only
# ever sees the Cloudflare error if ALL attempts are blocked.
# --------------------------------------------------------------------------- #
all_listings, meta, scrape_error = [], {}, None
with st.spinner("Scraping listings from SPEEDHOME… (auto-retrying if blocked)"):
    for attempt in range(MAX_SCRAPE_ATTEMPTS):
        if attempt > 0:
            cached_scrape.clear()      # discard the cached blocked result
            time.sleep(2)              # brief back-off before re-attempting
        try:
            all_listings, meta = cached_scrape(active_query)
            scrape_error = None
        except Exception as exc:  # noqa: BLE001 — treat as a failed attempt
            all_listings, meta, scrape_error = [], {}, exc
        if scrape_error is None and not _scrape_blocked(all_listings, meta):
            break  # success — stop retrying

# A non-block exception (e.g. a code error) — surface it directly.
if scrape_error is not None:
    cached_scrape.clear()
    st.error(f"Scraping failed: {scrape_error}")
    st.stop()

area = meta.get("area", active_query)

# --------------------------------------------------------------------------- #
# Cloudflare-block handling — only reached if ALL retries above were blocked.
# --------------------------------------------------------------------------- #
if _scrape_blocked(all_listings, meta):
    cached_scrape.clear()  # don't persist the blocked result for later reruns
    st.error("🛡️ SPEEDHOME is currently blocking this request (Cloudflare).")
    st.markdown(
        f"The app automatically retried **{MAX_SCRAPE_ATTEMPTS} times** with fresh "
        "browser fingerprints, but each attempt received a **Cloudflare "
        "bot-protection challenge** instead of the listings page.\n\n"
        "This typically happens when the app runs on a **shared cloud server** "
        "(such as Streamlit Cloud), because Cloudflare treats datacenter traffic "
        "as suspicious. It is **not** a bug in the app — the same search usually "
        "works when the app is run from a normal home/office network."
    )
    # Optional manual retry as a last resort (auto-retries already ran above).
    if st.button("🔄 Try again", type="primary", key="cf_retry"):
        cached_scrape.clear()
        st.rerun()

    with st.expander("What can I do?"):
        st.markdown(
            "- **Wait a moment and click “Try again”** — blocks are often "
            "temporary.\n"
            "- **Run it locally** for a reliable demo: "
            "`python -m streamlit run app.py` on your own machine.\n"
            "- **For always-on cloud use**, route requests through a residential "
            "proxy (a paid add-on) so traffic doesn't look like a datacenter."
        )
    st.stop()

# SPEEDHOME's /rent/<area> is a radius search, so restrict to listings that are
# actually IN the searched area (toggle in the sidebar; on by default).
radius_total = len(all_listings)
if strict_area:
    all_listings = filter_by_area(all_listings, meta.get("area_term", area))
    removed = radius_total - len(all_listings)
    if removed > 0:
        st.caption(
            f"📍 Showing **{len(all_listings)}** listing(s) actually in **{area}** "
            f"· hid **{removed}** from nearby areas. "
            "Untick *“Only show listings in this area”* in the sidebar to see them all."
        )

# --------------------------------------------------------------------------- #
# Rental-type tabs
# --------------------------------------------------------------------------- #
tab_objects = st.tabs([label for label, _ in RENTAL_TABS])

for tab, (label, rental_type) in zip(tab_objects, RENTAL_TABS):
    with tab:
        listings = filter_by_rental_type(all_listings, rental_type)

        # --- Empty state -------------------------------------------------- #
        if not listings:
            if not all_listings and strict_area and radius_total > 0:
                st.warning(
                    f"🔍 None of the **{radius_total}** nearby listings are actually "
                    f"in **{area}** (matched by name/address). Untick "
                    "*“Only show listings in this area”* in the sidebar to see the "
                    "nearby results, or try a different area."
                )
            elif not all_listings:
                st.warning(
                    f"😕 No listings found for **{area}**. The area name may be "
                    "unrecognised, or SPEEDHOME has no active listings there. "
                    "Try another area or paste a direct URL."
                )
            elif rental_type == "daily":
                st.info(
                    "🗓️ **No daily / short-stay rentals here.** SPEEDHOME's "
                    f"`/rent/{area}` listings are monthly-tenancy leases, so daily "
                    "stays don't appear on this path. See the **Monthly** tab."
                )
            else:
                st.info(
                    f"No **{label.lower()}** rentals matched for **{area}**. "
                    "Try the **Monthly** tab for the full set."
                )
            continue

        df = pd.DataFrame(listings)

        # Clarify pricing semantics on the Monthly / Yearly tabs so users don't
        # read "Yearly" as an annual total (it's a minimum-lease filter).
        if rental_type in ("monthly", "yearly"):
            st.info(
                "**All prices are per month (RM/month).** The **Yearly** tab "
                "filters to listings with a **minimum rental duration of 12 "
                "months** — it does **not** show an annual total price. That's why "
                "Monthly and Yearly average prices can look similar.",
                icon="ℹ️",
            )

        # Top-line metrics.
        c1, c2, c3 = st.columns(3)
        c1.metric(f"{label} listings", len(listings))
        c2.metric("Pages scraped", meta["pages_scraped"])
        priced = df["monthly_price"].dropna()
        c3.metric(
            "Avg price (RM)",
            f"{priced.mean():,.0f}" if not priced.empty else "—",
        )

        # --- Price summary table ----------------------------------------- #
        summary = build_summary(df)
        st.subheader("📊 Price Summary by Unit Type")
        if summary.empty:
            st.info("Not enough priced data to build a summary.")
        else:
            st.dataframe(summary, use_container_width=True, hide_index=True)

        # --- Auto-generated insights ------------------------------------- #
        st.subheader("💡 Insights")
        for sentence in generate_insights(summary, area, len(listings)):
            st.markdown(f"- {sentence}")

        # --- Box plot distribution --------------------------------------- #
        st.subheader("📦 Price Distribution per Unit Type")
        plot_df = df.dropna(subset=["monthly_price"])
        if not plot_df.empty:
            from utils import UNIT_TYPE_ORDER

            order = [u for u in UNIT_TYPE_ORDER if u in plot_df["unit_type"].unique()]
            fig = px.box(
                plot_df,
                x="unit_type",
                y="monthly_price",
                category_orders={"unit_type": order},
                points="outliers",
                labels={"unit_type": "Unit Type", "monthly_price": "Monthly Price (RM)"},
                color="unit_type",
            )
            fig.update_layout(showlegend=False, margin=dict(t=10, b=10))
            # Unique key per rental-type tab — avoids StreamlitDuplicateElementId
            # when two tabs render structurally identical box plots.
            st.plotly_chart(
                fig, use_container_width=True, key=f"boxplot_{rental_type}"
            )
        else:
            st.info("No priced listings to plot.")

        # --- Listings table ---------------------------------------------- #
        st.subheader("📋 Unit Listings")
        display = df.rename(
            columns={
                "title": "Title",
                "property_name": "Property name",
                "address": "Address",
                "room_type": "Room type",
                "monthly_price": "Monthly price (RM)",
                "annual_price": "Annual price (RM)",
                "sqft": "sqft",
                "furnishing": "Furnishing status",
                "link": "Listing",
            }
        )[
            [
                "Title", "Property name", "Address", "Room type",
                "Monthly price (RM)", "Annual price (RM)", "sqft",
                "Furnishing status", "Listing",
            ]
        ]

        # --- Sort & filter controls -------------------------------------- #
        room_options = sorted(display["Room type"].dropna().unique().tolist())
        sort_options = {
            "Default": (None, True),
            "Monthly price ↑ (low→high)": ("Monthly price (RM)", True),
            "Monthly price ↓ (high→low)": ("Monthly price (RM)", False),
            "sqft ↑ (small→large)": ("sqft", True),
            "sqft ↓ (large→small)": ("sqft", False),
        }
        fcol, scol = st.columns([2, 1])
        chosen_rooms = fcol.multiselect(
            "Filter by room type",
            options=room_options,
            default=room_options,
            key=f"roomfilter_{rental_type}",
        )
        sort_choice = scol.selectbox(
            "Sort by",
            options=list(sort_options.keys()),
            index=0,
            key=f"sort_{rental_type}",
        )

        view = display[display["Room type"].isin(chosen_rooms)] if chosen_rooms else display
        sort_col, ascending = sort_options[sort_choice]
        if sort_col:
            view = view.sort_values(sort_col, ascending=ascending, na_position="last")

        # On-screen view: format numbers and replace any None/NaN with "—".
        # (The numeric `display` frame is kept for the Excel export below.)
        table_view = humanize_for_display(
            view,
            numeric_cols=("Monthly price (RM)", "Annual price (RM)", "sqft"),
            skip_cols=("Listing",),
        )
        if view.empty:
            st.info("No listings match the selected room-type filter.")
        else:
            st.dataframe(
                table_view,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Title": st.column_config.TextColumn("Title", width="medium"),
                    "Property name": st.column_config.TextColumn(
                        "Property name", width="medium"
                    ),
                    "Address": st.column_config.TextColumn("Address", width="large"),
                    # Clickable verification link — kept as the LAST column.
                    "Listing": st.column_config.LinkColumn(
                        "View Listing",
                        display_text="🔗 View Listing",
                        width="medium",
                    ),
                },
            )
            st.caption(
                f"Showing **{len(view)}** of **{len(display)}** listings · "
                "click any cell to expand text, or use the **View Listing** link "
                "to open the original on SPEEDHOME."
            )

        # --- Excel download ---------------------------------------------- #
        st.subheader("⬇️ Export")
        xlsx_bytes = make_excel(summary, display)
        st.download_button(
            label="Download Excel (.xlsx)",
            data=xlsx_bytes,
            file_name=excel_filename(area),
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=False,
            key=f"dl_{rental_type}",
        )

        st.caption(f"Source: {meta['first_url']}")
