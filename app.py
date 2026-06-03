"""
app.py — Property Price Intelligence (SPEEDHOME.com) Streamlit UI.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import time

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from scraper import (
    AREAS,
    filter_by_area,
    filter_by_rental_type,
    is_speedhome_url,
    load_demo_data,
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

MAX_SCRAPE_ATTEMPTS = 3

# Column config for the Price Summary table — adds an explanatory tooltip to the
# Fair Price column. (Fair Price = trimmed mean: drop the top & bottom 10% of
# prices, then average — reduces the influence of outliers.)
SUMMARY_COL_CONFIG = {
    "Fair Price (RM)": st.column_config.NumberColumn(
        "Fair Price (RM)",
        help=(
            "Fair Price = rata-rata harga setelah memangkas 10% data termurah "
            "dan termahal (trimmed mean), memberikan estimasi harga wajar dengan "
            "mengurangi pengaruh outlier."
        ),
    ),
}


# --------------------------------------------------------------------------- #
# Cached scraping (session-level caching)
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner=False, ttl=3600)
def cached_scrape(query: str):
    return scrape_area(query)


# --------------------------------------------------------------------------- #
# Helper: scrape with retries, return (listings, meta) or raise/show error
# --------------------------------------------------------------------------- #
def _scrape_blocked(listings, meta_) -> bool:
    dbg = (meta_ or {}).get("debug", {})
    return (not listings) and (
        dbg.get("challenge_detected")
        or dbg.get("http_status") not in (200, None)
        or not dbg.get("has_next_data")
    )


def scrape_with_retries(query: str):
    """Returns (listings, meta, blocked:bool, error). Clears cache on failure."""
    all_listings, meta, scrape_error = [], {}, None
    for attempt in range(MAX_SCRAPE_ATTEMPTS):
        if attempt > 0:
            cached_scrape.clear()
            time.sleep(2)
        try:
            all_listings, meta = cached_scrape(query)
            scrape_error = None
        except Exception as exc:  # noqa: BLE001
            all_listings, meta, scrape_error = [], {}, exc
        if scrape_error is None and not _scrape_blocked(all_listings, meta):
            break
    blocked = _scrape_blocked(all_listings, meta)
    return all_listings, meta, blocked, scrape_error


# --------------------------------------------------------------------------- #
# Helper: render one full area result inside a container
# (reused by both single-search and compare mode)
# --------------------------------------------------------------------------- #
def render_area_results(
    all_listings: list,
    meta: dict,
    strict_area: bool,
    rental_type: str = "monthly",
    key_suffix: str = "",
    compact: bool = False,
):
    """Render price summary, insights, box plot, listings table for ONE area.

    compact=True → skip listings table & Excel export (used in Compare view).
    key_suffix   → unique string to avoid StreamlitDuplicateElementId.
    """
    area = meta.get("area", "")

    # Apply strict-area filter
    radius_total = len(all_listings)
    if strict_area:
        all_listings = filter_by_area(all_listings, meta.get("area_term", area))
        removed = radius_total - len(all_listings)
        if removed > 0:
            st.caption(
                f"📍 **{len(all_listings)}** listings in **{area}** "
                f"· hid **{removed}** nearby. "
            )

    listings = filter_by_rental_type(all_listings, rental_type)
    if not listings:
        st.info(f"No **{rental_type}** rentals found for **{area}**.")
        return None  # signal: no data

    df = pd.DataFrame(listings)
    summary = build_summary(df)

    # Metrics row
    priced = df["monthly_price"].dropna()
    c1, c2, c3 = st.columns(3)
    c1.metric("Listings", len(listings))
    c2.metric("Pages scraped", meta.get("pages_scraped", "—"))
    c3.metric("Avg price (RM)", f"{priced.mean():,.0f}" if not priced.empty else "—")

    # Price summary table
    st.subheader("📊 Price Summary")
    if summary.empty:
        st.info("Not enough priced data.")
    else:
        st.dataframe(summary, use_container_width=True, hide_index=True,
                     column_config=SUMMARY_COL_CONFIG)

    if not compact:
        # Insights
        st.subheader("💡 Insights")
        for sentence in generate_insights(summary, area, len(listings)):
            st.markdown(f"- {sentence}")

        # Box plot
        st.subheader("📦 Price Distribution")
        plot_df = df.dropna(subset=["monthly_price"])
        if not plot_df.empty:
            from utils import UNIT_TYPE_ORDER
            order = [u for u in UNIT_TYPE_ORDER if u in plot_df["unit_type"].unique()]
            fig = px.box(
                plot_df, x="unit_type", y="monthly_price",
                category_orders={"unit_type": order},
                points="outliers",
                labels={"unit_type": "Unit Type", "monthly_price": "Monthly Price (RM)"},
                color="unit_type",
            )
            fig.update_layout(showlegend=False, margin=dict(t=10, b=10))
            st.plotly_chart(fig, use_container_width=True, key=f"boxplot_{key_suffix}")

        # Listings table
        st.subheader("📋 Unit Listings")
        display = df.rename(columns={
            "title": "Title", "property_name": "Property name",
            "address": "Address", "room_type": "Room type",
            "monthly_price": "Monthly price (RM)", "annual_price": "Annual price (RM)",
            "sqft": "sqft", "furnishing": "Furnishing status", "link": "Listing",
        })[["Title", "Property name", "Address", "Room type",
            "Monthly price (RM)", "Annual price (RM)", "sqft", "Furnishing status", "Listing"]]

        room_options = sorted(display["Room type"].dropna().unique().tolist())
        sort_options = {
            "Default": (None, True),
            "Monthly price ↑": ("Monthly price (RM)", True),
            "Monthly price ↓": ("Monthly price (RM)", False),
            "sqft ↑": ("sqft", True),
            "sqft ↓": ("sqft", False),
        }
        fcol, scol = st.columns([2, 1])
        chosen_rooms = fcol.multiselect(
            "Filter by room type", options=room_options, default=room_options,
            key=f"roomfilter_{key_suffix}",
        )
        sort_choice = scol.selectbox(
            "Sort by", options=list(sort_options.keys()), index=0,
            key=f"sort_{key_suffix}",
        )
        view = display[display["Room type"].isin(chosen_rooms)] if chosen_rooms else display
        sort_col, ascending = sort_options[sort_choice]
        if sort_col:
            view = view.sort_values(sort_col, ascending=ascending, na_position="last")

        table_view = humanize_for_display(
            view, numeric_cols=("Monthly price (RM)", "Annual price (RM)", "sqft"),
            skip_cols=("Listing",),
        )
        if view.empty:
            st.info("No listings match the selected filter.")
        else:
            st.dataframe(
                table_view, use_container_width=True, hide_index=True,
                column_config={
                    "Title": st.column_config.TextColumn("Title", width="medium"),
                    "Property name": st.column_config.TextColumn("Property name", width="medium"),
                    "Address": st.column_config.TextColumn("Address", width="large"),
                    "Listing": st.column_config.LinkColumn(
                        "View Listing", display_text="🔗 View Listing", width="medium"
                    ),
                },
            )
            st.caption(
                f"Showing **{len(view)}** of **{len(display)}** listings · "
                "click **View Listing** to open on SPEEDHOME."
            )

        # Excel download
        st.subheader("⬇️ Export")
        xlsx_bytes = make_excel(summary, display)
        st.download_button(
            label="Download Excel (.xlsx)",
            data=xlsx_bytes,
            file_name=excel_filename(area),
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=False,
            key=f"dl_{key_suffix}",
        )

    st.caption(f"Source: {meta.get('first_url', '')}")
    return summary  # return for comparison use


# --------------------------------------------------------------------------- #
# Sidebar — APP MODE selector
# --------------------------------------------------------------------------- #
st.sidebar.header("🏠 Property Price Intelligence")

app_mode = st.sidebar.radio(
    "Mode",
    ["🔍 Single Search", "⚖️ Compare Areas"],
    help="Single Search: analyse one area. Compare Areas: compare two areas side-by-side.",
)

# =========================================================================== #
# MODE 1 — SINGLE SEARCH (original behaviour)
# =========================================================================== #
if app_mode == "🔍 Single Search":

    st.sidebar.subheader("🔎 Search")

    mode = st.sidebar.radio(
        "Search by",
        ["Area name", "Direct URL"],
        help="Pick an area from the seed list, or paste a SPEEDHOME URL.",
    )

    if mode == "Area name":
        query_input = st.sidebar.selectbox(
            "Area (autocomplete)", options=SEED_AREAS, index=0,
        )
    else:
        query_input = st.sidebar.text_input(
            "SPEEDHOME URL",
            value="https://speedhome.com/rent/mont-kiara",
        )

    strict_area = st.sidebar.checkbox(
        "Only show listings in this area", value=True,
        help="SPEEDHOME returns nearby areas too. When ticked, only listings "
             "whose name/address actually mentions the searched area are kept.",
    )
    st.sidebar.caption(
        "Polite scraping: 1.5s delay · only `/rent/` paths · all pages fetched."
    )
    search = st.sidebar.button("Search", type="primary", use_container_width=True)

    if search:
        st.session_state["active_query"] = (query_input or "").strip()

    active_query = st.session_state.get("active_query")

    # Header
    st.title("🏠 Property Price Intelligence")
    st.caption("Rental market insights scraped live from **SPEEDHOME.com**")

    if not active_query:
        st.info("👈 Choose an **area** or paste a **SPEEDHOME URL** in the sidebar, then click **Search**.")
        st.stop()

    if mode == "Direct URL" and not is_speedhome_url(active_query):
        st.error("That doesn't look like a SPEEDHOME URL. It must contain `speedhome.com`.")
        st.stop()

    # Scrape
    with st.spinner("Scraping listings from SPEEDHOME… (auto-retrying if blocked)"):
        all_listings, meta, blocked, scrape_error = scrape_with_retries(active_query)

    if scrape_error is not None:
        cached_scrape.clear()
        st.error(f"Scraping failed: {scrape_error}")
        st.stop()

    area = meta.get("area", active_query)

    if blocked:
        # Fallback: if we have bundled sample data for this area, show it.
        demo_listings, demo_meta = load_demo_data(active_query)
        if demo_listings:
            st.warning(
                "⚠️ Showing cached sample data (live scraping temporarily unavailable)"
            )
            all_listings, meta = demo_listings, demo_meta
            area = meta.get("area", active_query)
        else:
            cached_scrape.clear()
            st.error("🛡️ SPEEDHOME is currently blocking this request (Cloudflare).")
            st.markdown(
                f"The app automatically retried **{MAX_SCRAPE_ATTEMPTS} times** but each "
                "attempt received a **Cloudflare challenge**. This typically happens on "
                "shared cloud servers — not a bug in the app."
            )
            if st.button("🔄 Try again", type="primary", key="cf_retry"):
                cached_scrape.clear()
                st.rerun()
            with st.expander("What can I do?"):
                st.markdown(
                    "- **Wait and click 'Try again'** — blocks are often temporary.\n"
                    "- **Run locally** for a reliable demo: `python -m streamlit run app.py`.\n"
                    "- **For always-on use**, route requests through a residential proxy."
                )
            st.stop()

    # Data freshness timestamp (Asia/Kuala_Lumpur).
    st.caption(f"🕒 Data scraped at: **{meta.get('scraped_at', '—')} MYT**")

    # Apply strict area filter (for caption display before tabs)
    radius_total = len(all_listings)
    if strict_area:
        filtered = filter_by_area(all_listings, meta.get("area_term", area))
        removed = radius_total - len(filtered)
        if removed > 0:
            st.caption(
                f"📍 Showing **{len(filtered)}** listing(s) actually in **{area}** "
                f"· hid **{removed}** from nearby areas. "
                "Untick 'Only show listings in this area' in the sidebar to see them all."
            )
        all_listings_filtered = filtered
    else:
        all_listings_filtered = all_listings

    # Rental-type tabs. Hide the Daily tab when it has no listings (SPEEDHOME's
    # /rent listings are monthly-tenancy, so Daily is almost always empty — an
    # empty tab makes users think the app is broken). Monthly & Yearly always show.
    active_tabs = [
        (label, rtype)
        for label, rtype in RENTAL_TABS
        if rtype != "daily" or filter_by_rental_type(all_listings_filtered, "daily")
    ]
    if not active_tabs:  # safety net — always render at least the Monthly tab
        active_tabs = [("Monthly", "monthly")]

    tab_objects = st.tabs([label for label, _ in active_tabs])

    for tab, (label, rental_type) in zip(tab_objects, active_tabs):
        with tab:
            listings = filter_by_rental_type(all_listings_filtered, rental_type)

            if not listings:
                if not all_listings_filtered and strict_area and radius_total > 0:
                    st.warning(
                        f"🔍 **Tidak ada listing yang ditemukan di {area}.**\n\n"
                        "Coba: (1) Matikan filter **'Only show listings in this area'** "
                        "di sidebar, atau (2) Pilih area lain."
                    )
                elif not all_listings_filtered:
                    st.warning(
                        f"😕 **Tidak ada listing yang ditemukan di {area}.** "
                        "Coba pilih area lain dari daftar."
                    )
                else:
                    st.info(
                        f"Tidak ada listing **{label.lower()}** untuk **{area}**. "
                        "Lihat tab **Monthly** untuk data lengkap."
                    )
                continue

            df = pd.DataFrame(listings)

            if rental_type in ("monthly", "yearly"):
                st.info(
                    "**All prices are per month (RM/month).** The **Yearly** tab filters to "
                    "listings with a minimum 12-month lease — it does **not** show an annual total.",
                    icon="ℹ️",
                )

            # Top-line metrics
            c1, c2, c3 = st.columns(3)
            c1.metric(f"{label} listings", len(listings))
            c2.metric("Pages scraped", meta["pages_scraped"])
            priced = df["monthly_price"].dropna()
            c3.metric("Avg price (RM)", f"{priced.mean():,.0f}" if not priced.empty else "—")

            # Price summary table
            summary = build_summary(df)

            # "So what?" highlight (Monthly tab) — plain-language takeaway using
            # the most-listed unit type's Fair Price and Min.
            if rental_type == "monthly" and not summary.empty:
                top = summary.sort_values("Count", ascending=False).iloc[0]
                fair, lo = top.get("Fair Price (RM)"), top.get("Min (RM)")
                if pd.notna(fair) and pd.notna(lo):
                    st.success(
                        f"💡 Untuk **{top['Unit Type']}** di **{area}**, harga wajar "
                        f"sekitar **RM {fair:,.0f}/bulan**. Budget di bawah "
                        f"**RM {lo:,.0f}** termasuk murah untuk area ini."
                    )

            st.subheader("📊 Price Summary by Unit Type")
            if summary.empty:
                st.info("Not enough priced data to build a summary.")
            else:
                st.dataframe(summary, use_container_width=True, hide_index=True,
                     column_config=SUMMARY_COL_CONFIG)

            # Insights
            st.subheader("💡 Insights")
            for sentence in generate_insights(summary, area, len(listings)):
                st.markdown(f"- {sentence}")

            # Price distribution — simple grouped bar (Average vs Median) by
            # default; the box plot lives behind an "Advanced" expander.
            st.subheader("📊 Average vs Median Price per Unit Type")
            if not summary.empty:
                fig_bar = go.Figure(data=[
                    go.Bar(name="Average", x=summary["Unit Type"],
                           y=summary["Average (RM)"], marker_color="#4C8BF5"),
                    go.Bar(name="Median", x=summary["Unit Type"],
                           y=summary["Median (RM)"], marker_color="#FF7043"),
                ])
                fig_bar.update_layout(
                    barmode="group", yaxis_title="Monthly Rent (RM)",
                    margin=dict(t=30, b=10),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02,
                                xanchor="right", x=1),
                )
                st.plotly_chart(fig_bar, use_container_width=True, key=f"bar_{rental_type}")

                with st.expander("📦 Advanced: Price Distribution (box plot)"):
                    plot_df = df.dropna(subset=["monthly_price"])
                    if not plot_df.empty:
                        from utils import UNIT_TYPE_ORDER
                        order = [u for u in UNIT_TYPE_ORDER if u in plot_df["unit_type"].unique()]
                        fig = px.box(
                            plot_df, x="unit_type", y="monthly_price",
                            category_orders={"unit_type": order}, points="outliers",
                            labels={"unit_type": "Unit Type",
                                    "monthly_price": "Monthly Price (RM)"},
                            color="unit_type",
                        )
                        fig.update_layout(showlegend=False, margin=dict(t=10, b=10))
                        st.plotly_chart(fig, use_container_width=True,
                                        key=f"boxplot_{rental_type}")
                    else:
                        st.info("No priced listings to plot.")
            else:
                st.info("No priced listings to plot.")

            # Listings table
            st.subheader("📋 Unit Listings")
            display = df.rename(columns={
                "title": "Title", "property_name": "Property name",
                "address": "Address", "room_type": "Room type",
                "monthly_price": "Monthly price (RM)", "annual_price": "Annual price (RM)",
                "sqft": "sqft", "furnishing": "Furnishing status", "link": "Listing",
            })[["Title", "Property name", "Address", "Room type",
                "Monthly price (RM)", "Annual price (RM)", "sqft", "Furnishing status", "Listing"]]

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
                "Filter by room type", options=room_options, default=room_options,
                key=f"roomfilter_{rental_type}",
            )
            sort_choice = scol.selectbox(
                "Sort by", options=list(sort_options.keys()), index=0,
                key=f"sort_{rental_type}",
            )
            view = display[display["Room type"].isin(chosen_rooms)] if chosen_rooms else display
            sort_col, ascending = sort_options[sort_choice]
            if sort_col:
                view = view.sort_values(sort_col, ascending=ascending, na_position="last")

            table_view = humanize_for_display(
                view,
                numeric_cols=("Monthly price (RM)", "Annual price (RM)", "sqft"),
                skip_cols=("Listing",),
            )
            if view.empty:
                st.info("No listings match the selected room-type filter.")
            else:
                st.dataframe(
                    table_view, use_container_width=True, hide_index=True,
                    column_config={
                        "Title": st.column_config.TextColumn("Title", width="medium"),
                        "Property name": st.column_config.TextColumn("Property name", width="medium"),
                        "Address": st.column_config.TextColumn("Address", width="large"),
                        "Listing": st.column_config.LinkColumn(
                            "View Listing", display_text="🔗 View Listing", width="medium"
                        ),
                    },
                )
                st.caption(
                    f"Showing **{len(view)}** of **{len(display)}** listings · "
                    "click any cell to expand text, or use the **View Listing** link."
                )

            # Excel download
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


# =========================================================================== #
# MODE 2 — COMPARE AREAS
# =========================================================================== #
else:
    st.sidebar.subheader("⚖️ Compare Two Areas")

    area_a = st.sidebar.selectbox(
        "Area A", options=SEED_AREAS, index=0, key="cmp_a",
        help="First area to compare.",
    )
    area_b = st.sidebar.selectbox(
        "Area B", options=SEED_AREAS, index=1, key="cmp_b",
        help="Second area to compare.",
    )
    strict_cmp = st.sidebar.checkbox(
        "Only show listings in each area", value=True, key="strict_cmp",
    )
    compare_btn = st.sidebar.button("Compare", type="primary", use_container_width=True)

    if compare_btn:
        st.session_state["cmp_query_a"] = area_a
        st.session_state["cmp_query_b"] = area_b

    q_a = st.session_state.get("cmp_query_a")
    q_b = st.session_state.get("cmp_query_b")

    # ---------- Header ----------
    st.title("⚖️ Area Comparison")
    st.caption("Compare rental prices between two areas — data scraped live from **SPEEDHOME.com**")

    if not q_a or not q_b:
        st.info("👈 Pick **Area A** and **Area B** in the sidebar, then click **Compare**.")
        st.stop()

    if q_a == q_b:
        st.warning("⚠️ Please choose two **different** areas to compare.")
        st.stop()

    # ---------- Scrape both areas ----------
    col_spin_a, col_spin_b = st.columns(2)
    with col_spin_a:
        with st.spinner(f"Scraping **{q_a}**…"):
            listings_a, meta_a, blocked_a, err_a = scrape_with_retries(q_a)
    with col_spin_b:
        with st.spinner(f"Scraping **{q_b}**…"):
            listings_b, meta_b, blocked_b, err_b = scrape_with_retries(q_b)

    # Error handling
    for err, q in [(err_a, q_a), (err_b, q_b)]:
        if err:
            cached_scrape.clear()
            st.error(f"Scraping failed for **{q}**: {err}")
            st.stop()

    # Fallback to bundled sample data per area when blocked.
    used_demo = False
    if blocked_a:
        dl, dm = load_demo_data(q_a)
        if dl:
            listings_a, meta_a, blocked_a, used_demo = dl, dm, False, True
    if blocked_b:
        dl, dm = load_demo_data(q_b)
        if dl:
            listings_b, meta_b, blocked_b, used_demo = dl, dm, False, True

    for blocked, q in [(blocked_a, q_a), (blocked_b, q_b)]:
        if blocked:
            cached_scrape.clear()
            st.error(f"🛡️ Cloudflare blocked the request for **{q}**.")
            if st.button("🔄 Try again", type="primary", key=f"cf_retry_cmp_{q}"):
                cached_scrape.clear()
                st.rerun()
            st.stop()

    if used_demo:
        st.warning(
            "⚠️ Showing cached sample data (live scraping temporarily unavailable)"
        )

    name_a = meta_a.get("area", q_a)
    name_b = meta_b.get("area", q_b)

    # Data freshness timestamp (Asia/Kuala_Lumpur) for both areas.
    st.caption(
        f"🕒 Data scraped at: **{meta_a.get('scraped_at', '—')} MYT** ({name_a}) · "
        f"**{meta_b.get('scraped_at', '—')} MYT** ({name_b})"
    )

    # Rental type for the comparison (Monthly / Yearly). All metrics, charts and
    # summary tables below use the selected type.
    cmp_rental = st.radio(
        "Rental type", ["Monthly", "Yearly"], horizontal=True, key="cmp_rental",
    ).lower()
    st.caption(
        "Semua harga tetap per bulan (RM/bulan). **Yearly** = listing dengan "
        "sewa minimum 12 bulan, bukan total tahunan."
    )

    # Apply strict filter
    if strict_cmp:
        listings_a = filter_by_area(listings_a, meta_a.get("area_term", name_a))
        listings_b = filter_by_area(listings_b, meta_b.get("area_term", name_b))

    # Filter to the chosen rental type
    sel_a = filter_by_rental_type(listings_a, cmp_rental)
    sel_b = filter_by_rental_type(listings_b, cmp_rental)

    df_a = pd.DataFrame(sel_a) if sel_a else pd.DataFrame()
    df_b = pd.DataFrame(sel_b) if sel_b else pd.DataFrame()

    # ------------------------------------------------------------------ #
    # SECTION 1 — HEADLINE METRICS COMPARISON
    # ------------------------------------------------------------------ #
    st.markdown("---")
    st.subheader(f"📊 Head-to-Head: Key Metrics ({cmp_rental.title()} Rentals)")

    def get_metrics(df: pd.DataFrame) -> dict:
        if df.empty or "monthly_price" not in df.columns:
            return {}
        priced = df["monthly_price"].dropna()
        sqft_priced = df.dropna(subset=["monthly_price", "sqft"])
        sqft_priced = sqft_priced[sqft_priced["sqft"] > 0]
        if sqft_priced.empty:
            ppsqft = None
        else:
            ppsqft = (sqft_priced["monthly_price"] / sqft_priced["sqft"]).mean()
        return {
            "Listings": len(df),
            "Average (RM)": priced.mean() if not priced.empty else None,
            "Median (RM)": priced.median() if not priced.empty else None,
            "Min (RM)": priced.min() if not priced.empty else None,
            "Max (RM)": priced.max() if not priced.empty else None,
            "Avg Price/sqft (RM)": ppsqft,
        }

    m_a = get_metrics(df_a)
    m_b = get_metrics(df_b)

    metrics_keys = ["Listings", "Average (RM)", "Median (RM)", "Min (RM)", "Max (RM)", "Avg Price/sqft (RM)"]

    # Render metric cards side-by-side
    header_cols = st.columns([2, 3, 3])
    header_cols[0].markdown("**Metric**")
    header_cols[1].markdown(f"**🔵 {name_a}**")
    header_cols[2].markdown(f"**🟠 {name_b}**")

    st.markdown("---")

    for key in metrics_keys:
        val_a = m_a.get(key)
        val_b = m_b.get(key)

        # Determine winner (lower = cheaper = green, except Listings)
        def fmt(v):
            if v is None:
                return "—"
            if key == "Listings":
                return f"{int(v):,}"
            return f"RM {v:,.0f}" if "sqft" not in key else f"RM {v:.2f}"

        row = st.columns([2, 3, 3])
        row[0].markdown(f"**{key}**")

        # Highlight logic: for price metrics, lower is better (greener)
        if key != "Listings" and val_a is not None and val_b is not None:
            if val_a < val_b:
                row[1].success(f"✅ {fmt(val_a)}")
                row[2].markdown(fmt(val_b))
            elif val_b < val_a:
                row[1].markdown(fmt(val_a))
                row[2].success(f"✅ {fmt(val_b)}")
            else:
                row[1].markdown(fmt(val_a))
                row[2].markdown(fmt(val_b))
        else:
            row[1].markdown(fmt(val_a))
            row[2].markdown(fmt(val_b))

    st.markdown("---")
    st.caption("✅ Green = lower price (better value) for that metric.")

    # ------------------------------------------------------------------ #
    # SECTION 2 — BAR CHART COMPARISON
    # ------------------------------------------------------------------ #
    st.subheader("📊 Price Comparison Chart")

    chart_metrics = {
        "Average (RM)": "Average",
        "Median (RM)": "Median",
        "Min (RM)": "Minimum",
        "Max (RM)": "Maximum",
    }
    chart_vals_a = [m_a.get(k) for k in chart_metrics]
    chart_vals_b = [m_b.get(k) for k in chart_metrics]
    chart_labels = list(chart_metrics.values())

    fig_cmp = go.Figure(data=[
        go.Bar(name=name_a, x=chart_labels, y=chart_vals_a,
               marker_color="#4C8BF5", text=[f"RM {v:,.0f}" if v else "—" for v in chart_vals_a],
               textposition="outside"),
        go.Bar(name=name_b, x=chart_labels, y=chart_vals_b,
               marker_color="#FF7043", text=[f"RM {v:,.0f}" if v else "—" for v in chart_vals_b],
               textposition="outside"),
    ])
    fig_cmp.update_layout(
        barmode="group",
        yaxis_title="Monthly Rent (RM)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(t=40, b=20),
        height=420,
    )
    st.plotly_chart(fig_cmp, use_container_width=True, key="cmp_bar_chart")

    # Per-unit-type summaries — reused by the chart below AND the tables (Section 4).
    summary_a = build_summary(df_a) if not df_a.empty else pd.DataFrame()
    summary_b = build_summary(df_b) if not df_b.empty else pd.DataFrame()

    # ------------------------------------------------------------------ #
    # SECTION 3 — PRICE PER UNIT TYPE
    # Default: grouped bar (Average & Median per unit type, Area A vs Area B),
    # consistent with Single Search. The box plot lives behind an expander.
    # ------------------------------------------------------------------ #
    st.subheader("📊 Price per Unit Type")

    from utils import UNIT_TYPE_ORDER
    units = [
        u for u in UNIT_TYPE_ORDER
        if (not summary_a.empty and u in summary_a["Unit Type"].values)
        or (not summary_b.empty and u in summary_b["Unit Type"].values)
    ]

    def _unit_vals(summary, col):
        if summary.empty:
            return [None] * len(units)
        s = summary.set_index("Unit Type")[col]
        return [float(s[u]) if u in s.index and pd.notna(s[u]) else None for u in units]

    if units:
        fig_unit = go.Figure(data=[
            go.Bar(name=f"{name_a} Avg", x=units, y=_unit_vals(summary_a, "Average (RM)"),
                   marker_color="#4C8BF5"),
            go.Bar(name=f"{name_b} Avg", x=units, y=_unit_vals(summary_b, "Average (RM)"),
                   marker_color="#FF7043"),
            go.Bar(name=f"{name_a} Median", x=units, y=_unit_vals(summary_a, "Median (RM)"),
                   marker_color="#A9C7F8"),
            go.Bar(name=f"{name_b} Median", x=units, y=_unit_vals(summary_b, "Median (RM)"),
                   marker_color="#FFB59B"),
        ])
        fig_unit.update_layout(
            barmode="group", yaxis_title="Monthly Rent (RM)",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            margin=dict(t=40, b=10), height=440,
        )
        st.plotly_chart(fig_unit, use_container_width=True, key="cmp_unit_bar")

        with st.expander("📦 Advanced: Price Distribution (box plot)"):
            combined_plot = []
            for df_, label in [(df_a, name_a), (df_b, name_b)]:
                if not df_.empty and "monthly_price" in df_.columns:
                    tmp = df_.dropna(subset=["monthly_price"])[["monthly_price", "unit_type"]].copy()
                    tmp["area"] = label
                    combined_plot.append(tmp)
            if combined_plot:
                plot_df = pd.concat(combined_plot, ignore_index=True)
                fig_box = px.box(
                    plot_df, x="unit_type", y="monthly_price", color="area",
                    points="outliers",
                    labels={"unit_type": "Unit Type", "monthly_price": "Monthly Price (RM)",
                            "area": "Area"},
                    color_discrete_map={name_a: "#4C8BF5", name_b: "#FF7043"},
                )
                fig_box.update_layout(
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                    margin=dict(t=40, b=10),
                )
                st.plotly_chart(fig_box, use_container_width=True, key="cmp_box_chart")
            else:
                st.info("Not enough priced data to render distribution chart.")
    else:
        st.info("Not enough priced data to render charts.")

    # ------------------------------------------------------------------ #
    # SECTION 4 — PRICE SUMMARY TABLES (side by side)
    # ------------------------------------------------------------------ #
    st.subheader("📋 Price Summary by Unit Type")
    col_a, col_b = st.columns(2)

    with col_a:
        st.markdown(f"**🔵 {name_a}**")
        if summary_a.empty:
            st.info("No data.")
        else:
            st.dataframe(summary_a, use_container_width=True, hide_index=True,
                         column_config=SUMMARY_COL_CONFIG)

    with col_b:
        st.markdown(f"**🟠 {name_b}**")
        if summary_b.empty:
            st.info("No data.")
        else:
            st.dataframe(summary_b, use_container_width=True, hide_index=True,
                         column_config=SUMMARY_COL_CONFIG)

    # ------------------------------------------------------------------ #
    # SECTION 5 — AUTO VERDICT
    # ------------------------------------------------------------------ #
    st.subheader("🏆 Verdict")

    avg_a = m_a.get("Average (RM)")
    avg_b = m_b.get("Average (RM)")
    ppsqft_a = m_a.get("Avg Price/sqft (RM)")
    ppsqft_b = m_b.get("Avg Price/sqft (RM)")

    verdict_lines = []

    if avg_a and avg_b:
        cheaper = name_a if avg_a < avg_b else name_b
        diff = abs(avg_a - avg_b)
        diff_pct = diff / max(avg_a, avg_b) * 100
        verdict_lines.append(
            f"💰 **{cheaper}** has a lower average monthly rent by "
            f"**RM {diff:,.0f}** ({diff_pct:.1f}%)."
        )

    if ppsqft_a and ppsqft_b:
        better_val = name_a if ppsqft_a < ppsqft_b else name_b
        verdict_lines.append(
            f"📐 **{better_val}** offers better value per sqft "
            f"(RM {min(ppsqft_a, ppsqft_b):.2f}/sqft vs RM {max(ppsqft_a, ppsqft_b):.2f}/sqft)."
        )

    cnt_a = m_a.get("Listings", 0)
    cnt_b = m_b.get("Listings", 0)
    if cnt_a and cnt_b:
        more = name_a if cnt_a > cnt_b else name_b
        verdict_lines.append(
            f"🏘️ **{more}** has more active listings ({max(cnt_a, cnt_b)} vs {min(cnt_a, cnt_b)}), "
            "indicating higher supply."
        )

    if verdict_lines:
        for line in verdict_lines:
            st.markdown(f"- {line}")
    else:
        st.info("Not enough data to generate a verdict.")

    st.caption(
        f"Data sources: {meta_a.get('first_url', '')} · {meta_b.get('first_url', '')}"
    )
