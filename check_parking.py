#!/usr/bin/env python3
"""
Palisades Tahoe Parking Availability Checker (lightweight)

Periodically checks parking availability at Palisades Tahoe (Palisades or Alpine base)
and alerts when spots open up for your target date(s).

Uses curl_cffi to impersonate a real browser's TLS fingerprint, bypassing Cloudflare
without needing a full headless browser. ~100x faster and lighter than Playwright.

Usage:
    python check_parking.py --date this-weekend --location palisades
    python check_parking.py --date this-weekend next-weekend --location both --notify ntfy
    python check_parking.py --date 2026-02-21 --location alpine --interval 60

Requirements:
    pip install curl_cffi requests
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

import requests as std_requests
from curl_cffi import requests as cffi_requests

GRAPHQL_URL = "https://platform.honkmobile.com/graphql"
PLATFORM_URL = "https://platform.honkmobile.com/"
SITE_URL = "https://reservenski.parkpalisadestahoe.com/select-parking"

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

AVAILABILITY_QUERY = """query PublicParkingAvailability($id: ID!, $cartStartTime: String!, $startDay: Int!, $endDay: Int!, $year: Int!, $cfToken: String) {
  publicParkingAvailability(id: $id, cartStartTime: $cartStartTime, startDay: $startDay, endDay: $endDay, year: $year, cfToken: $cfToken)
}"""

IMPERSONATIONS = ["chrome110", "chrome116", "chrome119", "chrome120", "chrome124"]

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


def resolve_dates(tokens):
    """
    Resolve date tokens into concrete YYYY-MM-DD strings.
    Supports: "this-weekend", "next-weekend", "YYYY-MM-DD"
    """
    today = datetime.now().date()
    days_until_saturday = (5 - today.weekday()) % 7
    if days_until_saturday == 0 and today.weekday() == 5:
        this_sat = today
    elif days_until_saturday == 0:
        this_sat = today + timedelta(days=7)
    else:
        this_sat = today + timedelta(days=days_until_saturday)
    this_sun = this_sat + timedelta(days=1)
    next_sat = this_sat + timedelta(days=7)
    next_sun = next_sat + timedelta(days=1)

    keywords = {
        "this-weekend": [this_sat, this_sun],
        "next-weekend": [next_sat, next_sun],
    }

    resolved = []
    for token in tokens:
        lower = token.lower()
        if lower in keywords:
            for d in keywords[lower]:
                ds = d.strftime("%Y-%m-%d")
                if ds not in resolved:
                    resolved.append(ds)
        else:
            try:
                datetime.strptime(token, "%Y-%m-%d")
            except ValueError:
                print(f"Error: '{token}' is not a valid date or keyword.", file=sys.stderr)
                print(f"  Use YYYY-MM-DD, this-weekend, or next-weekend.", file=sys.stderr)
                sys.exit(1)
            if token not in resolved:
                resolved.append(token)

    return resolved


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
    except Exception:
        print("\a", end="", flush=True)


def notify_ntfy(title, message, topic=None, server=None):
    """Send push notification via ntfy.sh."""
    topic = topic or os.environ.get("NTFY_TOPIC")
    server = server or os.environ.get("NTFY_SERVER", "https://ntfy.sh")
    if not topic:
        print("  [ntfy] Skipped: no topic set (use --ntfy-topic or NTFY_TOPIC env var)")
        return False
    try:
        resp = std_requests.post(
            f"{server}/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "high", "Tags": "parking_space,ski"},
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
    """Send SMS via carrier email-to-SMS gateway."""
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
    return notify_email(title, message[:140], smtp_to=f"{digits}{gateway}",
                        smtp_from=smtp_from, smtp_host=smtp_host,
                        smtp_port=smtp_port, smtp_user=smtp_user, smtp_pass=smtp_pass)


def send_alerts(title, message, methods, args):
    """Dispatch notifications to all requested methods."""
    for method in methods:
        if method == "desktop":
            notify_desktop(title, message)
        elif method == "ntfy":
            notify_ntfy(title, message, topic=getattr(args, "ntfy_topic", None))
        elif method == "email":
            notify_email(title, message)
        elif method == "sms":
            notify_sms(title, message)


# ---------------------------------------------------------------------------
# Availability fetching (lightweight — no browser needed)
# ---------------------------------------------------------------------------

def _create_session():
    """
    Create an HTTP session that bypasses Cloudflare by impersonating
    a real browser's TLS fingerprint via curl_cffi.
    """
    for imp in IMPERSONATIONS:
        try:
            session = cffi_requests.Session(impersonate=imp)
            session.get(PLATFORM_URL, timeout=10)
            test_payload = json.dumps({
                "operationName": "PublicParkingAvailability",
                "query": AVAILABILITY_QUERY,
                "variables": {
                    "id": "G6HG",
                    "cartStartTime": "2026-01-01T06:00:00-08:00",
                    "startDay": 1, "endDay": 31, "year": 2026, "cfToken": None,
                },
            })
            resp = session.post(GRAPHQL_URL, data=test_payload,
                                headers={"Content-Type": "application/json"}, timeout=10)
            if resp.status_code == 200:
                return session
        except Exception:
            continue
    return None


def _month_day_range(year, month):
    """Return (start_day_of_year, end_day_of_year) for a given month."""
    first = datetime(year, month, 1)
    if month == 12:
        last = datetime(year, 12, 31)
    else:
        last = datetime(year, month + 1, 1) - timedelta(days=1)
    return first.timetuple().tm_yday, last.timetuple().tm_yday


def fetch_availability(session, location_key, months_needed):
    """
    Fetch parking availability for the given location across all needed months.
    Returns a merged dict keyed by date ISO strings, or None on failure.
    """
    loc = LOCATIONS[location_key]
    merged = {}

    for year, month in sorted(months_needed):
        start_day, end_day = _month_day_range(year, month)
        payload = json.dumps({
            "operationName": "PublicParkingAvailability",
            "query": AVAILABILITY_QUERY,
            "variables": {
                "id": loc["inventory_id"],
                "cartStartTime": f"{year}-{month:02d}-01T06:00:00-08:00",
                "startDay": start_day,
                "endDay": end_day,
                "year": year,
                "cfToken": None,
            },
        })
        try:
            resp = session.post(GRAPHQL_URL, data=payload,
                                headers={"Content-Type": "application/json"}, timeout=15)
            if resp.status_code != 200:
                print(f"  API returned {resp.status_code} for {location_key} {year}-{month:02d}")
                continue
            data = resp.json().get("data", {}).get("publicParkingAvailability")
            if data and isinstance(data, dict):
                merged.update(data)
        except Exception as e:
            print(f"  Error fetching {location_key} {year}-{month:02d}: {e}")

    return merged if merged else None


# ---------------------------------------------------------------------------
# Result checking / formatting
# ---------------------------------------------------------------------------

def check_date(avail, target_date):
    """Check availability for a specific date."""
    matching_key = None
    for key in avail:
        if key.split("T")[0] == target_date:
            matching_key = key
            break

    if matching_key is None:
        return {
            "date": target_date, "found": False,
            "message": "Date not in availability data (may be outside reservation season)",
        }

    day_data = avail[matching_key]
    status = day_data.get("status", {})
    result = {
        "date": target_date, "found": True,
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
            })

    return result


def format_result(result, location):
    """Format a single date's result for console display."""
    lines = []
    date = result["date"]
    loc_label = location.upper()

    if not result["found"]:
        lines.append(f"  {loc_label} | {date}: {result['message']}")
    elif result["reservation_not_needed"]:
        lines.append(f"  {loc_label} | {date}: No reservation needed (open parking)")
    elif result["unavailable"]:
        lines.append(f"  {loc_label} | {date}: UNAVAILABLE")
    elif result["sold_out"] and not any(r["available"] for r in result["rates"]):
        lines.append(f"  {loc_label} | {date}: SOLD OUT")
    elif result["rates"]:
        for rate in result["rates"]:
            tag = "AVAILABLE" if rate["available"] else "sold out"
            price_str = f"${rate['price']}" if rate["price"] != "0.0" else "FREE"
            lines.append(f"  {loc_label} | {date}: [{tag}] {rate['description']} ({price_str})")
    else:
        lines.append(f"  {loc_label} | {date}: No rate info available")

    return "\n".join(lines)


def build_notification_message(results_by_loc):
    """Build a notification message from available results."""
    lines = []
    for loc, results in results_by_loc.items():
        for r in results:
            for rate in r.get("rates", []):
                if rate["available"]:
                    price = f"${rate['price']}" if rate["price"] != "0.0" else "FREE"
                    lines.append(f"{loc.upper()} {r['date']}: {rate['description']} ({price})")
    return "\n".join(lines) if lines else None


def build_status_summary(results_by_loc):
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

def run_check(session, locations, target_dates, notify_methods, args):
    """Run one check cycle. Returns True if any notify-date has availability."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}")
    print(f"  Checking at {now}")
    print(f"{'='*60}")

    notify_dates = set(args.notify_dates) if getattr(args, "notify_dates", None) else None

    months_needed = set()
    for d in target_dates:
        dt = datetime.strptime(d, "%Y-%m-%d")
        months_needed.add((dt.year, dt.month))

    any_available = False
    results_by_loc = {}

    for loc_key in locations:
        avail = fetch_availability(session, loc_key, months_needed)
        if avail is None:
            print(f"  {loc_key.upper()}: Failed to fetch availability data")
            continue

        loc_results = []
        for target_date in target_dates:
            result = check_date(avail, target_date)
            print(format_result(result, loc_key))
            loc_results.append(result)

            triggers_alert = (notify_dates is None) or (target_date in notify_dates)
            if triggers_alert and result.get("found") and not result.get("unavailable"):
                if any(r["available"] for r in result.get("rates", [])):
                    any_available = True

        results_by_loc[loc_key] = loc_results

    if any_available and notify_methods:
        alert_results = {}
        for loc, results in results_by_loc.items():
            filtered = [r for r in results if (notify_dates is None) or (r["date"] in notify_dates)]
            if filtered:
                alert_results[loc] = filtered
        msg = build_notification_message(alert_results)
        if msg:
            send_alerts("Palisades Parking Available!", msg, notify_methods, args)

    if getattr(args, "healthcheck", False) and notify_methods:
        summary = build_status_summary(results_by_loc)
        send_alerts(
            "Parking Checker Heartbeat",
            f"Checker is running as of {now}\n\n{summary}",
            notify_methods, args,
        )

    return any_available


def main():
    parser = argparse.ArgumentParser(
        description="Check Palisades Tahoe parking availability",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --date this-weekend --location palisades
  %(prog)s --date this-weekend next-weekend --location both --notify ntfy
  %(prog)s --date this-weekend next-weekend --notify-dates this-weekend --notify ntfy
  %(prog)s --date 2026-03-01 -l palisades -i 30 --notify ntfy email

Date keywords:
  this-weekend    upcoming Saturday + Sunday
  next-weekend    the following Saturday + Sunday

Notification setup:
  ntfy:   --notify ntfy --ntfy-topic MY_TOPIC  (or set NTFY_TOPIC env var)
  email:  --notify email  (set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS,
                           SMTP_FROM, SMTP_TO env vars)
  sms:    --notify sms    (set SMS_PHONE, SMS_CARRIER + SMTP vars)
        """,
    )
    parser.add_argument("--date", "-d", nargs="+", required=True,
                        help="Date(s) or keywords: YYYY-MM-DD, this-weekend, next-weekend")
    parser.add_argument("--location", "-l", choices=["palisades", "alpine", "both"],
                        default="palisades", help="Parking location (default: palisades)")
    parser.add_argument("--interval", "-i", type=int, default=0,
                        help="Check interval in seconds. 0 = single check (default: 0)")
    parser.add_argument("--notify", "-n", nargs="+",
                        choices=["desktop", "ntfy", "email", "sms"], default=[],
                        help="Notification method(s) when spots are found")
    parser.add_argument("--ntfy-topic", default=None,
                        help="ntfy.sh topic name (or set NTFY_TOPIC env var)")
    parser.add_argument("--notify-dates", nargs="+", default=None,
                        help="Only alert for these dates/keywords (subset of --date)")
    parser.add_argument("--stop-on-found", "-s", action="store_true",
                        help="Stop checking once availability is found")
    parser.add_argument("--healthcheck", action="store_true",
                        help="Send a status notification regardless of availability")

    args = parser.parse_args()
    args.date = resolve_dates(args.date)
    if args.notify_dates:
        args.notify_dates = resolve_dates(args.notify_dates)

    locations = list(LOCATIONS.keys()) if args.location == "both" else [args.location]

    print("Palisades Tahoe Parking Checker (lightweight)")
    print(f"  Location(s): {', '.join(loc.upper() for loc in locations)}")
    print(f"  Date(s):     {', '.join(args.date)}")
    if args.notify_dates:
        print(f"  Alert for:   {', '.join(args.notify_dates)}")
    if args.interval > 0:
        print(f"  Interval:    every {args.interval}s")
    if args.notify:
        print(f"  Notify via:  {', '.join(args.notify)}")
    print(f"  Reservation: {SITE_URL}")

    print("\n  Establishing session...", end=" ", flush=True)
    session = _create_session()
    if not session:
        print("FAILED")
        print("  Could not bypass Cloudflare. Falling back may be needed.", file=sys.stderr)
        sys.exit(1)
    print("OK")

    while True:
        try:
            found = run_check(session, locations, args.date, args.notify, args)

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
            # Re-establish session on error
            print("  Re-establishing session...")
            session = _create_session()
            if not session:
                print("  Session failed. Retrying later...")
            print(f"  Retrying in {args.interval}s...")
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
