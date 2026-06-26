# Strava Bulk Gear Updater

A Python CLI tool that authenticates with the Strava API via OAuth2 and
**bulk-updates gear (bikes and shoes) across your historical Strava activities**,
with helpers to list your gear IDs and inspect API rate-limit usage.

## Setup

1. **Create a Strava API application**

   Go to <https://www.strava.com/settings/api> and create an app. Note your
   **Client ID** and **Client Secret**. For the "Authorization Callback Domain"
   enter `localhost`.

2. **Install dependencies** (a virtualenv is recommended)

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Configure credentials**

   ```bash
   cp .env.example .env
   # edit .env and fill in STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET
   ```

## Usage

The CLI has three subcommands (`assign`, `gear`, and `quota`). The first run
opens your browser to authorize the app; tokens are cached in
`.strava_tokens.json` and auto-refreshed on later runs.

### 1. Find your gear ID

```bash
python -m strava_equipment gear
```

Lists your registered bikes (`b…` IDs) and shoes (`g…` IDs) with their distances.

### 2. Bulk-assign gear to historical activities

Assign a gear item to **every** past activity of a given type. Always preview
with `--dry-run` first:

```bash
# Preview: which Rides would get bike b1234567?
python -m strava_equipment assign Ride b1234567 --dry-run

# Apply for real (throttle to stay under Strava's rate limit)
python -m strava_equipment assign Ride b1234567 --throttle 0.5

# Only fill in activities that have no gear yet, and ride out rate limits
python -m strava_equipment assign Run g1234567 --only-unassigned --retries 5
```

- `activity_type` matches an activity's `type` or `sport_type`, case-insensitive
  (e.g. `Run`, `Ride`, `VirtualRide`). Get the gear ID from the `gear` command.
- Activities already using the target gear are always skipped.
- Bikes use `b…` IDs, shoes use `g…` IDs.

Options:

- `--dry-run` — preview without making changes.
- `--only-unassigned` — skip any activity that already has *some* gear; only
  touch activities with none.
- `--throttle SECONDS` — pause between updates to spread requests out.
- `--retries N` — on a `429 Too Many Requests`, wait for the rate-limit window
  to reset and retry, up to `N` times (default: `2`).
- `--retry-wait SECONDS` — minimum wait before a retry (default: `60`).

### 3. Check your rate-limit usage

```bash
python -m strava_equipment quota
```

Shows current 15-minute and daily usage against both limit tiers, read straight
from the response headers (the check itself costs one request).

### Rate limits

Strava enforces two non-upload caps that apply to this tool (reads *and* gear
updates): **100 requests / 15 minutes** and **1,000 / day**. The 15-minute
window resets on the clock at `:00/:15/:30/:45` (not 15 min after your last call),
and a `429` carries no `Retry-After` header.

On a `429`, the client reads the `X-RateLimit-Usage` headers and:

- waits until the next quarter-hour boundary, then retries (up to `--retries`);
- aborts immediately with a clear message if the **daily** quota is exhausted,
  since that only resets at midnight UTC.

For a large back-catalog spanning more than one 15-min window, raise the retry
count (e.g. `--retries 10`) and prefer `--only-unassigned` to avoid redundant
writes.

### Requesting higher limits

The `100/15-min` and `1,000/day` caps are the default for unapproved apps. Strava
grants higher limits only to apps approved through its
[Developer Program](https://developers.strava.com/docs/getting-started/), which
is aimed at multi-athlete apps rather than personal single-user scripts.

## How it works

- `auth.py`     — OAuth2 authorization-code flow with a temporary local callback
  server, plus token persistence and refresh.
- `client.py`   — thin Strava API client (`get_athlete`, `get_gear`,
  `rate_limit_status`, `iter_activities`, `update_activity_gear`).
- `cli.py`      — argparse CLI with the `assign`, `gear`, and `quota` subcommands.
- `__main__.py` — dispatches to the CLI.

## Notes

- Reassigning gear requires the `activity:write` scope. If you authorized before
  this scope was added, delete `.strava_tokens.json` and re-run to re-authorize.
- Tokens are cached in `.strava_tokens.json` (gitignored).
