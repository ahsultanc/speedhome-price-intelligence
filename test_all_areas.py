"""
test_all_areas.py — Smoke-test every area in scraper.AREAS.

For each area it runs a full scrape and reports:
  1. Areas that successfully return data (listing count)
  2. Areas that get Cloudflare-blocked
  3. Areas that return 0 listings (reachable, but empty)
  4. Response time per area

Run:  python test_all_areas.py
"""

from __future__ import annotations

import csv
import time

from scraper import AREAS, filter_by_area, scrape_area

CSV_PATH = "test_results.csv"
CSV_FIELDS = ["area", "status", "total", "in_area", "http", "source", "secs", "error"]

# Result categories
OK = "OK"            # returned listings
EMPTY = "EMPTY"      # reachable (HTTP 200, page data) but 0 listings
BLOCKED = "BLOCKED"  # Cloudflare challenge / non-200 / no page data
ERROR = "ERROR"      # unexpected exception


def classify(listings, meta) -> str:
    dbg = meta.get("debug", {})
    if listings:
        return OK
    # Empty result: was it blocked, or genuinely empty?
    blocked = (
        dbg.get("challenge_detected")
        or dbg.get("http_status") not in (200, None)
        or not dbg.get("has_next_data")
    )
    return BLOCKED if blocked else EMPTY


def run() -> list[dict]:
    rows = []
    print(f"Testing {len(AREAS)} areas...\n")
    for i, area in enumerate(AREAS, 1):
        t0 = time.time()
        try:
            listings, meta = scrape_area(area)
            status = classify(listings, meta)
            dbg = meta.get("debug", {})
            in_area = len(filter_by_area(listings, meta.get("area_term", area)))
            row = {
                "area": area,
                "status": status,
                "total": len(listings),
                "in_area": in_area,
                "http": dbg.get("http_status"),
                "source": meta.get("source", "—"),
                "secs": round(time.time() - t0, 1),
            }
        except Exception as exc:  # noqa: BLE001
            row = {
                "area": area, "status": ERROR, "total": 0, "in_area": 0,
                "http": None, "source": "—", "secs": round(time.time() - t0, 1),
                "error": str(exc)[:60],
            }
        rows.append(row)
        print(f"  [{i:2}/{len(AREAS)}] {area:16} {row['status']:8} "
              f"total={row['total']:3} in-area={row['in_area']:3} "
              f"http={row['http']} {row['secs']:5}s")
    return rows


def summary(rows: list[dict]) -> None:
    width = 78
    print("\n" + "=" * width)
    print("SUMMARY — SPEEDHOME area scrape test")
    print("=" * width)
    header = f"{'Area':16} {'Status':8} {'Total':>6} {'In-area':>8} {'HTTP':>5} {'Source':>10} {'Time':>7}"
    print(header)
    print("-" * width)
    for r in rows:
        print(f"{r['area']:16} {r['status']:8} {r['total']:>6} {r['in_area']:>8} "
              f"{str(r['http']):>5} {str(r['source']):>10} {r['secs']:>6}s")
    print("-" * width)

    by = {OK: [], EMPTY: [], BLOCKED: [], ERROR: []}
    for r in rows:
        by[r["status"]].append(r["area"])

    times = [r["secs"] for r in rows]
    print(f"\nTotals: {len(rows)} areas tested")
    print(f"  ✅ OK (data)      : {len(by[OK]):2}  {', '.join(by[OK]) or '—'}")
    print(f"  ⚪ EMPTY (0 list) : {len(by[EMPTY]):2}  {', '.join(by[EMPTY]) or '—'}")
    print(f"  🛡️ BLOCKED        : {len(by[BLOCKED]):2}  {', '.join(by[BLOCKED]) or '—'}")
    print(f"  ❌ ERROR          : {len(by[ERROR]):2}  {', '.join(by[ERROR]) or '—'}")
    if times:
        print(f"\nResponse time: avg {sum(times)/len(times):.1f}s · "
              f"min {min(times):.1f}s · max {max(times):.1f}s · total {sum(times):.1f}s")


def write_csv(rows: list[dict], path: str = CSV_PATH) -> None:
    """Write per-area results to a CSV for review."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    print(f"\n📄 Results written to {path}")


if __name__ == "__main__":
    results = run()
    summary(results)
    write_csv(results)
