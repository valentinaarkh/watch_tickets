#!/usr/bin/env python3
"""
Giselle ticket watcher for opera.koobin.com
--------------------------------------------
Checks one or more performance dates for the ballet "Giselle" at the
Lithuanian National Opera and Ballet Theatre, and sends you a Telegram
message (with a screenshot of the seat map) when it finds at least 2
free seats next to each other in the same row.

WHY A SCREENSHOT TOO: the seat map is rendered client-side and the exact
DOM structure (class names, seat numbering per row) can only be confirmed
by inspecting the live page in your own browser. This script tries to
parse seat elements automatically, but always attaches a screenshot so
you can visually confirm adjacency before you rush to buy.

SETUP
-----
1. pip install playwright python-telegram-bot --break-system-packages
   playwright install chromium

2. Create a Telegram bot:
   - Message @BotFather on Telegram, send /newbot, follow the prompts.
   - Copy the bot token it gives you.
   - Message your new bot once (anything), then visit:
     https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
     and find your numeric "chat":{"id": ...} — that's your CHAT_ID.

3. Fill in TELEGRAM_TOKEN and CHAT_ID below (or set them as env vars).

4. Run once manually to test:
     python3 watch_giselle.py --once

5. Schedule it (every 10-15 min) via cron, e.g.:
     */15 * * * * cd /path/to/opera_bot && /usr/bin/python3 watch_giselle.py --once >> watch.log 2>&1

IMPORTANT: This polls a third-party site. Keep the interval reasonable
(10+ minutes) so you don't hammer their server or risk getting blocked.
This is for personal use to buy tickets you intend to pay for — not for
resale/scalping automation.
"""

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import requests
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# CONFIG — edit these
# ---------------------------------------------------------------------------

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# One entry per performance date you want watched.
# Add/remove URLs as needed — these are the "Tickets" links per date
# from https://www.opera.lt/en/whatss-on/ballet-c7/giselle-e52
EVENTS = [
    {"label": "Giselle - Wed 14 Oct 2026, 18:30", "url": "https://opera.koobin.com/ZIZEL-261014?idioma=EN"},
    {"label": "Giselle - Thu 15 Oct 2026, 18:30", "url": "https://opera.koobin.com/ZIZEL-261015?idioma=EN"},
    {"label": "Giselle - Fri 16 Oct 2026, 18:30", "url": "https://opera.koobin.com/ZIZEL-261016?idioma=EN"},
    {"label": "Giselle - Sat 17 Oct 2026, 18:30", "url": "https://opera.koobin.com/ZIZEL-261017?idioma=EN"},
    {"label": "Giselle - Sun 18 Oct 2026, 18:00", "url": "https://opera.koobin.com/ZIZEL-261018?idioma=EN"},
]

MIN_ADJACENT_SEATS = 2  # how many seats-in-a-row you need
STATE_FILE = Path(__file__).parent / "last_state.json"
SCREENSHOT_DIR = Path(__file__).parent / "screenshots"
SCREENSHOT_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------


def send_telegram_message(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"})
    if resp.ok:
        print("[telegram] message sent OK")
    else:
        print(f"[telegram] message FAILED: HTTP {resp.status_code}", file=sys.stderr)
        print(f"[telegram] response body: {resp.text}", file=sys.stderr)


def send_telegram_photo(photo_path: Path, caption: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    with open(photo_path, "rb") as f:
        resp = requests.post(url, data={"chat_id": CHAT_ID, "caption": caption}, files={"photo": f})
    if not resp.ok:
        print(f"[telegram] photo failed: {resp.status_code} {resp.text}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Seat map scraping
# ---------------------------------------------------------------------------


async def accept_cookies(page):
    for text in ["Accept all", "I agree", "Reject all"]:
        try:
            btn = page.get_by_text(text, exact=False)
            if await btn.count() > 0:
                await btn.first.click(timeout=2000)
                await page.wait_for_timeout(300)
                return
        except Exception:
            pass


def setup_debug_logging(page, label):
    """Print browser console messages and JS errors to help diagnose
    why the seat map isn't rendering (e.g. bot-detection, blocked
    third-party requests, JS exceptions)."""
    page.on("console", lambda msg: print(f"  [console:{label}] {msg.type}: {msg.text}"))
    page.on("pageerror", lambda exc: print(f"  [pageerror:{label}] {exc}"))
    page.on("requestfailed", lambda req: print(f"  [requestfailed:{label}] {req.url} - {req.failure}"))


async def extract_seats(page):
    """
    Try to extract seat elements from the rendered map.
    Koobin seat maps are typically SVG with <g>/<rect>/<circle> nodes per
    seat, carrying data attributes or classes indicating row/seat/status.
    THIS SELECTOR LIST IS A BEST GUESS — inspect the live DOM (F12 ->
    right-click a seat -> Inspect) and adjust `seat_selector` and the
    attribute names below to match what you actually see.
    """
    seat_selector = "svg [data-row], svg [data-seat], svg .asiento, svg .seat"
    seats = []
    try:
        elements = await page.query_selector_all(seat_selector)
    except Exception:
        elements = []

    for el in elements:
        row = await el.get_attribute("data-row") or await el.get_attribute("data-fila")
        seat = await el.get_attribute("data-seat") or await el.get_attribute("data-asiento")
        cls = (await el.get_attribute("class")) or ""
        # Heuristic: koobin commonly uses class names containing
        # "libre" (free) / "ocupado" or "occupied" for taken seats.
        status = "available" if re.search(r"libre|available|free", cls, re.I) else (
            "occupied" if re.search(r"ocupad|occupied|reserved|assigned", cls, re.I) else "unknown"
        )
        if row is not None and seat is not None:
            seats.append({"row": row, "seat": seat, "status": status, "class": cls})
    return seats


def find_adjacent_free_seats(seats, min_run=MIN_ADJACENT_SEATS):
    """Group by row, sort by seat number, find runs of consecutive
    available seats of length >= min_run."""
    by_row = {}
    for s in seats:
        if s["status"] != "available":
            continue
        try:
            seat_num = int(re.sub(r"\D", "", s["seat"]))
        except (ValueError, TypeError):
            continue
        by_row.setdefault(s["row"], []).append(seat_num)

    findings = []
    for row, nums in by_row.items():
        nums = sorted(set(nums))
        run = [nums[0]] if nums else []
        for n in nums[1:]:
            if n == run[-1] + 1:
                run.append(n)
            else:
                if len(run) >= min_run:
                    findings.append((row, run[:]))
                run = [n]
        if len(run) >= min_run:
            findings.append((row, run))
    return findings


async def check_event(browser, event, debug=False):
    page = await browser.new_page(
        viewport={"width": 1400, "height": 1000},
        user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0.0.0 Safari/537.36"),
    )
    if debug:
        setup_debug_logging(page, event["label"])

    result = {"label": event["label"], "url": event["url"], "findings": [], "screenshot": None, "seat_count": 0}
    safe_name = re.sub(r'[^A-Za-z0-9]+', '_', event['label'])
    try:
        await page.goto(event["url"], wait_until="networkidle", timeout=30000)
        await accept_cookies(page)

        # Give the seat map JS time to render — wait longer and scroll
        # to trigger any lazy-loading.
        await page.wait_for_timeout(2000)
        try:
            await page.mouse.wheel(0, 400)
        except Exception:
            pass
        await page.wait_for_timeout(3000)

        # ALWAYS take a full-page screenshot first — this is the ground
        # truth for debugging, independent of whether seat parsing works.
        fullpage_path = SCREENSHOT_DIR / f"{safe_name}_fullpage.png"
        await page.screenshot(path=str(fullpage_path), full_page=True)

        seats = await extract_seats(page)
        result["seat_count"] = len(seats)
        result["findings"] = find_adjacent_free_seats(seats)

        # Try to screenshot just the largest <svg> on the page (likely
        # the seat map, not a small logo/icon svg). Fall back to the
        # full-page shot if no svg is found.
        svg_handles = await page.query_selector_all("svg")
        shot_path = fullpage_path
        if svg_handles:
            best, best_area = None, 0
            for h in svg_handles:
                box = await h.bounding_box()
                if box and box["width"] * box["height"] > best_area:
                    best, best_area = h, box["width"] * box["height"]
            if best and best_area > 5000:  # ignore tiny icon svgs
                shot_path = SCREENSHOT_DIR / f"{safe_name}_map.png"
                await best.screenshot(path=str(shot_path))

        if debug:
            print(f"  [{event['label']}] found {len(svg_handles)} <svg> elements on page, "
                  f"{result['seat_count']} seats parsed")

        result["screenshot"] = shot_path
    except Exception as e:
        print(f"[error] {event['label']}: {e}", file=sys.stderr)
    finally:
        await page.close()
    return result


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


async def run_once(debug=False):
    state = load_state()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not debug)
        for event in EVENTS:
            result = await check_event(browser, event, debug=debug)
            key = event["url"]
            found_count = len(result["findings"])
            prev_count = state.get(key, {}).get("found_count", 0)

            print(f"[{datetime.now().isoformat(timespec='seconds')}] {event['label']}: "
                  f"{result['seat_count']} seats parsed, {found_count} adjacent-pair groups found")

            if found_count > 0:
                # Only ping if this is new (avoid spamming every 15 min for the same seats)
                if found_count != prev_count:
                    lines = [f"🎭 <b>{event['label']}</b>", "Possible adjacent seats found:"]
                    for row, run in result["findings"][:10]:
                        lines.append(f"  Row {row}: seats {run[0]}-{run[-1]} ({len(run)} in a row)")
                    lines.append(event["url"])
                    send_telegram_message("\n".join(lines))
                    if result["screenshot"]:
                        send_telegram_photo(result["screenshot"], f"Seat map: {event['label']}")
                else:
                    print("  (same as last check, not re-notifying)")

            state[key] = {"found_count": found_count, "checked_at": datetime.now().isoformat()}
        await browser.close()
    save_state(state)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run a single check and exit")
    parser.add_argument("--interval", type=int, default=900, help="Seconds between checks if not --once (default 900 = 15 min)")
    parser.add_argument("--debug", action="store_true",
                         help="Run with a visible browser window + console/network logging, "
                              "to diagnose why the seat map isn't rendering")
    parser.add_argument("--test-telegram", action="store_true",
                         help="Send a test message immediately, bypassing all scraping logic, "
                              "to verify your TELEGRAM_TOKEN / CHAT_ID are correct")
    args = parser.parse_args()

    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("!! Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID as environment variables before running "
              "(in GitHub Actions, these come from repo Secrets).", file=sys.stderr)
        sys.exit(1)

    if args.test_telegram:
        print(f"Sending test message to chat_id={CHAT_ID} using token ending in ...{TELEGRAM_TOKEN[-6:]}")
        send_telegram_message("✅ Test message from watch_giselle.py — your bot is wired up correctly!")
        return

    if args.once or args.debug:
        asyncio.run(run_once(debug=args.debug))
    else:
        async def loop():
            while True:
                await run_once()
                await asyncio.sleep(args.interval)
        asyncio.run(loop())


if __name__ == "__main__":
    main()