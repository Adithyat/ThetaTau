# Palisades Tahoe Parking Checker

Periodically checks parking reservation availability at **Palisades Tahoe** (Palisades or Alpine base areas) and alerts you when spots open up.

Built against the [Reserve 'N Ski](https://reservenski.parkpalisadestahoe.com/select-parking) reservation system.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

On Linux you may also need system dependencies:

```bash
playwright install-deps chromium
```

## Usage

### Single check

```bash
# Check Palisades for Feb 21
python check_parking.py --date 2026-02-21 --location palisades

# Check Alpine for Mar 1
python check_parking.py --date 2026-03-01 --location alpine

# Check both locations for multiple dates
python check_parking.py --date 2026-02-21 2026-02-22 --location both
```

### Periodic monitoring

```bash
# Check every 60 seconds with desktop alerts
python check_parking.py --date 2026-02-21 -l palisades --interval 60 --alert

# Check every 30 seconds, stop once availability is found
python check_parking.py --date 2026-02-21 -l palisades -i 30 --alert --stop-on-found
```

### Options

| Flag | Short | Description |
|------|-------|-------------|
| `--date` | `-d` | One or more dates in `YYYY-MM-DD` format (required) |
| `--location` | `-l` | `palisades`, `alpine`, or `both` (default: `palisades`) |
| `--interval` | `-i` | Seconds between checks. `0` = single check (default: `0`) |
| `--alert` | `-a` | Play sound + desktop notification when spots open |
| `--stop-on-found` | `-s` | Exit after first successful find |

## Output

Each check prints availability status per date:

```
============================================================
  Checking at 2026-02-20 10:30:45
============================================================
  PALISADES | 2026-02-21: SOLD OUT
  PALISADES | 2026-02-22: [AVAILABLE] Free Reservations Incl ADA 6AM-1PM PST (FREE)
  PALISADES | 2026-02-22: [sold out] Advanced Paid Reservations Incl ADA 6AM-1PM PST ($30.0)
```

## How it works

The checker uses a headless Chromium browser (via Playwright) to load the Palisades Tahoe parking reservation page and intercept the GraphQL API response containing availability data. This approach handles the Cloudflare protection on the API endpoint.

## Reservation dates (2025-26 season)

Parking reservations are required 6 AM - 1 PM on select dates:

- **December**: 6, 7, 13, 14, 20-31
- **January**: 1-4, 10, 11, 17-19, 24, 25, 31
- **February**: 1, 7, 8, 14-16, 21, 22, 28
- **March**: 1, 7, 8, 14, 15, 21, 22, 28, 29
- **April**: 4, 5, 11, 12
