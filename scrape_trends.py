#!/usr/bin/env python3
"""
scrape_trends.py

Renders the real Google Trends "Trending Now" page with a headless
browser (so client-side JS actually runs), extracts the top trend for
a given geo/category, and writes the result to a JSON file that your
PHP site can fetch with a plain curl request — no rendering needed on
the PHP side.

Intended to run on a cron job (e.g. every 15-30 minutes) on a free VM
(Oracle Cloud Always Free tier, for example).

Usage:
    python3 scrape_trends.py --geo UG --category 17 --out /var/www/trends/trending.json
    python3 scrape_trends.py --geo KE --category 17 --out /var/www/trends/trending-ke.json
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

TRENDS_URL = "https://trends.google.com/trending"


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--geo", default="UG", help="Geo code, e.g. UG, KE, US")
    p.add_argument(
        "--category", default="17", help="Google Trends category id (17 = Sports)"
    )
    p.add_argument(
        "--out", default="trending.json", help="Path to write the JSON output"
    )
    p.add_argument(
        "--headful", action="store_true", help="Run with a visible browser (debugging)"
    )
    return p.parse_args()


def build_url(geo: str, category: str) -> str:
    return (
        f"{TRENDS_URL}?geo={geo}&category={category}&sort=search-volume&status=active"
    )


def scrape(geo: str, category: str, headful: bool = False) -> dict:
    url = build_url(geo, category)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headful)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = context.new_page()
        page.goto(url, wait_until="networkidle", timeout=45000)

        # The trend table renders as a series of rows; wait for at least
        # one row of trend data to be present before reading anything.
        try:
            page.wait_for_selector("[class*='mZ3RIc'], table tbody tr", timeout=20000)
        except PlaywrightTimeoutError:
            # Fall back to a fixed wait if selectors change on Google's end —
            # brittle, but better than failing outright.
            page.wait_for_timeout(5000)

        # Give the SPA a moment to finish painting rows after the network
        # goes idle (React can lag slightly behind networkidle).
        page.wait_for_timeout(1500)

        rows = extract_rows(page)

        browser.close()

    if not rows:
        raise RuntimeError(
            "No trend rows found — Google's page structure may have changed"
        )

    # Filter for football/sports-related trends only
    football_keywords = [
        "football",
        "soccer",
        "premier league",
        "champions league",
        "la liga",
        "serie a",
        "bundesliga",
        "ligue 1",
        "world cup",
        "afcon",
        "epl",
        "betting",
        "odds",
        "match",
        "derby",
        "qualifier",
        "transfer",
        "real madrid",
        "barcelona",
        "manchester",
        "liverpool",
        "chelsea",
        "arsenal",
        "tottenham",
        "bayern",
        "psg",
        "juventus",
        "milan",
        "napoli",
        "dortmund",
        "ajax",
        "celtic",
        "rangers",
        "al ahly",
        "kaizer chiefs",
        "tusker",
        "gor mahia",
        "afc leopards",
        "simba",
        "yanga",
        "brighton",
        "villa",
        "west ham",
        "newcastle",
        "wolves",
        "fulham",
        "bournemouth",
        "brentford",
        "everton",
        "nations league",
        "europa league",
        "conference league",
        "copa",
        "fa cup",
        "playoff",
        "relegation",
        "promotion",
        "scorer",
        "hat trick",
        "brace",
    ]
    non_sports = [
        "kuccps",
        "university",
        "school",
        "exam",
        "results",
        "scholarship",
        "visa",
        "passport",
        "weather",
        "recipe",
        "movie",
        "song",
        "crypto",
        "bitcoin",
        "election",
        "salary",
        "loan",
        "pregnancy",
        "weight",
    ]

    def is_football(title):
        t = title.lower()
        for kw in non_sports:
            if kw in t:
                return False
        for kw in football_keywords:
            if kw in t:
                return True
        return False

    filtered = [r for r in rows if is_football(r["title"])]

    if not filtered:
        # No football trend found — return error so PHP side knows
        raise RuntimeError(
            f"No football trends found for {geo} after scanning {len(rows)} items"
        )

    top = filtered[0]

    return {
        "geo": geo,
        "category": category,
        "url": url,
        "top_trend": top["title"],
        "top_trend_volume": top.get("volume"),
        "all_trends": filtered[:25],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def extract_rows(page) -> list:
    """
    Pull trend titles (and search-volume text where available) out of the
    rendered page. Google's class names are obfuscated/change over time,
    so this reads from the accessible text content of each row rather
    than relying on exact CSS classes, and is intentionally permissive.
    """
    rows = []

    # Each trend row contains a title plus a "<N>+ searches" volume string.
    # We grab all table row text and parse it with a regex, which is more
    # resilient to Google's shifting class names than exact selectors.
    row_texts = page.eval_on_selector_all(
        "table tbody tr",
        "els => els.map(e => e.innerText)",
    )

    for text in row_texts:
        if not text or not text.strip():
            continue
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if not lines:
            continue

        title = lines[0]

        volume_match = re.search(r"([\d,]+K?\+?)\s*searches", text, re.IGNORECASE)
        volume = volume_match.group(1) if volume_match else None

        # Skip header/garbage rows (Google sometimes renders an empty
        # header row with no real title text)
        if len(title) < 2 or title.lower() in ("search", "trends"):
            continue

        rows.append({"title": title, "volume": volume})

    return rows


def main():
    args = parse_args()

    try:
        data = scrape(args.geo, args.category, headful=args.headful)
    except Exception as e:
        # Write an error marker rather than crashing silently, so your PHP
        # side can detect a stale/failed scrape and fall back gracefully.
        error_payload = {
            "error": str(e),
            "geo": args.geo,
            "category": args.category,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(args.out, "w") as f:
            json.dump(error_payload, f, indent=2)
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    with open(args.out, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"OK: top trend for {args.geo} = {data['top_trend']!r} -> {args.out}")


if __name__ == "__main__":
    main()
