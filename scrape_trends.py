#!/usr/bin/env python3
"""
scrape_trends.py

Render Google Trends Trending Now for a selected geography/category and write
an ordered JSON feed for the blog generator. Category 17 is Google Trends'
Sports category, so the scraper trusts that category instead of applying a
second, brittle football-keyword filter.

The scraper preserves Google's displayed approximate volume label when it is
available, for example ``2K+``. It also stores a parsed numeric approximation,
the extraction source, and the original row rank. Google Trends values are
approximate search-interest indicators, not exact search counts.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from playwright.sync_api import Playwright, TimeoutError as PlaywrightTimeoutError, sync_playwright

TRENDS_URL = "https://trends.google.com/trending"
ROW_SELECTOR = "table tbody tr, [role='row']"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--geo", default="UG", help="Geo code, e.g. UG, KE, US")
    parser.add_argument(
        "--category",
        default="17",
        help="Google Trends category id; 17 is Sports",
    )
    parser.add_argument(
        "--hours",
        default="48",
        help="Trending Now lookback window in hours",
    )
    parser.add_argument(
        "--out",
        default="trending.json",
        help="Path to write the JSON output",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Run with a visible browser for debugging",
    )
    return parser.parse_args()


def build_url(geo: str, category: str, hours: str = "48") -> str:
    return (
        f"{TRENDS_URL}?geo={geo}&category={category}"
        f"&hours={hours}&sort=search-volume"
    )


def normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def parse_volume_numeric(raw: Optional[str]) -> Optional[int]:
    """Convert a displayed value such as 2K+ or 1.5M into an approximation."""
    if not raw:
        return None
    text = normalize_space(raw).upper().replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*([KMB])?", text)
    if not match:
        return None
    value = float(match.group(1))
    multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(
        match.group(2), 1
    )
    return int(value * multiplier)


def _clean_volume(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    cleaned = normalize_space(raw).upper().replace(" ", "")
    return cleaned or None


def extract_volume(row: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Extract volume from labels, row text, cells, or attributes in that order."""
    inner_text = normalize_space(row.get("innerText"))
    text_content = normalize_space(row.get("textContent"))
    cells = [normalize_space(v) for v in row.get("cells", []) if normalize_space(v)]
    attributes = [
        normalize_space(v) for v in row.get("attributes", []) if normalize_space(v)
    ]

    labeled_sources = [
        ("label", inner_text),
        ("label", text_content),
        ("attribute", " ".join(attributes)),
        ("cell", " ".join(cells)),
    ]
    # Google has used variants such as "2K+ searches", "2K+ searches in the
    # past 24 hours", and "Search volume: 2K+".
    labeled_patterns = [
        re.compile(
            r"(?i)(\d[\d,.]*(?:\.\d+)?\s*[KMB]?\+?)\s*"
            r"(?:search(?:es)?|search volume|queries?)(?:\b|\s)"
        ),
        re.compile(
            r"(?i)(?:search(?:es)?|search volume|queries?|traffic)"
            r"[^\d]{0,30}(\d[\d,.]*(?:\.\d+)?\s*[KMB]?\+?)"
        ),
    ]
    for source, value in labeled_sources:
        for pattern in labeled_patterns:
            match = pattern.search(value)
            if match:
                return _clean_volume(match.group(1)), source

    # If the volume is rendered as its own cell/aria label without the word
    # "searches", accept a standalone token with a K/M/B suffix or plus sign.
    standalone_pattern = re.compile(
        r"(?i)^(\d[\d,.]*(?:\.\d+)?\s*[KMB]\+?|\d[\d,.]*\+)$"
    )
    for source, values in (("attribute", attributes), ("cell", cells)):
        for value in values:
            match = standalone_pattern.fullmatch(value.strip())
            if match:
                return _clean_volume(match.group(1)), source

    # Last fallback for a row-level token such as "2K+" embedded in a label.
    suffix_pattern = re.compile(r"(?i)(?<![\w.])(\d[\d,.]*(?:\.\d+)?\s*[KMB]\+?)(?![\w.])")
    for source, value in labeled_sources:
        match = suffix_pattern.search(value)
        if match:
            return _clean_volume(match.group(1)), source

    return None, None


def _is_non_title(value: str) -> bool:
    value = normalize_space(value)
    if not value or value.lower() in {"search", "trends", "active"}:
        return True
    if re.fullmatch(r"\d+[.)]?", value):
        return True
    if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", value):
        return True
    if re.fullmatch(r"\d[\d,.]*(?:\s*[KMB]\+?)?\s*(?:searches?)?", value, re.I):
        return True
    return False


def extract_title(row: dict[str, Any]) -> Optional[str]:
    cells = [normalize_space(v) for v in row.get("cells", []) if normalize_space(v)]
    lines = [
        normalize_space(line)
        for line in normalize_space(row.get("innerText")).split(" ")
        if normalize_space(line)
    ]

    # Table cells are preferred because rank/time/volume are usually separate
    # cells. Fall back to the first meaningful line of innerText.
    for candidate in cells:
        if not _is_non_title(candidate) and len(candidate) >= 2:
            return candidate

    raw_lines = [
        normalize_space(line)
        for line in str(row.get("innerText") or "").splitlines()
        if normalize_space(line)
    ]
    for candidate in raw_lines + lines:
        if not _is_non_title(candidate) and len(candidate) >= 2:
            return candidate
    return None


def extract_rows(page) -> list[dict[str, Any]]:
    """Extract unique rendered rows and resiliently preserve volume metadata."""
    rendered_rows = page.eval_on_selector_all(
        ROW_SELECTOR,
        """
        els => {
          const seen = new Set();
          const out = [];
          for (const el of els) {
            if (seen.has(el)) continue;
            seen.add(el);
            const cellNodes = Array.from(el.querySelectorAll('td, th, [role="cell"]'));
            const attrNodes = Array.from(el.querySelectorAll('[aria-label], [title], [data-value], [data-tooltip]'));
            const attrs = [];
            for (const node of attrNodes) {
              for (const name of ['aria-label', 'title', 'data-value', 'data-tooltip']) {
                const value = node.getAttribute(name);
                if (value) attrs.push(value);
              }
            }
            out.push({
              innerText: el.innerText || '',
              textContent: el.textContent || '',
              cells: cellNodes.map(node => node.innerText || node.textContent || ''),
              attributes: attrs,
            });
          }
          return out;
        }
        """,
    )

    rows: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for rendered in rendered_rows:
        title = extract_title(rendered)
        if not title:
            continue
        normalized_title = normalize_space(title)
        title_key = normalized_title.casefold()
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        volume, volume_source = extract_volume(rendered)
        rows.append(
            {
                "rank": len(rows) + 1,
                "title": normalized_title,
                "volume": volume,
                "volume_numeric": parse_volume_numeric(volume),
                "volume_source": volume_source,
                "volume_available": volume is not None,
            }
        )
    return rows


def _open_page(playwright: Playwright, url: str, headful: bool):
    browser = playwright.chromium.launch(headless=not headful)
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="en-US",
    )
    page = context.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PlaywrightTimeoutError:
        pass
    try:
        page.wait_for_selector(ROW_SELECTOR, timeout=20000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(3000)
    return browser, page


def scrape(geo: str, category: str, hours: str = "48", headful: bool = False) -> dict:
    url = build_url(geo, category, hours)
    browser = None
    try:
        with sync_playwright() as playwright:
            browser, page = _open_page(playwright, url, headful)
            rows = extract_rows(page)
            browser.close()
            browser = None
    finally:
        if browser is not None:
            browser.close()

    if not rows:
        raise RuntimeError(
            "No trend rows found — Google Trends may have changed its page structure"
        )

    # Category 17 is already the Google Trends Sports feed. Do not apply a
    # second keyword allowlist/denylist: it discarded valid clubs, leagues,
    # fixtures, and local sports terms such as Yanga.
    top = rows[0]
    volume_count = sum(1 for row in rows if row.get("volume_available"))
    return {
        "geo": geo,
        "category": category,
        "category_trusted": str(category) == "17",
        "hours": hours,
        "url": url,
        "top_trend": top["title"],
        "top_trend_volume": top.get("volume"),
        "top_trend_volume_numeric": top.get("volume_numeric"),
        "volume_available_count": volume_count,
        "volume_status": "available" if volume_count else "unavailable",
        "all_trends": rows[:25],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> None:
    args = parse_args()
    try:
        data = scrape(args.geo, args.category, args.hours, headful=args.headful)
    except Exception as exc:
        error_payload = {
            "error": str(exc),
            "geo": args.geo,
            "category": args.category,
            "hours": args.hours,
            "url": build_url(args.geo, args.category, args.hours),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(args.out, "w", encoding="utf-8") as output:
            json.dump(error_payload, output, indent=2, ensure_ascii=False)
        print(f"ERROR: {exc}", file=sys.stderr)
        return

    with open(args.out, "w", encoding="utf-8") as output:
        json.dump(data, output, indent=2, ensure_ascii=False)

    if data.get("top_trend"):
        print(
            f"OK: top trend for {args.geo} = {data['top_trend']!r} "
            f"volume={data.get('top_trend_volume')!r} -> {args.out}"
        )
    else:
        print(f"WARN: No trend rows for {args.geo} -> {args.out}")


if __name__ == "__main__":
    main()
