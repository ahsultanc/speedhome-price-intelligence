# 🏠 Property Price Intelligence — SPEEDHOME.com

**Property Price Intelligence** is a Streamlit web app that scrapes live rental
listings from **[SPEEDHOME.com](https://speedhome.com)** for any Malaysian area,
then turns them into an at-a-glance market report: a price summary by unit type,
a price-distribution chart, auto-generated insights, a verifiable listings table,
and a one-click Excel export.

You search by area (e.g. *Mont Kiara*) or paste a SPEEDHOME URL; the app fetches
every result page, keeps only the listings that are genuinely in your area,
groups them by unit type (Studio / 1BR / 2BR / 3BR / 4BR+), and reports the
**count, average, median, mode, "fair price", and average size** for each — all
in **RM** and **sqft**.

---

## Features

- **Flexible search** — choose an area from a 21-item autocomplete dropdown, or
  paste a direct SPEEDHOME URL (e.g. `https://speedhome.com/rent/mont-kiara`).
- **Area-accurate results** — SPEEDHOME's `/rent/<area>` page is a *radius* search
  that also returns neighbouring areas; the app filters those out so you only see
  listings actually in the area you searched (see **Area filtering** below).
- **Price summary table** grouped by unit type with **Count, Average, Median,
  Mode, Fair Price** (trimmed mean dropping the top & bottom 10%), **Min, Max,
  Avg sqft**, and **Price/sqft** (average monthly rent per square foot).
- **Unit listings table** — Title, Property name, Address, Room type,
  Monthly price (RM), Annual price (RM), sqft, Furnishing status, and a clickable
  **View Listing** link (last column) that opens the original listing on SPEEDHOME.
  Includes **sort** (by monthly price or sqft, ascending/descending) and a
  **filter by room type**; missing values render as a clean "—".
- **Rental-type tabs** — Daily / Monthly / Yearly, split by each listing's minimum
  lease duration, each with a clear empty-state message when there's no data.
- **Box-plot price distribution** per unit type (Plotly).
- **Auto-generated insights** — cheapest/priciest unit type, most-listed type, and
  best value per sqft, written as plain-English sentences.
- **Excel export** → `SPEEDHOME_[Area]_[YYYYMMDD].xlsx` with two sheets
  (**Summary** + **Listings**).
- **Wide, responsive layout** with horizontally scrollable tables.
- **Session caching** via `st.cache_data` — each area is scraped once and reused
  across the tabs, so re-clicking is instant and the site isn't hit repeatedly.

---

## How to run it locally

> Requires **Python 3.11+**. Tested on Windows (PowerShell) with Python 3.14.

```powershell
# 1. (recommended) create and activate a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. install dependencies
pip install -r requirements.txt
#   ...or, for an exact reproducible environment:
#   pip install -r requirements.lock.txt

# 3. launch the app
python -m streamlit run app.py
```

Then open **http://localhost:8501** in your browser, pick an area (or paste a
SPEEDHOME URL) in the left sidebar, and click **Search**.

> **Tip:** use `python -m streamlit run app.py` (rather than the bare `streamlit`
> command) unless your virtual environment's `Scripts/` folder is on your `PATH`.

On macOS / Linux the only difference is activating the venv with
`source .venv/bin/activate`.

---

## Testing all areas

`test_all_areas.py` is a smoke-test harness that scrapes **every area** in
`scraper.AREAS` and reports the outcome for each:

1. **OK** — listings returned (with the count)
2. **BLOCKED** — Cloudflare challenge / non-200 / no page data
3. **EMPTY** — reachable but 0 listings
4. **Response time** per area

Run it from the project root (with dependencies installed):

```powershell
python test_all_areas.py
```

It prints a live progress line per area, then a summary table and totals, and
writes a `test_results.csv` (columns: `area, status, total, in_area, http,
source, secs, error`) for review. The CSV is regenerated on each run and is
**git-ignored**, so it won't clutter the repo.

> Note: run from a normal home/office network, every area should return `OK`.
> The `BLOCKED` status is mainly useful when running from a datacenter IP (e.g.
> Streamlit Cloud), where Cloudflare is more aggressive — see **Limitations**.

---

## Area filtering (important)

SPEEDHOME's `/rent/<area>` endpoint is a **geographic radius search** — searching
*Mont Kiara* also returns listings in adjacent areas such as Segambut, Sentul,
Dutamas and Chow Kit. No single structured field reliably identifies the true
area (`city` is always "Kuala Lumpur", and a postcode like `50480` spans several
neighbourhoods), so the app filters by **text match**:

> A listing is kept only if the normalised search term appears in its **title,
> property name, address, or the SPEEDHOME URL slug** (e.g.
> `sophia-condominium-mont-kiara-...`). Matching is case- and space-insensitive,
> so *"Desa ParkCity"* also matches *"Desa Park City"*.

**What this means in practice:** a *Mont Kiara* search might show ~8 results
instead of the ~67 the radius search returned — that's intentional. The other ~59
were in nearby areas. A caption above the tabs shows how many were hidden:

> 📍 Showing **8** listing(s) actually in **Mont Kiara** · hid **59** from nearby areas.

Prefer to see everything the radius search returned? Untick **"Only show listings
in this area"** in the sidebar (it's on by default).

---

## Polite, robust scraping

- Only `/rent/...` paths are requested (per the documented allowed paths).
- `time.sleep(1.5)` between **every** HTTP request.
- A real desktop browser `User-Agent` plus `Accept` / `Accept-Language` / `Referer`
  headers on every request.
- **All** result pages are fetched (pagination is read from the page's own
  `totalPages` metadata).

> ⚠️ This tool is for **educational / personal-research** use. Respect SPEEDHOME's
> Terms of Service and `robots.txt`, and scrape responsibly at low volume.

---

## How it works

SPEEDHOME is a **Next.js server-side-rendered** site, so the listing data is
embedded in the raw HTML inside the `<script id="__NEXT_DATA__">` tag as JSON. The
app reads it directly from `props.pageProps.propertyList.content` and maps each
item's fields (`name`, `address`, `price`, `sqft`, `bedroom`, `furnishType`,
`minRentalDuration`, `slug`). Prices are in **RM**, sizes in **sqft**, and the
annual price is simply `monthly × 12`. Each listing links to its detail page at
`https://speedhome.com/details/<slug>`.

**Getting past Cloudflare.** SPEEDHOME sits behind Cloudflare, which fingerprints
the TLS handshake — a plain `requests` call is answered with an HTTP **403
"Just a moment…"** challenge instead of the page. Rotating User-Agents or headers
doesn't help because the block is below HTTP, at the TLS layer. The app therefore
fetches with **`curl_cffi`** (which reproduces Chrome's TLS/JA3 fingerprint),
trying several impersonation profiles, and falls back to **`cloudscraper`** if
needed. Both return a clean HTTP 200 with the `__NEXT_DATA__` JSON intact. If the
JSON shape ever disappears, a BeautifulSoup HTML-card parser is used as a backstop.

The **Daily / Monthly / Yearly** tabs are derived from each listing's minimum
lease duration (`minRentalDuration`): *Monthly* shows everything, *Yearly* the
leases of ≥ 12 months, and *Daily* the rare short-stay (≤ 1 month) listings —
SPEEDHOME's `/rent` listings are monthly-tenancy, so Daily is usually empty and
shows a clear message.

---

## Project structure

```
speedhome-app/
├── app.py                  # Streamlit UI (search, tabs, tables, chart, export)
├── scraper.py              # Fetch (Cloudflare-aware) + parse + area/rental filters
├── utils.py                # Stats, unit classification, insights, Excel export
├── test_all_areas.py       # Smoke-test harness: scrapes every area, writes CSV
├── requirements.txt        # Dependencies (minimum versions)
├── requirements.lock.txt   # Exact pinned versions for reproducible installs
└── README.md
```

### Dependencies

`streamlit`, `curl_cffi` + `cloudscraper` (Cloudflare-aware fetching),
`requests`, `beautifulsoup4` + `lxml` (HTML parsing), `pandas` (data wrangling),
`plotly` (box plot), and `openpyxl` (Excel export).

---

## Limitations & known caveats

- **The area filter is text-based.** A listing is kept only if the area name
  appears in its title, property name, address, or URL slug. This is precise in
  practice, but it can occasionally:
  - **miss** a genuinely in-area listing whose name/address/slug never spells out
    the area (e.g. it's described only by a road name or building), and
  - **keep** a nearby listing whose text mentions the area as a selling point
    (e.g. *"5 min to Mont Kiara"*).
  There's no perfect signal in SPEEDHOME's data; this trades a little recall for
  much higher precision. Untick *"Only show listings in this area"* to see the
  unfiltered radius results.
- **Unit-type classification relies on the bedroom count.** Listings with a
  missing/zero bedroom count (including some single-room rentals) are bucketed as
  **Studio**, which may not always be the intended category.
- **Rental-type tabs are derived, not native.** SPEEDHOME's `/rent` listings are
  monthly-tenancy, so Daily/Yearly are inferred from `minRentalDuration` rather
  than being a true product split — Daily is usually empty.
- **No JavaScript is executed.** Data comes from the server-rendered
  `__NEXT_DATA__` JSON. Anything the page loads later via client-side API calls
  (e.g. lazy-loaded extras) is not captured.
- **Live scraping is best-effort.** Results depend on SPEEDHOME's availability and
  Cloudflare's mood; transient blocks, rate-limiting, or markup/field-name changes
  on the site can reduce or break results until the scraper is adjusted.
- **Prices/sizes are taken as published.** No currency conversion or unit
  normalisation is applied; missing values are simply excluded from the relevant
  statistics. The annual price is a straight `monthly × 12`.
- **Caching can show slightly stale data.** Each area is cached for up to one hour
  (`st.cache_data(ttl=3600)`); newly posted listings may not appear until the
  cache expires.

## Notes & troubleshooting

- **Far fewer listings than expected?** That's the area filter working — see
  **Area filtering** above. Untick the sidebar option to see nearby areas too.
- **No data / a 403 or "challenge" error?** Cloudflare may be temporarily blocking
  the client. The app already retries across several browser fingerprints and a
  fallback engine; wait a little and try again. Ensure `curl_cffi` and
  `cloudscraper` are installed (`pip install -r requirements.txt`).
- **Daily / Yearly tab empty?** Expected for most areas — SPEEDHOME's `/rent`
  listings are monthly-tenancy leases. The **Monthly** tab has the full set.
- SPEEDHOME's internal field names can change over time; the scraper is written
  defensively but may need small tweaks if the site restructures its data.
