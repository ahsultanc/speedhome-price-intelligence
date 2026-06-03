"""
utils.py — Helper functions for the Property Price Intelligence app.

Contains:
  - Unit-type classification (Studio / 1BR / 2BR / 3BR / 4BR+)
  - Statistics helpers (mean, median, mode, trimmed "fair price")
  - Price-summary aggregation grouped by unit type
  - Excel (.xlsx) export with two sheets (Summary + Listings)
  - Small parsing helpers (price / size string -> number)
"""

from __future__ import annotations

import io
import re
from datetime import datetime
from statistics import StatisticsError, mean, median, multimode

import pandas as pd

# Canonical order we always present unit types in.
UNIT_TYPE_ORDER = ["Studio", "1BR", "2BR", "3BR", "4BR+"]

# Placeholder shown in the UI for missing values.
DASH = "—"


# --------------------------------------------------------------------------- #
# Display formatting
# --------------------------------------------------------------------------- #
def _is_missing(v) -> bool:
    """True for None, NaN, or empty/placeholder-ish strings."""
    if v is None:
        return True
    try:
        if isinstance(v, float) and pd.isna(v):
            return True
    except TypeError:
        pass
    return str(v).strip() in ("", "None", "nan", "NaN", "NaT")


def humanize_for_display(
    df: pd.DataFrame,
    numeric_cols: tuple[str, ...] = (),
    skip_cols: tuple[str, ...] = (),
) -> pd.DataFrame:
    """Return a copy of ``df`` formatted for on-screen display.

    * ``numeric_cols`` → integer strings with thousands separators (e.g.
      "1,200"), or ``—`` when missing.
    * every other column → its value, or ``—`` when missing/empty.
    * ``skip_cols`` (e.g. URL/link columns) are left exactly as-is.

    This is for *display only* — keep the numeric DataFrame for Excel/maths.
    """
    out = df.copy()
    numeric, skip = set(numeric_cols), set(skip_cols)
    for col in out.columns:
        if col in skip:
            continue
        if col in numeric:
            out[col] = out[col].map(
                lambda v: DASH if _is_missing(v) else f"{float(v):,.0f}"
            )
        else:
            out[col] = out[col].map(lambda v: DASH if _is_missing(v) else v)
    return out


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def parse_price(text) -> float | None:
    """Extract a numeric RM price from a string like 'RM 2,500 / month'.

    Returns None when no sensible number can be found.
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    # Grab the first number that may contain thousands separators / decimals.
    match = re.search(r"(\d[\d,]*\.?\d*)", str(text).replace(" ", ""))
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_size(text) -> float | None:
    """Extract a numeric sqft value from a string like '1,200 sqft'."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    match = re.search(r"(\d[\d,]*\.?\d*)", str(text).replace(" ", ""))
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def classify_unit_type(bedrooms, title: str = "") -> str:
    """Map a bedroom count (and/or title text) to a canonical unit type.

    A bedroom count of 0 (or the word 'studio') -> 'Studio'.
    4 or more bedrooms -> '4BR+'.
    """
    text = (title or "").lower()

    # Bedroom count takes priority when available.
    if bedrooms is not None:
        try:
            n = int(bedrooms)
        except (TypeError, ValueError):
            n = None
        if n is not None:
            if n <= 0:
                return "Studio"
            if n == 1:
                return "1BR"
            if n == 2:
                return "2BR"
            if n == 3:
                return "3BR"
            return "4BR+"

    # Fall back to scanning the title.
    if "studio" in text:
        return "Studio"
    m = re.search(r"(\d+)\s*(?:bed|br|bedroom|room)", text)
    if m:
        n = int(m.group(1))
        if n <= 0:
            return "Studio"
        if n >= 4:
            return "4BR+"
        return f"{n}BR"
    return "Studio"  # sensible default when nothing else is known


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #
def fair_price(values, trim_pct: float = 0.10) -> float | None:
    """Trimmed mean: drop the top & bottom `trim_pct` then average the rest.

    With small samples (where trimming would remove everything) we fall back
    to the plain mean so the column is never empty when data exists.
    """
    nums = sorted(v for v in values if v is not None)
    if not nums:
        return None
    n = len(nums)
    k = int(n * trim_pct)
    trimmed = nums[k : n - k] if n - 2 * k > 0 else nums
    return mean(trimmed) if trimmed else None


def safe_mode(values) -> float | None:
    """Return a single mode; if multimodal, return the lowest of the modes."""
    nums = [v for v in values if v is not None]
    if not nums:
        return None
    try:
        modes = multimode(nums)
        return min(modes) if modes else None
    except StatisticsError:
        return None


def build_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Build the price-summary table grouped by unit type.

    Expects columns: 'unit_type', 'monthly_price', 'sqft'.
    Returns one row per unit type that has data, in canonical order.
    """
    columns = [
        "Unit Type", "Count", "Average (RM)", "Median (RM)", "Mode (RM)",
        "Fair Price (RM)", "Min (RM)", "Max (RM)", "Avg sqft", "Price/sqft (RM)",
    ]
    rows = []
    if df.empty:
        return pd.DataFrame(columns=columns)

    def _clean(values, *, positive_only=False):
        """Drop None and NaN (and non-positive when requested)."""
        out = []
        for v in values:
            if v is None or v != v:  # v != v is True only for NaN
                continue
            if positive_only and v <= 0:
                continue
            out.append(float(v))
        return out

    for unit in UNIT_TYPE_ORDER:
        sub = df[df["unit_type"] == unit]
        prices = _clean(sub["monthly_price"].tolist(), positive_only=True)
        if not prices:
            continue
        sizes = _clean(sub["sqft"].tolist(), positive_only=True)
        avg_price = mean(prices)
        avg_sqft = mean(sizes) if sizes else None
        rows.append(
            {
                "Unit Type": unit,
                "Count": len(prices),
                "Average (RM)": round(avg_price, 2),
                "Median (RM)": round(median(prices), 2),
                "Mode (RM)": round(safe_mode(prices), 2) if safe_mode(prices) else None,
                "Fair Price (RM)": round(fair_price(prices), 2) if fair_price(prices) else None,
                "Min (RM)": round(min(prices), 2),
                "Max (RM)": round(max(prices), 2),
                "Avg sqft": round(avg_sqft, 1) if avg_sqft else None,
                # Average monthly rent per square foot (None when size unknown).
                "Price/sqft (RM)": round(avg_price / avg_sqft, 2)
                if avg_sqft else None,
            }
        )

    return pd.DataFrame(rows, columns=columns)


# --------------------------------------------------------------------------- #
# Insights
# --------------------------------------------------------------------------- #
def generate_insights(summary: pd.DataFrame, area: str, total: int) -> list[str]:
    """Produce human-readable insight sentences from the summary table."""
    insights: list[str] = []
    if summary.empty:
        return [f"No listings were found for **{area}**."]

    insights.append(
        f"Found **{total}** listings across **{len(summary)}** unit type(s) in **{area}**."
    )

    # Cheapest / most expensive unit type by average.
    by_avg = summary.sort_values("Average (RM)")
    cheapest = by_avg.iloc[0]
    priciest = by_avg.iloc[-1]
    insights.append(
        f"**{cheapest['Unit Type']}** units are the most affordable on average at "
        f"**RM {cheapest['Average (RM)']:,.0f}**/month."
    )
    if priciest["Unit Type"] != cheapest["Unit Type"]:
        insights.append(
            f"**{priciest['Unit Type']}** units command the highest average rent at "
            f"**RM {priciest['Average (RM)']:,.0f}**/month."
        )

    # Most common unit type by count.
    most_common = summary.sort_values("Count", ascending=False).iloc[0]
    insights.append(
        f"**{most_common['Unit Type']}** is the most listed type "
        f"({most_common['Count']} listings)."
    )

    # Per-sqft value note where we have sizes.
    valued = summary.dropna(subset=["Avg sqft"])
    valued = valued[valued["Avg sqft"] > 0]
    if not valued.empty:
        valued = valued.assign(
            per_sqft=valued["Average (RM)"] / valued["Avg sqft"]
        ).sort_values("per_sqft")
        best = valued.iloc[0]
        insights.append(
            f"Best value per sqft: **{best['Unit Type']}** at "
            f"**RM {best['per_sqft']:.2f}/sqft**."
        )

    return insights


# --------------------------------------------------------------------------- #
# Excel export
# --------------------------------------------------------------------------- #
def make_excel(summary: pd.DataFrame, listings: pd.DataFrame) -> bytes:
    """Build an .xlsx workbook (Summary + Listings sheets) and return bytes."""
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        (summary if not summary.empty else pd.DataFrame({"Info": ["No data"]})).to_excel(
            writer, sheet_name="Summary", index=False
        )
        (listings if not listings.empty else pd.DataFrame({"Info": ["No data"]})).to_excel(
            writer, sheet_name="Listings", index=False
        )

        # Auto-fit-ish column widths for readability.
        for sheet_name, frame in (("Summary", summary), ("Listings", listings)):
            if frame.empty:
                continue
            worksheet = writer.sheets[sheet_name]
            for idx, col in enumerate(frame.columns, start=1):
                max_len = max(
                    [len(str(col))]
                    + [len(str(v)) for v in frame[col].head(200).tolist()]
                )
                worksheet.column_dimensions[
                    worksheet.cell(row=1, column=idx).column_letter
                ].width = min(max_len + 2, 60)

    buffer.seek(0)
    return buffer.getvalue()


def excel_filename(area: str) -> str:
    """SPEEDHOME_[Area]_[YYYYMMDD].xlsx with a filesystem-safe area slug."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", (area or "Area").strip()).strip("-") or "Area"
    return f"SPEEDHOME_{slug}_{datetime.now():%Y%m%d}.xlsx"
