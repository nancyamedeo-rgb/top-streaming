#!/usr/bin/env python3
"""
Scrapes FlixPatrol's daily Top 10 streaming charts (across Netflix, HBO Max,
Disney+, Amazon Prime, Apple TV+) and writes them to data/streaming.json for
the Dakboard widget to consume via raw.githubusercontent.com.

No official free API exists for cross-platform streaming charts, so this
reads FlixPatrol's public daily Top 10 page — a long-running, publicly
documented aggregator (flixpatrol.com) whose data refreshes once a day.
This script runs once a day to match, rather than hammering the page.

Posters come from TMDb (The Movie Database), same as the box office widget
— free for non-commercial use, optional (skipped gracefully with no key).

Dependencies: requests, beautifulsoup4 (installed by the GitHub Action).
"""

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://flixpatrol.com/top10/streaming/world/{date}/"
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "streaming.json")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

# Platforms to include, in display order. Keys match the "TOP Movies/TV
# Shows on <Platform>" section headers FlixPatrol uses on the combined page.
PLATFORMS = ["Netflix", "HBO Max", "Disney+", "Amazon Prime", "Apple TV"]
MAX_PER_SECTION = 5  # keep widget focused: top 5 per platform/category, not all 10

TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
TMDB_SEARCH_MOVIE_URL = "https://api.themoviedb.org/3/search/movie"
TMDB_SEARCH_TV_URL = "https://api.themoviedb.org/3/search/tv"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w185"


def fetch_poster_url(title, is_tv):
    """Look up a title's poster on TMDb. Returns a full image URL, or None
    if not found / lookup fails / no API key. Never raises."""
    if not TMDB_API_KEY:
        return None
    url = TMDB_SEARCH_TV_URL if is_tv else TMDB_SEARCH_MOVIE_URL
    try:
        resp = requests.get(
            url, params={"api_key": TMDB_API_KEY, "query": title}, timeout=15
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return None
        poster_path = results[0].get("poster_path")
        return f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else None
    except Exception as e:
        print(f"  poster lookup failed for '{title}': {e}", file=sys.stderr)
        return None


def get(url):
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def find_latest_available_date():
    """FlixPatrol's combined page for 'today' is sometimes thin/incomplete
    until late in the day. Try today, then yesterday as a fallback, picking
    whichever returns a page with real section content."""
    today = datetime.now(timezone.utc).date()
    for days_back in (0, 1, 2):
        candidate = today - timedelta(days=days_back)
        date_str = candidate.isoformat()
        url = BASE_URL.format(date=date_str)
        try:
            html = get(url)
            soup = BeautifulSoup(html, "html.parser")
            if soup.find_all("table"):
                return url, date_str, html
        except Exception as e:
            print(f"  fetch failed for {date_str}: {e}", file=sys.stderr)
            continue
    raise RuntimeError("Could not find a usable FlixPatrol chart page in the last 3 days.")


def parse_section(heading_text, table, category, platform):
    """Parse one 'TOP Movies/TV Shows on <Platform>' table into entries."""
    entries = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        rank_text = cells[0].get_text(strip=True).rstrip(".")
        if not rank_text.isdigit():
            continue
        link = cells[1].find("a")
        if not link:
            continue
        # FlixPatrol repeats the title text twice in the link (visual +
        # accessible label) - take the longer/cleaner of the two halves.
        raw_text = link.get_text(strip=True)
        half = len(raw_text) // 2
        title = raw_text[:half].strip() if raw_text[:half] == raw_text[half:].strip() else raw_text

        points_text = cells[2].get_text(strip=True) if len(cells) > 2 else ""
        points = int(re.sub(r"[^\d]", "", points_text)) if points_text else None

        entries.append({
            "rank": int(rank_text),
            "title": title,
            "platform": platform,
            "category": category,  # "movie" or "tv"
            "points": points,
        })
        if len(entries) >= MAX_PER_SECTION:
            break
    return entries


def parse_streaming_chart(html):
    soup = BeautifulSoup(html, "html.parser")
    all_entries = []

    # FlixPatrol's combined page has h2 headings like
    # "TOP Movies on Netflix on June 20, 2026" followed by a table.
    headings = soup.find_all("h2")
    for h in headings:
        text = h.get_text(strip=True)
        m = re.match(r"TOP (Movies|TV Shows) on (.+?) on ", text)
        if not m:
            continue
        category_raw, platform = m.group(1), m.group(2).strip()
        if platform not in PLATFORMS:
            continue
        category = "tv" if category_raw == "TV Shows" else "movie"

        table = h.find_next("table")
        if not table:
            continue
        entries = parse_section(text, table, category, platform)
        all_entries.extend(entries)

    return all_entries


def main():
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)

    previous = None
    if os.path.exists(OUTPUT_PATH):
        try:
            with open(OUTPUT_PATH, "r") as f:
                previous = json.load(f)
        except (json.JSONDecodeError, OSError):
            previous = None

    try:
        source_url, date_str, html = find_latest_available_date()
        entries = parse_streaming_chart(html)
        if not entries:
            raise RuntimeError("Parsed 0 entries from FlixPatrol page — markup may have changed.")

        if TMDB_API_KEY:
            print(f"Looking up posters for {len(entries)} titles via TMDb...")
            for e in entries:
                e["poster"] = fetch_poster_url(e["title"], is_tv=(e["category"] == "tv"))
        else:
            print("TMDB_API_KEY not set — skipping poster lookup (widget will show fallback icons).")
            for e in entries:
                e["poster"] = None

        output = {
            "chartDate": date_str,
            "entries": entries,
            "platforms": PLATFORMS,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "source": "flixpatrol.com",
            "posterSource": "themoviedb.org" if TMDB_API_KEY else None,
            "status": "ok",
        }
        print(f"Fetched {len(entries)} entries across {len(PLATFORMS)} platforms for {date_str}")

    except Exception as e:
        print(f"WARNING: fetch failed: {e}", file=sys.stderr)
        if previous:
            output = previous
            output["status"] = "stale"
            output["lastError"] = str(e)
            output["lastErrorAt"] = datetime.now(timezone.utc).isoformat()
            print("Falling back to previous cached data.")
        else:
            output = {
                "chartDate": None,
                "entries": [],
                "platforms": PLATFORMS,
                "updatedAt": datetime.now(timezone.utc).isoformat(),
                "source": "flixpatrol.com",
                "status": "error",
                "lastError": str(e),
            }
            with open(OUTPUT_PATH, "w") as f:
                json.dump(output, f, indent=2)
            sys.exit(1)

    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
