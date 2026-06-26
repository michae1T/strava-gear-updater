"""Command-line interface for the Strava equipment tools."""

from __future__ import annotations

import argparse
import os
import sys
import time

from dotenv import load_dotenv

from .auth import get_tokens
from .client import StravaClient


def _build_client(
    require_scope: str | None = None,
    max_retries: int = 0,
    retry_wait: float = 60.0,
) -> StravaClient:
    """Load config, run auth/refresh, and return a ready API client.

    If `require_scope` is given and the saved token doesn't carry it, exit with
    instructions to re-authorize — rather than failing later on an API 401.
    """
    load_dotenv()

    client_id = os.getenv("STRAVA_CLIENT_ID")
    client_secret = os.getenv("STRAVA_CLIENT_SECRET")
    if not client_id or not client_secret:
        print(
            "Missing credentials. Copy .env.example to .env and set "
            "STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    host = os.getenv("STRAVA_REDIRECT_HOST", "localhost")
    port = int(os.getenv("STRAVA_REDIRECT_PORT", "8721"))

    tokens = get_tokens(client_id, client_secret, host, port)

    if require_scope and not tokens.has_scope(require_scope):
        print(
            f"Your saved authorization is missing the '{require_scope}' scope, "
            "which is required to modify activities.\n"
            "Re-authorize and be sure to tick the matching permission box on "
            "Strava's consent screen:\n"
            "    rm .strava_tokens.json\n"
            "    python -m strava_equipment assign ...   # re-runs the browser login",
            file=sys.stderr,
        )
        raise SystemExit(1)

    return StravaClient(tokens.access_token, max_retries=max_retries, retry_wait=retry_wait)


# --------------------------------------------------------------------------- #
# gear
# --------------------------------------------------------------------------- #

def _format_distance(meters: float) -> str:
    return f"{meters / 1000:,.0f} km"


def _print_gear(label: str, items: list[dict]) -> None:
    print(f"\n{label} ({len(items)}):")
    if not items:
        print("  (none)")
        return
    for g in items:
        name = g.get("name", "Unnamed")
        dist = _format_distance(g.get("distance", 0))
        primary = " *primary" if g.get("primary") else ""
        retired = " [retired]" if g.get("retired") else ""
        print(f"  - {name}: {dist}{primary}{retired}  (id={g.get('id')})")


def cmd_gear(args: argparse.Namespace) -> int:
    client = _build_client()
    athlete = client.get_athlete()
    name = f"{athlete.get('firstname', '')} {athlete.get('lastname', '')}".strip()
    print(f"Authenticated as {name or 'athlete'} (id={athlete.get('id')})")

    _print_gear("Bikes", athlete.get("bikes", []))
    _print_gear("Shoes", athlete.get("shoes", []))
    return 0


# --------------------------------------------------------------------------- #
# quota
# --------------------------------------------------------------------------- #

def _print_quota_line(label: str, status: dict) -> None:
    usage, limit = status["usage"], status["limit"]
    if not usage or not limit:
        print(f"  {label}: (headers not reported)")
        return
    u15, uday = usage
    l15, lday = limit
    print(
        f"  {label}: 15-min {u15}/{l15} ({l15 - u15} left), "
        f"daily {uday}/{lday} ({lday - uday} left)"
    )


def cmd_quota(args: argparse.Namespace) -> int:
    client = _build_client()
    status = client.rate_limit_status()
    print("Rate-limit usage (this check counts as one request):")
    _print_quota_line("Overall (X-RateLimit)        ", status["overall"])
    _print_quota_line("Read/non-upload (X-ReadRate) ", status["read"])
    print("\n15-min windows reset at :00/:15/:30/:45; daily resets at midnight UTC.")
    return 0


# --------------------------------------------------------------------------- #
# assign
# --------------------------------------------------------------------------- #

def _matches_type(activity: dict, wanted: str) -> bool:
    """Case-insensitive match against an activity's type or sport_type."""
    wanted = wanted.casefold()
    return wanted in {
        str(activity.get("type", "")).casefold(),
        str(activity.get("sport_type", "")).casefold(),
    }


def cmd_assign(args: argparse.Namespace) -> int:
    client = _build_client(
        require_scope="activity:write",
        max_retries=args.retries,
        retry_wait=args.retry_wait,
    )

    # Validate the gear id up front so we fail fast on a typo.
    try:
        gear = client.get_gear(args.gear_id)
        print(f"Target gear: {gear.get('name', args.gear_id)} (id={args.gear_id})")
    except Exception as exc:  # noqa: BLE001 - surface any API error clearly
        print(f"Could not look up gear '{args.gear_id}': {exc}", file=sys.stderr)
        return 1

    mode = "DRY RUN — no changes will be made" if args.dry_run else "LIVE — updating activities"
    print(f"{mode}\nScanning activities of type '{args.activity_type}'...\n")

    matched = updated = skipped = failed = 0
    for activity in client.iter_activities():
        if not _matches_type(activity, args.activity_type):
            continue
        matched += 1

        act_id = activity["id"]
        name = activity.get("name", "")
        current_gear = activity.get("gear_id")

        if current_gear == args.gear_id:
            skipped += 1
            print(f"  = {act_id}  {name!r} already has this gear, skipping")
            continue
        if args.only_unassigned and current_gear:
            skipped += 1
            print(f"  = {act_id}  {name!r} already has gear {current_gear}, skipping")
            continue

        if args.dry_run:
            print(f"  ~ {act_id}  {name!r} would be updated")
            updated += 1
            continue

        try:
            client.update_activity_gear(act_id, args.gear_id)
            updated += 1
            print(f"  + {act_id}  {name!r} updated")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ! {act_id}  {name!r} failed: {exc}", file=sys.stderr)

        if args.throttle:
            time.sleep(args.throttle)

    print(
        f"\nDone. matched={matched} "
        f"{'would-update' if args.dry_run else 'updated'}={updated} "
        f"skipped={skipped} failed={failed}"
    )
    if args.dry_run and updated:
        print("Re-run without --dry-run to apply these changes.")
    return 1 if failed else 0


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="strava_equipment",
        description="Authenticate with Strava and manage equipment.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_gear = sub.add_parser("gear", help="List your bikes and shoes.")
    p_gear.set_defaults(func=cmd_gear)

    p_quota = sub.add_parser("quota", help="Show current rate-limit usage.")
    p_quota.set_defaults(func=cmd_quota)

    p_assign = sub.add_parser(
        "assign",
        help="Bulk-assign a gear to all historical activities of a given type.",
    )
    p_assign.add_argument(
        "activity_type",
        help="Activity type to match, e.g. Run, Ride, VirtualRide (case-insensitive).",
    )
    p_assign.add_argument("gear_id", help="Gear id to assign, e.g. b1234567 or g1234567.")
    p_assign.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without making any updates.",
    )
    p_assign.add_argument(
        "--throttle",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="Seconds to pause between updates to stay under rate limits.",
    )
    p_assign.add_argument(
        "--only-unassigned",
        action="store_true",
        help="Only assign to activities that have no gear yet; skip any that "
        "already have some gear.",
    )
    p_assign.add_argument(
        "--retries",
        type=int,
        default=2,
        metavar="N",
        help="On a 429, wait for the 15-min window to reset and retry, up to N "
        "times (default: 2). Each retry can wait up to ~15 minutes.",
    )
    p_assign.add_argument(
        "--retry-wait",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="Minimum seconds to wait before a retry (default: 60). The actual "
        "wait is until the next :00/:15/:30/:45 boundary, never less than this.",
    )
    p_assign.set_defaults(func=cmd_assign)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
