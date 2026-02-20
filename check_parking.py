#!/usr/bin/env python3
"""
Palisades Tahoe Parking Availability Checker

Periodically checks parking availability at Palisades Tahoe (Palisades or Alpine base)
and alerts when spots open up for your target date(s).

Notification methods: email (SMTP), push (ntfy.sh), SMS (email-to-SMS gateway),
and local desktop alerts.

Usage:
    python check_parking.py --date 2026-02-21 --location palisades
    python check_parking.py --date 2026-02-21 --location alpine --interval 60
    python check_parking.py --date 2026-02-21 2026-02-22 --location both --notify ntfy

Requirements:
    pip install playwright requests
    playwright install chromium
"""

import argparse
import json
import os
import smtplib
import sys
import time
import subprocess
import platform
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
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

# Common US carrier email-to-SMS gateways
SMS_GATEWAYS = {
    "att":      "@txt.att.net",
    "tmobile":  "@tmomail.net",
    "verizon":  "@vtext.com",
    "sprint":   "@messaging.sprintpcs.com",
    "uscellular": "@email.uscc.net",
    "cricket":  "@sms.cricketwireless.net",
    "boost":    "@sms.myboostmobile.com",
    "metro":    "@mymetropcs.com",
    "mint":     "@tmomail.net",
    "google_fi": "@msg.fi.google.com",
    "xfinity":  "@vtext.com",
    "visible":  "@vtext.com",
}

# ---------------------------------------------------------------------------
# Notification backends
# ---------------------------------------------------------------------------

def notify_desktop(title, message):
    """Best-effort local desktop notification."""
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run([
                "osascript", "-e",
                f'display notification "{message}" with title "{title}" sound name "Glass"'
            ], timeout=5)
            subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], timeout=5)
        elif system == "Linux":
            subprocess.run(["notify-send", title, message], timeout=5)
        elif system == "Windows":
            ps = (
                f'[System.Reflection.Assembly]::LoadWithPartialName("System.Windows.Forms") | Out-Null;'
                f'$b = New-Object System.Windows.Forms.NotifyIcon;'
                f'$b.Icon = [System.Drawing.SystemIcons]::Information;'
                f'$b.BalloonTipTitle = "{title}";'
                f'$b.BalloonTipText = "{message}";'
                f'$b.Visible = $true;'
                f'$b.ShowBalloonTip(10000)'
            )
            subprocess.run(["powershell", "-Command", ps], timeout=5)
    except Exception:
        print("\a", end="", flush=True)


def notify_ntfy(title, message, topic=None, server=None):
    """
    Send push notification via ntfy.sh (free, no account needed).
    Install the ntfy app on iOS/Android and subscribe to your topic.
    """
    topic = topic or os.environ.get("NTFY_TOPIC")
    server = server or os.environ.get("NTFY_SERVER", "https://ntfy.sh")
    if not topic:
        print("  [ntfy] Skipped: no topic set (use --ntfy-topic or NTFY_TOPIC env var)")
        return False
    try:
        resp = requests.post(
            f"{server}/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": "high",
                "Tags": "parking_space,ski",
            },
            timeout=10,
        )
        if resp.ok:
            print(f"  [ntfy] Push sent to {topic}")
            return True
        else:
            print(f"  [ntfy] Failed ({resp.status_code}): {resp.text[:100]}")
            return False
    except Exception as e:
        print(f"  [ntfy] Error: {e}")
        return False


def notify_email(title, message, smtp_to=None, smtp_from=None,
                 smtp_host=None, smtp_port=None, smtp_user=None, smtp_pass=None):
    """Send notification via email (SMTP)."""
    to_addr = smtp_to or os.environ.get("SMTP_TO")
    from_addr = smtp_from or os.environ.get("SMTP_FROM")
    host = smtp_host or os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(smtp_port or os.environ.get("SMTP_PORT", "587"))
    user = smtp_user or os.environ.get("SMTP_USER")
    password = smtp_pass or os.environ.get("SMTP_PASS")

    if not all([to_addr, from_addr, user, password]):
        print("  [email] Skipped: missing SMTP config (see README)")
        return False

    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = title
    msg.attach(MIMEText(message, "plain"))

    try:
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.starttls()
            server.login(user, password)
            server.sendmail(from_addr, [to_addr], msg.as_string())
        print(f"  [email] Sent to {to_addr}")
        return True
    except Exception as e:
        print(f"  [email] Error: {e}")
        return False


def notify_sms(title, message, phone=None, carrier=None,
               smtp_from=None, smtp_host=None, smtp_port=None,
               smtp_user=None, smtp_pass=None):
    """Send SMS via carrier email-to-SMS gateway (uses SMTP)."""
    phone = phone or os.environ.get("SMS_PHONE")
    carrier = (carrier or os.environ.get("SMS_CARRIER", "")).lower()

    if not phone or not carrier:
        print("  [sms] Skipped: missing phone/carrier (see README)")
        return False

    gateway = SMS_GATEWAYS.get(carrier)
    if not gateway:
        print(f"  [sms] Unknown carrier '{carrier}'. Supported: {', '.join(SMS_GATEWAYS.keys())}")
        return False

    digits = "".join(c for c in phone if c.isdigit())
    sms_addr = f"{digits}{gateway}"

    short_msg = message[:140]
    return notify_email(
        title, short_msg,
        smtp_to=sms_addr,
        smtp_from=smtp_from,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_pass=smtp_pass,
    )


def send_alerts(title, message, methods, args):
    """Dispatch notifications to all requested methods."""
    for method in methods:
        if method == "desktop":
            notify_desktop(title, message)
        elif method == "ntfy":
            notify_ntfy(title, message, topic=args.ntfy_topic)
        elif method == "email":
            notify_email(title, message)
        elif method == "sms":
            notify_sms(title, message)

# ---------------------------------------------------------------------------
# Availability fetching
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Result checking / formatting
# ---------------------------------------------------------------------------

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


def build_notification_message(results_by_loc):
    """Build a combined notification message from all available results."""
    lines = []
    for loc, results in results_by_loc.items():
        for r in results:
            avail_rates = [rate for rate in r.get("rates", []) if rate["available"]]
            if avail_rates:
                for rate in avail_rates:
                    price = f"${rate['price']}" if rate["price"] != "0.0" else "FREE"
                    lines.append(f"{loc.upper()} {r['date']}: {rate['description']} ({price})")
    return "\n".join(lines) if lines else None


def build_status_summary(results_by_loc, target_dates):
    """Build a compact status summary for healthcheck messages."""
    lines = []
    for loc, results in results_by_loc.items():
        for r in results:
            date = r["date"]
            if not r.get("found"):
                lines.append(f"{loc.upper()} {date}: not found")
            elif r.get("reservation_not_needed"):
                lines.append(f"{loc.upper()} {date}: no reservation needed")
            elif r.get("sold_out") and not any(rt["available"] for rt in r.get("rates", [])):
                lines.append(f"{loc.upper()} {date}: SOLD OUT")
            elif r.get("unavailable"):
                lines.append(f"{loc.upper()} {date}: unavailable")
            else:
                avail = [rt for rt in r.get("rates", []) if rt["available"]]
                sold = [rt for rt in r.get("rates", []) if not rt["available"]]
                lines.append(f"{loc.upper()} {date}: {len(avail)} available, {len(sold)} sold out")
    return "\n".join(lines) if lines else "No data"

# ---------------------------------------------------------------------------
# Main check loop
# ---------------------------------------------------------------------------

def run_check(pw_instance, locations, target_dates, notify_methods, args):
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
    results_by_loc = {}

    for loc_key in locations:
        avail = fetch_availability(pw_instance, loc_key, months_needed)
        if avail is None:
            print(f"  {loc_key.upper()}: Failed to fetch availability data")
            continue

        loc_results = []
        for target_date in target_dates:
            result = check_date(avail, target_date)
            print(format_result(result, loc_key))
            loc_results.append(result)

            if result.get("found") and not result.get("unavailable"):
                has_spots = any(r["available"] for r in result.get("rates", []))
                if has_spots:
                    any_available = True

        results_by_loc[loc_key] = loc_results

    if any_available and notify_methods:
        msg = build_notification_message(results_by_loc)
        if msg:
            send_alerts("Palisades Parking Available!", msg, notify_methods, args)

    if getattr(args, "healthcheck", False) and notify_methods:
        summary = build_status_summary(results_by_loc, target_dates)
        send_alerts(
            "Parking Checker Heartbeat",
            f"Checker is running as of {now}\n\n{summary}",
            notify_methods,
            args,
        )

    return any_available


def main():
    parser = argparse.ArgumentParser(
        description="Check Palisades Tahoe parking availability",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --date 2026-02-21 --location palisades
  %(prog)s --date 2026-02-21 --location alpine --interval 60
  %(prog)s --date 2026-02-21 2026-02-22 --location both --notify ntfy
  %(prog)s --date 2026-03-01 -l palisades -i 30 --notify ntfy email

Notification setup:
  ntfy:   --notify ntfy --ntfy-topic MY_TOPIC  (or set NTFY_TOPIC env var)
  email:  --notify email  (set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS,
                           SMTP_FROM, SMTP_TO env vars)
  sms:    --notify sms    (set SMS_PHONE, SMS_CARRIER + SMTP vars)
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
        "--notify", "-n",
        nargs="+",
        choices=["desktop", "ntfy", "email", "sms"],
        default=[],
        help="Notification method(s) to use when spots are found",
    )
    parser.add_argument(
        "--ntfy-topic",
        default=None,
        help="ntfy.sh topic name (or set NTFY_TOPIC env var)",
    )
    parser.add_argument(
        "--stop-on-found", "-s",
        action="store_true",
        help="Stop checking once availability is found",
    )
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="Send a status notification regardless of availability (for heartbeat monitoring)",
    )

    args = parser.parse_args()

    for d in args.date:
        try:
            datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            print(f"Error: Invalid date format '{d}'. Use YYYY-MM-DD.", file=sys.stderr)
            sys.exit(1)

    locations = list(LOCATIONS.keys()) if args.location == "both" else [args.location]

    print("Palisades Tahoe Parking Checker")
    print(f"  Location(s): {', '.join(loc.upper() for loc in locations)}")
    print(f"  Date(s):     {', '.join(args.date)}")
    if args.interval > 0:
        print(f"  Interval:    every {args.interval}s")
    if args.notify:
        print(f"  Notify via:  {', '.join(args.notify)}")
    if args.stop_on_found:
        print(f"  Stop on hit: YES")
    print(f"  Reservation: {SITE_URL}")

    with sync_playwright() as pw:
        while True:
            try:
                found = run_check(pw, locations, args.date, args.notify, args)

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
