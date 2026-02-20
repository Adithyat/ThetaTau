#!/usr/bin/env python3
"""
Palisades Tahoe Parking Availability Checker

Periodically checks parking availability at Palisades Tahoe (Palisades or Alpine base)
and alerts when spots open up for your target date(s).

Usage:
    python check_parking.py --date 2026-02-21 --location palisades --interval 60
    python check_parking.py --date 2026-02-21 --location alpine --interval 30
    python check_parking.py --date 2026-02-21 2026-02-22 --location palisades
    python check_parking.py --date 2026-03-01 --location both --interval 120

Requirements:
    pip install playwright
    playwright install chromium
"""

import argparse
import json
import sys
import time
import subprocess
import platform
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

SITE_URL = "https://reservenski.parkpalisadestahoe.com/select-parking"
GRAPHQL_URL = "https://platform.honkmobile.com/graphql"
RSVP_PORTAL_ID = "9JU5"

LOCATIONS = {
    "palisades": {
        "label": "PALISADES",
        "inventory_id": "G6HG",
    },
    "alpine": {
        "label": "ALPINE",
        "inventory_id": "eauZ",
    },
}


def send_notification(title, message):
    """Best-effort desktop/system notification."""
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run([
                "osascript", "-e",
                f'display notification "{message}" with title "{title}" sound name "Glass"'
            ], timeout=5)
        elif system == "Linux":
            subprocess.run(["notify-send", title, message], timeout=5)
        elif system == "Windows":
            ps = (
                f'[System.Reflection.Assembly]::LoadWithPartialName("System.Windows.Forms") | Out-Null;'
                f'$balloon = New-Object System.Windows.Forms.NotifyIcon;'
                f'$balloon.Icon = [System.Drawing.SystemIcons]::Information;'
                f'$balloon.BalloonTipTitle = "{title}";'
                f'$balloon.BalloonTipText = "{message}";'
                f'$balloon.Visible = $true;'
                f'$balloon.ShowBalloonTip(10000)'
            )
            subprocess.run(["powershell", "-Command", ps], timeout=5)
    except Exception:
        pass


def play_alert_sound():
    """Best-effort audible alert."""
    try:
        if platform.system() == "Darwin":
            subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], timeout=5)
        else:
            print("\a", end="", flush=True)
    except Exception:
        print("\a", end="", flush=True)


def _month_day_range(year, month):
    """Return (start_day_of_year, end_day_of_year) for a given month."""
    first = datetime(year, month, 1)
    if month == 12:
        last = datetime(year, 12, 31)
    else:
        last = datetime(year, month + 1, 1) - timedelta(days=1)
    return first.timetuple().tm_yday, last.timetuple().tm_yday


def _cart_start_time(year, month):
    """Build cartStartTime ISO string for the first of the month at 6 AM PST."""
    return f"{year}-{month:02d}-01T06:00:00-08:00"


def fetch_availability(pw_instance, location_key, months_needed):
    """
    Launches a headless browser, loads the reservation page, and for each
    month needed, uses route interception to patch the GraphQL request
    variables before the app sends them. Clicking a zone triggers the
    app's own Apollo client to make the API call (passing Cloudflare).

    Returns a merged availability dict keyed by date ISO strings, or None.
    """
    loc = LOCATIONS[location_key]
    merged_avail = {}

    browser = pw_instance.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )
    try:
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 390, "height": 844},
            is_mobile=True,
        )

        for year, month in sorted(months_needed):
            page = context.new_page()
            captured = {}

            start_day, end_day = _month_day_range(year, month)
            target_inventory_id = loc["inventory_id"]
            target_cart_start = _cart_start_time(year, month)

            def make_route_handler(inv_id, cart_start, sd, ed, yr):
                def handler(route, request):
                    if request.method == "POST" and request.post_data:
                        try:
                            body = json.loads(request.post_data)
                            if body.get("operationName") == "PublicParkingAvailability":
                                body["variables"]["id"] = inv_id
                                body["variables"]["cartStartTime"] = cart_start
                                body["variables"]["startDay"] = sd
                                body["variables"]["endDay"] = ed
                                body["variables"]["year"] = yr
                                route.continue_(post_data=json.dumps(body))
                                return
                        except (json.JSONDecodeError, KeyError):
                            pass
                    route.continue_()
                return handler

            def make_response_handler(store):
                def handler(response):
                    if "graphql" not in response.url:
                        return
                    try:
                        body = response.json()
                        raw = (body.get("data") or {}).get("publicParkingAvailability")
                        if raw and isinstance(raw, dict):
                            store.update(raw)
                    except Exception:
                        pass
                return handler

            page.route(
                "**/graphql*",
                make_route_handler(
                    target_inventory_id, target_cart_start, start_day, end_day, year
                ),
            )
            page.on("response", make_response_handler(captured))

            page.goto(SITE_URL, timeout=30000)
            page.wait_for_timeout(3000)

            zone_card = page.locator(
                ".SelectZone_card__ssmqG", has_text=loc["label"]
            )
            if zone_card.count() > 0:
                zone_card.first.click()
                page.wait_for_timeout(4000)
            else:
                print(f"  Warning: Could not find {loc['label']} zone card")

            merged_avail.update(captured)
            page.close()

        context.close()
    finally:
        browser.close()

    return merged_avail if merged_avail else None


def parse_date_key(date_str):
    """Extract just the YYYY-MM-DD from an ISO datetime key."""
    return date_str.split("T")[0]


def check_date(avail, target_date):
    """Check availability for a specific date. Returns a status dict."""
    matching_key = None
    for key in avail:
        if parse_date_key(key) == target_date:
            matching_key = key
            break

    if matching_key is None:
        return {
            "date": target_date,
            "found": False,
            "message": "Date not in availability data (may be outside reservation season)",
        }

    day_data = avail[matching_key]
    status = day_data.get("status", {})

    result = {
        "date": target_date,
        "found": True,
        "sold_out": status.get("sold_out", False),
        "unavailable": status.get("unavailable", False),
        "reservation_not_needed": status.get("reservation_not_needed", False),
        "rates": [],
    }

    for key, val in day_data.items():
        if key == "status":
            continue
        if isinstance(val, dict) and "available" in val:
            result["rates"].append({
                "id": val.get("hashid", key),
                "description": val.get("description", "Unknown"),
                "price": val.get("price", "?"),
                "available": val.get("available", False),
                "notification": val.get("notification", False),
            })

    return result


def format_result(result, location):
    """Format a single date's result for console display."""
    lines = []
    date = result["date"]
    loc_label = location.upper()

    if not result["found"]:
        lines.append(f"  {loc_label} | {date}: {result['message']}")
        return "\n".join(lines)

    if result["reservation_not_needed"]:
        lines.append(f"  {loc_label} | {date}: No reservation needed (open parking)")
        return "\n".join(lines)

    if result["unavailable"]:
        lines.append(f"  {loc_label} | {date}: UNAVAILABLE")
        return "\n".join(lines)

    if result["sold_out"] and not any(r["available"] for r in result["rates"]):
        lines.append(f"  {loc_label} | {date}: SOLD OUT")
        return "\n".join(lines)

    for rate in result["rates"]:
        tag = "AVAILABLE" if rate["available"] else "sold out"
        price_str = f"${rate['price']}" if rate["price"] != "0.0" else "FREE"
        lines.append(
            f"  {loc_label} | {date}: [{tag}] {rate['description']} ({price_str})"
        )

    if not result["rates"]:
        lines.append(f"  {loc_label} | {date}: No rate info available")

    return "\n".join(lines)


def run_check(pw_instance, locations, target_dates):
    """Run one check cycle. Returns True if any target date has availability."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}")
    print(f"  Checking at {now}")
    print(f"{'='*60}")

    months_needed = set()
    for d in target_dates:
        dt = datetime.strptime(d, "%Y-%m-%d")
        months_needed.add((dt.year, dt.month))

    any_available = False

    for loc_key in locations:
        avail = fetch_availability(pw_instance, loc_key, months_needed)
        if avail is None:
            print(f"  {loc_key.upper()}: Failed to fetch availability data")
            continue

        for target_date in target_dates:
            result = check_date(avail, target_date)
            print(format_result(result, loc_key))

            if result.get("found") and not result.get("unavailable"):
                has_spots = any(r["available"] for r in result.get("rates", []))
                if has_spots:
                    any_available = True

    return any_available


def main():
    parser = argparse.ArgumentParser(
        description="Check Palisades Tahoe parking availability",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --date 2026-02-21 --location palisades
  %(prog)s --date 2026-02-21 --location alpine --interval 60
  %(prog)s --date 2026-02-21 2026-02-22 --location both
  %(prog)s --date 2026-03-01 --location palisades --interval 30 --alert
        """,
    )
    parser.add_argument(
        "--date", "-d",
        nargs="+",
        required=True,
        help="Target date(s) to check in YYYY-MM-DD format",
    )
    parser.add_argument(
        "--location", "-l",
        choices=["palisades", "alpine", "both"],
        default="palisades",
        help="Parking location to check (default: palisades)",
    )
    parser.add_argument(
        "--interval", "-i",
        type=int,
        default=0,
        help="Check interval in seconds. 0 = single check (default: 0)",
    )
    parser.add_argument(
        "--alert", "-a",
        action="store_true",
        help="Play sound and send desktop notification when spots are available",
    )
    parser.add_argument(
        "--stop-on-found", "-s",
        action="store_true",
        help="Stop checking once availability is found",
    )

    args = parser.parse_args()

    for d in args.date:
        try:
            datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            print(f"Error: Invalid date format '{d}'. Use YYYY-MM-DD.", file=sys.stderr)
            sys.exit(1)

    locations = list(LOCATIONS.keys()) if args.location == "both" else [args.location]

    print(f"Palisades Tahoe Parking Checker")
    print(f"  Location(s): {', '.join(loc.upper() for loc in locations)}")
    print(f"  Date(s):     {', '.join(args.date)}")
    if args.interval > 0:
        print(f"  Interval:    every {args.interval}s")
        print(f"  Alert:       {'ON' if args.alert else 'OFF'}")
        print(f"  Stop on hit: {'YES' if args.stop_on_found else 'NO'}")
    print(f"  Reservation: {SITE_URL}")

    with sync_playwright() as pw:
        while True:
            try:
                found = run_check(pw, locations, args.date)

                if found and args.alert:
                    avail_msg = f"Parking available at Palisades Tahoe for {', '.join(args.date)}!"
                    print(f"\n  >>> {avail_msg}")
                    send_notification("Parking Available!", avail_msg)
                    play_alert_sound()

                if found and args.stop_on_found:
                    print("\n  Availability found. Stopping.")
                    break

                if args.interval <= 0:
                    break

                print(f"\n  Next check in {args.interval}s... (Ctrl+C to quit)")
                time.sleep(args.interval)

            except KeyboardInterrupt:
                print("\n\nStopped by user.")
                break
            except Exception as e:
                print(f"\n  Error during check: {e}")
                if args.interval <= 0:
                    sys.exit(1)
                print(f"  Retrying in {args.interval}s...")
                time.sleep(args.interval)


if __name__ == "__main__":
    main()
