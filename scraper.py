"""
scraper.py — All scraping logic for SPEEDHOME.com.

IMPORTANT — why this is not a plain `requests` scraper
------------------------------------------------------
SPEEDHOME sits behind **Cloudflare bot protection**. Cloudflare fingerprints the
TLS/JA3 handshake of the HTTP client, so a normal `requests` call is answered
with an HTTP **403 "Just a moment…"** challenge page — no listing data at all.
Rotating User-Agents or adding headers does NOT help, because the block happens
at the TLS layer, below HTTP.

The fix is to use a client that reproduces a real browser's TLS fingerprint:
  * Primary:  ``curl_cffi`` with ``impersonate="chrome"`` (mimics Chrome's JA3).
  * Fallback: ``cloudscraper`` (solves Cloudflare's JS challenge).
Both return a clean HTTP 200 with the Next.js ``__NEXT_DATA__`` JSON intact.

Once we have the page, the listing data lives at:
    __NEXT_DATA__ -> props -> pageProps -> propertyList -> content   (list)
with pagination metadata on ``propertyList`` (``totalPages``, ``last`` …).
Each listing's detail page is ``https://speedhome.com/details/<slug>``.

Politeness:
  * Only ``/rent/...`` paths are requested (per the documented allowed paths).
  * ``time.sleep(1.5)`` between every HTTP request.
  * A real desktop browser User-Agent + Accept / Accept-Language / Referer.

Public entry point:
  scrape_area(query, max_pages=20) -> (list[dict], meta)
"""

from __future__ import annotations

import json
import os
import random
import re
import tempfile
import time
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from utils import classify_unit_type, parse_price, parse_size

# Optional HTTP backends — imported defensively so the module still loads even
# if one isn't installed (the UI will surface a clear error at fetch time).
try:
    from curl_cffi import requests as curl_requests
except Exception:  # noqa: BLE001
    curl_requests = None

try:
    import cloudscraper
except Exception:  # noqa: BLE001
    cloudscraper = None

BASE_URL = "https://speedhome.com"
REQUEST_DELAY = 1.5  # seconds between requests (politeness)
TIMEOUT = 30
MAX_RETRIES = 3      # full retry rounds before giving up on a request
RETRY_DELAY_RANGE = (2.0, 4.0)  # random backoff (seconds) between retries

# Canonical list of supported areas (single source of truth — the UI's
# autocomplete and the test harness both import this).
AREAS = [
    "Mont Kiara", "Bangsar", "KLCC", "Petaling Jaya", "Chow Kit", "Ampang",
    "Sri Hartamas", "Damansara", "Subang Jaya", "Shah Alam", "Cyberjaya",
    "Bukit Jalil", "Puchong", "Kepong", "Setapak", "Wangsa Maju", "Cheras",
    "Desa ParkCity", "Sunway", "Ara Damansara", "Tropicana",
]

# Five realistic desktop browser User-Agents, each paired with a matching
# curl_cffi TLS-impersonation profile (so the TLS fingerprint matches the UA).
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
]
# Paired curl_cffi impersonation targets (invalid/unknown ones are caught and
# skipped at request time, so the list is safe across curl_cffi versions).
IMPERSONATE_PROFILES = ["chrome124", "chrome120", "chrome116", "safari17_0", "edge101"]

# Accept-Language variants, rotated per attempt for slight header variation.
ACCEPT_LANGUAGES = [
    "en-US,en;q=0.9",
    "en-GB,en;q=0.8",
    "en-US,en;q=0.9,ms;q=0.6",
    "en;q=0.9",
    "en-US,en;q=0.8,zh-CN;q=0.5",
]

# Last response status code / backend seen (exposed via debug for the UI).
_last_status: int | None = None
_last_backend: str = "—"
_request_counter = 0  # drives UA/profile rotation across calls


def _varied_headers(ua: str, attempt: int) -> dict:
    """Build request headers that differ slightly on each retry attempt."""
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": ACCEPT_LANGUAGES[attempt % len(ACCEPT_LANGUAGES)],
        "Upgrade-Insecure-Requests": "1",
    }
    # Vary a few optional headers so repeated attempts don't look identical.
    if attempt % 2 == 0:
        headers["Referer"] = "https://speedhome.com/"
    else:
        headers["Referer"] = "https://www.google.com/"
    if "Chrome" in ua or "Edg" in ua:
        headers["Sec-Ch-Ua"] = '"Chromium";v="124", "Not-A.Brand";v="99"'
        headers["Sec-Ch-Ua-Mobile"] = "?0"
        headers["Sec-Ch-Ua-Platform"] = '"Windows"' if "Windows" in ua else '"macOS"'
    if attempt > 0:
        headers["Cache-Control"] = "no-cache"
    return headers


def _base_headers(ua: str) -> dict:
    """Backwards-compatible default headers (first-attempt variant)."""
    return _varied_headers(ua, 0)


# --------------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------------- #
def slugify_area(area: str) -> str:
    """'Mont Kiara' -> 'mont-kiara'."""
    return re.sub(r"[^a-z0-9]+", "-", area.strip().lower()).strip("-")


def build_url(query: str, page: int = 1) -> str:
    """Build a /rent/<area> URL, or normalise a directly-pasted URL.

    `query` may be either a free-text area name or a full speedhome URL.
    """
    query = (query or "").strip()
    if query.lower().startswith("http"):
        path = urlparse(query).path
    else:
        path = f"/rent/{slugify_area(query)}"

    url = urljoin(BASE_URL, path)
    if page and page > 1:
        url = f"{url}?page={page}"
    return url


def is_speedhome_url(text: str) -> bool:
    text = (text or "").strip().lower()
    return text.startswith("http") and "speedhome.com" in text


# Lookup for restoring canonical casing (e.g. "klcc" -> "KLCC",
# "desa parkcity" -> "Desa ParkCity") from any user/URL-derived spelling.
_AREA_CANONICAL = {re.sub(r"[^a-z0-9]", "", a.lower()): a for a in AREAS}


def canonical_area_name(name: str) -> str:
    """Return the canonical, properly-cased area name.

    Matches against the known ``AREAS`` list (ignoring case/spacing) so
    "klcc" -> "KLCC" and "chow kit" -> "Chow Kit". Falls back to Title Case
    for unknown areas.
    """
    key = re.sub(r"[^a-z0-9]", "", str(name or "").lower())
    return _AREA_CANONICAL.get(key, str(name or "").strip().title())


def detail_url(slug: str | None) -> str:
    """SPEEDHOME listing detail pages live at /details/<slug>."""
    if not slug:
        return BASE_URL
    if str(slug).startswith("http"):
        return str(slug)
    return f"{BASE_URL}/details/{str(slug).lstrip('/')}"


# --------------------------------------------------------------------------- #
# HTTP — Cloudflare-aware fetch
# --------------------------------------------------------------------------- #
def _looks_like_challenge(html: str) -> bool:
    return (
        "Just a moment" in html
        or "challenges.cloudflare.com" in html
        or "cf-browser-verification" in html
    )


def _get(url: str, extra_headers: dict | None = None) -> tuple[str, int]:
    """GET a URL past Cloudflare, with retries. Returns (body, status_code).

    Strategy (per request):
      * Up to ``MAX_RETRIES`` rounds with ``curl_cffi`` (real Chrome/Safari/Edge
        TLS fingerprints), each round using a DIFFERENT User-Agent + matching
        impersonation profile and slightly varied headers.
      * A polite 1.5s delay before the first attempt; a random 2–4s back-off
        before each retry.
      * If every curl_cffi round is blocked, one ``cloudscraper`` fallback
        attempt (which can solve the JS challenge).

    ``extra_headers`` are merged into every attempt (used to send the
    ``x-nextjs-data`` header when fetching the JSON data endpoint).

    NOTE: rotating UAs/headers helps with *transient* blocks, but cannot defeat
    a block based on IP reputation (e.g. datacenter IPs on Streamlit Cloud) —
    Cloudflare decides that below the HTTP layer.
    """
    global _last_status, _last_backend, _request_counter

    n = len(USER_AGENTS)

    def _headers(ua: str, attempt: int) -> dict:
        h = _varied_headers(ua, attempt)
        if extra_headers:
            h.update(extra_headers)
        return h

    # 1) Primary backend: curl_cffi, retried with a fresh fingerprint each round.
    if curl_requests is not None:
        for attempt in range(MAX_RETRIES):
            idx = (_request_counter + attempt) % n
            ua = USER_AGENTS[idx]
            profile = IMPERSONATE_PROFILES[idx]
            headers = _headers(ua, attempt)

            # Polite delay first time; randomised back-off before each retry.
            if attempt == 0:
                time.sleep(REQUEST_DELAY)
            else:
                time.sleep(random.uniform(*RETRY_DELAY_RANGE))

            try:
                resp = curl_requests.get(
                    url, headers=headers, impersonate=profile, timeout=TIMEOUT
                )
                html = resp.text
                _last_status = resp.status_code
                _last_backend = f"curl_cffi[{profile}] (try {attempt + 1}/{MAX_RETRIES})"
                if resp.status_code == 200 and not _looks_like_challenge(html):
                    _request_counter += 1
                    return html, resp.status_code
            except Exception:  # noqa: BLE001 — retry with the next fingerprint
                pass
        _request_counter += 1

    # 2) Fallback backend: cloudscraper (solves the JS challenge).
    if cloudscraper is not None:
        try:
            time.sleep(random.uniform(*RETRY_DELAY_RANGE))
            scraper = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "desktop": True}
            )
            resp = scraper.get(
                url, headers=_headers(USER_AGENTS[0], 1), timeout=TIMEOUT + 15
            )
            html = resp.text
            _last_status = resp.status_code
            _last_backend = "cloudscraper"
            if resp.status_code == 200 and not _looks_like_challenge(html):
                return html, resp.status_code
        except Exception:  # noqa: BLE001
            pass

    # Nothing worked — return whatever we last saw so the UI can report it.
    return "", (_last_status or 0)


# --------------------------------------------------------------------------- #
# Next.js JSON data endpoint  (preferred over HTML scraping)
#
# A Next.js SSR page exposes the exact same `pageProps` JSON it renders from at
#   /_next/data/<buildId>/rent/<slug>.json
# when called with the `x-nextjs-data: 1` header. We prefer this because it is
# pure JSON (no HTML parsing), ~30% smaller, and — being a data/asset-style
# route — may be subjected to less aggressive Cloudflare challenging than the
# full HTML page. The HTML scrape remains as a fallback.
#
# The buildId rotates on every SPEEDHOME deploy, so we learn it from a page's
# __NEXT_DATA__ and cache it; a stale id returns 404 and triggers a refresh.
# --------------------------------------------------------------------------- #
_NEXTDATA_HEADER = {"x-nextjs-data": "1"}
# A recent known buildId, used to bootstrap the JSON endpoint on a cold start
# (so the very first search can skip the HTML page). If SPEEDHOME has since
# redeployed, this returns 404 and we transparently refresh it from the HTML.
_BUILD_ID_SEED = "build-1779964875559"
_BUILD_ID_CACHE = os.path.join(tempfile.gettempdir(), "speedhome_build_id.txt")


def _load_cached_build_id() -> str:
    try:
        with open(_BUILD_ID_CACHE, encoding="utf-8") as fh:
            cached = fh.read().strip()
            if cached:
                return cached
    except OSError:
        pass
    return _BUILD_ID_SEED


def _save_build_id(build_id: str) -> None:
    global _build_id
    _build_id = build_id
    try:
        with open(_BUILD_ID_CACHE, "w", encoding="utf-8") as fh:
            fh.write(build_id)
    except OSError:
        pass  # cache is best-effort; in-memory value still applies


_build_id: str | None = _load_cached_build_id()


def _rent_slug(query: str) -> str | None:
    """Return the /rent/<slug> area slug for a query, or None if not applicable.

    Works for plain area names and for direct ``…/rent/<slug>`` URLs. Returns
    None for any other URL shape (so we fall back to HTML for those).
    """
    query = (query or "").strip()
    if query.lower().startswith("http"):
        parts = [p for p in urlparse(query).path.split("/") if p]
        if len(parts) == 2 and parts[0].lower() == "rent":
            return parts[1].lower()
        return None
    return slugify_area(query)


def _data_url(slug: str, page: int) -> str:
    url = f"{BASE_URL}/_next/data/{_build_id}/rent/{slug}.json?location={slug}"
    if page and page > 1:
        url += f"&page={page}"
    return url


def _fetch_json_property_list(slug: str, page: int):
    """Fetch one page via the JSON data endpoint.

    Returns (property_list_dict | None, status_code). Requires a known
    ``_build_id``; returns (None, status) on 404 (stale id) or any error.
    """
    if not _build_id:
        return None, 0
    headers = dict(_NEXTDATA_HEADER)
    headers["Referer"] = f"{BASE_URL}/rent/{slug}"
    body, status = _get(_data_url(slug, page), extra_headers=headers)
    if status != 200 or not body or body[:1] not in "{[":
        return None, status
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None, status
    return _get_property_list(data), status


# --------------------------------------------------------------------------- #
# __NEXT_DATA__ extraction
# --------------------------------------------------------------------------- #
def _extract_next_data(html: str) -> dict | None:
    """Pull and parse the JSON inside <script id="__NEXT_DATA__">."""
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        return None
    try:
        return json.loads(tag.string)
    except (json.JSONDecodeError, TypeError):
        return None


def _get_property_list(next_data: dict | None) -> dict | None:
    """Return the propertyList object (content + pagination), or None.

    Handles both shapes:
      * HTML __NEXT_DATA__  -> {"props": {"pageProps": {"propertyList": ...}}}
      * JSON data endpoint  -> {"pageProps": {"propertyList": ...}}
    """
    if not isinstance(next_data, dict):
        return None
    page_props = next_data.get("props", {}).get("pageProps")
    if not isinstance(page_props, dict):
        page_props = next_data.get("pageProps")
    if not isinstance(page_props, dict):
        return None
    pl = page_props.get("propertyList")
    return pl if isinstance(pl, dict) else None


# --------------------------------------------------------------------------- #
# Field mapping (exact, based on the live SPEEDHOME schema)
# --------------------------------------------------------------------------- #
_FURNISH_LABELS = {
    "FULL": "Fully furnished",
    "PARTIAL": "Partially furnished",
    "NONE": "Unfurnished",
}


def _furnishing_label(raw_value) -> str:
    if not raw_value:
        return "—"
    return _FURNISH_LABELS.get(str(raw_value).upper(), str(raw_value).title())


def _room_type_label(raw: dict, unit_type: str) -> str:
    """Human-readable room/property descriptor for the listing table."""
    ptype = (raw.get("type") or "").upper()
    rtype = raw.get("roomType")
    if ptype == "ROOM":
        return f"{str(rtype).title()} room" if rtype else "Room"
    if ptype == "LANDED":
        return f"Landed · {unit_type}"
    return unit_type  # HIGHRISE / condo etc. -> just the unit type


def _building_name(name: str) -> str:
    """'Sophia Condominium, Mont Kiara' -> 'Sophia Condominium' (the part
    before the locality), so the Property-name column stays short."""
    first = str(name).split(",")[0].strip()
    return first or str(name).strip() or "—"


def _normalise_listing(raw: dict) -> dict | None:
    """Map a raw SPEEDHOME propertyList item to our flat schema."""
    if not isinstance(raw, dict):
        return None

    name = raw.get("name") or "Untitled listing"
    address = raw.get("address") or "—"
    slug = str(raw.get("slug") or "")
    price = parse_price(raw.get("price"))
    sqft = parse_size(raw.get("sqft"))
    bedroom = raw.get("bedroom")
    min_duration = raw.get("minRentalDuration")
    building = _building_name(name)

    unit_type = classify_unit_type(bedroom, str(name))
    # A rented single room with no bedroom count is best shown as "Room".
    if (raw.get("type") or "").upper() == "ROOM" and bedroom in (None, 0):
        unit_type = "Studio"  # keep within the 5 summary buckets

    if price is None and not name:
        return None

    return {
        "title": str(name),
        "property_name": building,
        "address": str(address),
        "room_type": _room_type_label(raw, unit_type),
        "unit_type": unit_type,
        "monthly_price": price,
        "annual_price": round(price * 12, 2) if price is not None else None,
        "sqft": sqft,
        "furnishing": _furnishing_label(raw.get("furnishType")),
        "min_duration": int(min_duration) if isinstance(min_duration, (int, float)) else None,
        # Text used to confirm a listing is actually in the searched area:
        # title, property name, address AND the URL slug (e.g. "...-mont-kiara-...").
        "_match_text": f"{name} {building} {address} {slug}",
        "link": detail_url(raw.get("slug")),
    }


# --------------------------------------------------------------------------- #
# HTML fallback parser (only used if the JSON shape ever disappears)
# --------------------------------------------------------------------------- #
def _parse_html_cards(html: str) -> list[dict]:
    """Best-effort parse of rendered listing cards when JSON isn't usable."""
    soup = BeautifulSoup(html, "lxml")
    listings: list[dict] = []
    anchors = soup.select('a[href*="/details/"], a[href*="/listing"], a[href*="/rent/"]')
    seen = set()
    for a in anchors:
        href = a.get("href", "")
        if not href or href in seen:
            continue
        card = a.find_parent(["article", "div", "li"]) or a
        text = card.get_text(" ", strip=True)
        price = parse_price(text) if "rm" in text.lower() else None
        if price is None:
            continue
        seen.add(href)
        title = a.get_text(" ", strip=True) or "Untitled listing"
        m_bed = re.search(r"(\d+)\s*(?:bed|br|room)", text, re.I)
        unit_type = classify_unit_type(m_bed.group(1) if m_bed else None, title + " " + text)
        m_size = re.search(r"(\d[\d,]*)\s*sqft", text, re.I)
        listings.append(
            {
                "title": title,
                "property_name": _building_name(title),
                "address": "—",
                "room_type": unit_type,
                "unit_type": unit_type,
                "monthly_price": price,
                "annual_price": round(price * 12, 2),
                "sqft": parse_size(m_size.group(1)) if m_size else None,
                "furnishing": "—",
                "min_duration": None,
                "_match_text": f"{text} {href}",
                "link": urljoin(BASE_URL, href),
            }
        )
    return listings


# --------------------------------------------------------------------------- #
# Debug helpers
# --------------------------------------------------------------------------- #
def _safe_sample(value, _depth: int = 0):
    """Return a JSON-serialisable, size-bounded preview of a raw value."""
    if _depth > 3:
        return "…"
    if isinstance(value, dict):
        return {k: _safe_sample(v, _depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        return [_safe_sample(v, _depth + 1) for v in value[:3]]
    if isinstance(value, str):
        return value if len(value) <= 200 else value[:200] + "…"
    return value


def _build_debug(property_list: dict | None, html: str, source: str) -> dict:
    """Diagnostics for the Cloudflare-block detection (and optional inspection).

    ``source`` is 'json' (data endpoint), 'html' (page scrape) or 'none'.
    """
    content = property_list.get("content", []) if isinstance(property_list, dict) else []
    first = content[0] if content and isinstance(content[0], dict) else None
    return {
        "http_status": _last_status,
        "backend": _last_backend,
        "source": source,
        "build_id": _build_id,
        "challenge_detected": _looks_like_challenge(html),
        "html_head": html[:2000],
        "has_next_data": bool(property_list),
        "raw_listings_found": len(content),
        "listing_keys": sorted(first.keys()) if isinstance(first, dict) else [],
        "sample_listing": _safe_sample(first) if first else None,
    }


# --------------------------------------------------------------------------- #
# Page + area scraping
# --------------------------------------------------------------------------- #
def _listings_from_property_list(pl: dict | None) -> list[dict]:
    listings: list[dict] = []
    if isinstance(pl, dict):
        for raw in pl.get("content", []):
            if isinstance(raw, dict):
                norm = _normalise_listing(raw)
                if norm:
                    listings.append(norm)
    return listings


def _load_page(query: str, slug: str | None, page: int):
    """Load one results page, preferring the JSON data endpoint.

    Order:
      1. JSON data endpoint (if we know a buildId and have a /rent/<slug>).
      2. HTML page scrape — also (re)learns the buildId for future JSON calls.
      3. HTML-card fallback parse.

    Returns (listings, property_list, html, source).
    """
    global _build_id

    # 1) Preferred: pure-JSON data endpoint.
    if slug and _build_id:
        pl, status = _fetch_json_property_list(slug, page)
        if pl is not None:
            return _listings_from_property_list(pl), pl, "", "json"
        # 404 → our buildId is stale; fall through to HTML to refresh it.

    # 2) HTML page (also teaches us a fresh buildId).
    html, _ = _get(build_url(query, page=page))
    next_data = _extract_next_data(html)
    if isinstance(next_data, dict) and next_data.get("buildId"):
        _save_build_id(next_data["buildId"])
        # Retry JSON now that we have a fresh buildId (keeps later pages on JSON).
        if slug:
            pl, _ = _fetch_json_property_list(slug, page)
            if pl is not None:
                return _listings_from_property_list(pl), pl, html, "html+json"

    pl = _get_property_list(next_data)
    if pl is not None:
        return _listings_from_property_list(pl), pl, html, "html"

    # 3) Last resort: parse rendered cards.
    cards = _parse_html_cards(html) if html else []
    return cards, None, html, ("html-cards" if cards else "none")


def scrape_area(query: str, max_pages: int = 20):
    """Scrape ALL result pages for an area / URL.

    Prefers SPEEDHOME's Next.js JSON data endpoint (``/_next/data/...json``) and
    falls back to HTML scraping. Each listing carries ``min_duration`` (minimum
    lease in months) which the UI uses to split the Daily / Monthly / Yearly
    tabs. Returns (listings, meta).
    """
    area = query
    if is_speedhome_url(query):
        seg = [s for s in urlparse(query).path.split("/") if s]
        area = seg[-1].replace("-", " ").title() if seg else "Area"

    slug = _rent_slug(query)
    first_url = build_url(query, page=1)

    listings, pl, html, source = _load_page(query, slug, page=1)
    total_pages = int(pl.get("totalPages", 1) or 1) if isinstance(pl, dict) else 1
    is_last = bool(pl.get("last", True)) if isinstance(pl, dict) else True

    pages_to_get = min(total_pages, max_pages)
    seen_links = {it["link"] for it in listings}

    if not is_last:
        for page in range(2, pages_to_get + 1):
            page_listings, _, _, _ = _load_page(query, slug, page=page)
            if not page_listings:
                break
            new_items = [it for it in page_listings if it["link"] not in seen_links]
            if not new_items:
                break
            for it in new_items:
                seen_links.add(it["link"])
            listings.extend(new_items)

    # Canonical casing for display/insights ("klcc" -> "KLCC").
    area_name = canonical_area_name(area)
    meta = {
        "area": area_name,
        "area_term": area_name,
        "pages_scraped": pages_to_get,
        "total_pages": total_pages,
        "first_url": first_url,
        "count": len(listings),
        "used_json": source in ("json", "html+json"),
        "source": source,
        "debug": _build_debug(pl, html, source),
    }
    return listings, meta


# --------------------------------------------------------------------------- #
# Rental-type filtering (data-driven, based on minimum lease duration)
# --------------------------------------------------------------------------- #
def _norm_text(s) -> str:
    """Lowercase, alphanumeric-only — so 'Desa ParkCity' == 'Desa Park City'."""
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _keywords(s) -> list[str]:
    """Split a phrase into lowercase alphanumeric keywords.

    'Shah Alam' -> ['shah', 'alam']; 'Sri Hartamas' -> ['sri', 'hartamas'].
    """
    return re.findall(r"[a-z0-9]+", str(s or "").lower())


# Some areas are known by several names / sub-localities. When the user searches
# the key, a listing counts as "in area" if it matches the search term OR any of
# these alias phrases. Keys are matched on their normalised form (see
# _norm_text), so they're case/space/punctuation-insensitive ("KLCC" == "klcc").
AREA_ALIASES = {
    "klcc": ["KLCC", "Kuala Lumpur City Centre", "KL City Centre"],
    "srihartamas": ["Sri Hartamas", "Hartamas"],
    "desaparkcity": ["Desa ParkCity", "Desa Park City", "ParkCity"],
    "bukitjalil": ["Bukit Jalil"],
}


def _area_keyword_sets(area_term: str) -> list[list[str]]:
    """Build the keyword sets a listing may satisfy to count as in-area.

    Returns one keyword list per accepted phrase: the search term plus any known
    aliases. A listing matches if it contains ALL keywords of ANY one set.
    """
    if not _norm_text(area_term):
        return []
    phrases = AREA_ALIASES.get(_norm_text(area_term), [area_term])
    # Ensure the original search term is always one of the accepted phrases.
    if area_term not in phrases:
        phrases = [area_term, *phrases]
    sets, seen = [], set()
    for phrase in phrases:
        kws = _keywords(phrase)
        key = tuple(kws)
        if kws and key not in seen:
            seen.add(key)
            sets.append(kws)
    return sets


def filter_by_area(listings: list[dict], area_term: str) -> list[dict]:
    """Keep only listings that actually belong to the searched area.

    SPEEDHOME's /rent/<area> endpoint is a *radius* search and returns nearby
    areas too (e.g. searching Mont Kiara also returns Segambut, Sentul…).

    A listing is kept if its searchable text (title, property name, address and
    the SPEEDHOME URL slug) contains **all keywords** of the search term — or of
    any known alias phrase (see ``AREA_ALIASES``). Keyword matching is
    case-insensitive and order-independent, so "Shah Alam" matches text
    containing both "shah" and "alam" anywhere. Returns all listings when
    ``area_term`` is empty.
    """
    keyword_sets = _area_keyword_sets(area_term)
    if not keyword_sets:
        return list(listings)

    def matches(listing: dict) -> bool:
        text = str(listing.get("_match_text", "")).lower()
        return any(all(kw in text for kw in kws) for kws in keyword_sets)

    return [l for l in listings if matches(l)]


def filter_by_rental_type(listings: list[dict], rental_type: str) -> list[dict]:
    """Split listings by rental type using each item's ``min_duration``.

    SPEEDHOME's /rent listings are monthly-tenancy, so:
      * monthly -> all listings (the canonical, complete view)
      * yearly  -> long leases requiring >= 12 months
      * daily   -> short-stay (<= 1 month); rarely present on this endpoint
    """
    rt = (rental_type or "monthly").lower()
    if rt == "monthly":
        return list(listings)
    if rt == "yearly":
        return [l for l in listings if (l.get("min_duration") or 0) >= 12]
    if rt == "daily":
        return [
            l for l in listings
            if l.get("min_duration") is not None and l["min_duration"] <= 1
        ]
    return list(listings)
