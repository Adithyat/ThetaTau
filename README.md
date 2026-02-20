# Palisades Tahoe Parking Checker

Automatically checks parking reservation availability at **Palisades Tahoe** (Palisades or Alpine base areas) and sends you a push notification, email, or SMS when spots open up.

Runs for free on **GitHub Actions** — no server, no computer left on, works from your iPhone/iPad.

Built against the [Reserve 'N Ski](https://reservenski.parkpalisadestahoe.com/select-parking) reservation system.

---

## Quick Start (5 minutes)

### 1. Get push notifications on your iPhone

The easiest notification method is [ntfy.sh](https://ntfy.sh) — free, no account needed.

1. Install the **ntfy** app from the [App Store](https://apps.apple.com/us/app/ntfy/id1625396347)
2. Open it and tap **+** to subscribe to a topic (pick something unique, e.g. `palisades-parking-yourname`)
3. That's it — you'll get push notifications on your phone

### 2. Configure GitHub Actions

1. Go to your repo on GitHub: **Settings > Secrets and variables > Actions**
2. Click **New repository secret** and add:
   - **Name:** `NTFY_TOPIC` **Value:** the topic name from step 1 (e.g. `palisades-parking-yourname`)
3. Edit the dates and location in `.github/workflows/check-parking.yml`:

```yaml
env:
  CHECK_DATES: "2026-02-22"       # space-separated dates
  CHECK_LOCATION: "palisades"     # palisades, alpine, or both
```

4. Push to the `palisades` branch. The workflow will run automatically on schedule.

### 3. You're done

The checker runs every 10 minutes during peak hours (5 AM - 3 PM PST) and pushes a notification to your phone the moment a spot opens up.

You can also trigger it manually: **Actions tab > Check Palisades Parking > Run workflow**.

---

## Notification Methods

### Push Notifications (ntfy.sh) — Recommended

Best for iPhone/iPad. Free, instant, no account required.

| Secret | Value |
|--------|-------|
| `NTFY_TOPIC` | Your unique topic name (e.g. `palisades-parking-john`) |

**Setup:** Install [ntfy iOS app](https://apps.apple.com/us/app/ntfy/id1625396347), subscribe to your topic.

### Email (SMTP)

Works with Gmail, Outlook, or any SMTP provider.

| Secret | Value | Example |
|--------|-------|---------|
| `SMTP_HOST` | SMTP server | `smtp.gmail.com` |
| `SMTP_PORT` | SMTP port | `587` |
| `SMTP_USER` | Login username | `you@gmail.com` |
| `SMTP_PASS` | Login password or app password | `abcd efgh ijkl mnop` |
| `SMTP_FROM` | Sender address | `you@gmail.com` |
| `SMTP_TO` | Recipient address | `you@gmail.com` |

**Gmail setup:** Go to [Google App Passwords](https://myaccount.google.com/apppasswords), generate one, use it as `SMTP_PASS`.

### SMS (via email-to-SMS gateway)

Free SMS using your carrier's email gateway. Requires the SMTP secrets above plus:

| Secret | Value | Example |
|--------|-------|---------|
| `SMS_PHONE` | Your 10-digit phone number | `5551234567` |
| `SMS_CARRIER` | Your carrier name | `tmobile` |

**Supported carriers:** `att`, `tmobile`, `verizon`, `sprint`, `uscellular`, `cricket`, `boost`, `metro`, `mint`, `google_fi`, `xfinity`, `visible`

---

## Adjusting the Schedule

Edit the cron expression in `.github/workflows/check-parking.yml`:

```yaml
schedule:
  - cron: "*/10 13-23 * * *"   # Every 10 min, 5 AM-3 PM PST
```

Common examples:

| Schedule | Cron (UTC) | Description |
|----------|-----------|-------------|
| Every 5 min, all day | `*/5 * * * *` | Aggressive (uses more Actions minutes) |
| Every 10 min, morning | `*/10 13-19 * * *` | 5 AM - 11 AM PST |
| Every 30 min | `*/30 * * * *` | Light monitoring |
| Hourly | `0 * * * *` | Once per hour |

**Note:** GitHub Actions free tier includes 2,000 minutes/month. At 10-min intervals for 10 hours/day, that's ~60 runs/day x ~1 min each = ~1,800 min/month — well within the limit.

---

## Running Locally

If you want to run the checker on your own machine instead of GitHub Actions:

```bash
# Install
pip install -r requirements.txt
playwright install chromium

# Single check
python check_parking.py --date 2026-02-22 --location palisades

# Monitor every 60s with phone notification
python check_parking.py --date 2026-02-22 -l palisades -i 60 --notify ntfy --ntfy-topic YOUR_TOPIC

# Multiple dates, both locations, email + push
python check_parking.py --date 2026-02-22 2026-03-01 -l both --notify ntfy email

# Check every 30s, stop when found
python check_parking.py --date 2026-02-22 -l palisades -i 30 --notify ntfy -s
```

### CLI Options

| Flag | Short | Description |
|------|-------|-------------|
| `--date` | `-d` | One or more dates in `YYYY-MM-DD` format (required) |
| `--location` | `-l` | `palisades`, `alpine`, or `both` (default: `palisades`) |
| `--interval` | `-i` | Seconds between checks. `0` = single check (default: `0`) |
| `--notify` | `-n` | Notification method(s): `desktop`, `ntfy`, `email`, `sms` |
| `--ntfy-topic` | | ntfy.sh topic (or use `NTFY_TOPIC` env var) |
| `--stop-on-found` | `-s` | Exit after first availability found |

### Environment Variables (for email/SMS)

Set these in your shell or a `.env` file:

```bash
export NTFY_TOPIC="palisades-parking-yourname"
export SMTP_HOST="smtp.gmail.com"
export SMTP_PORT="587"
export SMTP_USER="you@gmail.com"
export SMTP_PASS="your-app-password"
export SMTP_FROM="you@gmail.com"
export SMTP_TO="you@gmail.com"
export SMS_PHONE="5551234567"
export SMS_CARRIER="tmobile"
```

---

## Output

```
============================================================
  Checking at 2026-02-20 10:30:45
============================================================
  PALISADES | 2026-02-21: SOLD OUT
  PALISADES | 2026-02-22: [AVAILABLE] Free Reservations Incl ADA 6AM-1PM PST (FREE)
  PALISADES | 2026-02-22: [sold out] Advanced Paid Reservations Incl ADA 6AM-1PM PST ($30.0)
  [ntfy] Push sent to palisades-parking-john
```

## How it works

1. A headless Chromium browser (Playwright) loads the reservation page
2. It intercepts the GraphQL API call, patching the request for the target month
3. The response contains per-day availability with rate types and prices
4. If spots are open, it fires notifications to your configured channels
5. GitHub Actions runs this on a cron schedule — your phone buzzes when spots open

## Reservation dates (2025-26 season)

Parking reservations required 6 AM - 1 PM on select dates:

- **December:** 6, 7, 13, 14, 20-31
- **January:** 1-4, 10, 11, 17-19, 24, 25, 31
- **February:** 1, 7, 8, 14-16, 21, 22, 28
- **March:** 1, 7, 8, 14, 15, 21, 22, 28, 29
- **April:** 4, 5, 11, 12
